#!/usr/bin/env python3
"""Host-wide, non-secret VM reservation registry.

The registry prevents two fresh-instance plans from claiming the same name,
disk root, bridge, guest subnet, address, or published port.  Reservations
are intentionally retained after a failed deployment; cleanup is an explicit
operator action after inspecting the partial instance.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ipaddress
import json
import os
import re
from pathlib import Path
import secrets
import subprocess
import tempfile
from typing import Any, Iterator
import importlib.util


def _services(network: dict[str, Any]) -> list[dict[str, Any]]:
    # Older rendered instances do not ship the optional policy module.  Keep
    # their legacy network metadata usable when no extras were declared; a
    # non-empty declaration still fails closed instead of silently dropping
    # its boundary policy.
    raw = network.get("published_services", [])
    if raw is None or raw == []:
        return []
    path = Path(__file__).with_name("service_policy.py")
    spec = importlib.util.spec_from_file_location("virtops_service_policy_registry", path)
    if spec is None or spec.loader is None:
        raise ValueError("published service policy is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.normalize(network)


REGISTRY_DIR = "registry"
LOCK_NAME = ".registry.lock"


def _path(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or "\n" in str(path) or "\r" in str(path):
        raise ValueError("host_root must be a clean absolute path")
    return path


def _safe_dir(path: Path, *, create: bool = False) -> Path:
    path = _path(path)
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    while True:
        if current.is_symlink():
            raise ValueError(f"registry ancestor is a symlink: {current}")
        if os.name == "posix":
            stat = current.stat()
            if stat.st_mode & 0o022:
                raise ValueError(f"registry ancestor is group/world writable: {current}")
            if os.geteuid() == 0 and stat.st_uid != 0:
                raise ValueError(f"registry ancestor is not root-owned: {current}")
        if current == current.parent:
            break
        current = current.parent
    if path.is_symlink():
        raise ValueError(f"registry path is a symlink: {path}")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError(f"registry path is not a directory: {path}")
    mode = path.stat().st_mode
    if os.name == "posix":
        if mode & 0o022:
            raise ValueError(f"registry directory is group/world writable: {path}")
        if os.geteuid() == 0 and path.stat().st_uid != 0:
            raise ValueError(f"registry directory is not root-owned: {path}")
    return path


def registry_dir(host_root: str | Path, *, create: bool = False) -> Path:
    root = _safe_dir(_path(host_root), create=create)
    return _safe_dir(root / REGISTRY_DIR, create=create)


@contextmanager
def _lock(path: Path, name: str = LOCK_NAME) -> Iterator[None]:
    lock = path / name
    if lock.is_symlink():
        raise ValueError(f"registry lock is a symlink: {lock}")
    lock.touch(mode=0o600, exist_ok=True)
    if os.name == "posix":
        stat = lock.stat()
        if stat.st_mode & 0o022 or (os.geteuid() == 0 and stat.st_uid != 0):
            raise ValueError(f"registry lock ownership/mode is unsafe: {lock}")
        lock.chmod(0o600)
    if os.name == "posix":
        import fcntl
        with lock.open("r+") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            yield
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    else:
        # Windows is used for offline profile tests only; Linux deployment is
        # protected by the fcntl lock above.
        yield


def _metadata(data: dict[str, Any]) -> dict[str, Any]:
    network, windows, resources = data["network"], data["windows"], data["resources"]
    labels = data.get("labels") or {}
    return {
        "schema_version": 1,
        "name": data["name"], "uuid": data["uuid"], "root": data["root"],
        "bridge": network["bridge"], "subnet": network["subnet"],
        "publish_ip": network["publish_ip"],
        "rdp_port": network["rdp_port"], "winrm_port": network["winrm_port"],
        "published_services": _services(network),
        "owner": labels.get("owner", data.get("owner", "")),
        "purpose": labels.get("purpose", data.get("purpose", "")),
        "admin_user": windows["admin_user"],
        "resources": {key: resources.get(key) for key in (
            "memory_gib", "host_limit_gib", "vcpus", "storage_gib", "cpu_set"
        )},
        "start_free_gib": (data.get("limits") or {}).get("start_free_gib"),
    }


def _valid_metadata(item: dict[str, Any]) -> None:
    for field in ("name", "uuid", "root", "bridge", "subnet", "publish_ip", "rdp_port", "winrm_port"):
        if not item.get(field):
            raise ValueError(f"registry metadata missing {field}")
    ipaddress.ip_network(item["subnet"])
    ipaddress.ip_address(item["publish_ip"])
    for key in ("rdp_port", "winrm_port"):
        if not isinstance(item[key], int) or not 1 <= item[key] <= 65535:
            raise ValueError(f"invalid {key}")
    services = item.get("published_services", [])
    if not isinstance(services, list):
        raise ValueError("invalid published_services")
    if not services:
        return
    # Registry rows are an identity boundary, so do not trust a hand-edited
    # service summary for port conflict checks.  Re-run the neutral policy
    # canonicalizer using the registry's guest/publish data.  client_cidr is
    # deliberately synthetic here: profile validation owns the actual client
    # bound, while this check still rejects 0/0 sources and all other policy
    # violations before a row can enter the registry.
    try:
        canonical = _services({
            "subnet": item["subnet"],
            "publish_ip": item["publish_ip"],
            "rdp_port": item["rdp_port"],
            "winrm_port": item["winrm_port"],
            "client_cidr": "0.0.0.0/0",
            "published_services": services,
        })
    except (KeyError, TypeError, ValueError, ipaddress.AddressValueError,
            ipaddress.NetmaskValueError) as exc:
        raise ValueError("invalid published service metadata") from exc
    if services != canonical:
        raise ValueError("published service metadata is not canonical")


def _conflict(candidate: dict[str, Any], existing: dict[str, Any]) -> str | None:
    for key in ("name", "uuid", "root", "bridge"):
        if candidate[key] == existing.get(key):
            return f"{key} already reserved by {existing.get('name')}"
    new_net = ipaddress.ip_network(candidate["subnet"])
    old_net = ipaddress.ip_network(existing["subnet"])
    if new_net.overlaps(old_net):
        return f"subnet overlaps reservation {existing.get('name')}"
    existing_ports = {existing.get("rdp_port"), existing.get("winrm_port")}
    existing_ports.update(s.get("publish_port") for s in existing.get("published_services", []) if isinstance(s, dict))
    candidate_ports = {candidate.get("rdp_port"), candidate.get("winrm_port")}
    candidate_ports.update(s.get("publish_port") for s in candidate.get("published_services", []) if isinstance(s, dict))
    for port in candidate_ports:
        if port in existing_ports:
            return f"published port {port} already reserved by {existing.get('name')}"
    def cpu_ids(record):
        text=(record.get('resources') or {}).get('cpu_set')
        if not text:return set()
        result=set()
        for part in text.split(','):
            if not re.fullmatch(r'\d+(?:-\d+)?',part):raise ValueError('invalid registry CPU reservation')
            bounds=[int(n) for n in part.split('-')]
            if bounds[0]>bounds[-1]:raise ValueError('reversed registry CPU reservation')
            result.update(range(bounds[0],bounds[-1]+1))
        return result
    if cpu_ids(candidate)&cpu_ids(existing):
        return f"CPU set overlaps reservation {existing.get('name')}"
    return None


def _records(directory: Path) -> list[tuple[Path, dict[str, Any]]]:
    result = []
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            _valid_metadata(item)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid registry entry {path}: {exc}") from exc
        result.append((path, item))
    return result


def reserve(data: dict[str, Any], *, allow_legacy: bool = False) -> dict[str, Any]:
    host_root = _path(data.get("host_root") or str(Path(data["root"]).parent))
    root = _path(data["root"])
    try:
        root.relative_to(host_root)
    except ValueError as exc:
        raise ValueError("instance root must be below host_root") from exc
    if root == host_root and not allow_legacy:
        raise ValueError("instance root must be a child of host_root")
    item = _metadata(data)
    _valid_metadata(item)
    directory = registry_dir(host_root, create=True)
    with _lock(directory):
        for _, existing in _records(directory):
            if existing == item:
                return item
            reason = _conflict(item, existing)
            if reason:
                raise ValueError(reason)
        destination = directory / f"{item['name']}.json"
        if destination.exists() or destination.is_symlink():
            raise ValueError(f"name already reserved: {item['name']}")
        fd, temporary = tempfile.mkstemp(prefix=f".{item['name']}.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(item, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o644)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return item


def claim(data: dict[str, Any]) -> dict[str, Any]:
    """Reserve before storage creation; retained on later stage failure."""
    return reserve(data)


def update_owner(data: dict[str, Any]) -> dict[str, Any]:
    """Update only owner-facing labels/account for an existing reservation."""
    host_root = _path(data.get("host_root") or str(Path(data["root"]).parent))
    directory = registry_dir(host_root)
    candidate = _metadata(data)
    immutable = ("name", "uuid", "root", "bridge", "subnet", "publish_ip",
                 "rdp_port", "winrm_port", "published_services", "resources", "start_free_gib")
    with _lock(directory):
        rows = _records(directory)
        match = next((pair for pair in rows if pair[1].get("name") == candidate["name"]), None)
        if match is None:
            raise FileNotFoundError(f"registry reservation not found: {candidate['name']}")
        path, existing = match
        for key in immutable:
            # Rows written before optional published-services support omit the
            # field; treat that legacy shape as the canonical empty list.
            old_value = existing.get(key, []) if key == "published_services" else existing.get(key)
            new_value = candidate.get(key, []) if key == "published_services" else candidate.get(key)
            if old_value != new_value:
                raise ValueError(f"registry immutable field changed: {key}")
        updated = dict(existing)
        for key in ("owner", "purpose", "admin_user"):
            updated[key] = candidate[key]
        fd, temporary = tempfile.mkstemp(prefix=f".{candidate['name']}.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(updated, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o644)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return updated


def list_entries(host_root: str | Path) -> list[dict[str, Any]]:
    directory = registry_dir(host_root)
    result = []
    for path, item in _records(directory):
        row = dict(item)
        virsh = shutil_which("virsh")
        if virsh:
            probe = subprocess.run([virsh, "--readonly", "-c", "qemu:///system", "domstate", item["name"]],
                                   text=True, capture_output=True, check=False)
            row["runtime_state"] = probe.stdout.strip() if probe.returncode == 0 else "undefined"
        else:
            row["runtime_state"] = "unknown"
        row["rdp_uri"] = f"rdp://{item['publish_ip']}:{item['rdp_port']}"
        row["winrm_uri"] = f"https://{item['publish_ip']}:{item['winrm_port']}/wsman"
        row["windows_user"] = f"{item['name'].upper()}\\{item['admin_user']}"
        result.append(row)
    return result


def shutil_which(name: str) -> str | None:
    import shutil
    return shutil.which(name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-root", required=True, type=Path)
    parser.add_argument("action", choices=["list"])
    args = parser.parse_args()
    print(json.dumps(list_entries(args.host_root), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
