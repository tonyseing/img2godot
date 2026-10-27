import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import time
from pathlib import Path
from unittest.mock import patch

import install as installer

REPO = Path(__file__).resolve().parent.parent
NAME = "game-asset-3d-pipeline"


def install(agents, claude, *extra, source=REPO, revision=None):
    if revision is None:
        revision = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--verify", "HEAD"],
                                  capture_output=True, text=True, check=True).stdout.strip()
    return subprocess.run([sys.executable, str(source / "install.py"), "--agents-home", str(agents),
                           "--claude-home", str(claude), "--source-revision", revision, *extra],
                          capture_output=True, text=True)


def fork_fixture(root, name):
    source = root / name
    source.mkdir()
    shutil.copy2(REPO / "install.py", source / "install.py")
    shutil.copy2(REPO / "PROVENANCE.json", source / "PROVENANCE.json")
    for resource in installer.RESOURCES:
        destination = source / resource
        if destination.exists():
            continue
        if (REPO / resource).is_dir():
            destination.mkdir()
        else:
            destination.write_text(f"{name}: isolated installer resource fixture\n")
    (source / "runtime").mkdir()
    runtime = (REPO / "runtime/godot_coordinator.py").read_bytes()
    (source / "runtime/godot_coordinator.py").write_bytes(runtime + f"\n# {name} fixture version\n".encode())
    return source


def wait_for_path(path, process, timeout=5):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(f"Installer exited before reaching gate: {stdout}\n{stderr}")
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


class ClaudeLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.agents, self.claude = self.tmp / "agents", self.tmp / "claude"
        self.target, self.link = self.agents / "skills" / NAME, self.claude / "skills" / NAME
        self.coordinator_home = self.agents / "godot-coordinator"
        self.coordinator_bin = self.agents / "bin" / "godot-coordinator"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def installation_state(self):
        def snapshot(path):
            if path.is_symlink():
                return ("symlink", os.readlink(path))
            if not path.exists():
                return ("absent",)
            if path.is_dir():
                return ("directory", path.stat().st_mode,
                        {child.name: snapshot(child) for child in sorted(path.iterdir())})
            return ("file", path.stat().st_mode, path.read_bytes())

        return {str(path): snapshot(path) for path in (
            self.target, self.link, self.coordinator_bin, self.coordinator_home,
            self.agents / "skill-backups",
        )}

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

    def test_installs_content_addressed_runtime_outside_skill(self):
        result = install(self.agents, self.claude)
        self.assertEqual(result.returncode, 0, result.stderr)
        source = REPO / "runtime" / "godot_coordinator.py"
        expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        runtime = self.coordinator_home / expected_hash / "godot_coordinator.py"
        self.assertEqual(self.coordinator_bin.resolve(), runtime.resolve())
        self.assertEqual(runtime.read_bytes(), source.read_bytes())
        self.assertTrue(os.access(runtime, os.X_OK))
        self.assertFalse((self.target / "runtime").exists())
        marker = json.loads((self.target / ".img2godot-install.json").read_text())
        dependency = marker["coordinator"]
        self.assertEqual(dependency["sha256"], expected_hash)
        self.assertEqual(dependency["sourcePath"], "runtime/godot_coordinator.py")
        self.assertRegex(dependency["sourceRevision"], r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
        self.assertEqual(dependency["sourceRepository"], "https://github.com/tonyseing/img2godot")
        self.assertEqual(json.loads(result.stdout)["coordinator"], dependency)

    def test_explicit_coordinator_locations_are_isolated(self):
        versions = self.tmp / "versions"
        binary = self.tmp / "commands" / "godot-coordinator"
        result = install(self.agents, self.claude, "--coordinator-home", str(versions),
                         "--coordinator-bin", str(binary))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(binary.is_symlink())
        self.assertEqual(binary.resolve().parent.parent, versions.resolve())
        self.assertFalse(self.coordinator_home.exists())

    def test_foreign_coordinator_entry_refused_before_skill_install(self):
        self.coordinator_bin.parent.mkdir(parents=True)
        self.coordinator_bin.write_text("unrelated executable\n")
        result = install(self.agents, self.claude)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing", result.stderr)
        self.assertEqual(self.coordinator_bin.read_text(), "unrelated executable\n")
        self.assertFalse(self.target.exists())

    def test_modified_versioned_runtime_refused(self):
        first = install(self.agents, self.claude)
        self.assertEqual(first.returncode, 0, first.stderr)
        runtime = self.coordinator_bin.resolve()
        runtime.write_text(runtime.read_text() + "\n# local modification\n")
        before = (self.target / ".img2godot-install.json").read_bytes()
        result = install(self.agents, self.claude)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.target / ".img2godot-install.json").read_bytes(), before)

    def test_coordinator_upgrade_and_rollback_restore_matching_version(self):
        first = install(self.agents, self.claude)
        self.assertEqual(first.returncode, 0, first.stderr)
        previous = self.coordinator_bin.resolve()
        copy = self.tmp / "src"
        shutil.copytree(REPO, copy, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        (copy / "README.md").write_text((copy / "README.md").read_text() + "\nchanged\n")
        runtime = copy / "runtime" / "godot_coordinator.py"
        runtime.write_text(runtime.read_text() + "\n# distinct installation fixture\n")
        upgraded = install(self.agents, self.claude, source=copy)
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        backup = json.loads(upgraded.stdout)["rollbackBackup"]
        current = self.coordinator_bin.resolve()
        self.assertNotEqual(previous, current)
        restored = install(self.agents, self.claude, "--rollback", backup)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(self.coordinator_bin.resolve(), previous)
        self.assertTrue(current.is_file())

    def test_failed_coordinator_activation_restores_skill_and_links(self):
        result = install(self.agents, self.claude)
        self.assertEqual(result.returncode, 0, result.stderr)
        original = (self.target / "README.md").read_bytes()
        previous = Path(os.readlink(self.coordinator_bin))
        stage = self.agents / "staged-skill"
        shutil.copytree(self.target, stage)
        (stage / "README.md").write_text("changed staged skill\n")
        destination = self.coordinator_home / "unpublished-fixture" / "godot_coordinator.py"
        with patch.object(installer, "replace_coordinator_link", side_effect=OSError("activation failure")):
            with self.assertRaisesRegex(OSError, "activation failure"):
                installer.activate_skill(stage, self.target, self.agents / "skill-backups", self.link,
                                         self.coordinator_bin, self.coordinator_home, destination, previous)
        self.assertEqual((self.target / "README.md").read_bytes(), original)
        self.assertEqual(Path(os.readlink(self.coordinator_bin)), previous)
        self.assertEqual(Path(os.readlink(self.link)), self.target)
        self.assertTrue(stage.is_dir())

    def test_archive_revisions_require_exact_git_hash_lengths(self):
        source = self.tmp / "archive"
        source.mkdir()
        for length in (41, 63):
            with self.subTest(length=length), self.assertRaises(ValueError):
                installer.source_revision(source, "a" * length)
        for length in (40, 64):
            self.assertEqual(installer.source_revision(source, "a" * length), "a" * length)

    def test_rollback_refuses_runtime_sidecar_provenance_drift(self):
        first = install(self.agents, self.claude)
        self.assertEqual(first.returncode, 0, first.stderr)
        old_runtime = self.coordinator_bin.resolve()
        source = fork_fixture(self.tmp, "upgrade")
        upgrade = install(self.agents, self.claude, source=source)
        self.assertEqual(upgrade.returncode, 0, upgrade.stderr)
        backup = json.loads(upgrade.stdout)["rollbackBackup"]
        sidecar = old_runtime.parent / installer.COORDINATOR_MARKER
        provenance = json.loads(sidecar.read_text())
        provenance["sourceRevision"] = "a" * 40 if provenance["sourceRevision"] != "a" * 40 else "b" * 40
        sidecar.write_text(json.dumps(provenance))
        current = self.coordinator_bin.resolve()
        marker = (self.target / installer.MARKER).read_bytes()
        result = install(self.agents, self.claude, "--rollback", backup)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.coordinator_bin.resolve(), current)
        self.assertEqual((self.target / installer.MARKER).read_bytes(), marker)
        self.assertTrue(Path(backup).is_dir())

    def test_reinstall_refuses_current_runtime_sidecar_revision_drift(self):
        source = fork_fixture(self.tmp, "baseline")
        first = install(self.agents, self.claude, source=source, revision="1" * 40)
        self.assertEqual(first.returncode, 0, first.stderr)
        marker_path = self.target / installer.MARKER
        original = json.loads(marker_path.read_text())
        dependency = original["coordinator"]
        self.assertEqual(dependency["sourceRevision"], "1" * 40)
        runtime = self.coordinator_bin.resolve()
        self.assertEqual(runtime, Path(dependency["path"]).resolve())
        self.assertEqual(hashlib.sha256(runtime.read_bytes()).hexdigest(), dependency["sha256"])
        sidecar = runtime.parent / installer.COORDINATOR_MARKER
        provenance = json.loads(sidecar.read_text())
        self.assertEqual(provenance, {key: value for key, value in dependency.items()
                                      if key not in ("path", "launcher")})
        provenance["sourceRevision"] = "2" * 40
        sidecar.write_text(json.dumps(provenance))
        before = self.installation_state()

        result = install(self.agents, self.claude, source=source, revision="1" * 40)

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertRegex(result.stderr.lower(), r"provenance|coordinator")
        self.assertEqual(self.installation_state(), before)
        self.assertEqual(json.loads(marker_path.read_text()), original)
        self.assertEqual(self.coordinator_bin.resolve(), runtime)

    def test_rollback_refuses_backup_source_revision_drift(self):
        baseline = fork_fixture(self.tmp, "baseline")
        first = install(self.agents, self.claude, source=baseline, revision="1" * 40)
        self.assertEqual(first.returncode, 0, first.stderr)
        original = json.loads((self.target / installer.MARKER).read_text())
        old_runtime = self.coordinator_bin.resolve()
        upgrade_source = fork_fixture(self.tmp, "upgrade")
        upgrade = install(self.agents, self.claude, source=upgrade_source, revision="3" * 40)
        self.assertEqual(upgrade.returncode, 0, upgrade.stderr)
        backup = Path(json.loads(upgrade.stdout)["rollbackBackup"])
        backup_marker = backup / installer.MARKER
        self.assertEqual(json.loads(backup_marker.read_text()), original)
        self.assertEqual(installer.inventory(backup), original["files"])
        current_runtime = self.coordinator_bin.resolve()
        self.assertNotEqual(current_runtime, old_runtime)
        current_marker = (self.target / installer.MARKER).read_bytes()
        altered = dict(original, sourceRevision="2" * 40)
        backup_marker.write_text(json.dumps(altered))
        self.assertEqual(altered["coordinator"], original["coordinator"])
        before = self.installation_state()

        result = install(self.agents, self.claude, "--rollback", str(backup),
                         source=upgrade_source, revision="3" * 40)

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertRegex(result.stderr.lower(), r"rollback|backup")
        self.assertEqual(self.installation_state(), before)
        self.assertEqual((self.target / installer.MARKER).read_bytes(), current_marker)
        self.assertEqual(self.coordinator_bin.resolve(), current_runtime)
        self.assertEqual(json.loads(backup_marker.read_text()), altered)

    def test_new_source_revision_reuses_identical_runtime_provenance(self):
        source = fork_fixture(self.tmp, "release")
        first = install(self.agents, self.claude, source=source, revision="1" * 40)
        self.assertEqual(first.returncode, 0, first.stderr)
        original = json.loads((self.target / installer.MARKER).read_text())
        runtime = self.coordinator_bin.resolve()
        sidecar = (runtime.parent / installer.COORDINATOR_MARKER).read_bytes()

        release = install(self.agents, self.claude, source=source, revision="2" * 40)

        self.assertEqual(release.returncode, 0, release.stderr)
        current = json.loads((self.target / installer.MARKER).read_text())
        self.assertEqual(current["sourceRevision"], "2" * 40)
        self.assertEqual(current["files"], original["files"])
        self.assertEqual(current["coordinator"], original["coordinator"])
        self.assertEqual(self.coordinator_bin.resolve(), runtime)
        self.assertEqual((runtime.parent / installer.COORDINATOR_MARKER).read_bytes(), sidecar)
        backup = Path(json.loads(release.stdout)["rollbackBackup"])
        self.assertEqual(json.loads((backup / installer.MARKER).read_text()), original)
        self.assertEqual(installer.inventory(backup), original["files"])
        restored = install(self.agents, self.claude, "--rollback", str(backup),
                           source=source, revision="2" * 40)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(json.loads((self.target / installer.MARKER).read_text()), original)
        self.assertEqual(self.coordinator_bin.resolve(), runtime)

    def test_historical_pre_coordinator_backup_restores_without_runtime_switch(self):
        historical_source = fork_fixture(self.tmp, "historical")
        first = install(self.agents, self.claude, source=historical_source, revision="1" * 40)
        self.assertEqual(first.returncode, 0, first.stderr)
        marker_path = self.target / installer.MARKER
        installed = json.loads(marker_path.read_text())
        historical = {key: installed[key] for key in ("name", "pipeline", "contentSha256", "files")}
        marker_path.write_text(json.dumps(historical))
        current_source = fork_fixture(self.tmp, "current")
        upgrade = install(self.agents, self.claude, source=current_source, revision="2" * 40)
        self.assertEqual(upgrade.returncode, 0, upgrade.stderr)
        backup = Path(json.loads(upgrade.stdout)["rollbackBackup"])
        self.assertEqual(json.loads((backup / installer.MARKER).read_text()), historical)
        self.assertEqual(installer.inventory(backup), historical["files"])
        runtime = self.coordinator_bin.resolve()
        runtime_bytes = runtime.read_bytes()
        sidecar = (runtime.parent / installer.COORDINATOR_MARKER).read_bytes()

        result = install(self.agents, self.claude, "--rollback", str(backup),
                         source=current_source, revision="2" * 40)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(json.loads(result.stdout)["coordinator"])
        self.assertEqual(json.loads(marker_path.read_text()), historical)
        self.assertEqual(installer.inventory(self.target), historical["files"])
        self.assertEqual(self.coordinator_bin.resolve(), runtime)
        self.assertEqual(runtime.read_bytes(), runtime_bytes)
        self.assertEqual((runtime.parent / installer.COORDINATOR_MARKER).read_bytes(), sidecar)
        self.assertEqual(Path(os.readlink(self.link)), self.target)

    def test_unchanged_activation_failure_preserves_missing_claude_link(self):
        result = install(self.agents, self.claude)
        self.assertEqual(result.returncode, 0, result.stderr)
        revision = json.loads((self.target / installer.MARKER).read_text())["sourceRevision"]
        alternate, _ = installer.prepare_coordinator(fork_fixture(self.tmp, "alternate"), revision,
                                                     self.coordinator_home)
        installer.replace_coordinator_link(self.coordinator_bin, alternate)
        self.link.unlink()
        marker = (self.target / installer.MARKER).read_bytes()
        argv = [str(REPO / "install.py"), "--agents-home", str(self.agents),
                "--claude-home", str(self.claude), "--source-revision", revision]
        with patch.object(sys, "argv", argv), patch.object(
                installer, "replace_coordinator_link", side_effect=OSError("activation failure")):
            with self.assertRaisesRegex(OSError, "activation failure"):
                installer.main()
        self.assertFalse(self.link.is_symlink())
        self.assertFalse(self.link.exists())
        self.assertEqual(self.coordinator_bin.resolve(), alternate.resolve())
        self.assertEqual((self.target / installer.MARKER).read_bytes(), marker)

    def test_concurrent_installers_publish_matching_skill_and_runtime(self):
        baseline = install(self.agents, self.claude)
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        revision = json.loads((self.target / installer.MARKER).read_text())["sourceRevision"]
        source_a = fork_fixture(self.tmp, "installer-a")
        source_b = fork_fixture(self.tmp, "installer-b")
        ready, release, entered_b = (self.tmp / name for name in ("ready", "release", "entered-b"))
        script = """
import pathlib,sys,time
source,agents,claude,revision,kind,ready,release,entered = sys.argv[1:]
sys.path.insert(0,source)
import install
if kind == 'a':
    publish = install.replace_coordinator_link
    def gated_publish(binary,destination):
        pathlib.Path(ready).touch()
        deadline = time.monotonic()+15
        while not pathlib.Path(release).exists():
            if time.monotonic()>deadline:
                raise RuntimeError('parent did not release installer gate')
            time.sleep(.01)
        return publish(binary,destination)
    install.replace_coordinator_link = gated_publish
else:
    preserve = install.preserve
    def observed_preserve(path,backups):
        pathlib.Path(entered).touch()
        return preserve(path,backups)
    install.preserve = observed_preserve
sys.argv = [str(pathlib.Path(source)/'install.py'),'--agents-home',agents,
            '--claude-home',claude,'--source-revision',revision]
install.main()
"""
        common = [str(self.agents), str(self.claude), revision]
        a = subprocess.Popen([sys.executable, "-c", script, str(source_a), *common, "a",
                              str(ready), str(release), str(entered_b)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        b = None
        try:
            self.assertTrue(wait_for_path(ready, a))
            b = subprocess.Popen([sys.executable, "-c", script, str(source_b), *common, "b",
                                  str(ready), str(release), str(entered_b)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            # A is paused between publishing its skill and selecting its runtime.
            # B must not enter skill publication during this transaction.
            deadline = time.monotonic() + 1
            while not entered_b.exists() and b.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            overlapped = entered_b.exists()
            release.touch()
            out_a, err_a = a.communicate(timeout=10)
            out_b, err_b = b.communicate(timeout=10)
            self.assertEqual(a.returncode, 0, out_a + err_a)
            self.assertEqual(b.returncode, 0, out_b + err_b)
            dependency = json.loads((self.target / installer.MARKER).read_text())["coordinator"]
            selected = self.coordinator_bin.resolve()
            self.assertEqual(selected, Path(dependency["path"]).resolve())
            self.assertEqual(hashlib.sha256(selected.read_bytes()).hexdigest(), dependency["sha256"])
            self.assertFalse(overlapped, "Second installer entered the first installer's publication transaction")
        finally:
            release.touch()
            for process in (a, b):
                if process is not None and process.poll() is None:
                    process.communicate(timeout=20)


if __name__ == "__main__":
    unittest.main()
