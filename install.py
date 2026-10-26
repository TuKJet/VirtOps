#!/usr/bin/env python3
"""Install the local Skill package; never connects to a VM or host."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

NAME = "windows-vm-sandbox"
SOURCE = Path(__file__).resolve().parent / "skill" / NAME
RECEIPT = ".virtops-install.json"


def inventory(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"symlink is not a package file: {path}")
        if not path.is_file() or "__pycache__" in path.parts or path.name == RECEIPT:
            continue
        result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def check_source() -> dict[str, str]:
    files = inventory(SOURCE)
    text = (SOURCE / "SKILL.md").read_text(encoding="utf-8")
    if not text.startswith("---\nname: windows-vm-sandbox\n") or "description:" not in text:
        raise ValueError("invalid Skill frontmatter")
    for required in ("references/initialize.md", "references/operate.md", "references/reset.md", "references/windows-agent.md", "references/instance-lifecycle.md", "scripts/profile.py", "scripts/prepare_host.py"):
        if required not in files:
            raise ValueError(f"missing required resource: {required}")
    for name in files:
        if Path(name).suffix.lower() in {".clixml", ".iso", ".qcow2", ".raw", ".wim", ".key", ".pfx"}:
            raise ValueError(f"private or large runtime data in package: {name}")
    return files


def installed_inventory(target: Path) -> dict[str, str]:
    if target.is_symlink() or not (target / RECEIPT).is_file():
        raise ValueError("existing destination is not a VirtOps-managed installation")
    recorded = json.loads((target / RECEIPT).read_text(encoding="utf-8"))["files"]
    actual = inventory(target)
    if actual != recorded:
        raise ValueError("installed Skill has local changes or additional files; preserve/merge them before upgrading")
    return actual


def install(skills_dir: Path, *, upgrade: bool) -> Path:
    expected = check_source()
    target = skills_dir / NAME
    if skills_dir.is_symlink():
        raise ValueError("skills directory is a symlink; use its explicit resolved destination")
    if target.exists() or target.is_symlink():
        previous = installed_inventory(target)
        if previous == expected:
            return target
        if not upgrade:
            raise ValueError("different managed version exists; use --upgrade")
    skills_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".virtops-", dir=skills_dir) as temporary:
        stage = Path(temporary) / NAME
        stage.mkdir()
        for name in expected:
            output = stage / name
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE / name, output)
        (stage / RECEIPT).write_text(json.dumps({"project": "VirtOps", "files": expected}, indent=2), encoding="utf-8")
        if inventory(stage) != expected:
            raise ValueError("staged copy differs from source")
        backup = Path(temporary) / "previous"
        if target.exists():
            target.rename(backup)
        try:
            stage.rename(target)
        except Exception:
            if backup.exists() and not target.exists():
                backup.rename(target)
            raise
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for mode in ("check", "install", "upgrade", "verify-installed"):
        modes.add_argument("--" + mode, action="store_true")
    default = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "skills"
    parser.add_argument("--skills-dir", type=Path, default=default)
    args = parser.parse_args()
    try:
        if args.check:
            result = {"valid": True, "files": len(check_source()), "source": str(SOURCE), "default_target": str(args.skills_dir / NAME)}
        elif args.verify_installed:
            result = {"valid": installed_inventory(args.skills_dir / NAME) == check_source(), "target": str(args.skills_dir / NAME)}
            if not result["valid"]:
                raise ValueError("installed version differs from this project")
        else:
            result = {"installed": str(install(args.skills_dir, upgrade=args.upgrade))}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
