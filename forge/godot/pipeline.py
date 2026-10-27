#!/usr/bin/env python3
"""Reference-conditioned, agent-authored bpy → GLB → Godot reconstruction.

Apache-2.0. Adapted from img2threejs d508b596cb221f1509f6e50ab68fce275a5cfb44:
probe_image.probe and glb_container.parse_glb are reused directly;
build_detail_inventory.TARGET_MIN_DETAILS and normalized inspection zones are reused;
validate_sculpt_spec.validate_detail_inventory's mapped feature coverage and
orchestrate_passes.completed_passes/review_completes_pass's contiguous visual
admission are ported below. Their Three.js sculpt-spec validators and factory
are NOT compatible with arbitrary Blender specs, and are not called here.

diagnose_render's tier-1-before-vision and shape-before-material doctrine is
retained: fresh imported facts precede vision; clay is allowed before material.
Technical success is never visual success. Semantic analysis and asset-specific
code must be authored by an agent inspecting the immutable reference, just as
upstream reconstructs by authored code. No automatic image-to-mesh claim.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from forge._shared.glb_container import parse_glb
from forge.stage1_intake.probe_image import probe, detect_size
from forge.stage1_intake.build_detail_inventory import TARGET_MIN_DETAILS, build_zones

STAGES = ('blockout', 'structural', 'form', 'material', 'surface', 'lighting', 'interaction', 'optimization')
# Ported stage evidence; no browser, generated pixel baking, or incompatible PBR mandate.
STAGE_EVIDENCE = {
    'blockout': ['reference silhouette and primary proportions without relying on materials'],
    'structural': ['component hierarchy, attachment/ground contacts, spacing and negative spaces'],
    'form': ['reference-specific curvature, asymmetry and local geometric features'],
    'material': ['UV-mapped generated image texture, visible palette and material response'],
    'surface': ['mapped identity details, local surface variation and seam/stretch inspection'],
    'lighting': ['neutral and grazing-light material readability; no lighting baked into new pixels'],
    'interaction': ['game-distance phone/tablet readability, meter scale, ground pivot and orientation'],
    'optimization': ['fresh triangle/texture budgets and final renders retaining every accepted detail'],
}


class PipelineError(ValueError):
    pass


def sha256(path):
    with Path(path).open('rb') as handle:
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise PipelineError(f'{path}: expected JSON object')
    return value


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def require(condition, message):
    if not condition:
        raise PipelineError(message)


def prose(value):
    return isinstance(value, str) and bool(value.strip()) and value.strip().lower() not in {'unassessed', 'todo', 'unknown', 'n/a'}


def strings(value):
    return isinstance(value, list) and bool(value) and all(prose(item) for item in value)


def file_record(path):
    path = Path(path).expanduser().resolve()
    require(path.is_file(), f'missing input: {path}')
    return {'path': str(path), 'sha256': sha256(path)}


def copy_accepted_file(path, target, expected_sha256):
    require(sha256(path) == expected_sha256, f'publication input does not match accepted hash: {path}')
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, target)
    require(sha256(target) == expected_sha256, f'publication copy does not match accepted hash: {path}')


def check_record(record, label):
    require(isinstance(record, dict) and prose(record.get('path')), f'{label}: missing path/hash')
    require(Path(record['path']).is_absolute(), f'{label}: path must be absolute')
    require(file_record(record['path']) == {'path': record['path'], 'sha256': record.get('sha256')}, f'{label}: stale input/hash')


def validate_spec(spec, source):
    """Godot schema admission, not the upstream Three.js sculpt-spec validator."""
    require(spec.get('schemaVersion') == 1, 'spec schemaVersion must be 1')
    require(prose(spec.get('assetId')) and prose(spec.get('role')), 'spec needs assetId and role')
    require(spec.get('source') == source, 'spec source must match immutable intake')
    check_record(source, 'source')
    coordinates = spec.get('coordinates', {})
    require(coordinates.get('units') == 'meters' and coordinates.get('up') == 'Y' and coordinates.get('forward') in {'-Z', '+X'} and coordinates.get('pivot') == 'ground', 'coordinates must be meters, Y-up, explicit -Z or +X forward, ground pivot')
    budgets = spec.get('budgets', {})
    for key in ('maxTriangles', 'maxTextureDimension', 'maxTextureBytes', 'maxGlbBytes'):
        require(type(budgets.get(key)) is int and budgets[key] > 0, f'budget {key} must be positive integer')
    analysis = spec.get('analysis', {})
    require(prose(analysis.get('summary')) and strings(analysis.get('knownUnknowns')), 'analysis needs visual summary and explicit knownUnknowns')
    complexity = analysis.get('complexity')
    require(complexity in TARGET_MIN_DETAILS, 'analysis complexity must be an upstream complexity tier')
    links = {'component.localFeatures': set(), 'material.localOverrides': set()}
    for collection, features, kind in [('components', 'localFeatures', 'component.localFeatures'), ('materials', 'localOverrides', 'material.localOverrides')]:
        items = spec.get(collection)
        require(isinstance(items, list) and items, f'{collection} must describe observed asset')
        ids = set()
        for item in items:
            require(isinstance(item, dict) and prose(item.get('id')) and prose(item.get('description')), f'{collection} needs id/description')
            require(item['id'] not in ids, f'duplicate {collection} id')
            ids.add(item['id'])
            require(isinstance(item.get(features), list), f'{collection} needs {features}')
            for feature in item[features]:
                require(isinstance(feature, dict) and prose(feature.get('id')) and prose(feature.get('description')), f'{features} needs id/description')
                link = f"{item['id']}.{feature['id']}"
                require(link not in links[kind], f'duplicate feature {link}')
                links[kind].add(link)
    inventory = analysis.get('detailInventory', {})
    require(inventory.get('scanMethod') in {'component-zones', 'grid-3x3', 'grid-4x4'}, 'detailInventory requires zone-by-zone scanMethod')
    details = inventory.get('details')
    minimum = TARGET_MIN_DETAILS[complexity]
    require(isinstance(details, list) and len(details) >= minimum, f'{complexity} requires at least {minimum} observed identity details')
    seen = set()
    for detail in details:
        require(isinstance(detail, dict), 'detail must be object')
        for key in ('id', 'kind', 'description', 'scale', 'affects'):
            require(prose(detail.get(key)), f'detail needs observed {key}')
        require(detail['id'] not in seen, 'duplicate detail id')
        seen.add(detail['id'])
        maps = detail.get('mapsTo', {})
        require(maps.get('ref') in links.get(maps.get('type'), set()), f"detail {detail['id']} must map to real component.localFeatures/material.localOverrides")
        require(detail.get('stage') in STAGES, 'detail needs first-required stage')
        confidence = detail.get('confidence')
        require(type(confidence) in (int, float) and math.isfinite(confidence) and 0 < confidence <= 1, 'detail confidence must be observed, in (0,1]')
        region = detail.get('region', {})
        require(region.get('units') == 'normalized', 'detail region must use normalized source coordinates')
        for key in ('x', 'y', 'width', 'height'):
            require(type(region.get(key)) in (int, float) and math.isfinite(region[key]), 'detail needs finite normalized region')
        require(0 <= region['x'] < 1 and 0 <= region['y'] < 1 and 0 < region['width'] <= 1 - region['x'] + 1e-9 and 0 < region['height'] <= 1 - region['y'] + 1e-9, 'detail region outside source')
    research = spec.get('research')
    require(isinstance(research, list) and len(research) >= 2, 'research requires at least two source lookups')
    for entry in research:
        require(isinstance(entry, dict) and all(prose(entry.get(k)) for k in ('topic', 'url', 'takeaway')) and entry['url'].startswith('https://'), 'research needs topic, HTTPS URL, applied takeaway')
    goals = spec.get('stageGoals', {})
    for stage in STAGES:
        goal = goals.get(stage, {})
        require(prose(goal.get('description')) and strings(goal.get('acceptance')), f'{stage}: specify reconstruction and acceptance')
    textures = spec.get('textures')
    require(isinstance(textures, list) and textures, 'spec requires generated source textures')
    for texture in textures:
        check_record(texture, 'texture')
        check_record(texture.get('source'), 'generated texture source')
        processing = texture.get('processing', {})
        require(processing.get('operation') in {'unchanged', 'alpha', 'resize', 'pack'} and prose(processing.get('algorithm')), 'texture processing permits only identity, alpha, resize or pack with exact algorithm')
        if processing['operation'] == 'unchanged':
            require(texture['sha256'] == texture['source']['sha256'], 'unchanged texture must preserve source bytes')
        require(detect_size(Path(texture['path']).read_bytes()) is not None, 'texture dimensions unreadable')
    return spec


def enforce_budgets(facts, budgets, stage):
    require(type(facts.get('triangles')) is int and 0 < facts['triangles'] <= budgets['maxTriangles'], 'fresh imported triangles outside budget')
    require(0 < facts.get('glbBytes', 0) <= budgets['maxGlbBytes'], 'GLB bytes outside budget')
    images = facts.get('textures', [])
    total = 0
    for texture in images:
        width, height = texture.get('width'), texture.get('height')
        require(type(width) is int and type(height) is int and 0 < width <= budgets['maxTextureDimension'] and 0 < height <= budgets['maxTextureDimension'], 'texture dimension outside budget')
        total += width * height * 4
    require(total <= budgets['maxTextureBytes'], 'decoded RGBA texture bytes outside budget')
    if STAGES.index(stage) >= STAGES.index('material'):
        require(facts.get('textureCount', 0) > 0 and images and not facts.get('untexturedMeshes'), 'material and final stages require UV image-textured meshes, not vertex colors/flat materials')
    return {'decodedTextureBytes': total, 'triangles': facts['triangles'], 'glbBytes': facts['glbBytes']}


def embedded_texture_provenance(model, spec):
    document, binary, _ = parse_glb(model)
    require(document.get('asset', {}).get('version') == '2.0', 'GLB must declare glTF 2.0')
    require(all(not buffer.get('uri') for buffer in document.get('buffers', [])), 'self-contained GLB required')
    declared = {t['sha256'] for t in spec['textures']}
    embedded = []
    for image in document.get('images', []):
        require('uri' not in image and type(image.get('bufferView')) is int, 'GLB images must be embedded')
        index = image['bufferView']
        views = document.get('bufferViews', [])
        require(0 <= index < len(views), 'GLB image bufferView missing')
        view = views[index]
        start, length = view.get('byteOffset', 0), view.get('byteLength', 0)
        require(view.get('buffer', 0) == 0 and start >= 0 and length > 0 and start + length <= len(binary), 'GLB image bytes invalid')
        payload = binary[start:start + length]
        digest = hashlib.sha256(payload).hexdigest()
        require(digest in declared, 'embedded image bytes do not match declared generated texture provenance; synthetic/reencoded textures not admitted')
        embedded.append({'sha256': digest, 'bytes': length, 'dimensions': detect_size(payload)})
    return embedded


def run_command(command, log):
    # Preserve the inherited environment, including authenticated coordinator
    # context. The verifier owns its batch; only its engine leaves claim caches.
    result = subprocess.run([str(part) for part in command], capture_output=True, text=True, check=False)
    Path(log).write_text(result.stdout + '\n' + result.stderr, encoding='utf-8')
    require(result.returncode == 0, f'command failed ({result.returncode}); see {log}')
    return {'command': [str(part) for part in command], 'returncode': result.returncode, 'log': str(log)}


def godot_evidence(report_path, model_hash, require_visual=True):
    report = read_json(report_path)
    require(report.get('status') == 'passed' and not report.get('errors'), 'Godot verifier did not pass')
    require(report.get('modelSha256') == model_hash, 'Godot report model hash is stale')
    captures = report.get('captures', [])
    if require_visual:
        require(report.get('mode') == 'activation-suppressed capture' and captures, 'stage acceptance requires real Godot surface captures; headless facts alone are not visual evidence')
        profiles = set()
        for capture in captures:
            check_record(capture, 'Godot capture')
            dimensions = detect_size(Path(capture['path']).read_bytes())
            require(dimensions == (capture.get('width'), capture.get('height')), 'capture dimensions do not match actual PNG')
            profiles.add(dimensions)
        require({(844, 390), (1280, 800)} <= profiles, 'Godot review requires real phone and tablet captures')
    return report


def crosscheck_godot(report, blender_facts, spec, stage):
    facts = report.get('facts', {})
    require(facts.get('triangles') == blender_facts['triangles'], 'Godot and fresh Blender triangle measurements disagree')
    enforce_budgets({**facts, 'glbBytes': blender_facts['glbBytes']}, spec['budgets'], stage)
    position = facts.get('bounds', {}).get('position', [])
    require(isinstance(position, list) and len(position) == 3 and type(position[1]) in (int, float) and math.isfinite(position[1]) and abs(position[1]) <= 0.01, 'declared ground pivot needs imported Godot minY within 1cm of zero')


class Pipeline:
    def __init__(self, work):
        self.work = Path(work).expanduser().resolve()
        self.state_path = self.work / 'state.json'
        self.state = read_json(self.state_path) if self.state_path.exists() else None

    def save(self):
        write_json(self.state_path, self.state)

    def init(self, source):
        require(self.state is None, 'work already initialized; choose new work directory for changed reference')
        source = file_record(source)
        intake = probe(Path(source['path']))
        require(intake['width'] and intake['height'], 'reference image dimensions unreadable')
        self.state = {'schemaVersion': 1, 'source': source, 'imageProbe': intake, 'inspectionZones': build_zones('grid-3x3', None), 'stages': {}}
        self.save()
        write_json(self.work / 'intake.json', {**self.state, 'instruction': 'Open immutable source and scan zones; author semantic analysis/detail mapping and asset-specific bpy. No automatic semantic analysis has run.'})
        return self.status()

    def source_fresh(self):
        require(self.state is not None, 'run init first')
        check_record(self.state['source'], 'source')

    def admit_spec(self, path):
        self.source_fresh()
        record = file_record(path)
        require(Path(record['path']) != (self.work / 'spec.json').resolve(), 'authored spec must be outside reserved work/spec.json; use an external authored file')
        spec = read_json(record['path'])
        base = Path(record['path']).parent
        records = [spec.get('source', {})]
        for texture in spec.get('textures', []):
            records.extend((texture, texture.get('source', {})))
        for item in records:
            if prose(item.get('path')):
                item['path'] = str((base / Path(item['path']).expanduser()).resolve())
        spec = validate_spec(spec, self.state['source'])
        write_json(self.work / 'spec.json', spec)
        self.state['specInput'] = record
        self.state['specSha256'] = sha256(self.work / 'spec.json')
        self.state['stages'] = {}
        self.state.pop('script', None)
        self.save()
        return self.status()

    def spec(self):
        self.source_fresh()
        require('specInput' in self.state, 'run spec with visually authored analysis first')
        check_record(self.state['specInput'], 'authored spec')
        require(sha256(self.work / 'spec.json') == self.state['specSha256'], 'accepted spec bytes changed; re-admit spec')
        return validate_spec(read_json(self.work / 'spec.json'), self.state['source'])

    def artifact_binding(self, stage):
        self.spec()
        check_record(self.state.get('script'), 'build script')
        entry = self.state['stages'].get(stage, {})
        require(entry.get('status') in {'built', 'accepted', 'rejected'}, f'{stage}: no successful build')
        binding = entry['binding']
        require(binding['sourceSha256'] == self.state['source']['sha256'] and binding['specSha256'] == self.state['specSha256'] and binding['scriptSha256'] == self.state['script']['sha256'], 'build input hashes are stale')
        for key, record_key in [('modelSha256', 'model'), ('blenderReportSha256', 'blenderReport'), ('godotReportSha256', 'godotReport')]:
            require(sha256(entry[record_key]) == binding[key], f'{stage}: stale {record_key} artifact')
        facts = read_json(entry['blenderReport'])
        require(facts.get('status') == 'passed' and facts.get('modelSha256') == binding['modelSha256'], 'fresh Blender import report invalid')
        enforce_budgets(facts, self.spec()['budgets'], stage)
        embedded_texture_provenance(Path(entry['model']), self.spec())
        report = godot_evidence(entry['godotReport'], binding['modelSha256'])
        crosscheck_godot(report, facts, self.spec(), stage)
        return binding

    def completed(self):
        # Port of upstream completed_passes: never credit a stage beyond a gap.
        completed = []
        for stage in STAGES:
            entry = self.state['stages'].get(stage, {})
            if entry.get('status') != 'accepted':
                break
            try:
                binding = self.artifact_binding(stage)
                require(sha256(entry['reviewPath']) == entry['reviewSha256'], 'accepted review bytes changed')
                review = read_json(entry['reviewPath'])
                self.validate_review(stage, review, binding)
            except (PipelineError, OSError, KeyError, ValueError, TypeError):
                break
            completed.append(stage)
        return completed

    def status(self):
        try:
            self.source_fresh()
        except (PipelineError, OSError) as exc:
            return {'nextAction': 'init', 'nextStage': 'blockout', 'acceptedStages': [], 'reason': str(exc)}
        try:
            spec = self.spec()
        except (PipelineError, OSError, KeyError, ValueError) as exc:
            return {'nextAction': 'spec', 'nextStage': 'blockout', 'acceptedStages': [], 'reason': str(exc)}
        accepted = self.completed()
        if len(accepted) == len(STAGES):
            return {'nextAction': 'export', 'nextStage': 'complete', 'acceptedStages': accepted}
        stage = STAGES[len(accepted)]
        action = 'build'
        reason = None
        entry = self.state['stages'].get(stage, {})
        if entry.get('status') == 'built':
            try:
                self.artifact_binding(stage)
                action = 'review'
            except (PipelineError, OSError, KeyError, ValueError) as exc:
                reason = str(exc)
        return {'nextAction': action, 'nextStage': stage, 'acceptedStages': accepted, 'reason': reason,
                'requiredEvidence': STAGE_EVIDENCE[stage] + spec['stageGoals'][stage]['acceptance'],
                'reviewTemplate': str(self.work / 'stages' / stage / 'review-template.json')}

    def build(self, stage, script, blender, godot, launcher):
        spec = self.spec()
        require(stage in STAGES, 'unknown stage')
        record = file_record(script)
        # New authored code invalidates all stages, not only final review.
        if self.state.get('script') != record:
            self.state['stages'] = {}
            self.state['script'] = record
            self.save()
        accepted = self.completed()
        require(STAGES.index(stage) <= len(accepted), f'build must start with {STAGES[min(len(accepted), len(STAGES)-1)]}; previous stages need accepted visual reviews')
        for downstream in STAGES[STAGES.index(stage):]:
            self.state['stages'].pop(downstream, None)
        directory = self.work / 'stages' / stage
        directory.mkdir(parents=True, exist_ok=True)
        model = directory / 'model.glb'
        blender_report = directory / 'blender-report.json'
        report_path = directory / 'godot' / 'godot-report.json'
        for output in (model, blender_report, report_path):
            output.unlink(missing_ok=True)
        self.state['stages'][stage] = {'status': 'building'}
        self.save()
        try:
            runs = [run_command([blender, '--background', '--python-exit-code', '1', '--python', record['path'], '--', '--spec', self.work / 'spec.json', '--out', model, '--stage', stage], directory / 'build.log')]
            require(model.is_file(), 'authoring script succeeded but did not export requested GLB')
            check_record(record, 'build script')
            self.spec()
            embedded = embedded_texture_provenance(model, spec)
            runs.append(run_command([blender, '--background', '--python-exit-code', '1', '--python', Path(__file__).with_name('inspect_blender.py'), '--', '--model', model, '--out', blender_report], directory / 'inspect.log'))
            facts = read_json(blender_report)
            require(facts.get('modelSha256') == sha256(model), 'fresh Blender inspector did not inspect requested GLB')
            measured = enforce_budgets(facts, spec['budgets'], stage)
            command = [sys.executable, Path(__file__).with_name('verify.py'), '--model', model,
                       '--out', report_path.parent, '--godot', godot,
                       '--purpose', f'img2godot {self.work.name} {stage}']
            if launcher:
                command += ['--launcher', launcher]
            runs.append(run_command(command, directory / 'verify.log'))
            report = godot_evidence(report_path, sha256(model), require_visual=True)
            crosscheck_godot(report, facts, spec, stage)
            binding = {'sourceSha256': self.state['source']['sha256'], 'specSha256': self.state['specSha256'], 'scriptSha256': record['sha256'],
                       'modelSha256': sha256(model), 'blenderReportSha256': sha256(blender_report), 'godotReportSha256': sha256(report_path)}
            self.state['stages'][stage] = {'status': 'built', 'model': str(model), 'blenderReport': str(blender_report), 'godotReport': str(report_path),
                                         'binding': binding, 'measurements': measured, 'embeddedImages': embedded, 'runs': runs}
            self.save()
            template = {**binding, 'stage': stage, 'reviewer': '', 'verdict': '', 'inspectedReference': False, 'observations': [],
                        'requiredEvidence': STAGE_EVIDENCE[stage] + spec['stageGoals'][stage]['acceptance'],
                        'criteriaChecks': [{'criterion': c, 'verdict': '', 'observation': ''} for c in dict.fromkeys(STAGE_EVIDENCE[stage] + spec['stageGoals'][stage]['acceptance'])],
                        'captures': [{k: c[k] for k in ('path', 'sha256')} for c in report['captures']],
                        'detailChecks': [{'id': d['id'], 'verdict': '', 'observation': ''} for d in spec['analysis']['detailInventory']['details'] if STAGES.index(d['stage']) <= STAGES.index(stage)]}
            write_json(directory / 'review-template.json', template)
        except Exception as exc:
            self.state['stages'][stage] = {'status': 'failed', 'error': str(exc)}
            self.save()
            raise
        return self.status()

    def validate_review(self, stage, review, binding):
        require(review.get('stage') == stage and all(review.get(k) == v for k, v in binding.items()), 'review binding does not match fresh source/spec/script/GLB/render hashes')
        require(prose(review.get('reviewer')) and review.get('verdict') in {'pass', 'fail'} and strings(review.get('observations')), 'vision review needs reviewer, verdict and observed findings')
        require(review.get('inspectedReference') is True, 'vision reviewer must inspect immutable reference')
        criteria = set(STAGE_EVIDENCE[stage] + self.spec()['stageGoals'][stage]['acceptance'])
        criteria_checks = review.get('criteriaChecks', [])
        require(isinstance(criteria_checks, list) and len(criteria_checks) == len(criteria) and {c.get('criterion') for c in criteria_checks} == criteria, 'vision review must observe every stage acceptance criterion')
        for check in criteria_checks:
            require(check.get('verdict') in {'pass', 'fail'} and prose(check.get('observation')), 'stage criterion verdict needs observed finding')
        if review['verdict'] == 'pass':
            require(all(c['verdict'] == 'pass' for c in criteria_checks), 'failed stage criterion prevents acceptance')
        entry = self.state['stages'][stage]
        report = godot_evidence(entry['godotReport'], binding['modelSha256'])
        expected = {c['path']: c['sha256'] for c in report['captures']}
        actual = {c.get('path'): c.get('sha256') for c in review.get('captures', [])}
        require(actual == expected and len(review.get('captures', [])) == len(expected), 'review must bind every real Godot capture')
        details = self.spec()['analysis']['detailInventory']['details']
        due = {d['id'] for d in details if STAGES.index(d['stage']) <= STAGES.index(stage)}
        checks = review.get('detailChecks', [])
        require(isinstance(checks, list) and len(checks) == len(due) and {c.get('id') for c in checks} == due, 'vision review must cover all currently due mapped details')
        for check in checks:
            require(check.get('verdict') in {'pass', 'fail'} and prose(check.get('observation')), 'detail verdict needs observed failure/pass description')
        if review['verdict'] == 'pass':
            require(all(c['verdict'] == 'pass' for c in checks), 'failed identity detail prevents stage acceptance')
        return review['verdict'] == 'pass'

    def review(self, stage, path):
        require(stage in STAGES, 'unknown stage')
        binding = self.artifact_binding(stage)
        require(STAGES.index(stage) <= len(self.completed()), 'review cannot advance past earlier unaccepted stage')
        review = read_json(path)
        passed = self.validate_review(stage, review, binding)
        for downstream in STAGES[STAGES.index(stage)+1:]:
            self.state['stages'].pop(downstream, None)
        owned = self.work / 'stages' / stage / 'review.json'
        write_json(owned, review)
        self.state['stages'][stage].update(status='accepted' if passed else 'rejected', reviewPath=str(owned), reviewSha256=sha256(owned))
        self.save()
        return self.status()

    def export(self, destination):
        spec = self.spec()
        require(self.completed() == list(STAGES), 'publication requires all eight stages accepted with fresh evidence')
        final = self.state['stages']['optimization']
        self.artifact_binding('optimization')
        destination = Path(destination).expanduser().resolve()
        provenance = destination.with_suffix('.provenance')
        require(destination.suffix.lower() == '.glb', 'destination must explicitly name a .glb')
        require(not destination.exists() and not provenance.exists(), 'publication refuses to overwrite destination/provenance')
        destination.parent.mkdir(parents=True, exist_ok=True)
        provenance.mkdir()
        files = []
        def copy(path, relative, expected_sha256):
            target = provenance / relative
            copy_accepted_file(path, target, expected_sha256)
            files.append({'path': relative, 'sha256': expected_sha256})
            return target
        try:
            copy(self.state['source']['path'], 'reference/' + Path(self.state['source']['path']).name, self.state['source']['sha256'])
            copy(self.state['script']['path'], 'build/' + Path(self.state['script']['path']).name, self.state['script']['sha256'])
            copy(self.work / 'spec.json', 'spec.json', self.state['specSha256'])
            copy(self.state['specInput']['path'], 'analysis-spec-input.json', self.state['specInput']['sha256'])
            # Deliberate validated in-memory snapshot, not unaccepted live disk state.
            write_json(provenance / 'state.json', self.state)
            files.append({'path': 'state.json', 'sha256': sha256(provenance / 'state.json')})
            for index, texture in enumerate(spec['textures']):
                copy(texture['path'], f'textures/{index}/' + Path(texture['path']).name, texture['sha256'])
                copy(texture['source']['path'], f'texture-sources/{index}/' + Path(texture['source']['path']).name, texture['source']['sha256'])
            for stage in STAGES:
                entry = self.state['stages'][stage]
                binding = entry['binding']
                expected_hashes = {'model': binding['modelSha256'], 'blenderReport': binding['blenderReportSha256'], 'godotReport': binding['godotReportSha256'], 'reviewPath': entry['reviewSha256']}
                copied = {}
                for key, expected_hash in expected_hashes.items():
                    copied[key] = copy(entry[key], f'stages/{stage}/' + Path(entry[key]).name, expected_hash)
                report = read_json(copied['godotReport'])
                for index, capture in enumerate(report['captures']):
                    copy(capture['path'], f'stages/{stage}/captures/{index}-' + Path(capture['path']).name, capture['sha256'])
            manifest = {'schemaVersion': 1, 'assetId': spec['assetId'], 'source': self.state['source'], 'build': self.state['script'],
                        'specSha256': self.state['specSha256'], 'modelSha256': final['binding']['modelSha256'], 'acceptedStages': list(STAGES),
                        'textureProvenance': spec['textures'], 'measurements': final['measurements'], 'files': files,
                        'upstream': {'repository': 'https://github.com/img2threejs/img2threejs', 'revision': 'd508b596cb221f1509f6e50ab68fce275a5cfb44', 'license': 'Apache-2.0'}}
            write_json(provenance / 'manifest.json', manifest)
            copy_accepted_file(final['model'], destination, manifest['modelSha256'])
        except Exception:
            # Only newly created outputs owned by this command, never user data.
            destination.unlink(missing_ok=True)
            shutil.rmtree(provenance)
            raise
        return {'destination': str(destination), 'provenance': str(provenance), 'modelSha256': sha256(destination), 'artisticStatus': 'stage reviews accepted; not developer shipping approval'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('init', 'spec', 'build', 'review', 'status', 'next', 'export'):
        cmd = commands.add_parser(name)
        cmd.add_argument('--work', required=True, type=Path)
        if name == 'init':
            cmd.add_argument('--source', required=True, type=Path)
        if name in {'spec', 'review'}:
            cmd.add_argument('--file', required=True, type=Path)
        if name in {'build', 'review'}:
            cmd.add_argument('--stage', required=True, choices=STAGES)
        if name == 'build':
            cmd.add_argument('--script', required=True, type=Path)
            cmd.add_argument('--blender', required=True)
            cmd.add_argument('--godot', required=True)
            cmd.add_argument('--launcher', type=Path, help='required for actual stage visual evidence; no headed fallback')
        if name == 'export':
            cmd.add_argument('--destination', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        pipeline = Pipeline(args.work)
        if args.command == 'init':
            result = pipeline.init(args.source)
        elif args.command == 'spec':
            result = pipeline.admit_spec(args.file)
        elif args.command == 'build':
            result = pipeline.build(args.stage, args.script, args.blender, args.godot, args.launcher)
        elif args.command == 'review':
            result = pipeline.review(args.stage, args.file)
        elif args.command == 'export':
            result = pipeline.export(args.destination)
        else:
            result = pipeline.status()
        print(json.dumps(result, indent=2))
        return 0
    except (PipelineError, OSError, KeyError, ValueError, TypeError) as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
