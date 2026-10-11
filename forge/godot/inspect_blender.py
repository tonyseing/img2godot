#!/usr/bin/env python3
"""Fresh exported GLB reimport facts, not artistic approval (Apache-2.0)."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

import bpy
from mathutils import Vector


def inspect(model):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(model))
    meshes = []
    used_images = {}
    untextured = []
    corners = []
    for obj in bpy.context.scene.objects:
        if obj.type != 'MESH':
            continue
        mesh = obj.data
        mesh.calc_loop_triangles()
        textured = bool(mesh.uv_layers)
        material_indices = {poly.material_index for poly in mesh.polygons}
        for index in material_indices:
            mat = mesh.materials[index] if index < len(mesh.materials) else None
            images = []
            if mat and mat.use_nodes:
                for node in mat.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image and any(socket.is_linked for socket in node.outputs):
                        images.append(node.image)
            textured = textured and bool(images)
            for image in images:
                used_images[image.name] = {'name': image.name, 'width': image.size[0], 'height': image.size[1]}
        if not textured:
            untextured.append(obj.name)
        corners.extend(obj.matrix_world @ Vector(corner) for corner in obj.bound_box)
        meshes.append({'name': obj.name, 'triangles': len(mesh.loop_triangles), 'vertices': len(mesh.vertices),
                       'uvLayers': len(mesh.uv_layers), 'textured': textured})
    if not meshes:
        raise ValueError('fresh Blender import contains no meshes')
    minimum = [min(p[i] for p in corners) for i in range(3)]
    maximum = [max(p[i] for p in corners) for i in range(3)]
    return {'schemaVersion': 1, 'status': 'passed', 'mode': 'headless', 'blenderVersion': bpy.app.version_string,
            'modelSha256': hashlib.sha256(model.read_bytes()).hexdigest(), 'glbBytes': model.stat().st_size,
            'meshes': meshes, 'meshCount': len(meshes), 'triangles': sum(m['triangles'] for m in meshes),
            'materials': len(bpy.data.materials), 'textures': list(used_images.values()), 'textureCount': len(used_images),
            'untexturedMeshes': untextured, 'bounds': {'space': 'Blender Z-up meters', 'min': minimum, 'max': maximum}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    report = inspect(args.model.resolve())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
