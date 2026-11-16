"""Acquire pinned repair inputs and build complete answer-separated Skills offline."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request

from harness_client import HarnessError

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "agents/software-repair/case.json"
SKILLS = ["software-repair", "software-repair-repository-data"]
MAX_BYTES = 16_000_000
MAX_FILES = 512


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def save(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def settings() -> dict:
    config = json.loads(CONFIG.read_text())
    if (config["schema_version"] != 1 or config["repository"] != "pytest-dev/pytest"
            or config["case_id"] != "repair-001"
            or config["instance_id"] != "pytest-dev__pytest-10051"
            or config["evaluator_version"] != "4.1.0"):
        raise HarnessError("Unexpected repair task configuration.")
    for name in ["base_commit", "dataset_revision"]:
        if not re.fullmatch(r"[0-9a-f]{40}", config[name]):
            raise HarnessError(f"Require immutable commit: {name}")
    for name in ["dataset_sha256", "case_sha256", "repository_tree_sha256", "task_sha256"]:
        if not re.fullmatch(r"[0-9a-f]{64}", config[name]):
            raise HarnessError(f"Invalid pinned fingerprint: {name}")
    if (config["dataset_bytes"] <= 0 or config["dataset_bytes"] > 20_000_000
            or config["repository_files"] <= 0
            or not re.fullmatch(r"swebench/[a-zA-Z0-9_.-]+@sha256:[0-9a-f]{64}",
                                config["evaluator_image"])):
        raise HarnessError("Invalid source bound or evaluator image digest.")
    return config


def archive_files(path: Path, config: dict) -> dict[str, bytes]:
    files = {}
    total = 0
    with tarfile.open(path) as archive:
        roots = set()
        for member in archive:
            posix = PurePosixPath(member.name)
            if (posix.is_absolute() or ".." in posix.parts or "\\" in member.name
                    or not posix.parts):
                raise HarnessError(f"Unsafe archive path: {member.name}")
            roots.add(posix.parts[0])
            if len(roots) != 1:
                raise HarnessError("Archive has multiple roots.")
            if member.isdir():
                continue
            if not member.isfile() or len(posix.parts) < 2:
                raise HarnessError(f"Nonregular archive member: {member.name}")
            relative = PurePosixPath(*posix.parts[1:]).as_posix()
            if relative in files or relative.startswith(".git/") or relative == ".git":
                raise HarnessError(f"Duplicate or Git metadata archive member: {relative}")
            total += member.size
            if total > 20_000_000 or len(files) >= config["repository_files"]:
                raise HarnessError("Archive exceeds pinned file count or unpacked byte bound.")
            stream = archive.extractfile(member)
            if stream is None:
                raise HarnessError(f"Cannot read archive member: {relative}")
            files[relative] = stream.read()
    hashes = {name: digest(raw) for name, raw in files.items()}
    if (len(files) != config["repository_files"]
            or digest(canonical(hashes)) != config["repository_tree_sha256"]):
        raise HarnessError("Complete repository content fingerprint mismatch.")
    return files


def fetch(url: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "repair-input-preparation/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise HarnessError("Download exceeds input bound.")
    return raw


def download(output: Path) -> None:
    config = settings()
    output.mkdir(parents=True, exist_ok=False)
    try:
        raw = fetch(
            "https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified/resolve/"
            + config["dataset_revision"] + "/data/test-00000-of-00001.parquet",
            config["dataset_bytes"],
        )
        if len(raw) != config["dataset_bytes"] or digest(raw) != config["dataset_sha256"]:
            raise HarnessError("Pinned dataset fingerprint mismatch.")
        (output / "verified.parquet").write_bytes(raw)
        raw = fetch(
            f"https://codeload.github.com/{config['repository']}/tar.gz/{config['base_commit']}",
            20_000_000,
        )
        archive = output / "repository.tar.gz"
        archive.write_bytes(raw)
        archive_files(archive, config)
        save(output / "download.json", {
            "status": "verified", "dataset_sha256": config["dataset_sha256"],
            "archive_sha256": digest(raw),
            "repository_tree_sha256": config["repository_tree_sha256"],
        })
    except (OSError, ValueError, HarnessError, tarfile.TarError) as error:
        save(output / "failure.json", {"error": str(error)})
        raise


def load_case(source: Path, config: dict) -> dict:
    raw = (source / "verified.parquet").read_bytes()
    if len(raw) != config["dataset_bytes"] or digest(raw) != config["dataset_sha256"]:
        raise HarnessError("Dataset fingerprint mismatch.")
    try:
        import pyarrow
        import pyarrow.parquet as parquet
    except ImportError as error:
        raise HarnessError("Install the pinned dependency from requirements-repair-prep.txt.") from error
    if pyarrow.__version__ != "21.0.0":
        raise HarnessError("Require PyArrow 21.0.0 for task extraction.")
    matches = [row for row in parquet.read_table(source / "verified.parquet").to_pylist()
               if row["instance_id"] == config["instance_id"]]
    if len(matches) != 1 or digest(canonical(matches[0])) != config["case_sha256"]:
        raise HarnessError("Pinned benchmark case fingerprint mismatch.")
    case = matches[0]
    if case["repo"] != config["repository"] or case["base_commit"] != config["base_commit"]:
        raise HarnessError("Task repository/base commit mismatch.")
    return case


def check_packages(packages: Path, files: dict[str, bytes], task: dict) -> dict:
    recovered = {}
    sizes = {}
    for skill in SKILLS:
        folder = packages / skill
        members = [p for p in folder.rglob("*") if p.is_file()]
        if any(p.is_symlink() for p in folder.rglob("*")):
            raise HarnessError("Symlink in generated package.")
        size = sum(p.stat().st_size for p in members)
        if len(members) > MAX_FILES or size >= MAX_BYTES:
            raise HarnessError("Skill exceeds file-count or uncompressed byte limit.")
        sizes[skill] = {"files": len(members), "bytes": size}
        allowed_metadata = {"SKILL.md", "DATA-NOTICE.txt"}
        allowed_metadata |= ({"task.json", "input-manifest.json", "verify_inputs.py"}
                             if skill == SKILLS[0] else {"LICENSE"})
        for member in members:
            relative = member.relative_to(folder).as_posix()
            if relative.startswith("repository/"):
                name = relative.removeprefix("repository/")
                if name in recovered:
                    raise HarnessError("Repository file duplicated across Skills.")
                recovered[name] = member.read_bytes()
            elif relative not in allowed_metadata:
                raise HarnessError(f"Unexpected model-facing metadata: {relative}")
    if recovered != files:
        raise HarnessError("Packaged repository differs from complete pinned snapshot.")
    actual_task = json.loads((packages / SKILLS[0] / "task.json").read_text())
    if actual_task != task or set(actual_task) != {"case_id", "repository", "base_commit", "issue"}:
        raise HarnessError("Unexpected task data; evaluator fields must be excluded.")
    return sizes


def build(source: Path, output: Path) -> None:
    config = settings()
    case = load_case(source, config)
    files = archive_files(source / "repository.tar.gz", config)
    if source.resolve() == output.resolve() or source.resolve().is_relative_to(output.resolve()):
        raise HarnessError("Output must not contain or replace the source directory.")
    output.mkdir(parents=True, exist_ok=False)
    try:
        evaluator = output / "evaluator"
        evaluator.mkdir()
        save(evaluator / "case.json", [case])
        packages = output / "packages"
        for skill in SKILLS:
            folder = packages / skill
            folder.mkdir(parents=True)
            shutil.copyfile(ROOT / "skills" / skill / "SKILL.md", folder / "SKILL.md")
            shutil.copyfile(ROOT / "agents/software-repair/DATA-NOTICE.txt", folder / "DATA-NOTICE.txt")
        for name, raw in files.items():
            skill = SKILLS[1] if name.startswith("doc/") else SKILLS[0]
            target = packages / skill / "repository" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        main, data = [packages / skill for skill in SKILLS]
        shutil.copyfile(ROOT / "skills/software-repair/verify_inputs.py", main / "verify_inputs.py")
        (data / "LICENSE").write_bytes(files["LICENSE"])
        task = {"case_id": config["case_id"], "repository": case["repo"],
                "base_commit": case["base_commit"], "issue": case["problem_statement"]}
        save(main / "task.json", task)
        if digest((main / "task.json").read_bytes()) != config["task_sha256"]:
            raise HarnessError("Model-facing task differs from historical pinned bytes.")
        inputs = [(main, main / "task.json")] + [
            (folder, path) for folder in [main, data]
            for path in sorted((folder / "repository").rglob("*")) if path.is_file()
        ]
        manifest = dict(
            case_id=task["case_id"], base_commit=task["base_commit"],
            files=[{
                "name": path.relative_to(folder).as_posix(), "source_skill": folder.name,
                "bytes": path.stat().st_size, "sha256": digest(path.read_bytes()),
            } for folder, path in inputs],
        )
        save(main / "input-manifest.json", manifest)
        sizes = check_packages(packages, files, task)
        verification = subprocess.run(
            [sys.executable, str((main / "verify_inputs.py").resolve())],
            capture_output=True, text=True, timeout=60,
        )
        if verification.returncode:
            raise HarnessError(f"Runtime verifier failed: {verification.stderr.strip()}")
        actual = json.loads(verification.stdout)
        expected = {
            "status": "verified", "case_id": task["case_id"], "base_commit": task["base_commit"],
            "files_count": len(inputs), "manifest_sha256": digest((main / "input-manifest.json").read_bytes()),
        }
        if actual != expected:
            raise HarnessError("Unexpected runtime verification output.")
        save(output / "runtime-verification.json", actual)
        save(output / "preparation.json", {
            "status": "verified", "config_sha256": digest(CONFIG.read_bytes()),
            "preparer_sha256": digest(Path(__file__).read_bytes()),
            "repository_tree_sha256": config["repository_tree_sha256"],
            "packages": sizes, "package_sha256": {
                p.relative_to(packages).as_posix(): digest(p.read_bytes())
                for p in sorted(packages.rglob("*")) if p.is_file()
            },
            "scope": "Offline preparation only. Evaluator answers outside model packages.",
        })
    except (OSError, ValueError, HarnessError, subprocess.SubprocessError) as error:
        save(output / "failure.json", {"error": str(error)})
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch_parser = commands.add_parser("download")
    fetch_parser.add_argument("--output-dir", type=Path, required=True)
    build_parser = commands.add_parser("build")
    build_parser.add_argument("--source-dir", type=Path, required=True)
    build_parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "download":
            download(args.output_dir)
        else:
            build(args.source_dir, args.output_dir)
        print(f"{args.command} verified: {args.output_dir}")
        return 0
    except (HarnessError, OSError, ValueError, KeyError, tarfile.TarError,
            subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
