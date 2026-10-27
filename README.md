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

The historical verified deployment is **`godot-pipeline-2026-10-15`**, fork commit
`b7a1bef85a543bde3b40c638295d4e599fa49743`. Keep that tag immutable; it predates the shared coordinator.
The first scoped coordinator release **`godot-coordinator-2026-10-27`**, commit
`0720d3ff8ee1616ee6458675a8f4c5e2c05a9ce7`, stays immutable. Its verifier was subsequently found
to mishandle authenticated context and signal cleanup; do not use that pin for the repaired verifier.
The repaired scoped coordinator release is **`godot-coordinator-2026-10-27.1`**.
Its unchanged `runtime/godot_coordinator.py` SHA-256 is
`c4f749453a3dcbe6240d2f3e3b9578070abac1f4613f711cf6c2291d0415f6d9`;
the repaired `forge/godot/verify.py` SHA-256 is
`8431383b41c1497bf894938f6e26bedcd5ffa0f79b6679ae34f9755be175f789`.
Independent repair review closed and actual headless native SIGTERM cleanup passed on both engine pins.
This accepts bounded coordinator/verifier integration, not asset art, full-game visuals or device budgets.
Install from a new clean clone of the immutable tag:

```bash
git clone --branch godot-coordinator-2026-10-27.1 --single-branch https://github.com/tonyseing/img2godot.git img2godot-coordinator-2026-10-27.1
cd img2godot-coordinator-2026-10-27.1
git checkout --detach godot-coordinator-2026-10-27.1
git rev-parse 'godot-coordinator-2026-10-27.1^{commit}'
python3 install.py
```

Record the resolved full commit ID externally with the installer's resource/runtime inventory.
Do not replace the immutable tag with a moving branch or pass an invented source revision.
The [first-release receipt](docs/plans/2026-10-27-global-godot-coordination.acceptance.json)
and full-gate log retain their original source identity; they are not repinned to the repaired verifier.
See the [patch receipt](docs/plans/2026-10-27-global-godot-coordination.patch1-acceptance.json)
and [implementation plan](docs/plans/2026-10-26-global-godot-coordination.md)
for the distinct repair evidence, failed history and limits.

The installer copies the fork skill/resources to `~/.agents/skills/game-asset-3d-pipeline` and creates the same-name
Claude link under `~/.claude/skills/`. The one canonical coordinator implementation is installed **outside the
skill** at `~/.local/share/godot-coordinator/<source-sha256>/godot_coordinator.py`, with a stable executable
symlink `~/.local/bin/godot-coordinator`. No runtime implementation is copied into the skill or product project.
Python consumers resolve that link and import its public `lease(...)` API. CLI consumers execute the stable link.
Both resolve an explicit `GODOT_COORDINATOR` path first and otherwise use that exact default path; missing
coordination fails closed, never falls back to an independent lock.

The skill marker `.img2godot-install.json` retains the exact resource SHA-256 inventory/content digest, source
repository and fork revision, and coordinator dependency. Each immutable runtime version also records its exact
source repository, revision, source path and source-byte SHA-256 in `.godot-coordinator-install.json`; the hash is
both its directory name and the installed runtime hash. Source archives without Git metadata require
`--source-revision` naming the verified commit. A revision alone does not attest uncommitted contents: retain the
installer's exact resource and runtime hashes as deployment evidence.
Normal reinstall checks the current skill's complete coordinator dependency against the runtime sidecar before
reuse or publication; provenance drift is refused without replacing the skill, selector or backups. A new fork
revision with identical runtime bytes may legitimately reuse the original immutable runtime provenance.

