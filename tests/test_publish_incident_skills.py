import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from harness_client import HarnessError
from scripts import publish_incident_skills as publish_skills

ENDPOINT = "https://example.services.ai.azure.com/api/projects/demo"


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prepared = self.root / "prepared"
        for skill in publish_skills.SKILLS["incident"]:
            folder = self.prepared / "packages" / skill
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(skill)
        self.output = self.root / "publication"

    def fake_command(self, args, log):
        if args[2] == "update":
            return json.dumps({"name": args[3], "version": "7"})
        skill = args[3]
        destination = Path(args[args.index("--output-dir") + 1])
        shutil.copytree(self.prepared / "packages" / skill, destination)
        return "downloaded"

    def test_all_published_files_compared_and_versions_recorded(self):
        with patch.object(publish_skills, "verify_prepared", return_value={"status": "verified"}), \
             patch.object(publish_skills, "command", side_effect=self.fake_command) as command:
            publish_skills.publish("incident", self.prepared, ENDPOINT, self.output, "update")
        self.assertEqual(command.call_count, 6)
        result = json.loads((self.output / "publication.json").read_text())
        self.assertEqual(result["versions"], {skill: "7" for skill in publish_skills.SKILLS["incident"]})
        self.assertEqual(result["package_sha256"], publish_skills.package_hashes(self.prepared / "packages"))

    def test_corrupted_download_stops_without_publication_success(self):
        def corrupt(args, log):
            result = self.fake_command(args, log)
            if args[2] == "download":
                Path(args[args.index("--output-dir") + 1], "SKILL.md").write_text("changed")
            return result
        with patch.object(publish_skills, "verify_prepared", return_value={}), \
             patch.object(publish_skills, "command", side_effect=corrupt) as command:
            with self.assertRaisesRegex(HarnessError, "bytes differ"):
                publish_skills.publish("incident", self.prepared, ENDPOINT, self.output, "update")
        self.assertEqual(command.call_count, 2)
        self.assertTrue((self.output / "failure.json").exists())
        self.assertFalse((self.output / "publication.json").exists())

    def test_publication_failure_is_not_retried(self):
        with patch.object(publish_skills, "verify_prepared", return_value={}), \
             patch.object(publish_skills, "command", side_effect=HarnessError("write timeout")) as command:
            with self.assertRaisesRegex(HarnessError, "timeout"):
                publish_skills.publish("incident", self.prepared, ENDPOINT, self.output, "update")
        self.assertEqual(command.call_count, 1)
        self.assertFalse(json.loads((self.output / "failure.json").read_text())["retried"])

    def test_existing_output_refuses_all_writes(self):
        self.output.mkdir()
        with patch.object(publish_skills, "verify_prepared", return_value={}), \
             patch.object(publish_skills, "command") as command:
            with self.assertRaises(FileExistsError):
                publish_skills.publish("incident", self.prepared, ENDPOINT, self.output, "update")
        command.assert_not_called()

    def test_package_mutation_rejected_before_verifier(self):
        from scripts.prepare_incident import CATALOG, ROOT
        (self.prepared / "preparation.json").write_text(json.dumps({
            "status": "verified", "package_sha256": {},
            "catalog_sha256": publish_skills.digest(CATALOG.read_bytes()),
            "preparer_sha256": publish_skills.digest((ROOT / "scripts/prepare_incident.py").read_bytes()),
            "dependency_manifest_sha256": publish_skills.digest((ROOT / "requirements-incident.txt").read_bytes()),
        }))
        with self.assertRaisesRegex(HarnessError, "fingerprints"):
            publish_skills.verify_prepared("incident", self.prepared)

    def test_changed_preparation_provenance_stops_before_publication(self):
        (self.prepared / "preparation.json").write_text('{"status":"verified"}')
        with patch.object(publish_skills, "command") as command:
            with self.assertRaisesRegex(HarnessError, "provenance"):
                publish_skills.publish("incident", self.prepared, ENDPOINT, self.output, "create")
            command.assert_not_called()

    def test_symlink_rejected(self):
        (self.prepared / "packages/link").symlink_to(self.root / "outside")
        with self.assertRaisesRegex(HarnessError, "Symlink"):
            publish_skills.package_hashes(self.prepared / "packages")

    def test_cli_failure_preserves_command_and_logs(self):
        failed = subprocess.CompletedProcess(["azd"], 1, "stdout", "stderr")
        with patch.object(publish_skills.subprocess, "run", return_value=failed) as command:
            with self.assertRaisesRegex(HarnessError, "No automatic"):
                publish_skills.command(["ai", "skill", "update", "test"], self.root / "write.log")
        self.assertEqual(command.call_count, 1)
        self.assertTrue((self.root / "write.command.json").exists())
        self.assertEqual((self.root / "write.log").read_text(), "stdout\nstderr")


if __name__ == "__main__":
    unittest.main()
