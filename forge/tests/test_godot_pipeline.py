"""Consumer edges for the Godot replacement; no engine launches required."""
from __future__ import annotations

import copy
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from forge.godot.pipeline import Pipeline, PipelineError, STAGES, STAGE_EVIDENCE, enforce_budgets, validate_spec, sha256, file_record, write_json


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




if __name__ == "__main__":
    unittest.main()