Before replacement, recognized skills move to **`~/.agents/skill-backups/`**, outside discovery. Unrelated or
modified skill directories, coordinator executables, runtime versions and Claude entries are refused. Runtime
versions are immutable and retained; atomic link replacement selects a version. A failed activation restores
the displaced skill/link state, and rollback selects the exact coordinator dependency recorded by the backup.
Historical pre-coordinator backups leave the current external runtime untouched. Identical installs are no-ops.
Installer transactions acquire resource-specific locks for the skill directory, stable coordinator executable
and Claude entry before rechecking ownership, and retain them through publication or restoration. Concurrent
installers cannot interleave those resources. Unchanged installations use the same link-restoration behavior
on failure. Rollback compares the complete recorded runtime dependency, including source provenance, not only
the runtime hash. Explicit source revisions must be full 40- or 64-character Git object IDs.
Coordinator-era backup identities bind the resource inventory together with the skill's source repository,
source revision and complete coordinator dependency; changing only marker provenance also invalidates rollback.
Explicit historical pre-coordinator backups retain their content-only identity. Backups made by an earlier
coordinator installer without this provenance binding are not accepted as historical pre-coordinator backups.

For isolated installer checks, supply **both** `--agents-home /absolute/temporary-agents-home` and
`--claude-home /absolute/temporary-claude-home`; never test against live skill directories. Custom skill homes
derive isolated coordinator locations under the selected agents home (`godot-coordinator/` and
`bin/godot-coordinator`), rather than touching the real `~/.local`. Override them explicitly with
`--coordinator-home /absolute/temporary-versions` and `--coordinator-bin /absolute/temporary-bin/godot-coordinator`.
Reuse the same home/location arguments on rollback, with the exact backup path printed by installation:

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

`godot-report.json` binds the model SHA-256, engine version, mode, measured facts and captures. Headless import/statistics are mandatory; the launcher adds real phone **844×390** and tablet **1280×800** renders. The shared coordinator serializes each canonical project/cache root while allowing different roots to run concurrently. One capture permit covers the whole verifier batch (maximum **two capture batches** machine-wide); each headless engine/import and launcher engine owns the actual unique staged project separately. Never hold that project around a launcher that acquires it itself, or reuse an ancestor project token for another engine. Nested capture work reuses its authenticated ancestor batch permit. Timing/performance/thermal measurements and active play use fair machine-exclusive admission, draining shared work before entering. Foreign/unowned engines are never stopped. Missing coordination or unsupported shared-to-exclusive upgrades fail closed/actionably. Do not activate/focus game windows; preserve muted/headless operation and activation-suppressed capture, and read saved artifacts only. Retain launcher provenance including editor-presence evidence. Without the supported launcher there is no phone/tablet acceptance proof. Verify Godot **4.5 and 4.7** with the same GLB independently and record both reports; a single engine's success is not cross-version proof.

Residual unmanaged-engine discovery treats `--gdscript-docs`, inline or multiple selectors, and leading-dash names ending in `project.godot` as unknown project roots. Any native-bucket `project.godot` positional/operand mixed with other native arguments is also unknown: option operands cannot safely be distinguished from project positionals. A sole native absolute `project.godot` argument is known only when that file exists and its canonical parent matches the kernel-reported working directory. An absolute `--path` without ambiguous positionals is known only when its canonical root matches the kernel-reported working directory and directly contains `project.godot`. Unmanaged relative `--path` is always unknown because its post-parser resolution cannot be proved; use an absolute root or the canonical authenticated coordinator protocol. Any engine-bucket `-u`/`--upwards` is unknown: metadata existence alone cannot prove file loading or upward fallback. With no selector, only a direct `project.godot` at the actual working directory is supported; discovery never walks ancestors. Tokens after `--` or `++` belong to the ignored user bucket.

The coordinator CLI records the first cancellation without raising until explicit safe points. Owned cleanup preserves the original signal/timeout outcome while allowing authenticated nested supervisors to drain; the KILL veto and actual owned-group signal are atomic within the existing state transaction, and bounded cleanup has a **0.3-second cap**. Unknown or incomplete drains retain visible claims. Cleanup never signals foreign PIDs or a process group after its owned leader has been reaped.

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
