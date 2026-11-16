import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from harness_client import HarnessError
from scripts import prepare_software_repair as preparation

try:
    import pyarrow as pa
    import pyarrow.parquet as parquet
except ImportError:
    pa = None


def make_archive(path, entries):
    with tarfile.open(path, "w:gz") as archive:
        for name, content, kind in entries:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = len(content) if kind == tarfile.REGTYPE else 0
            if kind == tarfile.SYMTYPE:
                info.linkname = "/etc/passwd"
            archive.addfile(info, io.BytesIO(content) if kind == tarfile.REGTYPE else None)


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "repository.tar.gz"
        self.files = {"LICENSE": b"MIT fixture\n", "src/example.py": b"value = 1\n",
                      "doc/example.rst": b"Documentation\n"}
        self.config = dict(preparation.settings(), repository_files=len(self.files),
                           repository_tree_sha256=preparation.digest(preparation.canonical({
                               name: preparation.digest(raw) for name, raw in self.files.items()
                           })))
        make_archive(self.archive, [
            (f"root/{name}", raw, tarfile.REGTYPE) for name, raw in self.files.items()
        ])

    def test_complete_archive_content_verified(self):
        self.assertEqual(preparation.archive_files(self.archive, self.config), self.files)

    def test_wrong_complete_tree_rejected(self):
        self.config["repository_tree_sha256"] = "0" * 64
        with self.assertRaisesRegex(HarnessError, "fingerprint"):
            preparation.archive_files(self.archive, self.config)

    def test_traversal_symlink_and_duplicate_rejected(self):
        for entries in [
            [("root/../bad", b"x", tarfile.REGTYPE)],
            [("root/link", b"", tarfile.SYMTYPE)],
            [("root/file", b"x", tarfile.REGTYPE), ("root/file", b"x", tarfile.REGTYPE)],
            [("root/file", b"x", tarfile.REGTYPE), ("other/file", b"x", tarfile.REGTYPE)],
        ]:
            make_archive(self.archive, entries)
            with self.assertRaises(HarnessError):
                preparation.archive_files(self.archive, self.config)

    def test_download_checks_hash_and_never_overwrites(self):
        config = dict(self.config, dataset_bytes=3, dataset_sha256=preparation.digest(b"abc"))
        with patch.object(preparation, "settings", return_value=config), \
             patch.object(preparation, "fetch", side_effect=[b"abc", self.archive.read_bytes()]) as fetch:
            preparation.download(self.root / "download")
            with self.assertRaises(FileExistsError):
                preparation.download(self.root / "download")
            self.assertEqual(fetch.call_count, 2)

    def test_bad_dataset_download_keeps_error(self):
        config = dict(self.config, dataset_bytes=3, dataset_sha256=preparation.digest(b"abc"))
        with patch.object(preparation, "settings", return_value=config), \
             patch.object(preparation, "fetch", return_value=b"bad"):
            with self.assertRaisesRegex(HarnessError, "fingerprint"):
                preparation.download(self.root / "failed")
        self.assertTrue((self.root / "failed/failure.json").exists())


@unittest.skipIf(pa is None, "Install requirements-repair-prep.txt for pinned dataset fixture tests.")
class BuildTests(PreparationTests):
    def test_offline_build_excludes_reference_and_preserves_all_repository_files(self):
        case = {
            "instance_id": self.config["instance_id"], "repo": self.config["repository"],
            "base_commit": self.config["base_commit"], "problem_statement": "Fix this fixture.",
            "patch": "REFERENCE_SECRET", "test_patch": "TEST_PATCH_SECRET",
            "hints_text": "HINT_SECRET", "FAIL_TO_PASS": '["test_new"]',
            "PASS_TO_PASS": '["test_old"]',
        }
        parquet.write_table(pa.Table.from_pylist([case]), self.root / "verified.parquet")
        task = {"case_id": self.config["case_id"], "repository": case["repo"],
                "base_commit": case["base_commit"], "issue": case["problem_statement"]}
        self.config.update(
            dataset_bytes=(self.root / "verified.parquet").stat().st_size,
            dataset_sha256=preparation.digest((self.root / "verified.parquet").read_bytes()),
            case_sha256=preparation.digest(preparation.canonical(case)),
            task_sha256=preparation.digest((json.dumps(task, indent=2) + "\n").encode()),
        )
        output = self.root / "built"
        with patch.object(preparation, "settings", return_value=self.config), \
             patch.object(preparation.urllib.request, "urlopen", side_effect=AssertionError("No network")):
            preparation.build(self.root, output)
            with self.assertRaises(FileExistsError):
                preparation.build(self.root, output)
        report = json.loads((output / "preparation.json").read_text())
        self.assertEqual(sum(v["files"] for v in report["packages"].values()), 11)
        for file in (output / "packages").rglob("*"):
            if file.is_file():
                self.assertNotIn(b"REFERENCE_SECRET", file.read_bytes())
                self.assertNotIn(b"TEST_PATCH_SECRET", file.read_bytes())
                self.assertNotIn(b"HINT_SECRET", file.read_bytes())
        self.assertEqual(json.loads((output / "runtime-verification.json").read_text())["files_count"], 4)
        self.assertEqual(json.loads((output / "evaluator/case.json").read_text())[0]["patch"], "REFERENCE_SECRET")
        from scripts import publish_repair_skills
        with patch.object(publish_repair_skills, "settings", return_value=self.config):
            self.assertEqual(publish_repair_skills.verify_prepared(output),
                             json.loads((output / "runtime-verification.json").read_text()))
        instructions = output / "packages/software-repair/SKILL.md"
        instructions.write_text(instructions.read_text() + "\nChanged\n")
        with self.assertRaisesRegex(HarnessError, "fingerprints"):
            publish_repair_skills.verify_prepared(output)
        instructions.write_bytes((preparation.ROOT / "skills/software-repair/SKILL.md").read_bytes())
        with patch.object(preparation, "MAX_FILES", 1):
            with self.assertRaisesRegex(HarnessError, "limit"):
                preparation.check_packages(output / "packages", self.files, task)
        (output / "packages/software-repair/leaked.json").write_text("SECRET")
        with self.assertRaisesRegex(HarnessError, "metadata"):
            preparation.check_packages(output / "packages", self.files, task)


if __name__ == "__main__":
    unittest.main()
