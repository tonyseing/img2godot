import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NAME = "game-asset-3d-pipeline"


def install(agents, claude, *extra, source=REPO):
    return subprocess.run([sys.executable, str(source / "install.py"), "--agents-home", str(agents),
                           "--claude-home", str(claude), *extra], capture_output=True, text=True)


class ClaudeLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.agents, self.claude = self.tmp / "agents", self.tmp / "claude"
        self.target, self.link = self.agents / "skills" / NAME, self.claude / "skills" / NAME

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_fresh_install_creates_link(self):
        result = install(self.agents, self.claude)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(Path(os.readlink(self.link)), self.target)
        self.assertEqual(json.loads(result.stdout)["claudeLink"], str(self.link))

    def test_identical_reinstall_keeps_link(self):
        install(self.agents, self.claude)
        result = install(self.agents, self.claude)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["unchanged"])
        self.assertTrue(self.link.is_symlink())

    def test_reinstall_recreates_missing_link(self):
        install(self.agents, self.claude)
        self.link.unlink()
        self.assertEqual(install(self.agents, self.claude).returncode, 0)
        self.assertTrue(self.link.is_symlink())

    def test_foreign_link_refused_before_install(self):
        self.link.parent.mkdir(parents=True)
        self.link.symlink_to(self.tmp)
        result = install(self.agents, self.claude)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing", result.stderr)
        self.assertFalse(self.target.exists())

    def test_directory_at_link_refused(self):
        self.link.mkdir(parents=True)
        result = install(self.agents, self.claude)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.target.exists())

    def test_modified_install_refused_and_link_untouched(self):
        install(self.agents, self.claude)
        (self.target / "SKILL.md").write_text("edited")
        result = install(self.agents, self.claude)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(Path(os.readlink(self.link)), self.target)

    def test_rollback_keeps_link(self):
        install(self.agents, self.claude)
        copy = self.tmp / "src"
        shutil.copytree(REPO, copy, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        (copy / "README.md").write_text((copy / "README.md").read_text() + "\nchanged\n")
        backup = json.loads(install(self.agents, self.claude, source=copy).stdout)["rollbackBackup"]
        result = install(self.agents, self.claude, "--rollback", backup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(os.readlink(self.link)), self.target)


if __name__ == "__main__":
    unittest.main()
