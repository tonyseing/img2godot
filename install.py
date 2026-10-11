#!/usr/bin/env python3
"""Install the img2godot skill atomically; preserve displaced skills for rollback."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

NAME = "game-asset-3d-pipeline"
LEGACY_SKILL_SHA256 = "0f500a6c410de5dc52b92d01a0f989074fe7d446f27aac9539d5e6f1b4fb6630"
RESOURCES = ("SKILL.md", "README.md", "LICENSE", "PROVENANCE.json", "forge", "grimoire", "docs")
MARKER = ".img2godot-install.json"


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


def intact_backup(path):
    if not owned(path):
        return False
    expected = f"{NAME}-{digest(inventory(path))}"
    if path.name == expected:
        return True
    return path.name.startswith(expected + "-") and path.name[len(expected) + 1:].isdigit()


def preserve(path, backups):
    files = inventory(path)
    backup = backups / f"{NAME}-{digest(files)}"
    if backup.exists():
        if inventory(backup) != files:
            raise ValueError(f"Backup collision: {backup}")
        # Preserve even identical installations without deleting their directory.
        index = 1
        while backup.with_name(f"{backup.name}-{index}").exists():
            index += 1
        backup = backup.with_name(f"{backup.name}-{index}")
    path.rename(backup)
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agents-home", type=Path, default=Path.home() / ".agents")
    parser.add_argument("--rollback", type=Path, help="Exact backup directory printed by an earlier installation")
    args = parser.parse_args()
    home = args.agents_home.expanduser().absolute()
    skills = home / "skills"
    backups = home / "skill-backups"
    target = skills / NAME
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
        inventory(source)
        skills.mkdir(parents=True, exist_ok=True)
        backups.mkdir(parents=True, exist_ok=True)
        displaced = preserve(target, backups) if target.exists() else None
        try:
            source.rename(target)
        except Exception:
            if displaced:
                displaced.rename(target)
            raise
        print(json.dumps({"restored": str(target), "displacedBackup": str(displaced) if displaced else None}))
        return
    source = Path(__file__).resolve().parent
    for resource in RESOURCES:
        if not (source / resource).exists():
            raise ValueError(f"Missing fork resource: {resource}")
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
        marker = {"name": NAME, "pipeline": "img2godot", "contentSha256": digest(files), "files": files}
        (stage / MARKER).write_text(json.dumps(marker, sort_keys=True, indent=2) + "\n")
        if target.exists():
            if inventory(target) == files:
                print(json.dumps({"installed": str(target), "unchanged": True}))
                return
            displaced = preserve(target, backups)
        try:
            stage.rename(target)
        except Exception:
            if displaced:
                displaced.rename(target)
            raise
        print(json.dumps({"installed": str(target), "contentSha256": digest(files), "rollbackBackup": str(displaced) if displaced else None}))
    finally:
        if stage.exists():
            shutil.rmtree(stage)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, json.JSONDecodeError) as error:
        raise SystemExit(str(error))
