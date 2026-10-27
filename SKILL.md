---
name: game-asset-3d-pipeline
description: Use when turning an approved 2D concept image into a textured 3D game asset, doing 2D-to-3D reconstruction, Blender modeling, materials or textures, mesh review, cleanup, or Godot GLB export. Applies to requests such as make this 3D and game model pipeline; do not use Meshy.
---

# img2godot: reference → authored Blender → GLB → Godot

**Modified by the img2godot fork for Blender/Godot.** Derived from img2threejs upstream revision
`d508b596cb221f1509f6e50ab68fce275a5cfb44`, Apache-2.0, Copyright 2026 hoainho. LICENSE is retained.
The Blender/Godot route is a fork modification, not an upstream-supported target. Pipeline replacement alone
does not establish better reference fidelity; claim improvement only after inspected real before/after comparison.

This is an agent skill with deterministic tools, not an image-to-mesh service or a pretrained model. Read the image, reason about its structure, author asset-specific `bpy`, and judge actual exported output. The canonical skill name remains `game-asset-3d-pipeline`. Commands below run from this skill directory, which contains the fork resources. Read `README.md` for installation and provenance; `PROVENANCE.json` identifies the upstream Apache-2.0 base. Historical Three.js helpers are not the default entrypoint.

## Intake and immutable reference

Use the approved isolated concept image and available orthographic views. Respect the harness/user image-model selection; do not silently select a model or fabricate a service API. Confirm role, dimensions, rigging, topology, destination, triangle and texture budgets from project context before asking for missing information. Default role guidance: hero 30–80k triangles and 2–4k textures; props 1–2k textures; background 5–20k triangles; far-field ≤5k and 512–1k textures. Project-specific tighter limits win. Record maximum texture bytes and GLB bytes, not resolution alone. Never increase a budget without developer approval.

`maxTextureBytes` is decoded RGBA memory of unique embedded images (`width × height × 4`), not compressed
PNG/JPEG size; `maxGlbBytes` is on-disk GLB bytes. Measurements must use the exported embedded images.

```bash
python3 forge/godot/pipeline.py init --work /absolute/asset-work/slug --source /absolute/approved.png
```

Source paths and hashes are immutable. Existing approved assets/builds are historical evidence, not candidates for automatic regeneration. Do not retrofit gates around an old GLB as a substitute for reference-conditioned reconstruction.

## Image analysis and authored specification

Open the reference with an image-reading tool. Describe semantic identity, silhouette, proportions, component relationships, materials, color regions, fine details and hidden-side uncertainty. Preserve upstream's systematic inventory: component-zone scanning for simple assets, 3×3/4×4 grid scanning for complex images. Inventory minimums are 3/6/10/16 details for simple/moderate/complex/ultra-complex. These are coverage floors, not quotas that justify inventing features. Map every observed detail to a named component feature or material override, with normalized image region, scale, visual effects, confidence and the first stage that must reproduce it. Read upstream `grimoire/intake/` references when deeper analysis guidance is needed, but do not follow their Three.js output route.

Before any build, research the installed Blender version's asset-specific modeling, topology, UV mapping and glTF export using at least two primary URLs. Research each new material type's export behavior. Record topic, URL and applied takeaway in the spec. Principled image textures survive glTF; procedural shader nodes do not. Do not bake synthetic/procedural imagery to bypass the generated-source policy.

