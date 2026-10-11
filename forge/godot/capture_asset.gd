extends SceneTree

const PROFILES := [{"name": "phone", "width": 844, "height": 390},
	{"name": "tablet", "width": 1280, "height": 800}]
# World-axis labels stay truthful for both -Z- and +X-facing authored assets.
const VIEWS := [{"name": "negative-z", "yaw": 180.0, "pitch": 22.0},
	{"name": "positive-x", "yaw": 90.0, "pitch": 22.0},
	{"name": "positive-z", "yaw": 0.0, "pitch": 22.0},
	{"name": "negative-x", "yaw": 270.0, "pitch": 22.0},
	{"name": "top", "yaw": 150.0, "pitch": 75.0},
	{"name": "detail", "yaw": 160.0, "pitch": 42.0}]

func _initialize() -> void:
	call_deferred("_capture")

func _vec(value: Array) -> Vector3:
	return Vector3(float(value[0]), float(value[1]), float(value[2]))

func _fail(message: String) -> void:
	push_error(message)
	quit(1)

func _frame(viewport: SubViewport) -> Image:
	# An offscreen SubViewport is independent of the parked native window size.
	for frame in range(4):
		await process_frame
		await RenderingServer.frame_post_draw
	return viewport.get_texture().get_image()

func _stats(image: Image, baseline: Image) -> Dictionary:
	var count := 0
	var changed := 0
	var luminance_sum := 0.0
	var square_sum := 0.0
	var minimum := 1.0
	var maximum := 0.0
	for y in range(0, image.get_height(), 4):
		for x in range(0, image.get_width(), 4):
			var pixel := image.get_pixel(x, y)
			var background := baseline.get_pixel(x, y)
			var difference: float = maxf(absf(pixel.r - background.r),
				maxf(absf(pixel.g - background.g), absf(pixel.b - background.b)))
			if difference > 0.02:
				changed += 1
			var luminance := pixel.r * 0.2126 + pixel.g * 0.7152 + pixel.b * 0.0722
			luminance_sum += luminance
			square_sum += luminance * luminance
			minimum = minf(minimum, luminance)
			maximum = maxf(maximum, luminance)
			count += 1
	var mean := luminance_sum / count
	return {"sampleStride": 4, "samples": count, "meanLuminance": mean,
		"luminanceStddev": sqrt(maxf(0.0, square_sum / count - mean * mean)),
		"minLuminance": minimum, "maxLuminance": maximum,
		"assetChangedFraction": float(changed) / count}

