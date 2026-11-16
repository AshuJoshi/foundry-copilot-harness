"""Verify a packaged repository snapshot without diagnosing or modifying it."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath


def verify(directory: Path) -> dict:
    raw_manifest = (directory / "input-manifest.json").read_bytes()
    manifest = json.loads(raw_manifest)
    expected = set()
    expected_repositories: dict[str, set[str]] = {}
    for entry in manifest["files"]:
        relative = PurePosixPath(entry["name"])
        if relative.is_absolute() or ".." in relative.parts or "\\" in entry["name"]:
            raise ValueError(f"Unsafe manifest path: {entry['name']}")
        if entry["name"] in expected:
            raise ValueError(f"Duplicate manifest entry: {entry['name']}")
        expected.add(entry["name"])
        source = entry.get("source_skill", "software-repair")
        if source not in {"software-repair", "software-repair-repository-data"}:
            raise ValueError(f"Unknown source Skill: {source}")
        source_directory = directory if source == "software-repair" else directory.parent / source
        if entry["name"].startswith("repository/"):
            expected_repositories.setdefault(source, set()).add(entry["name"])
        path = source_directory / entry["name"]
        if (path.is_symlink() or not path.is_file()
                or not path.resolve().is_relative_to(source_directory.resolve())):
            raise ValueError(f"Missing or nonregular input: {entry['name']}")
        raw = path.read_bytes()
        if len(raw) != entry["bytes"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError(f"Input fingerprint mismatch: {entry['name']}")
    for source, names in expected_repositories.items():
        source_directory = directory if source == "software-repair" else directory.parent / source
        actual = {p.relative_to(source_directory).as_posix()
                  for p in (source_directory / "repository").rglob("*") if p.is_file()}
        if actual != names:
            raise ValueError(f"Repository member set does not match manifest in {source}.")
    task = json.loads((directory / "task.json").read_text())
    if task["case_id"] != manifest["case_id"] or task["base_commit"] != manifest["base_commit"]:
        raise ValueError("Task identity does not match manifest.")
    return {
        "status": "verified",
        "case_id": manifest["case_id"],
        "base_commit": manifest["base_commit"],
        "files_count": len(expected),
        "manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(),
    }


if __name__ == "__main__":
    print(json.dumps(verify(Path(__file__).resolve().parent), sort_keys=True))
