# img2godot

**Modified by the img2godot fork for Blender/Godot**, from img2threejs revision
`d508b596cb221f1509f6e50ab68fce275a5cfb44` (Apache-2.0, Copyright 2026 hoainho).
The Blender/Godot route is this fork's implementation, not a claim of upstream support. Replacing the workflow
does not itself prove improved fidelity; that claim requires inspected real reference/output comparison.

Agent-driven **approved 2D image → semantic/detail analysis → asset-specific Blender authorship → textured GLB → Godot acceptance**. This standalone fork replaces the active 3D reconstruction workflow; it is not an extra review wrapper around previously generated models. It is not a pretrained image-to-mesh model, hosted reconstruction API, or Meshy client.

The skill retains the canonical name `game-asset-3d-pipeline`. Read [SKILL.md](SKILL.md) for the complete workflow, specification fields, generated-texture restrictions and artistic approval rules.

## Provenance and pinning

Fork: https://github.com/tonyseing/img2godot. Upstream: https://github.com/img2threejs/img2threejs at **`d508b596cb221f1509f6e50ab68fce275a5cfb44`**. Upstream copyright: **Copyright 2026 hoainho**, Apache-2.0; [LICENSE](LICENSE) is retained. [PROVENANCE.json](PROVENANCE.json) identifies the base and modifications. The base hash is not the modified fork deployment revision: pin the verified fork commit when deploying, and retain the installer's resource hash inventory.

The upstream `forge` helpers, `grimoire`, historical TypeScript/Three.js tools, integrations, package CLI and documentation remain in the repository for attribution/reference. They are **not** the Godot default quickstart or required runtime. No default entrypoint below invokes the legacy Three.js pipeline, environment generators or Meshy. Upstream material advice does not override this fork's generated-source-only texture policy.

**Supported img2godot installation is `python3 install.py` only.** Retained `package.json` metadata,
`bin/img2threejs.mjs` and `npx img2threejs` belong to the historical upstream development/distribution surface:
their installer targets upstream img2threejs, not this Blender/Godot skill. Do not use them to deploy img2godot.
Their retained presence is not an img2godot npm release or a compatibility shim.

## Install the canonical skill

Prerequisites: Python 3, Pillow (image probing), Blender and the actual target Godot engine(s). A configured coding agent supplies image understanding and authorship. It must be able to open the reference and capture PNGs. There is no reconstruction service API key. The product's activation-suppressed `tools/godot-window.mjs` launcher supplies real phone/tablet capture; a standalone user must provide an equivalent supported launcher, not a foreground fallback.

Clone the standalone fork at the verified deployment tag `godot-pipeline-2026-10-11`, then install:

```bash
git clone --branch godot-pipeline-2026-10-11 --single-branch https://github.com/tonyseing/img2godot.git ~/.img2godot/repo
python3 ~/.img2godot/repo/install.py
```

The installer copies the fork skill/resources to `~/.agents/skills/game-asset-3d-pipeline`. It never creates a second discoverable alias or invokes the retired workflow. Before replacement it moves recognized existing contents to **`~/.agents/skill-backups/`**, outside skill discovery. It recognizes the pinned legacy skill SHA-256 or an unmodified prior img2godot resource inventory; unrelated/modified directories and symlinks are refused. It stages the replacement first and restores the displaced directory if the atomic replacement fails. Identical installs are no-ops.

For isolated installer checks, use `--agents-home /absolute/temporary-agents-home`; do not test against the live skill directory. Restore using the exact backup path printed by installation:

```bash
python3 ~/.img2godot/repo/install.py --rollback /absolute/.agents/skill-backups/game-asset-3d-pipeline-PRINTED-DIGEST
```

Rollback refuses unrelated or altered backups and preserves any displaced current installation as another backup. Installation does not modify shipped assets, historical builds or project source. Do not install until the parent integration owner has verified the fork and rollback/refusal behavior.

## Actual reconstruction quickstart

Run from the fork checkout or installed skill directory. Paths below are deliberately explicit; the agent must author the spec and build script for the approved image, not merely copy placeholders or reuse a shipped GLB.