Write a real JSON spec, not a generic mesh recipe:
- `schemaVersion: 1`, `assetId`, `role`, `source: {path, sha256}` matching intake.
- `analysis: {summary, complexity, knownUnknowns, detailInventory: {scanMethod, details}}`; each detail has `id`, `kind`, `description`, `region: {x,y,width,height,units:"normalized"}`, `scale`, `affects`, `mapsTo: {type,ref}`, `confidence`, `stage`. Mapping types are `component.localFeatures` and `material.localOverrides`; refs use `componentId.featureId` or `materialId.overrideId`.
- `components: [{id,description,localFeatures:[{id,description}]}]`, `materials: [{id,description,localOverrides:[{id,description}]}]`, `research: [{topic,url,takeaway}]`.
- `coordinates: {units:"meters",up:"Y",forward:"-Z",pivot:"ground"}`; `forward` may instead be `"+X"` for an explicit consumer contract such as this product's creatures.
- `budgets: {maxTriangles,maxTextureDimension,maxTextureBytes,maxGlbBytes}` with positive integer limits.
- `textures: [{path,sha256,source:{path,sha256},processing:{operation,algorithm}}]`, with absolute paths.
- `stageGoals` maps every stage ID below to `{description,acceptance:[strings]}`. Goals must reflect this image, role and consumer, not generic checkboxes.

```bash
python3 forge/godot/pipeline.py spec --work /absolute/asset-work/slug --file /absolute/authored-spec.json
```

## Texture origin and provenance

Ship real UV-mapped image textures, not bare meshes, flat material colors or vertex colors alone. Texture visual content must originate in approved AI-generated source imagery. Map unchanged generated image bytes directly when appropriate. Deterministic derivation is limited to extracting transparency, resizing, or packing generated pieces into a container/atlas. Every processed output records source path and SHA-256, the exact algorithm, and output SHA-256; unchanged sources use operation `unchanged`. No drawing, recoloring, inpainting, synthetic pixels, procedural textures, generated normal/ORM bakes, or fabricated fallbacks. Requested channels that cannot be sourced compliantly are a missing prerequisite, not permission to synthesize them. Geometry authored deliberately from image analysis is permitted; generic primitive stand-ins are defects.

## Staged reconstruction and visual loop

Author a dedicated build script accepting `--spec <JSON> --out <GLB> --stage <ID>`. It must actually use the accepted image analysis and detail/component mappings to build this asset. Use meters, Y-up, declared -Z or +X forward and a ground pivot. Emit the consumer coordinate frame directly before measurements/reviews; do not rotate a reviewed export afterward. Keep Blender changes reversible. Stage IDs, in order:

| ID | Reconstruction focus |
| --- | --- |
| `blockout` | Reference silhouette, proportions, component placement |
| `structural` | Component relationships and functional construction |
| `form` | Asset-specific shape, curvature, secondary forms |
| `material` | UVs and compliant image-backed materials |
| `surface` | Visible source-derived local detail and seam quality |
| `lighting` | Material readability in acceptance lighting |
| `interaction` | Intended camera/distance, scale/pivot, required rig/contact behavior |
| `optimization` | Measured budgets without losing approved visual detail |

For each stage, run the same asset-specific script through the core:

```bash
python3 forge/godot/pipeline.py build --work /absolute/asset-work/slug --stage blockout --script /absolute/build_slug.py --blender /Applications/Blender.app/Contents/MacOS/Blender --godot /absolute/Godot.app/Contents/MacOS/Godot --launcher /absolute/product/tools/godot-window.mjs
```

The core calls background Blender with `-- --spec ... --out ... --stage ...`, exports `stages/<ID>/model.glb`, inspects the GLB in fresh Blender, and invokes Godot. Evidence includes `blender-report.json`, `godot/godot-report.json` and `review-template.json`. The default output is GLB; no TypeScript generation, browser or Three.js runtime is required.

Use the one external shared Godot coordinator installed by `install.py`, resolved from explicit `GODOT_COORDINATOR` or `~/.local/bin/godot-coordinator`; a missing runtime fails closed. Do not add a project/skill-local implementation or independent lock. One actual engine owns each canonical project/cache root at a time; different roots may run concurrently. The verifier holds one capture-batch permit for its whole verification (at most two capture batches machine-wide), with no project key on the batch. Each headless import/inspection and launcher engine claims the actual unique staged project separately; never double-hold a launcher's project or treat an ancestor project token as reusable authorization for sibling engines. Valid ancestor capture context reuses its batch permit. Timing/performance/thermal work and active play require fair machine-exclusive admission; unsupported shared-to-exclusive upgrades fail actionably. Foreign/unowned jobs stay untouched.