func _capture() -> void:
	if DisplayServer.get_name() == "headless":
		_fail("Real captures require the activation-suppressed launcher, not headless rendering")
		return
	if not bool(ProjectSettings.get_setting("img2godot/park_window", false)):
		_fail("Missing launcher ParkWindow safety configuration")
		return
	# A custom SceneTree --script can skip normal autoload initialization.
	# Reuse the launcher's unchanged project-side autoload in that case.
	var park: Node = root.get_node_or_null("ParkWindow")
	if park == null:
		var park_script: Script = load("res://core/park_window.gd") as Script
		if park_script == null:
			_fail("Missing launcher ParkWindow safety autoload")
			return
		park = park_script.new()
		park.name = "ParkWindow"
		root.add_child(park)
	if not OS.get_cmdline_user_args().has("--park-window"):
		_fail("Missing --park-window launcher contract")
		return
	if not root.get_flag(Window.FLAG_NO_FOCUS) or not root.borderless:
		_fail("ParkWindow did not inhibit native window focus safely")
		return
	# Cocoa guard coordinates can be scaled on Retina displays. Test actual
	# offscreen separation in Godot's coordinate space, not one native position.
	var window_rect := Rect2i(DisplayServer.window_get_position(), DisplayServer.window_get_size())
	for screen in range(DisplayServer.get_screen_count()):
		var screen_rect := Rect2i(DisplayServer.screen_get_position(screen), DisplayServer.screen_get_size(screen))
		if window_rect.intersects(screen_rect):
			_fail("ParkWindow native window intersects a physical screen")
			return
	var facts: Variant = JSON.parse_string(FileAccess.get_file_as_string("res://inspection.json"))
	if not facts is Dictionary or not facts.has("bounds"):
		_fail("Missing measured Godot bounds")
		return
	var packed: PackedScene = load("res://assets/model.glb") as PackedScene
	if packed == null:
		_fail("Cannot load actual imported GLB")
		return
	var viewport := SubViewport.new()
	viewport.name = "AssetAcceptance"
	viewport.own_world_3d = true
	viewport.render_target_update_mode = SubViewport.UPDATE_ALWAYS
	viewport.msaa_3d = Viewport.MSAA_4X
	root.add_child(viewport)
	var scene := Node3D.new()
	viewport.add_child(scene)
	var model := packed.instantiate() as Node3D
	if model == null:
		_fail("GLB root is not Node3D")
		return
	scene.add_child(model)
	var model_bounds := AABB(_vec(facts["bounds"]["position"]), _vec(facts["bounds"]["size"]))
	var center := model_bounds.get_center()
	var radius := model_bounds.size.length() * 0.5
	if radius <= 0.0:
		_fail("Asset bounds are empty")
		return
	var detail_center := center
	var detail_radius := radius
	var largest_volume := -1.0
	for mesh in facts["meshes"]:
		var mesh_bounds := AABB(_vec(mesh["bounds"]["position"]), _vec(mesh["bounds"]["size"]))
		var volume: float = mesh_bounds.size.x * mesh_bounds.size.y * mesh_bounds.size.z
		if volume > largest_volume:
			largest_volume = volume
			detail_center = mesh_bounds.get_center()
			detail_radius = maxf(mesh_bounds.size.length() * 0.5, radius * 0.08)
	var world_environment := WorldEnvironment.new()
	var environment := Environment.new()
	environment.background_mode = Environment.BG_COLOR
	environment.background_color = Color(0.19, 0.20, 0.22)
	environment.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	environment.ambient_light_color = Color(0.85, 0.87, 0.90)
	environment.ambient_light_energy = 0.55
	environment.reflected_light_source = Environment.REFLECTION_SOURCE_DISABLED
	environment.tonemap_mode = Environment.TONE_MAPPER_LINEAR
	world_environment.environment = environment
	scene.add_child(world_environment)
	var key := DirectionalLight3D.new()
	key.rotation_degrees = Vector3(-48.0, -35.0, 0.0)
	key.light_energy = 1.1
	key.shadow_enabled = true
	scene.add_child(key)
	var fill := DirectionalLight3D.new()
	fill.rotation_degrees = Vector3(-25.0, 140.0, 0.0)
	fill.light_energy = 0.45
	scene.add_child(fill)
	# This neutral floor is presentation-only geometry, never asset content.
	var floor_mesh := PlaneMesh.new()
	floor_mesh.size = Vector2(radius * 20.0, radius * 20.0)
	var floor_material := StandardMaterial3D.new()
	floor_material.albedo_color = Color(0.24, 0.25, 0.27)
	floor_material.roughness = 1.0
	var floor_instance := MeshInstance3D.new()
	floor_instance.mesh = floor_mesh
	floor_instance.material_override = floor_material
	floor_instance.position = Vector3(center.x, model_bounds.position.y - radius * 0.002, center.z)
	scene.add_child(floor_instance)
	var camera := Camera3D.new()
	camera.fov = 38.0
	camera.keep_aspect = Camera3D.KEEP_HEIGHT
	camera.near = maxf(radius * 0.001, 0.0001)
	camera.far = maxf(radius * 100.0, 1.0)
	scene.add_child(camera)
	camera.make_current()
	DirAccess.make_dir_recursive_absolute(ProjectSettings.globalize_path("res://captures"))
	var captures: Array = []
	for profile in PROFILES:
		viewport.size = Vector2i(int(profile["width"]), int(profile["height"]))
		for view in VIEWS:
			var target: Vector3 = detail_center if view["name"] == "detail" else center
			# Detail intentionally crops the chosen component rather than repeating
			# the full-asset framing when the GLB contains a single combined mesh.
			var frame_radius: float = detail_radius * 0.60 if view["name"] == "detail" else radius
			var yaw := deg_to_rad(float(view["yaw"]))
			var pitch := deg_to_rad(float(view["pitch"]))
			var direction := Vector3(sin(yaw) * cos(pitch), sin(pitch), cos(yaw) * cos(pitch))
			var distance := frame_radius / sin(deg_to_rad(camera.fov) * 0.5) * 1.18
			camera.position = target + direction * distance
			camera.look_at(target, Vector3.UP)
			model.visible = false
			var baseline: Image = await _frame(viewport)
			model.visible = true
			var image: Image = await _frame(viewport)
			if image == null or image.is_empty() or baseline == null or baseline.is_empty():
				_fail("GPU capture returned no pixels")
				return
			var stats := _stats(image, baseline)
			if float(stats["assetChangedFraction"]) < 0.0001:
				_fail("Actual GLB is invisible in " + str(profile["name"]) + "/" + str(view["name"]))
				return
			var filename: String = "captures/%s-%s.png" % [profile["name"], view["name"]]
			if image.save_png("res://" + filename) != OK:
				_fail("Cannot save GPU PNG: " + filename)
				return
			captures.append({"path": filename, "profile": profile["name"], "view": view["name"],
				"width": image.get_width(), "height": image.get_height(), "stats": stats,
				"camera": {"target": [target.x, target.y, target.z], "distance": distance,
					"yawDegrees": view["yaw"], "pitchDegrees": view["pitch"], "fovDegrees": camera.fov}})
	var output := FileAccess.open("res://capture-report.json", FileAccess.WRITE)
	if output == null:
		_fail("Cannot save capture report")
		return
	output.store_string(JSON.stringify({"captures": captures, "displayServer": DisplayServer.get_name(),
		"renderingMethod": ProjectSettings.get_setting("rendering/renderer/rendering_method"),
		"originalModelTransformPreserved": true, "floorY": floor_instance.position.y}, "\t"))
	output.close()
	print("IMG2GODOT_CAPTURE_COMPLETE")
	quit(0)
