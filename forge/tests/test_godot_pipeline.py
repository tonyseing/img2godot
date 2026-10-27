"""Consumer edges for the Godot replacement; no engine launches required."""
from __future__ import annotations

import copy
import os
import json
import signal
import subprocess
import struct
import sys
import tempfile
import time
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from forge.godot.pipeline import Pipeline, PipelineError, STAGES, STAGE_EVIDENCE, enforce_budgets, validate_spec, sha256, file_record, write_json
from forge.godot import verify


def specification(source):
    return {
        "schemaVersion": 1, "assetId": "reference-prop", "role": "background prop",
        "source": source, "coordinates": {"units": "meters", "up": "Y", "forward": "-Z", "pivot": "ground"},
        "budgets": {"maxTriangles": 1000, "maxTextureDimension": 2048, "maxTextureBytes": 16777216, "maxGlbBytes": 20000000},
        "analysis": {"summary": "Three distinct observed contours", "complexity": "simple", "knownUnknowns": ["Rear surface is occluded"],
                     "detailInventory": {"scanMethod": "component-zones", "details": [
                         {"id": f"d{i}", "kind": "silhouette", "description": f"Observed contour {i}",
                          "region": {"x": 0, "y": 0, "width": 1, "height": 1, "units": "normalized"},
                          "scale": "macro", "affects": "silhouette", "confidence": 0.8,
                          "mapsTo": {"type": "component.localFeatures", "ref": f"body.f{i}"}, "stage": "blockout"} for i in range(3)]}},
        "components": [{"id": "body", "description": "Observed body", "localFeatures": [{"id": f"f{i}", "description": f"Contour {i}"} for i in range(3)]}],
        "materials": [{"id": "stone", "description": "Reference stone", "localOverrides": []}],
        "textures": [{"path": source["path"], "sha256": source["sha256"], "source": source, "processing": {"operation": "unchanged", "algorithm": "identity bytes"}}],
        "research": [{"topic": "export", "url": "https://docs.blender.org/manual/en/latest/addons/import_export/scene_gltf2.html", "takeaway": "Use image texture"},
                     {"topic": "format", "url": "https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html", "takeaway": "Meters and Y up"}],
        "stageGoals": {stage: {"description": stage, "acceptance": [f"Observed {stage} matches reference"]} for stage in STAGES},
    }