```bash
python3 forge/godot/pipeline.py init --work /absolute/asset-work/slug --source /absolute/approved.png
python3 forge/godot/pipeline.py spec --work /absolute/asset-work/slug --file /absolute/authored-spec.json
python3 forge/godot/pipeline.py build --work /absolute/asset-work/slug --stage blockout --script /absolute/build_slug.py --blender /Applications/Blender.app/Contents/MacOS/Blender --godot /absolute/Godot.app/Contents/MacOS/Godot --launcher /absolute/product/tools/godot-window.mjs
```

Before `spec`, the agent opens the reference, analyzes identity/silhouette/proportions, inventories visible components and fine details, records hidden-side uncertainty, researches Blender/glTF and writes the validated Godot-specific spec described in SKILL.md. The upstream component-zone/grid detail inventory is retained, but reconstruction is authored `bpy`, not TypeScript. The asset-specific build implements `--spec <JSON> --out <GLB> --stage <ID>` and uses those mapped details to construct deliberate image-matched geometry. Generic primitive stand-ins are not acceptable.

Build produces `stages/blockout/model.glb`, `blender-report.json`, `godot/godot-report.json`, and `review-template.json`. It reimports the GLB in fresh Blender for measurements and uses Godot as the visual acceptance renderer. Open every capture PNG and the source image, then fill the generated template's reviewer/verdict/observations, reference-inspection flag and due detail checks without modifying hashes.

Fill every stage-specific/authored goal in `criteriaChecks: [{criterion,verdict,observation}]`; criterion text must
match the generated `requiredEvidence` item exactly. A failed criterion blocks an overall pass, not merely advancement
to a generic checkbox. Observations describe actually inspected findings, never inferred success.

```bash
python3 forge/godot/pipeline.py review --work /absolute/asset-work/slug --stage blockout --file /absolute/filled-review.json
python3 forge/godot/pipeline.py next --work /absolute/asset-work/slug
python3 forge/godot/pipeline.py status --work /absolute/asset-work/slug
```

Repeat the same build→inspect→review commands in order for **blockout → structural → form → material → surface → lighting → interaction → optimization**. A build never auto-passes. Failed or stale hash-bound reviews block advancement. Render success does not establish visual fidelity. Optimization must preserve observed detail within declared budgets, not hide missing work.

Godot inspection can also be run directly:

```bash
python3 forge/godot/verify.py --model /absolute/model.glb --out /absolute/evidence --godot /absolute/Godot.app/Contents/MacOS/Godot --launcher /absolute/product/tools/godot-window.mjs
```

`godot-report.json` binds the model SHA-256, engine version, mode, measured facts and captures. Headless import/statistics are mandatory; the launcher adds real phone **844×390** and tablet **1280×800** renders. Do not activate/focus game windows or start a second engine while an engine is running; read saved artifacts only. Without the supported launcher there is no phone/tablet acceptance proof. Verify Godot **4.5 and 4.7** with the same GLB independently and record both reports; a single engine's success is not cross-version proof.

After all eight mechanical/visual stage gates, export a working evaluation bundle to a **new asset-work destination**:

```bash
python3 forge/godot/pipeline.py export --work /absolute/asset-work/slug --destination /absolute/asset-work/evaluation/new-asset.glb
```

This internal export preserves evidence; it does **not** authorize shipping or constitute developer artistic approval.
Only after explicit developer approval may the same reviewed GLB be published into game assets and its manifest updated.
Residual shape/UV/material defects remain visible review findings, never hidden by the export gate.

Deliver source/reference, accepted spec, authored build, texture provenance, stage measurements/reviews/captures and final textured GLB. Do not replace historical approved assets or regenerate old builds as part of installing the pipeline.

## Budgets and texture origin

Use project-specific triangle, image-resolution, decoded image-memory and GLB-file budgets. `maxTextureBytes` is total decoded RGBA memory of unique embedded images (`width × height × 4`), not compressed PNG/JPEG bytes. `maxGlbBytes` is on-disk GLB size. The exported embedded image bytes must match declared texture hashes.

Visual texture content originates only in approved AI-generated imagery. UV-map unchanged generated images, or derive transparency extraction, resize, or packing with exact source path/hash, algorithm and output hash. No painting, procedural/synthetic pixels, recoloring, inpainting or texture baking that originates new imagery. Flat colors/vertex colors alone do not satisfy textured output. Preserve source concept generation tools such as the product's `tools/gen_image.py`; concept generation is not 3D reconstruction. Meshy has no general route here (the product's existing Kit-face exception remains project-local).
