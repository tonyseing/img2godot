extends SceneTree

var meshes: Array = []
var materials: Array = []
var textures: Array = []
var skeletons: Array = []
var animations: Array = []
var errors: Array[String] = []
var material_ids: Dictionary = {}
var texture_ids: Dictionary = {}
var bounds := AABB()
var has_bounds := false
var triangles := 0

func _initialize() -> void:
	call_deferred("_inspect")

func _vector(value: Vector3) -> Array:
	return [value.x, value.y, value.z]

func _texture(texture: Texture2D) -> int:
	var key := texture.get_instance_id()
	if texture_ids.has(key):
		return int(texture_ids[key])
	var index := textures.size()
	texture_ids[key] = index
	var width := texture.get_width()
	var height := texture.get_height()
	if width <= 0 or height <= 0:
		errors.append("Invalid imported texture dimensions: " + texture.resource_path)
	textures.append({"id": index, "path": texture.resource_path, "class": texture.get_class(),
		"width": width, "height": height})
	return index

func _material(material: Material) -> int:
	if material == null:
		return -1
	var key := material.get_instance_id()
	if material_ids.has(key):
		return int(material_ids[key])
	var index := materials.size()
	material_ids[key] = index
	var bindings: Array = []
	for property in material.get_property_list():
		var value: Variant = material.get(property["name"])
		if value is Texture2D:
			bindings.append({"slot": str(property["name"]), "texture": _texture(value)})
	var record := {"id": index, "name": material.resource_name,
		"class": material.get_class(), "textures": bindings}
	if material is BaseMaterial3D:
		record["albedo"] = [material.albedo_color.r, material.albedo_color.g,
			material.albedo_color.b, material.albedo_color.a]
		record["roughness"] = material.roughness
		record["metallic"] = material.metallic
		record["transparency"] = material.transparency
		record["cullMode"] = material.cull_mode
	materials.append(record)
	return index

func _walk(node: Node) -> void:
	if node is MeshInstance3D and node.mesh != null:
		var mesh: ArrayMesh = node.mesh as ArrayMesh
		if mesh == null:
			errors.append("Imported GLB mesh is not ArrayMesh at " + str(node.get_path()))
			return
		var surfaces: Array = []
		var instance_triangles := 0
		for surface in range(mesh.get_surface_count()):
			var arrays := mesh.surface_get_arrays(surface)
			if arrays.size() != Mesh.ARRAY_MAX or arrays[Mesh.ARRAY_VERTEX] == null:
				errors.append("Unreadable mesh surface at " + str(node.get_path()))
				continue
			var vertices: int = arrays[Mesh.ARRAY_VERTEX].size()
			var indices := 0
			if arrays[Mesh.ARRAY_INDEX] != null:
				indices = arrays[Mesh.ARRAY_INDEX].size()
			var element_count: int = indices if indices > 0 else vertices
			var primitive: int = mesh.surface_get_primitive_type(surface)
			var surface_triangles := 0
			if primitive == Mesh.PRIMITIVE_TRIANGLES:
				surface_triangles = element_count / 3
			elif primitive == Mesh.PRIMITIVE_TRIANGLE_STRIP:
				surface_triangles = maxi(0, element_count - 2)
			else:
				errors.append("Non-triangle GLB surface at " + str(node.get_path()))
			instance_triangles += surface_triangles
			var uv_count := 0
			if arrays[Mesh.ARRAY_TEX_UV] != null:
				uv_count = arrays[Mesh.ARRAY_TEX_UV].size()
			var normal_count := 0
			if arrays[Mesh.ARRAY_NORMAL] != null:
				normal_count = arrays[Mesh.ARRAY_NORMAL].size()
			surfaces.append({"index": surface, "vertices": vertices, "indices": indices,
				"triangles": surface_triangles, "uvVertices": uv_count,
				"normalVertices": normal_count, "material": _material(node.get_active_material(surface))})
		var world_bounds: AABB = node.global_transform * mesh.get_aabb()
		bounds = bounds.merge(world_bounds) if has_bounds else world_bounds
		has_bounds = true
		triangles += instance_triangles
		meshes.append({"path": str(node.get_path()), "name": node.name,
			"triangles": instance_triangles, "surfaces": surfaces,
			"visible": node.is_visible_in_tree(), "blendShapes": node.get_blend_shape_count(),
			"bounds": {"position": _vector(world_bounds.position), "size": _vector(world_bounds.size)}})
	if node is Skeleton3D:
		var bones: Array = []
		for index in range(node.get_bone_count()):
			bones.append({"name": node.get_bone_name(index), "parent": node.get_bone_parent(index)})
		skeletons.append({"path": str(node.get_path()), "boneCount": bones.size(), "bones": bones})
	if node is AnimationPlayer:
		for clip in node.get_animation_list():
			var animation: Animation = node.get_animation(clip)
			animations.append({"player": str(node.get_path()), "name": str(clip),
				"lengthSeconds": animation.length, "tracks": animation.get_track_count(),
				"loopMode": animation.loop_mode})
	for child in node.get_children():
		_walk(child)

func _inspect() -> void:
	var packed: PackedScene = load("res://assets/model.glb") as PackedScene
	if packed == null:
		push_error("GLB did not import as PackedScene")
		quit(1)
		return
	var model := packed.instantiate()
	root.add_child(model)
	_walk(model)
	if meshes.is_empty() or triangles <= 0 or not has_bounds or bounds.size.length() <= 0.0:
		errors.append("GLB has no nonempty renderable mesh bounds")
	var report := {"meshCount": meshes.size(), "meshes": meshes, "triangles": triangles,
		"materialCount": materials.size(), "materials": materials,
		"textureCount": textures.size(), "textures": textures,
		"bounds": {"position": _vector(bounds.position), "size": _vector(bounds.size),
			"end": _vector(bounds.end)}, "skeletons": skeletons, "animations": animations,
		"errors": errors, "engineVersion": Engine.get_version_info()}
	var output := FileAccess.open("res://inspection.json", FileAccess.WRITE)
	if output == null:
		push_error("Cannot write inspection.json")
		quit(1)
		return
	output.store_string(JSON.stringify(report, "\t"))
	output.close()
	print("IMG2GODOT_INSPECTION_COMPLETE")
	model.queue_free()
	quit(0 if errors.is_empty() else 1)
