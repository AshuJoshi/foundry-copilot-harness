"""Independently grade pinned repair controls/candidates with official Docker tests."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

from harness_client import HarnessError
from scripts.prepare_software_repair import (
    CONFIG, SKILLS, canonical, digest, save, settings,
)
from software_repair import validate_artifacts

BASELINE_PATCH = (
    "diff --git a/harness-baseline-control.txt b/harness-baseline-control.txt\n"
    "new file mode 100644\n--- /dev/null\n+++ b/harness-baseline-control.txt\n"
    "@@ -0,0 +1 @@\n+BASELINE_NO_SOURCE_CHANGE\n"
)


def check_report(report: dict, case: dict, mode: str) -> dict:
    if set(report) != {case["instance_id"]}:
        raise HarnessError("Official report task identity mismatch.")
    graded = report[case["instance_id"]]
    if graded.get("patch_successfully_applied") is not True:
        raise HarnessError("Official evaluator did not apply the patch.")
    counts = {}
    for group in ["FAIL_TO_PASS", "PASS_TO_PASS"]:
        expected_list = json.loads(case[group]) if isinstance(case[group], str) else case[group]
        expected = set(expected_list)
        result = graded["tests_status"][group]
        success, failure = set(result["success"]), set(result["failure"])
        if (not expected or len(expected_list) != len(expected)
                or len(success) != len(result["success"]) or len(failure) != len(result["failure"])
                or success & failure or success | failure != expected):
            raise HarnessError(f"Official test accounting mismatch: {group}")
        counts[group] = {"passed": len(success), "failed": len(failure)}
    resolved = all(counts[group]["failed"] == 0 for group in counts)
    if type(graded.get("resolved")) is not bool or graded["resolved"] != resolved:
        raise HarnessError("Official resolution flag disagrees with required test accounting.")
    if mode == "baseline" and (
        resolved or counts["PASS_TO_PASS"]["failed"] or counts["FAIL_TO_PASS"]["passed"]
    ):
        raise HarnessError("Negative control did not reproduce the expected failures.")
    if mode == "reference" and not resolved:
        raise HarnessError("Reference control failed.")
    return {"resolved": resolved, "tests": counts, "mode": mode}


def candidate_patch(
    prepared: Path, candidate: Path, expected_sha256: str, repository: Path, task: dict,
) -> str:
    if digest(candidate.read_bytes()) != expected_sha256:
        raise HarnessError("Candidate JSON fingerprint differs from supplied provenance.")
    for skill in SKILLS:
        shutil.copytree(prepared / "packages" / skill / "repository", repository, dirs_exist_ok=True)
    result = validate_artifacts([candidate, candidate.with_name("report.md")], task, repository)
    if result["status"] != "passed":
        raise HarnessError("Candidate is blocked or invalid; no official evaluation started.")
    return json.loads(candidate.read_text())["patch"]


def verify_prepared(prepared: Path, config: dict) -> tuple[dict, dict]:
    case_list = json.loads((prepared / "evaluator/case.json").read_text())
    if len(case_list) != 1 or digest(canonical(case_list[0])) != config["case_sha256"]:
        raise HarnessError("Prepared evaluator case fingerprint mismatch.")
    task = json.loads((prepared / "packages/software-repair/task.json").read_text())
    if digest((prepared / "packages/software-repair/task.json").read_bytes()) != config["task_sha256"]:
        raise HarnessError("Prepared model task fingerprint mismatch.")
    report = json.loads((prepared / "preparation.json").read_text())
    actual_members = {
        p.relative_to(prepared / "packages").as_posix()
        for p in (prepared / "packages").rglob("*") if p.is_file()
    }
    if actual_members != set(report["package_sha256"]):
        raise HarnessError("Prepared package member set changed.")
    files = {}
    for name, expected in report["package_sha256"].items():
        path = prepared / "packages" / name
        if path.is_symlink() or not path.resolve().is_relative_to((prepared / "packages").resolve()):
            raise HarnessError("Unsafe package manifest path.")
        if digest(path.read_bytes()) != expected:
            raise HarnessError(f"Prepared package fingerprint mismatch: {name}")
        parts = Path(name).parts
        if len(parts) > 2 and parts[1] == "repository":
            relative = Path(*parts[2:]).as_posix()
            if relative in files:
                raise HarnessError("Duplicate repository file.")
            files[relative] = digest(path.read_bytes())
    if len(files) != config["repository_files"] or digest(canonical(files)) != config["repository_tree_sha256"]:
        raise HarnessError("Prepared complete repository fingerprint mismatch.")
    return case_list[0], task


def run_official(case: dict, patch: str, mode: str, run_id: str, config: dict, timeout: int) -> dict:
    if importlib.metadata.version("swebench") != config["evaluator_version"]:
        raise HarnessError("Require the pinned swebench evaluator version.")
    import docker
    try:
        return run_docker(case, patch, mode, run_id, config, timeout)
    except docker.errors.DockerException as error:
        raise HarnessError(f"Docker evaluation error: {error}") from error


def run_docker(case: dict, patch: str, mode: str, run_id: str, config: dict, timeout: int) -> dict:
    import docker
    from swebench.harness.constants import RUN_EVALUATION_LOG_DIR
    from swebench.harness.run_evaluation import run_instance
    from swebench.harness.test_spec.test_spec import make_test_spec

    try:
        client = docker.from_env()
        client.ping()
    except docker.errors.DockerException as error:
        raise HarnessError(f"Docker daemon unavailable: {error}") from error
    spec = make_test_spec(case, namespace="swebench")
    image = client.images.get(spec.instance_image_key)
    if config["evaluator_image"] not in image.attrs.get("RepoDigests", []):
        raise HarnessError("Local official image tag does not match the approved digest.")
    container = client.containers.create(
        image.id, command=["/bin/bash", "-c",
            "cd /testbed && git rev-parse HEAD^{tree} "
            + config["base_commit"] + "^{tree} && git status --porcelain"],
        network_disabled=True,
    )
    try:
        container.start()
        status = container.wait(timeout=60)
        lines = container.logs().decode().splitlines()
        if status["StatusCode"] != 0 or len(lines) != 2 or lines[0] != lines[1]:
            raise HarnessError("Evaluator image is not a clean tree of the task base commit.")
        save(Path("image-verification.json"), {
            "image_id": image.id, "image_digests": image.attrs["RepoDigests"],
            "task_base_tree": lines[1], "image_head_tree": lines[0],
        })
    finally:
        container.remove(force=True)
    prediction = {"instance_id": case["instance_id"], "model_name_or_path": mode,
                  "model_patch": patch}
    save(Path("prediction.json"), prediction)
    log_folder = RUN_EVALUATION_LOG_DIR / run_id / mode / case["instance_id"]
    if log_folder.exists():
        raise HarnessError("Official evaluator log path exists; refuse cached result reuse.")
    outcome = run_instance(
        spec, prediction, rm_image=False, force_rebuild=False, client=client,
        run_id=run_id, timeout=timeout,
    )
    report_path = log_folder / "report.json"
    if not report_path.is_file():
        raise HarnessError(f"Official evaluation produced no report: {outcome}")
    return json.loads(report_path.read_text())


def evaluate(
    prepared: Path, output: Path, mode: str, run_id: str,
    candidate: Path | None = None, candidate_sha256: str | None = None, timeout: int = 600,
) -> None:
    if mode not in {"baseline", "reference", "candidate"}:
        raise HarnessError("Unknown evaluation mode.")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id) or timeout <= 0:
        raise HarnessError("Require a safe run ID and positive timeout.")
    prepared, output = prepared.resolve(), output.resolve()
    config = settings()
    case, task = verify_prepared(prepared, config)
    if mode == "candidate" and (candidate is None or candidate_sha256 is None):
        raise HarnessError("Candidate mode requires JSON path and its recorded SHA-256.")
    if mode != "candidate" and (candidate is not None or candidate_sha256 is not None):
        raise HarnessError("Candidate arguments are only allowed in candidate mode.")
    output.mkdir(parents=True, exist_ok=False)
    original_cwd = Path.cwd()
    try:
        if mode == "candidate":
            assert candidate is not None and candidate_sha256 is not None
            with tempfile.TemporaryDirectory(prefix="repair-candidate-base-") as temporary:
                patch = candidate_patch(
                    prepared, candidate.resolve(), candidate_sha256,
                    Path(temporary) / "repository", task,
                )
            save(output / "candidate-provenance.json", {
                "repair_json_sha256": candidate_sha256, "patch_sha256": digest(patch.encode()),
                "scope": "Supplied existing artifact; no new agent invocation.",
            })
        else:
            patch = BASELINE_PATCH if mode == "baseline" else case["patch"]
        (output / "patch.diff").write_text(patch, encoding="utf-8")
        save(output / "conditions.json", {
            "mode": mode, "run_id": run_id, "patch_sha256": digest(patch.encode()),
            "case_sha256": config["case_sha256"], "config_sha256": digest(CONFIG.read_bytes()),
            "evaluator_source_sha256": digest(Path(__file__).read_bytes()),
            "evaluator_version": config["evaluator_version"], "image": config["evaluator_image"],
            "scope": "Local Docker tests only; does not establish a new model success.",
        })
        os.chdir(output)
        report = run_official(case, patch, mode, run_id, config, timeout)
        save(output / "official-report.json", report)
        summary = check_report(report, case, mode)
        save(output / "summary.json", dict(
            summary, status="completed", patch_sha256=digest(patch.encode()),
        ))
    except (HarnessError, OSError, ValueError, KeyError, RuntimeError, ImportError,
            importlib.metadata.PackageNotFoundError) as error:
        save(output / "failure.json", {"status": "error", "error": str(error), "retried": False})
        raise
    finally:
        os.chdir(original_cwd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=["baseline", "reference", "candidate"], required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--candidate-sha256")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--allow-local-execution", action="store_true")
    args = parser.parse_args()
    try:
        if not args.allow_local_execution:
            raise HarnessError("Explicit --allow-local-execution required for repository/Docker code.")
        evaluate(args.prepared_dir, args.output_dir, args.mode, args.run_id,
                 args.candidate, args.candidate_sha256, args.timeout)
        print(f"Evaluation complete; inspect {args.output_dir / 'summary.json'}")
        return 0
    except (HarnessError, OSError, ValueError, KeyError, RuntimeError, ImportError,
            importlib.metadata.PackageNotFoundError) as error:
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