Never activate/focus a window. Preserve muted operation and mandatory headless inspection. Real phone 844×390 and tablet 1280×800 captures use only the project's activation-suppressed launcher; no direct headed fallback. Read saved PNGs/logs, never drive the background game window. Record whether each run was headless or activation-suppressed capture and retain launcher editor-presence provenance. Use live-editor MCP only for structural inspection/authoring, never `project_run` while the user works. Both historical tags `godot-pipeline-2026-10-15` and `godot-coordinator-2026-10-27` stay immutable. The repaired scoped release is `godot-coordinator-2026-10-27.1`, verifier SHA-256 `8431383b41c1497bf894938f6e26bedcd5ffa0f79b6679ae34f9755be175f789`, unchanged runtime SHA-256 `c4f749453a3dcbe6240d2f3e3b9578070abac1f4613f711cf6c2291d0415f6d9`. Independent review closed and actual headless native SIGTERM cleanup passed on both engine pins. Follow README's clean-clone instructions and record the resolved tag commit externally. Coordinator acceptance is not asset art, full-game visual or device-budget approval.

Open the reference and **every capture PNG** yourself after every build/revision. Inspect image-matched silhouette/proportions, every due inventory detail, image textures and seams, normals, game-distance readability, scale/pivot/orientation, and intended interaction. A successful render command is not a visual pass. Fill the generated review template without changing its bound hashes: reviewer, `verdict: "pass"|"fail"`, observations, `inspectedReference: true`, and `detailChecks: [{id,verdict,observation}]` for every detail due at this stage.

Also fill every `criteriaChecks` item: `{criterion,verdict,observation}` with `criterion` exactly matching its
`requiredEvidence` string. These cover stage-specific and authored acceptance goals; inspect and describe each
actual finding. Any failed criterion prevents an overall pass, even if the mapped detail checks pass.

```bash
python3 forge/godot/pipeline.py review --work /absolute/asset-work/slug --stage blockout --file /absolute/filled-review.json
python3 forge/godot/pipeline.py next --work /absolute/asset-work/slug
python3 forge/godot/pipeline.py status --work /absolute/asset-work/slug
```

Repeat build→view→review for all eight stages. Failed reviews block advancement. Source/spec/build/model changes invalidate corresponding acceptance; rebuild and inspect new artifacts, never reuse stale review. A mechanical pass is not developer artistic approval. Present phone/tablet renders and measured facts for explicit approval; after two artistic revision rounds ask whether to continue or park, never silently accept the closest output.

## Compatibility, approval and export

Measure actual imported GLB geometry/materials/textures, bounds and file bytes. Verify the real target Godot 4.5 consumer and Godot 4.7 compatibility independently; one engine's report cannot establish both. Use the same GLB hash for evidence. Textured output and the stricter project budgets remain required even if runtime shading flattens its colors. Rig in Blender when required, never substitute a humanoid auto-rigger for a quadruped.

After all eight mechanical/visual stage gates, export a working evaluation bundle to a **new asset-work destination**:

```bash
python3 forge/godot/pipeline.py export --work /absolute/asset-work/slug --destination /absolute/asset-work/evaluation/new-asset.glb
```

Internal evaluation export does not grant artistic approval or authorize shipping. Publish the same reviewed GLB into
game assets and update its manifest only after explicit developer artistic approval. Preserve residual findings honestly.

Deliver the GLB, original immutable reference, accepted spec, authored build, texture source/processing provenance, stage measurements/reviews and capture evidence. Update the destination manifest/docs without rewriting unrelated approved assets or builds. State unrigged output, hidden-side assumptions and compatibility gaps honestly. No Meshy, old environment generators, alias shim or review-only wrapper is a substitute for this pipeline.
