#!/usr/bin/env python3
"""Measure imported GLBs and optionally capture real, no-focus Godot pixels.

No launcher means headless measurements only, never visual acceptance. All engine
runs use a fresh isolated project; the source model is never imported in a game.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import zlib

HERE = Path(__file__).resolve().parent
ENGINE_ERRORS = re.compile(r"^\s*(?:SCRIPT ERROR:|USER ERROR:|ERROR:)", re.MULTILINE)
PROFILES = {"phone": (844, 390), "tablet": (1280, 800)}
VIEWS = {"negative-z", "positive-x", "positive-z", "negative-x", "top", "detail"}


class VerificationError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def dump(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise VerificationError(f"Missing or invalid artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise VerificationError(f"Artifact must be a JSON object: {path}")
    return value


def engine_busy(engine: Path) -> None:
    """Match executable identity, not a bridge mentioning Godot in its args."""
    try:
        snapshot = subprocess.run(["ps", "-axo", "pid=,comm="], check=True,
                                  capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise VerificationError(f"Cannot establish engine process safety: {exc}") from exc
    pidpath = None
    if sys.platform == "darwin":
        try:
            # ps comm can be relative argv[0]; libproc supplies the actual
            # executable path even for an editor started as ./Godot or godot.
            pidpath = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_pidpath
            pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
            pidpath.restype = ctypes.c_int
        except (OSError, AttributeError) as exc:
            raise VerificationError(f"Cannot resolve Darwin engine executable identities: {exc}") from exc
    matches = []
    for line in snapshot.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        pid, command = parts
        candidate = Path(command)
        # Linux ps 'comm' contains only a short name; /proc exposes the exact path.
        proc_exe = Path("/proc") / pid / "exe"
        try:
            if pidpath is not None:
                buffer = ctypes.create_string_buffer(4096)
                if pidpath(int(pid), buffer, len(buffer)) <= 0:
                    if candidate.name.lower() == "godot" and ctypes.get_errno() != 3:
                        raise VerificationError(f"Cannot resolve possible engine executable for PID {pid}")
                    continue
                candidate = Path(os.fsdecode(buffer.value))
            elif proc_exe.exists():
                candidate = proc_exe.resolve()
            elif not candidate.is_absolute():
                if candidate.name.lower() == "godot":
                    raise VerificationError(f"Cannot resolve possible engine executable for PID {pid}")
                continue
            binary = candidate.resolve()
            if binary == engine or (binary.name.lower() == "godot" and binary.is_file()):
                # Includes other installed engine versions and the launcher's
                # private copy, but never Python/Node Godot bridge processes.
                matches.append({"pid": int(pid), "binary": str(binary)})
        except OSError:
            continue  # A process that exited during the snapshot is not busy.
    if matches:
        raise VerificationError(f"Engine busy; executable-path matches: {matches}. "
                                "Other engines are never stopped.")


@contextlib.contextmanager
def engine_lock(engine: Path):
    # Global across engine versions: two verifiers must not contend for the GPU.
    lock_path = Path(tempfile.gettempdir()) / "img2godot-engine.lock"
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise VerificationError(f"Engine busy: another verifier owns {engine}") from exc
        try:
            engine_busy(engine)
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def run(command: list[str], report: dict, run_dir: Path, label: str, mode: str,
        engine: Path, timeout: int = 180) -> str:
    engine_busy(engine)
    stdout_path = run_dir / f"{label}-stdout.log"
    log_path = run_dir / f"{label}-engine.log"
    record = {"command": command, "stdout": str(stdout_path), "log": str(log_path), "mode": mode}
    report["runs"].append(record)
    try:
        # Only this owned process group is terminated on timeout, never a user's engine.
        with stdout_path.open("w") as output:
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            try:
                record["returncode"] = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                record["returncode"] = process.returncode
                raise VerificationError(f"{label} timed out; owned process group stopped") from exc
    except OSError as exc:
        raise VerificationError(f"Cannot start {label}: {exc}") from exc
    text = stdout_path.read_text(errors="replace")
    engine_log = log_path.read_text(errors="replace") if log_path.is_file() else ""
    if label == "capture":
        # Preserve the native guard's logs/provenance beside our own evidence.
        for field, pattern in [("launcherLog", r"stdout\+stderr -> (.+)$"),
                               ("engineProvenance", r"engine provenance -> (.+?)(?: \(private.*)?$")]:
            match = re.search(pattern, text, re.MULTILINE)
            if match:
                external = Path(match.group(1).strip())
                if external.is_file():
                    retained = run_dir / ("launcher-capture.log" if field == "launcherLog"
                                          else "capture-engine-provenance.json")
                    shutil.copyfile(external, retained)
                    record[field] = str(retained)
                    if field == "launcherLog":
                        engine_log += "\n" + retained.read_text(errors="replace")
        if "launcherLog" not in record or "engineProvenance" not in record:
            raise VerificationError("Launcher did not produce native guard log and engine provenance")
        if "guard installed" not in text:
            raise VerificationError("Launcher did not confirm native no-focus guard installation")
    if record["returncode"] != 0 or ENGINE_ERRORS.search(text + "\n" + engine_log):
        raise VerificationError(f"{label} failed (exit {record['returncode']}); inspect {stdout_path}")
    if label not in {"version"} and (not log_path.is_file() or not engine_log.strip()):
        raise VerificationError(f"{label} produced no Godot log artifact: {log_path}")
    return text


def png_dimensions(path: Path) -> tuple[int, int]:
    """Check full PNG chunk integrity and decompression, not just its header."""
    raw = path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise VerificationError(f"Not a PNG: {path}")
    offset = 8
    compressed = bytearray()
    dimensions = None
    channels = 0
    ended = False
    while offset < len(raw):
        if offset + 12 > len(raw):
            raise VerificationError(f"Truncated PNG: {path}")
        length = struct.unpack_from(">I", raw, offset)[0]
        kind = raw[offset + 4:offset + 8]
        data = raw[offset + 8:offset + 8 + length]
        end = offset + 12 + length
        if end > len(raw) or zlib.crc32(kind + data) != struct.unpack_from(">I", raw, end - 4)[0]:
            raise VerificationError(f"Corrupt PNG chunk: {path}")
        if kind == b"IHDR":
            if dimensions is not None or length != 13 or offset != 8:
                raise VerificationError(f"Invalid PNG header: {path}")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
            if depth != 8 or color not in (2, 6) or compression or filtering or interlace:
                raise VerificationError(f"Unsupported GPU PNG format: {path}")
            dimensions = width, height
            channels = 3 if color == 2 else 4
        elif kind == b"IDAT":
            compressed.extend(data)
        elif kind == b"IEND":
            ended = length == 0 and end == len(raw)
            break
        offset = end
    if dimensions is None or not ended or not compressed:
        raise VerificationError(f"Incomplete PNG: {path}")
    width, height = dimensions
    if (width, height) not in PROFILES.values():
        raise VerificationError(f"Unexpected GPU PNG dimensions: {path}: {dimensions}")
    decoded = zlib.decompress(compressed)
    stride = width * channels + 1
    if len(decoded) != stride * height or any(decoded[y * stride] > 4 for y in range(height)):
        raise VerificationError(f"Invalid GPU PNG pixel data: {path}")
    return dimensions


def validate_facts(facts: dict) -> None:
    for count, items in [("meshCount", "meshes"), ("materialCount", "materials"),
                         ("textureCount", "textures")]:
        if not isinstance(facts.get(items), list) or facts.get(count) != len(facts[items]):
            raise VerificationError(f"Inconsistent Godot inspection {count}/{items}")
    if facts.get("errors") or facts["meshCount"] < 1 or facts.get("triangles", 0) < 1:
        raise VerificationError(f"Godot asset has no valid geometry: {facts.get('errors', [])}")
    bounds = facts.get("bounds", {})
    for field in ("position", "size", "end"):
        values = bounds.get(field)
        if not isinstance(values, list) or len(values) != 3 or not all(math.isfinite(v) for v in values):
            raise VerificationError(f"Invalid measured bounds {field}")
    if any(v < 0 for v in bounds["size"]) or max(bounds["size"]) <= 0:
        raise VerificationError("Measured model bounds are empty")
    for texture in facts["textures"]:
        if texture.get("width", 0) <= 0 or texture.get("height", 0) <= 0:
            raise VerificationError("Imported texture has invalid dimensions")
    for field in ("skeletons", "animations"):
        if not isinstance(facts.get(field), list):
            raise VerificationError(f"Missing measured {field}")


def stage_project(model: Path, project: Path, launcher: Path | None, report: dict) -> None:
    (project / "assets").mkdir(parents=True)
    shutil.copyfile(model, project / "assets/model.glb")
    if sha256(project / "assets/model.glb") != report["modelSha256"]:
        raise VerificationError("Isolated GLB copy changed source bytes")
    for script in ("inspect_asset.gd", "capture_asset.gd"):
        shutil.copyfile(HERE / script, project / script)
    autoload = ""
    if launcher:
        # The explicit product launcher supplies the corresponding project-side
        # safety contract. Copy it unchanged instead of inventing another variant.
        park_source = launcher.parent.parent / "game/core/park_window.gd"
        if not park_source.is_file():
            raise VerificationError(f"Missing product ParkWindow autoload: {park_source}")
        (project / "core").mkdir()
        shutil.copyfile(park_source, project / "core/park_window.gd")
        report["parkWindow"] = {"source": str(park_source), "sha256": sha256(park_source)}
        autoload = '\n[autoload]\nParkWindow="*res://core/park_window.gd"\n'
    (project / "project.godot").write_text(
        'config_version=5\n\n[application]\nconfig/name="img2godot isolated acceptance"\n'
        '\n[display]\nwindow/size/viewport_width=844\nwindow/size/viewport_height=390\n'
        'window/size/window_width_override=844\nwindow/size/window_height_override=390\n'
        'window/energy_saving/keep_screen_on=false\n'
        '\n[rendering]\nrenderer/rendering_method="gl_compatibility"\n'
        'renderer/rendering_method.mobile="gl_compatibility"\n'
        '\n[img2godot]\npark_window=' + ('true' if launcher else 'false') + '\n' + autoload)
    report["project"] = str(project)


def verify(args: argparse.Namespace, report: dict, output: Path) -> None:
    model = Path(args.model).expanduser().resolve(strict=True)
    supplied_engine = Path(args.godot).expanduser()
    engine = supplied_engine.resolve(strict=True)
    launcher = Path(args.launcher).expanduser().resolve(strict=True) if args.launcher else None
    if not engine.is_file() or not os.access(engine, os.X_OK):
        raise VerificationError(f"Godot must name an executable binary: {engine}")
    if model.suffix.lower() != ".glb" or not model.is_file():
        raise VerificationError("--model must name a regular GLB file")
    with model.open("rb") as stream:
        header = stream.read(12)
    if len(header) != 12 or header[:4] != b"glTF" or struct.unpack_from("<II", header, 4) != (2, model.stat().st_size):
        raise VerificationError("Invalid GLB 2 container header/length")
    report["modelSha256"] = sha256(model)
    report["model"] = str(model)
    report["engine"] = {"path": str(engine), "sha256": sha256(engine)}
    if launcher and (launcher.name != "godot-window.mjs" or not launcher.is_file()):
        raise VerificationError("--launcher must explicitly name the product tools/godot-window.mjs")
    if launcher and sys.platform != "darwin":
        raise VerificationError("Product no-focus capture launcher supports macOS only; no headed fallback")
    node = shutil.which("node") if launcher else None
    if launcher and not node:
        raise VerificationError("Node.js is required by the explicitly selected capture launcher")
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=output))
    project = run_dir / "project"
    stage_project(model, project, launcher, report)
    with engine_lock(engine):
        version_text = run([str(engine), "--headless", "--version"], report, run_dir,
                           "version", "headless", engine, timeout=30).strip()
        version = version_text.splitlines()[0] if version_text else ""
        match = re.match(r"^(4\.[0-9]+\.[0-9]+)(?:\.|$)", version)
        if not match or tuple(map(int, match.group(1).split("."))) < (4, 5, 1):
            raise VerificationError(f"Godot 4.5.1 or newer 4.x is required, observed {version!r}")
        report["engine"]["version"] = version
        run([str(engine), "--headless", "--path", str(project), "--import", "--log-file",
             str(run_dir / "import-engine.log")], report, run_dir, "import", "headless", engine)
        text = run([str(engine), "--headless", "--path", str(project), "--script", "res://inspect_asset.gd",
                    "--log-file", str(run_dir / "inspect-engine.log")],
                   report, run_dir, "inspect", "headless", engine)
        if "IMG2GODOT_INSPECTION_COMPLETE" not in text:
            raise VerificationError("Godot inspection did not finish")
        facts = read_object(project / "inspection.json")
        validate_facts(facts)
        report["facts"] = facts
        if launcher:
            report["launcher"] = {"path": str(launcher), "sha256": sha256(launcher)}
            command = [node, str(launcher), "--project", str(project), "--engine", str(engine),
                       "--accept-version", match.group(1), "--resolution", "844x390", "--fixed-fps", "60",
                       "--script", "res://capture_asset.gd", "--log-file", str(run_dir / "capture-engine.log"),
                       "--quit-after", "1800", "--", "--out", str(project / "captures")]
            run(command, report, run_dir, "capture", "activation-suppressed capture", engine)
            captured = read_object(project / "capture-report.json")
            inventory = captured.get("captures")
            if not isinstance(inventory, list) or len(inventory) != len(PROFILES) * len(VIEWS):
                raise VerificationError("Capture report did not include all phone/tablet angles and detail views")
            seen = set()
            for item in inventory:
                key = item.get("profile"), item.get("view")
                if key in seen or key[0] not in PROFILES or key[1] not in VIEWS:
                    raise VerificationError(f"Invalid capture inventory entry: {key}")
                seen.add(key)
                path = (project / item["path"]).resolve(strict=True)
                if not path.is_relative_to(project / "captures") or not path.is_file():
                    raise VerificationError(f"Capture escaped isolated output: {path}")
                dimensions = png_dimensions(path)
                if dimensions != PROFILES[key[0]] or dimensions != (item.get("width"), item.get("height")):
                    raise VerificationError(f"Capture dimensions did not match requested profile: {path}")
                stats = item.get("stats", {})
                if (not stats or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in stats.values())
                        or stats.get("assetChangedFraction", 0) < 0.0001 or stats.get("luminanceStddev", 0) <= 0):
                    raise VerificationError(f"Empty or invalid measured capture pixels: {path}")
                report["captures"].append({**item, "path": str(path), "sha256": sha256(path)})
            report["captureEnvironment"] = {key: value for key, value in captured.items() if key != "captures"}
        if sha256(model) != report["modelSha256"] or sha256(project / "assets/model.glb") != report["modelSha256"]:
            raise VerificationError("Source or staged GLB changed during verification")
        if sha256(engine) != report["engine"]["sha256"]:
            raise VerificationError("Godot engine changed during verification")
    report["status"] = "passed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--godot", required=True)
    parser.add_argument("--launcher", help="Explicit product tools/godot-window.mjs; never a direct headed fallback")
    args = parser.parse_args()
    output = Path(args.out).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {"schemaVersion": 1, "status": "failed", "modelSha256": None,
              "mode": "activation-suppressed capture" if args.launcher else "headless",
              "engine": {}, "facts": {}, "captures": [], "runs": [], "errors": []}
    try:
        verify(args, report, output)
    except (VerificationError, OSError, ValueError, TypeError, KeyError, zlib.error) as exc:
        report["errors"].append(str(exc))
    dump(output / "godot-report.json", report)
    print(json.dumps({"status": report["status"], "report": str(output / "godot-report.json"),
                      "errors": report["errors"]}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