class PipelineEdges(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Technical probe fixture only, never a generated/shipping texture.
        self.image = self.root / "reference.png"
        self.image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", 1024, 1024))
        self.pipeline = Pipeline(self.root / "work")
        self.pipeline.init(self.image)
        self.spec = specification(self.pipeline.state["source"])
        self.specfile = self.root / "authored.json"
        self.specfile.write_text(json.dumps(self.spec))
        self.pipeline.admit_spec(self.specfile)

    def tearDown(self):
        self.tmp.cleanup()

    def test_unmapped_identity_detail_is_not_admitted(self):
        spec = copy.deepcopy(self.spec)
        spec["analysis"]["detailInventory"]["details"][0]["mapsTo"]["ref"] = "missing.feature"
        with self.assertRaisesRegex(PipelineError, "map"):
            validate_spec(spec, self.pipeline.state["source"])

    def test_detail_minimum_cannot_be_lowered_by_spec(self):
        spec = copy.deepcopy(self.spec)
        spec["analysis"]["detailInventory"]["details"].pop()
        with self.assertRaisesRegex(PipelineError, "3"):
            validate_spec(spec, self.pipeline.state["source"])

    def test_budget_counts_decoded_texture_bytes(self):
        facts = {"triangles": 10, "textureCount": 1, "textures": [{"width": 2048, "height": 2048}], "glbBytes": 900}
        budget = dict(self.spec["budgets"], maxTextureBytes=1000000)
        with self.assertRaisesRegex(PipelineError, "decoded"):
            enforce_budgets(facts, budget, "material")

    def test_final_cannot_be_vertex_color_only(self):
        with self.assertRaisesRegex(PipelineError, "texture"):
            enforce_budgets({"triangles": 10, "textureCount": 0, "textures": [], "glbBytes": 100}, self.spec["budgets"], "optimization")

    def test_failed_build_never_advances_and_keeps_failure(self):
        script = self.root / "build.py"
        script.write_text("raise RuntimeError('authoring failed')\n")
        with patch("forge.godot.pipeline.run_command", side_effect=PipelineError("authoring failed")):
            with self.assertRaisesRegex(PipelineError, "authoring failed"):
                self.pipeline.build("blockout", script, "blender", "godot", None)
        state = json.loads((self.pipeline.work / "state.json").read_text())
        self.assertEqual(state["stages"]["blockout"]["status"], "failed")
        self.assertEqual(self.pipeline.status()["nextStage"], "blockout")

    def test_changed_source_invalidates_existing_spec(self):
        self.image.write_bytes(self.image.read_bytes() + b"changed")
        self.assertEqual(self.pipeline.status()["nextAction"], "init")
        with self.assertRaisesRegex(PipelineError, "source"):
            self.pipeline.export(self.root / "published.glb")

    def test_changed_authored_spec_invalidates_acceptance(self):
        self.specfile.write_text(json.dumps(dict(self.spec, role="hero")))
        self.assertEqual(self.pipeline.status()["nextAction"], "spec")

    def test_out_of_order_build_is_rejected_before_subprocess(self):
        script = self.root / "build.py"
        script.write_text("pass\n")
        with patch("forge.godot.pipeline.run_command") as run:
            with self.assertRaisesRegex(PipelineError, "blockout"):
                self.pipeline.build("structural", script, "blender", "godot", None)
            run.assert_not_called()

    def test_unreviewed_stages_cannot_be_published(self):
        with self.assertRaisesRegex(PipelineError, "accepted"):
            self.pipeline.export(self.root / "published.glb")
        self.assertFalse((self.root / "published.glb").exists())

    def installed_blockout(self):
        """Recorded external engine observations as fixtures, not fake production generators."""
        directory = self.pipeline.work / "stages" / "blockout"
        directory.mkdir(parents=True)
        script = self.root / "build.py"
        script.write_text("# fixture authoring input\n")
        self.pipeline.state["script"] = file_record(script)
        model = directory / "model.glb"
        document = json.dumps({"asset": {"version": "2.0"}, "meshes": []}).encode()
        document += b" " * ((-len(document)) % 4)
        model.write_bytes(struct.pack("<4sII", b"glTF", 2, 20 + len(document)) + struct.pack("<II", len(document), 0x4E4F534A) + document)
        facts = {"schemaVersion": 1, "status": "passed", "modelSha256": sha256(model), "triangles": 10, "glbBytes": model.stat().st_size, "textures": [], "textureCount": 0, "bounds": {"position": [0, 0, 0]}}
        blender_report = directory / "blender-report.json"
        write_json(blender_report, facts)
        captures = []
        for width, height in ((844, 390), (1280, 800)):
            capture = directory / f"{width}.png"
            capture.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", width, height))
            captures.append({**file_record(capture), "width": width, "height": height})
        report = directory / "godot-report.json"
        write_json(report, {"status": "passed", "modelSha256": sha256(model), "mode": "activation-suppressed capture", "errors": [], "captures": captures, "facts": facts})
        binding = {"sourceSha256": self.pipeline.state["source"]["sha256"], "specSha256": self.pipeline.state["specSha256"], "scriptSha256": sha256(script),
                   "modelSha256": sha256(model), "blenderReportSha256": sha256(blender_report), "godotReportSha256": sha256(report)}
        self.pipeline.state["stages"]["blockout"] = {"status": "built", "model": str(model), "blenderReport": str(blender_report), "godotReport": str(report), "binding": binding}
        self.pipeline.save()
        review = {**binding, "stage": "blockout", "reviewer": "fixture vision reviewer", "verdict": "pass", "inspectedReference": True,
                  "observations": ["Fixture records a complete external observation"],
                  "captures": [{k: c[k] for k in ("path", "sha256")} for c in captures],
                  "detailChecks": [{"id": f"d{i}", "verdict": "pass", "observation": "Observed reference contour"} for i in range(3)],
                  "criteriaChecks": [{"criterion": criterion, "verdict": "pass", "observation": "Observed silhouette agreement"} for criterion in STAGE_EVIDENCE["blockout"] + self.spec["stageGoals"]["blockout"]["acceptance"]]}
        path = self.root / "review.json"
        write_json(path, review)
        return path, review

    def test_changed_capture_invalidates_accepted_stage(self):
        path, review = self.installed_blockout()
        self.pipeline.review("blockout", path)
        self.assertEqual(self.pipeline.status()["nextStage"], "structural")
        capture = Path(review["captures"][0]["path"])
        capture.write_bytes(capture.read_bytes() + b"changed")
        self.assertEqual(self.pipeline.status()["nextStage"], "blockout")
        with self.assertRaisesRegex(PipelineError, "capture"):
            self.pipeline.review("blockout", path)

    def test_changed_exported_glb_rejects_old_review(self):
        path, _ = self.installed_blockout()
        model = Path(self.pipeline.state["stages"]["blockout"]["model"])
        model.write_bytes(model.read_bytes() + b"changed")
        with self.assertRaisesRegex(PipelineError, "stale model"):
            self.pipeline.review("blockout", path)

    def test_observed_detail_failure_cannot_claim_pass(self):
        path, review = self.installed_blockout()
        review["detailChecks"][0].update(verdict="fail", observation="Observed wrong contour")
        write_json(path, review)
        with self.assertRaisesRegex(PipelineError, "failed identity"):
            self.pipeline.review("blockout", path)

    def test_stage_criterion_failure_cannot_claim_pass(self):
        path, review = self.installed_blockout()
        review["criteriaChecks"][0].update(verdict="fail", observation="Observed wrong silhouette")
        write_json(path, review)
        with self.assertRaisesRegex(PipelineError, "failed stage criterion"):
            self.pipeline.review("blockout", path)

    def test_reserved_spec_input_is_rejected_without_mutating_author_or_state(self):
        pipeline = Pipeline(self.root / "collision-work")
        pipeline.init(self.image)
        canonical = pipeline.work / "spec.json"
        portable = copy.deepcopy(self.spec)
        portable["source"]["path"] = "../reference.png"
        portable["textures"][0]["path"] = "../reference.png"
        portable["textures"][0]["source"]["path"] = "../reference.png"
        canonical.write_text(json.dumps(portable, separators=(",", ":")))
        authored_bytes = canonical.read_bytes()
        state_bytes = pipeline.state_path.read_bytes()
        with self.assertRaisesRegex(PipelineError, "external authored file"):
            pipeline.admit_spec(canonical)
        self.assertEqual(canonical.read_bytes(), authored_bytes)
        self.assertEqual(pipeline.state_path.read_bytes(), state_bytes)

    def test_publication_copy_rejects_source_mutated_during_copy(self):
        from forge.godot.pipeline import copy_accepted_file
        import shutil
        destination = self.root / "copied-reference.png"
        accepted = sha256(self.image)
        real_copy = shutil.copyfile
        def mutate_then_copy(source, target):
            Path(source).write_bytes(Path(source).read_bytes() + b"unreviewed")
            return real_copy(source, target)
        with patch("forge.godot.pipeline.shutil.copyfile", side_effect=mutate_then_copy):
            with self.assertRaisesRegex(PipelineError, "accepted hash"):
                copy_accepted_file(self.image, destination, accepted)

    def test_publication_copy_rejects_already_changed_evidence(self):
        from forge.godot.pipeline import copy_accepted_file
        path, review = self.installed_blockout()
        accepted = sha256(path)
        review["observations"] = ["Unaccepted rewrite"]
        write_json(path, review)
        destination = self.root / "copied-review.json"
        with self.assertRaisesRegex(PipelineError, "accepted hash"):
            copy_accepted_file(path, destination, accepted)
        self.assertFalse(destination.exists())

    def installed_complete(self):
        """Complete accepted engine-report fixture, passing through real review gates."""
        path, _ = self.installed_blockout()
        blockout = self.pipeline.state["stages"]["blockout"]
        model = Path(blockout["model"])
        pixels = self.image.read_bytes()
        document = json.dumps({"asset": {"version": "2.0"}, "buffers": [{"byteLength": len(pixels)}],
                               "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(pixels)}],
                               "images": [{"bufferView": 0, "mimeType": "image/png"}]}).encode()
        document += b" " * ((-len(document)) % 4)
        binary = pixels + b"\0" * ((-len(pixels)) % 4)
        model.write_bytes(struct.pack("<4sII", b"glTF", 2, 28 + len(document) + len(binary)) +
                          struct.pack("<II", len(document), 0x4E4F534A) + document +
                          struct.pack("<II", len(binary), 0x004E4942) + binary)
        original_report = json.loads(Path(blockout["godotReport"]).read_text())
        facts = {"status": "passed", "modelSha256": sha256(model), "triangles": 10, "glbBytes": model.stat().st_size,
                 "textureCount": 1, "textures": [{"width": 1024, "height": 1024}], "untexturedMeshes": [],
                 "bounds": {"position": [0, 0, 0]}}
        self.pipeline.state["stages"] = {}
        for stage in STAGES:
            directory = self.pipeline.work / "stages" / stage
            directory.mkdir(parents=True, exist_ok=True)
            blender_report = directory / "blender-report.json"
            godot_report = directory / "godot-report.json"
            write_json(blender_report, facts)
            report = dict(original_report, modelSha256=sha256(model), facts=facts)
            write_json(godot_report, report)
            binding = {"sourceSha256": self.pipeline.state["source"]["sha256"], "specSha256": self.pipeline.state["specSha256"],
                       "scriptSha256": self.pipeline.state["script"]["sha256"], "modelSha256": sha256(model),
                       "blenderReportSha256": sha256(blender_report), "godotReportSha256": sha256(godot_report)}
            self.pipeline.state["stages"][stage] = {"status": "built", "model": str(model), "blenderReport": str(blender_report),
                                                   "godotReport": str(godot_report), "binding": binding, "measurements": {"triangles": 10}}
            review = {**binding, "stage": stage, "reviewer": "external observation fixture", "verdict": "pass",
                      "inspectedReference": True, "observations": ["Observed accepted reference reconstruction"],
                      "captures": [{k: c[k] for k in ("path", "sha256")} for c in report["captures"]],
                      "detailChecks": [{"id": f"d{i}", "verdict": "pass", "observation": "Observed mapped reference contour"} for i in range(3)],
                      "criteriaChecks": [{"criterion": criterion, "verdict": "pass", "observation": "Observed acceptance evidence"}
                                         for criterion in STAGE_EVIDENCE[stage] + self.spec["stageGoals"][stage]["acceptance"]]}
            write_json(path, review)
            self.pipeline.review(stage, path)
        return self.pipeline

    def test_publication_retains_hash_consistent_accepted_bundle(self):
        pipeline = self.installed_complete()
        destination = self.root / "published.glb"
        result = pipeline.export(destination)
        provenance = Path(result["provenance"])
        manifest = json.loads((provenance / "manifest.json").read_text())
        self.assertEqual(sha256(destination), manifest["modelSha256"])
        for record in manifest["files"]:
            self.assertEqual(sha256(provenance / record["path"]), record["sha256"])

    def test_publication_rolls_back_copy_time_source_script_or_capture_mutation(self):
        import shutil
        pipeline = self.installed_complete()
        destination = self.root / "published.glb"
        capture = json.loads(Path(pipeline.state["stages"]["optimization"]["godotReport"]).read_text())["captures"][0]["path"]
        targets = [pipeline.state["source"]["path"], pipeline.state["script"]["path"], capture]
        real_copy = shutil.copyfile
        for target in targets:
            with self.subTest(target=target):
                original = Path(target).read_bytes()
                def mutate_selected(source, output):
                    if Path(source) == Path(target):
                        Path(source).write_bytes(original + b"unreviewed copy-time change")
                    return real_copy(source, output)
                try:
                    with patch("forge.godot.pipeline.shutil.copyfile", side_effect=mutate_selected):
                        with self.assertRaisesRegex(PipelineError, "accepted hash"):
                            pipeline.export(destination)
                    self.assertFalse(destination.exists())
                    self.assertFalse(destination.with_suffix(".provenance").exists())
                finally:
                    Path(target).write_bytes(original)


class VerifierCoordinatorEdges(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runtime = Path(__file__).resolve().parents[2] / "runtime" / "godot_coordinator.py"
        self.environment = patch.dict(os.environ, {
            "GODOT_COORDINATOR": str(self.runtime),
            "GODOT_COORDINATOR_STATE_DIR": str(self.root / "state"),
        })
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.tmp.cleanup()

    def test_isolated_engine_is_rejected_before_spawn_or_project_cache_write(self):
        project = self.root / "project"
        project.mkdir()
        config = project / "project.godot"
        original = b'config_version=5\n[application]\nconfig/name="Private fixture"\n'
        config.write_bytes(original)
        spawned = self.root / "unexpected-engine"
        engine = self.root / "Godot"
        # A finite technical process fixture, not an actual Godot executable.
        # Keep its conservative engine-class name so this tests domain admission.
        engine.write_text(
            f"#!{sys.executable}\nfrom pathlib import Path\n"
            f"Path({str(spawned)!r}).write_text('executed')\n"
            f"cache = Path({str(project)!r}) / '.godot'\n"
            "cache.mkdir()\n(cache / 'unauthorized-write').write_text('executed')\n"
            "print('4.5.1.fixture')\n")
        engine.chmod(0o700)
        failure = None
        try:
            verify.run([str(engine), "--version"], {"runs": []}, self.root,
                       "version", "headless", project, timeout=5)
        except verify.VerificationError as exc:
            failure = exc
        observed = {
            "exit_code": failure.exit_code if failure is not None else None,
            "coordinator_error": isinstance(failure.__cause__, verify.coordinator_module().CoordinatorError)
                                 if failure is not None else False,
            "domain_diagnostic": "isolated synthetic-test domain" in str(failure),
            "preexec_marker": spawned.exists(),
            "cache_created": (project / ".godot").exists(),
            "project_bytes_preserved": config.read_bytes() == original,
        }
        self.assertEqual(observed, {
            "exit_code": 2, "coordinator_error": True, "domain_diagnostic": True,
            "preexec_marker": False, "cache_created": False, "project_bytes_preserved": True,
        }, f"Raw verifier leaf bypassed domain admission: {observed!r}")

    def test_isolated_neutral_target_named_by_engine_symlink_still_executes(self):
        project = self.root / "project"
        project.mkdir()
        spawned = self.root / "synthetic-leaf"
        target = self.root / "neutral-fixture"
        target.write_text(
            f"#!{sys.executable}\nfrom pathlib import Path\n"
            f"Path({str(spawned)!r}).write_text('synthetic leaf executed')\n"
            "print('4.5.1.synthetic')\n")
        target.chmod(0o700)
        engine_alias = self.root / "Godot"
        engine_alias.symlink_to(target)
        report = {"runs": []}
        text = verify.run([str(engine_alias), "--version"], report, self.root,
                          "version", "headless", project, timeout=5)
        self.assertEqual(text.strip(), "4.5.1.synthetic")
        self.assertEqual(spawned.read_text(), "synthetic leaf executed")
        self.assertEqual(report["runs"][0]["returncode"], 0)

    def test_headless_leaf_passes_authenticated_project_context_to_child(self):
        project = self.root / "project"
        project.mkdir()
        report = {"runs": []}
        command = [sys.executable, "-c",
                   "import os,subprocess,sys; "
                   "sys.exit(subprocess.call([sys.executable,os.environ['GODOT_COORDINATOR'],"
                   "'check','--mode','shared']))"]
        verify.run(command, report, self.root, "version", "headless", project)
        self.assertEqual(report["runs"][0]["returncode"], 0)
        self.assertEqual(report["runs"][0]["project"], str(project.resolve()))

    def test_project_admission_failure_is_translated_without_spawning(self):
        project = self.root / "project"
        project.mkdir()
        spawned = self.root / "unexpected-child"
        command = [sys.executable, "-c",
                   f"from pathlib import Path; Path({str(spawned)!r}).touch()"]
        coordinator = verify.coordinator_module()
        with coordinator.lease(project=project, purpose="existing project owner"):
            with self.assertRaises(verify.VerificationError) as rejected:
                verify.run(command, {"runs": []}, self.root, "version", "headless", project)
        self.assertEqual(rejected.exception.exit_code, 2)
        self.assertIn("already owned by an ancestor", (self.root / "version-stderr.log").read_text())
        self.assertFalse(spawned.exists())

    def test_missing_shared_coordinator_fails_closed_before_spawning(self):
        with patch.dict(os.environ, {"GODOT_COORDINATOR": str(self.root / "missing")}):
            with self.assertRaisesRegex(verify.VerificationError, "coordinator"):
                verify.run([sys.executable, "-c", "raise AssertionError('must not spawn')"],
                           {"runs": []}, self.root, "version", "headless", self.root)

    def test_owned_child_uses_ancestor_remaining_budget_and_is_reaped(self):
        project = self.root / "project"
        project.mkdir()
        completed = self.root / "child-completed"
        command = [sys.executable, "-c",
                   f"import time; from pathlib import Path; time.sleep(1.5); Path({str(completed)!r}).touch()"]
        report = {"runs": []}
        started = time.monotonic()
        with self.assertRaises(verify.VerificationError) as timed_out:
            with verify.coordinator_lease(mode="capture", timeout=0.35, purpose="bounded verifier batch"):
                verify.run(command, report, self.root, "version", "headless", project, timeout=5)
        self.assertEqual(timed_out.exception.exit_code, 124)
        self.assertLess(time.monotonic() - started, 1.2, "Child ignored its ancestor's shorter deadline")
        self.assertFalse(completed.exists())
        self.assertIsNotNone(report["runs"][0]["returncode"])

    def test_sigint_reaps_owned_child_before_releasing_project_claim(self):
        project = self.root / "project"
        project.mkdir()
        pid_path = self.root / "owned-child.pid"
        completed = self.root / "child-completed"
        child = ("import os,time; from pathlib import Path; "
                 f"ready=Path({str(pid_path)!r}); temporary=ready.with_suffix('.tmp'); "
                 "temporary.write_text(str(os.getpid())); temporary.replace(ready); "
                 f"time.sleep(2); Path({str(completed)!r}).touch()")
        script = (
            "import sys; from pathlib import Path; from forge.godot import verify; "
            f"verify.run([sys.executable,'-c',{child!r}],{{'runs':[]}},"
            f"Path({str(self.root)!r}),'version','headless',Path({str(project)!r}),timeout=5)"
        )
        process = subprocess.Popen([sys.executable, "-c", script],
                                   cwd=Path(__file__).resolve().parents[2],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        try:
            deadline = time.monotonic() + 3
            while not pid_path.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(pid_path.exists(), "Owned child did not reach the readiness barrier")
            child_pid = int(pid_path.read_text())
            interrupted = time.monotonic()
            process.send_signal(signal.SIGINT)
            process.communicate(timeout=1)
            self.assertNotEqual(process.returncode, 0)
            self.assertLess(time.monotonic() - interrupted, 1)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
            self.assertFalse(completed.exists())
            status = subprocess.run([sys.executable, str(self.runtime), "status", "--json"],
                                    capture_output=True, text=True, timeout=3)
            self.assertEqual(status.returncode, 0, status.stderr)
            records = json.loads(status.stdout)
            self.assertEqual(records["active"], [])
            self.assertEqual(records["waiting"], [])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)

    def _exercise_actual_verifier_family(self, *, launcher, action):
        """Real verifier + canonical CLI, never an engine or a mocked lease.

        Reuses the packet owner's neutral-child/stop-file pattern. The launcher
        reproduces Node's actual topology: wrapper and canonical CLI share a
        group, while the canonical CLI owns a separately-sessioned leaf.
        """
        project = self.root / "project"
        project.mkdir()
        leaf = self.root / "neutral_leaf.py"
        leaf.write_text(textwrap.dedent("""\
            import json, os, pathlib, sys, time
            root = pathlib.Path(sys.argv[1])
            receipt = root / 'leaf.json'
            temporary = receipt.with_suffix('.tmp')
            temporary.write_text(json.dumps({
                'pid': os.getpid(), 'ppid': os.getppid(),
                'pgid': os.getpgrp(), 'sid': os.getsid(0),
                'deadline': os.environ.get('GODOT_COORDINATOR_DEADLINE'),
            }))
            temporary.replace(receipt)
            end = time.monotonic() + 12
            while time.monotonic() < end and not (root / 'stop').exists():
                time.sleep(.02)
            """))
        wrapper = self.root / "neutral_launcher.py"
        wrapper.write_text(textwrap.dedent("""\
            import json, os, pathlib, signal, subprocess, sys, time
            root = pathlib.Path(sys.argv[1])
            child = None
            received = None
            def interrupted(signum, frame):
                global received
                received = signum
                if child is not None and child.returncode is None:
                    child.send_signal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, interrupted)
            signal.signal(signal.SIGINT, interrupted)
            child = subprocess.Popen([
                sys.executable, os.environ['GODOT_COORDINATOR'],
                'run', '--mode', 'shared', '--project', str(root / 'project'),
                '--timeout', '10', '--purpose', 'neutral native-equivalent leaf',
                '--', sys.executable, str(root / 'neutral_leaf.py'), str(root),
            ])
            receipt = root / 'launcher.json'
            temporary = receipt.with_suffix('.tmp')
            temporary.write_text(json.dumps({
                'pid': os.getpid(), 'pgid': os.getpgrp(),
                'coordinator': child.pid,
            }))
            temporary.replace(receipt)
            if received is not None:
                child.send_signal(signal.SIGTERM)
            end = time.monotonic() + 12
            while child.returncode is None:
                if (root / 'stop').exists() or time.monotonic() >= end:
                    interrupted(signal.SIGTERM, None)
                try:
                    child.wait(timeout=.02)
                except subprocess.TimeoutExpired:
                    pass
            (root / 'launcher-drained.json').write_text(json.dumps({
                'signal': received, 'returncode': child.returncode,
            }))
            raise SystemExit(child.returncode)
            """))
        driver = self.root / "neutral_verifier.py"
        driver.write_text(textwrap.dedent(f"""\
            import importlib.util, json, os, pathlib, sys
            root = pathlib.Path(sys.argv[1])
            spec = importlib.util.spec_from_file_location('actual_verifier', {str(Path(verify.__file__).resolve())!r})
            verify = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = verify
            spec.loader.exec_module(verify)
            command = [sys.executable, str(root / {'neutral_launcher.py' if launcher else 'neutral_leaf.py'!r}), str(root)]
            report = {{'runs': []}}
            try:
                verify.run(command, report, root, 'version',
                           {'activation-suppressed capture' if launcher else 'headless'!r},
                           root / 'project', timeout={2 if launcher else 30},
                           launcher={launcher!r}, env=dict(os.environ),
                           deadline=float(os.environ['GODOT_COORDINATOR_DEADLINE']))
            except verify.VerificationError as error:
                (root / 'verifier-result.json').write_text(json.dumps({{
                    'exit_code': error.exit_code, 'message': str(error), 'report': report,
                }}))
                raise SystemExit(error.exit_code)
            """))
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("GODOT_COORDINATOR_")}
        environment.update({
            "GODOT_COORDINATOR": str(self.runtime),
            "GODOT_COORDINATOR_STATE_DIR": str(self.root / "state"),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        command = [sys.executable, str(self.runtime), "run", "--mode", "capture",
                   "--timeout", "4", "--purpose", "neutral verifier boundary",
                   "--", sys.executable, str(driver), str(self.root)]
        process = None
        survivor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(12)"],
                                    start_new_session=True)
        receipts = {}
        started = time.monotonic()

        def present(pid):
            # Observation only. Snapshot PIDs are never termination authority.
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            return True

        try:
            with (self.root / "boundary.log").open("w") as output:
                process = subprocess.Popen(command, env=environment, stdout=output,
                                           stderr=subprocess.STDOUT, start_new_session=True)
                ready_end = started + 3
                while not (self.root / "leaf.json").is_file():
                    self.assertIsNone(process.poll(), "Actual verifier exited before leaf readiness")
                    self.assertLess(time.monotonic(), ready_end, "Neutral leaf readiness timed out")
                    time.sleep(.02)
                receipts["leaf"] = json.loads((self.root / "leaf.json").read_text())
                self.assertEqual(receipts["leaf"]["pid"], receipts["leaf"]["pgid"])
                self.assertEqual(receipts["leaf"]["pid"], receipts["leaf"]["sid"])
                if launcher:
                    receipts["launcher"] = json.loads((self.root / "launcher.json").read_text())
                    self.assertNotEqual(receipts["leaf"]["pgid"], receipts["launcher"]["pgid"])
                    self.assertEqual(receipts["leaf"]["ppid"], receipts["launcher"]["coordinator"])
                    state = json.loads((self.root / "state/state.json").read_text())
                    claims = [record for record in state["records"]
                              if record.get("project") == str(project.resolve())]
                    self.assertEqual(len(claims), 1, "Launcher must not hold an ancestor project claim")
                    self.assertEqual(claims[0]["kind"], "command")
                if action != "timeout":
                    process.send_signal(signal.SIGTERM if action == "sigterm" else signal.SIGINT)
                process.wait(timeout=max(.1, started + 6 - time.monotonic()))
            log = (self.root / "boundary.log").read_text()
            self.assertEqual(process.returncode, {"timeout": 124, "sigterm": 143, "sigint": 130}[action], log)
            drain_end = min(started + 7, time.monotonic() + 1)
            while any(present(receipt["pid"]) for receipt in receipts.values()) and time.monotonic() < drain_end:
                time.sleep(.02)
            remaining = {name: receipt for name, receipt in receipts.items() if present(receipt["pid"])}
            self.assertIsNone(survivor.poll(), "Unrelated test-owned survivor was terminated")
            self.assertEqual(remaining, {}, "Actual verifier stranded its owned family\n" + log)
            if launcher:
                drained = json.loads((self.root / "launcher-drained.json").read_text())
                self.assertEqual(drained["signal"], signal.SIGTERM, "Launcher forwarding was bypassed")
            status = subprocess.run([sys.executable, str(self.runtime), "status", "--json"],
                                    env=environment, capture_output=True, text=True, timeout=2)
            self.assertEqual(status.returncode, 0, status.stderr)
            state = json.loads(status.stdout)
            self.assertEqual(state["active"], [], "Owned claims survived family cleanup")
            self.assertEqual(state["waiting"], [])
            self.assertLess(time.monotonic() - started, 8, "Cleanup extended the finite caller budget")
        finally:
            # RED must be safe too: cooperative stop reaches every neutral leaf,
            # even if the broken verifier killed its owner before forwarding.
            # Each fixture independently auto-expires within twelve seconds.
            (self.root / "stop").touch()
            if process is not None and process.poll() is None:
                process.terminate()  # Only our actual, unreaped Popen handle.
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            cleanup_end = started + 14
            while any(present(receipt["pid"]) for receipt in receipts.values()) and time.monotonic() < cleanup_end:
                time.sleep(.02)
            if survivor.poll() is None:
                survivor.terminate()
            survivor.wait(timeout=2)

    def test_actual_verifier_sigterm_drains_context_leaf(self):
        self._exercise_actual_verifier_family(launcher=False, action="sigterm")

    def test_actual_verifier_timeout_drains_context_leaf(self):
        self._exercise_actual_verifier_family(launcher=False, action="timeout")

    def test_actual_verifier_timeout_forwards_launcher_before_nested_leaf_cleanup(self):
        self._exercise_actual_verifier_family(launcher=True, action="timeout")

    def test_actual_verifier_sigterm_forwards_launcher_before_nested_leaf_cleanup(self):
        self._exercise_actual_verifier_family(launcher=True, action="sigterm")

    def test_actual_verifier_sigint_forwards_launcher_before_nested_leaf_cleanup(self):
        self._exercise_actual_verifier_family(launcher=True, action="sigint")

    def test_actual_verifier_preserves_nonzero_leaf_exit_code(self):
        project = self.root / "project"
        project.mkdir()
        report = {"runs": []}
        with self.assertRaises(verify.VerificationError) as failed:
            verify.run([sys.executable, "-c", "raise SystemExit(23)"],
                       report, self.root, "version", "headless", project, timeout=3)
        self.assertEqual(failed.exception.exit_code, 23)
        self.assertEqual(report["runs"][0]["returncode"], 23)

    def test_contended_version_keeps_cli_diagnostics_out_of_child_stdout(self):
        project = self.root / "project"
        project.mkdir()
        script = textwrap.dedent(f"""\
            import json, pathlib, sys
            from forge.godot import verify
            root = pathlib.Path({str(self.root)!r})
            report = {{'runs': []}}
            text = verify.run([sys.executable, '-c', "print('4.5.1.synthetic')"],
                              report, root, 'version', 'headless', root / 'project', timeout=5)
            (root / 'version-result.json').write_text(json.dumps({{'text': text, 'report': report}}))
            """)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("GODOT_COORDINATOR_")}
        environment.update({
            "GODOT_COORDINATOR": str(self.runtime),
            "GODOT_COORDINATOR_STATE_DIR": str(self.root / "state"),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        process = None
        try:
            with verify.coordinator_lease(project=project, timeout=5,
                                          purpose="neutral contended version holder"):
                process = subprocess.Popen([sys.executable, "-c", script],
                                           cwd=Path(__file__).resolve().parents[2],
                                           env=environment, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True,
                                           start_new_session=True)
                queued = False
                end = time.monotonic() + 2
                while time.monotonic() < end and process.poll() is None:
                    state = json.loads((self.root / "state/state.json").read_text())
                    queued = any(record["status"] == "waiting"
                                 and record.get("project") == str(project.resolve())
                                 for record in state["records"])
                    if queued:
                        break
                    time.sleep(.02)
                self.assertTrue(queued, "Version probe never exercised actual project contention")
            stdout, stderr = process.communicate(timeout=4)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            result = json.loads((self.root / "version-result.json").read_text())
            self.assertEqual(result["text"], "4.5.1.synthetic\n",
                             "Coordinator admission diagnostics corrupted parsed version stdout")
            diagnostics = stderr + "\n".join(path.read_text() for path in self.root.glob("*.log"))
            self.assertIn("godot-coordinator: waiting for", diagnostics,
                          "Contention diagnostics were suppressed rather than retained")
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=2)




if __name__ == "__main__":
    unittest.main()
