#!/usr/bin/env python3
"""Install the img2godot skill atomically; preserve displaced skills for rollback."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

NAME = "game-asset-3d-pipeline"
LEGACY_SKILL_SHA256 = "0f500a6c410de5dc52b92d01a0f989074fe7d446f27aac9539d5e6f1b4fb6630"
RESOURCES = ("SKILL.md", "README.md", "LICENSE", "PROVENANCE.json", "forge", "grimoire", "docs")
MARKER = ".img2godot-install.json"
COORDINATOR = "godot-coordinator"
COORDINATOR_MARKER = ".godot-coordinator-install.json"
REVISION = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def link_state(link, target):
    if not link.exists() and not link.is_symlink():
        return "absent"
    if link.is_symlink() and Path(os.readlink(link)) == target:
        return "ours"
    return "foreign"


def ensure_link(link, target):
    if link_state(link, target) == "absent":
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
    return str(link)


def inventory(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Refusing symlink: {path}")
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if path.is_file() and path != root / MARKER:
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def digest(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def owned(path):
    if path.is_symlink() or not path.is_dir():
        return False
    marker = path / MARKER
    if marker.is_file():
        data = json.loads(marker.read_text())
        return data.get("name") == NAME and data.get("pipeline") == "img2godot" and data.get("files") == inventory(path)
    skill = path / "SKILL.md"
    return skill.is_file() and hashlib.sha256(skill.read_bytes()).hexdigest() == LEGACY_SKILL_SHA256


def backup_digest(path, files):
    marker = path / MARKER
    data = json.loads(marker.read_text()) if marker.is_file() else {}
    provenance = {key: data[key] for key in ("sourceRevision", "sourceRepository", "coordinator")
                  if key in data}
    # Only explicit pre-coordinator backups retain the historical content-only identity.
    return digest({"files": files, "provenance": provenance}) if provenance else digest(files)


def intact_backup(path):
    if not owned(path):
        return False
    expected = f"{NAME}-{backup_digest(path, inventory(path))}"
    if path.name == expected:
        return True
    return path.name.startswith(expected + "-") and path.name[len(expected) + 1:].isdigit()


def preserve(path, backups):
    files = inventory(path)
    identity = backup_digest(path, files)
    backup = backups / f"{NAME}-{identity}"
    if backup.exists():
        if backup_digest(backup, inventory(backup)) != identity:
            raise ValueError(f"Backup collision: {backup}")
        # Preserve even identical installations without deleting their directory.
        index = 1
        while backup.with_name(f"{backup.name}-{index}").exists():
            index += 1
        backup = backup.with_name(f"{backup.name}-{index}")
    path.rename(backup)
    return backup


def source_revision(source, supplied=None):
    if supplied and not REVISION.fullmatch(supplied):
        raise ValueError("--source-revision must name the exact verified fork commit")
    observed = None
    if (source / ".git").exists():
        result = subprocess.run(["git", "-C", str(source), "rev-parse", "--verify", "HEAD"],
                                capture_output=True, text=True, check=False)
        observed = result.stdout.strip() if result.returncode == 0 else None
    if supplied and observed and supplied != observed:
        raise ValueError("--source-revision must match the exact fork checkout revision")
    revision = observed or supplied
    if not revision or not REVISION.fullmatch(revision):
        raise ValueError("Cannot establish fork revision; source archives require --source-revision <verified-commit>")
    return revision


def coordinator_paths(args, home):
    # Custom skill homes are disposable by default: never install into the real
    # user's ~/.local merely because a test supplied --agents-home.
    isolated = args.agents_home is not None or args.claude_home is not None
    root = home if isolated else Path.home() / ".local"
    versions = args.coordinator_home or (root / COORDINATOR if isolated else root / "share" / COORDINATOR)
    binary = args.coordinator_bin or root / "bin" / COORDINATOR
    versions, binary = versions.expanduser().absolute(), binary.expanduser().absolute()
    for directory in (versions, binary.parent):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ValueError(f"Refusing unrelated coordinator directory: {directory}")
    return versions, binary


def runtime_record(path, versions):
    version = path.parent
    if (path.name != "godot_coordinator.py" or version.parent != versions
            or version.is_symlink() or path.is_symlink() or not path.is_file()):
        raise ValueError(f"Refusing unrelated coordinator runtime: {path}")
    marker = version / COORDINATOR_MARKER
    if marker.is_symlink() or not marker.is_file():
        raise ValueError(f"Refusing unrecognized coordinator version: {version}")
    record = json.loads(marker.read_text())
    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    if not isinstance(record, dict) or not isinstance(record.get("sourceRevision"), str):
        raise ValueError(f"Refusing invalid coordinator provenance: {version}")
    if (record.get("name") != COORDINATOR or record.get("sha256") != content_hash
            or version.name != content_hash or not REVISION.fullmatch(record.get("sourceRevision", ""))
            or record.get("sourcePath") != "runtime/godot_coordinator.py"
            or record.get("sourceRepository") != "https://github.com/tonyseing/img2godot"
            or not os.access(path, os.X_OK)):
        raise ValueError(f"Refusing modified coordinator version: {version}")
    return record


def existing_coordinator(binary, versions):
    if not binary.exists() and not binary.is_symlink():
        return None
    if not binary.is_symlink():
        raise ValueError(f"Refusing unrelated coordinator executable: {binary}")
    previous = Path(os.readlink(binary))
    runtime_record(previous, versions)
    return previous


def prepare_coordinator(source, revision, versions):
    runtime = source / "runtime" / "godot_coordinator.py"
    if runtime.is_symlink() or not runtime.is_file():
        raise ValueError(f"Missing canonical coordinator runtime: {runtime}")
    content = runtime.read_bytes()
    content_hash = hashlib.sha256(content).hexdigest()
    destination = versions / content_hash / runtime.name
    if destination.parent.exists() or destination.parent.is_symlink():
        return destination, runtime_record(destination, versions)
    repository = json.loads((source / "PROVENANCE.json").read_text())["fork"]
    record = {"name": COORDINATOR, "sha256": content_hash, "sourceRevision": revision,
              "sourceRepository": repository, "sourcePath": "runtime/godot_coordinator.py"}
    versions.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=versions))
    try:
        (stage / runtime.name).write_bytes(content)
        (stage / runtime.name).chmod(0o755)
        (stage / COORDINATOR_MARKER).write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
        try:
            stage.rename(destination.parent)
        except OSError:
            if not destination.parent.is_dir():
                raise
            # Concurrent identical installers may publish the immutable version
            # first; only a recognized exact-content version is acceptable.
            record = runtime_record(destination, versions)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return destination, record


def coordinator_dependency(path, record, binary):
    return {**record, "path": str(path), "launcher": str(binary)}


def replace_coordinator_link(binary, destination):
    binary.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".godot-coordinator-", dir=binary.parent))
    try:
        link = stage / COORDINATOR
        link.symlink_to(destination)
        link.replace(binary)
    finally:
        shutil.rmtree(stage)


def restore_coordinator_link(binary, previous):
    if previous is None:
        if binary.is_symlink():
            binary.unlink()
    else:
        replace_coordinator_link(binary, previous)


@contextlib.contextmanager
def installation_locks(*resources):
    """Serialize each skill, stable runtime selector and Claude entry together."""
    paths = {resource.parent.resolve() / f".{resource.name}.img2godot-install.lock"
             for resource in resources}
    with contextlib.ExitStack() as stack:
        for path in sorted(paths):
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            stream = stack.enter_context(os.fdopen(descriptor, "a+"))
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield


def activate_links(link, target, binary, destination, previous):
    had_link = link.is_symlink()
    try:
        ensure_link(link, target)
        if destination is not None and destination != previous:
            replace_coordinator_link(binary, destination)
    except Exception:
        if destination is not None and binary.is_symlink() and Path(os.readlink(binary)) == destination:
            restore_coordinator_link(binary, previous)
        if not had_link and link.is_symlink() and Path(os.readlink(link)) == target:
            link.unlink()
        raise


def activate_skill(source, target, backups, link, binary, versions, destination, previous):
    if existing_coordinator(binary, versions) != previous:
        raise ValueError("Coordinator installation changed concurrently; retry without replacing it")
    if link_state(link, target) == "foreign":
        raise ValueError(f"Refusing unrelated Claude skill entry: {link}")
    displaced = preserve(target, backups) if target.exists() else None
    activated = False
    try:
        source.rename(target)
        activated = True
        activate_links(link, target, binary, destination, previous)
    except Exception:
        if activated:
            target.rename(source)
        if displaced:
            displaced.rename(target)
        raise
    return displaced


def install_locked(args, home, skills, backups, target, link, versions, binary):
    previous = existing_coordinator(binary, versions)
    if link_state(link, target) == "foreign":
        raise ValueError(f"Refusing unrelated Claude skill entry: {link}")
    for path in (home, skills, backups):
        if path.is_symlink():
            raise ValueError(f"Refusing symlink directory: {path}")
    if target.exists() or target.is_symlink():
        if not owned(target):
            raise ValueError(f"Refusing unrelated or modified skill directory: {target}")
    if args.rollback:
        source = args.rollback.expanduser().absolute()
        if source.parent != backups or not source.is_dir() or not intact_backup(source):
            raise ValueError("Rollback must name an unmodified recognized backup under .agents/skill-backups")
        marker_path = source / MARKER
        dependency = json.loads(marker_path.read_text()).get("coordinator") if marker_path.is_file() else None
        destination = Path(dependency["path"]) if dependency else None
        if dependency and dependency.get("launcher") != str(binary):
            raise ValueError("Rollback must use the coordinator home/bin locations recorded by its backup")
        if destination is not None:
            record = runtime_record(destination, versions)
            observed = coordinator_dependency(destination, record, binary)
            if observed != dependency:
                raise ValueError("Rollback coordinator dependency provenance changed")
        skills.mkdir(parents=True, exist_ok=True)
        backups.mkdir(parents=True, exist_ok=True)
        displaced = activate_skill(source, target, backups, link, binary, versions, destination, previous)
        print(json.dumps({"restored": str(target), "displacedBackup": str(displaced) if displaced else None,
                          "claudeLink": str(link), "coordinator": dependency}))
        return
    marker_path = target / MARKER
    current_dependency = json.loads(marker_path.read_text()).get("coordinator") if marker_path.is_file() else None
    if current_dependency:
        current_path = Path(current_dependency["path"])
        current_record = runtime_record(current_path, versions)
        if coordinator_dependency(current_path, current_record, binary) != current_dependency:
            raise ValueError("Refusing changed installed coordinator dependency provenance")
    source = Path(__file__).resolve().parent
    for resource in RESOURCES:
        if not (source / resource).exists():
            raise ValueError(f"Missing fork resource: {resource}")
    revision = source_revision(source, args.source_revision)
    destination, runtime = prepare_coordinator(source, revision, versions)
    dependency = coordinator_dependency(destination, runtime, binary)
    skills.mkdir(parents=True, exist_ok=True)
    backups.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{NAME}-", dir=home))
    displaced = None
    try:
        for resource in RESOURCES:
            src = source / resource
            if src.is_symlink():
                raise ValueError(f"Refusing symlink resource: {src}")
            if src.is_dir():
                inventory(src)
                shutil.copytree(src, stage / resource, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy2(src, stage / resource)
        files = inventory(stage)
        marker = {"name": NAME, "pipeline": "img2godot", "contentSha256": digest(files), "files": files,
                  "sourceRepository": json.loads((source / "PROVENANCE.json").read_text())["fork"],
                  "sourceRevision": revision, "coordinator": dependency}
        (stage / MARKER).write_text(json.dumps(marker, sort_keys=True, indent=2) + "\n")
        if target.exists() and (target / MARKER).is_file() and json.loads((target / MARKER).read_text()) == marker:
            activate_links(link, target, binary, destination, previous)
            print(json.dumps({"installed": str(target), "unchanged": True, "claudeLink": str(link),
                              "coordinator": dependency}))
            return
        displaced = activate_skill(stage, target, backups, link, binary, versions, destination, previous)
        print(json.dumps({"installed": str(target), "contentSha256": digest(files),
                          "rollbackBackup": str(displaced) if displaced else None,
                          "claudeLink": str(link), "coordinator": dependency}))
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agents-home", type=Path)
    parser.add_argument("--claude-home", type=Path)
    parser.add_argument("--coordinator-home", type=Path, help="External content-addressed runtime directory")
    parser.add_argument("--coordinator-bin", type=Path, help="Stable coordinator executable symlink")
    parser.add_argument("--source-revision", help="Exact verified fork commit for source archives without .git")
    parser.add_argument("--rollback", type=Path, help="Exact backup directory printed by an earlier installation")
    args = parser.parse_args()
    home = (args.agents_home or Path.home() / ".agents").expanduser().absolute()
    skills = home / "skills"
    backups = home / "skill-backups"
    target = skills / NAME
    link = (args.claude_home or Path.home() / ".claude").expanduser().absolute() / "skills" / NAME
    versions, binary = coordinator_paths(args, home)
    for directory in (home, skills, backups):
        if directory.is_symlink():
            raise ValueError(f"Refusing symlink directory: {directory}")
    # Resource-specific locks cover different skill homes sharing one stable bin
    # as well as different bins targeting one skill. Validate ownership only once
    # all resources are locked, then keep the locks through publication/rollback.
    with installation_locks(target, binary, link):
        install_locked(args, home, skills, backups, target, link, versions, binary)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, json.JSONDecodeError) as error:
        raise SystemExit(str(error))
