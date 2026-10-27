#!/usr/bin/env python3
"""Plan and apply a declared per-VM inbound service change.

The default is read-only.  This controller changes only the published service
declaration and the files generated from it; it never accepts an arbitrary
iptables rule or changes a VM's identity, resources, disk, or credentials.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import types
from typing import Any


HERE = Path(__file__).resolve().parent


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


profile = _module("publish_profile", HERE / "profile.py")
upgrade = _module("publish_upgrade", HERE / "upgrade_runtime.py")


def _policy(network: dict[str, Any]) -> list[dict[str, Any]]:
    return profile.normalize_services(network)


def _json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"unsafe or missing JSON file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _safe(path: Path, *, allow_missing: bool = False) -> None:
    upgrade.secure_ancestors(path.parent)
    if allow_missing and not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"managed file is missing or symlinked: {path}")
    info = path.stat()
    if info.st_nlink != 1 or (os.name == "posix" and (info.st_uid != 0 or info.st_mode & 0o022)):
        raise RuntimeError(f"managed file ownership/mode is unsafe: {path}")


def _atomic(data: bytes, target: Path, mode: int) -> None:
    _safe(target, allow_missing=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "posix":
            os.chown(temporary, 0, 0)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _service_input(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("services file is missing or symlinked")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("services file must contain a JSON array")
    return value


def _assert_no_active_transition(root: Path) -> None:
    for path in (root / "vol/recovery/owner-transition.json",
                 root / "vol/state/private/network-transition.json"):
        if not path.exists():
            continue
        state = _json(path)
        if state.get("phase") != "complete":
            raise RuntimeError(f"active maintenance transition: {path}")


def _assert_host_ports(profile_data: dict[str, Any], services: list[dict[str, Any]]) -> None:
    if os.name != "posix":
        return
    net = profile_data["network"]
    ip = shutil.which("ip")
    ss = shutil.which("ss")
    if not ip or not ss:
        raise RuntimeError("ip and ss are required for published service preflight")
    addresses = subprocess.run([ip, "-j", "-4", "addr", "show"], text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               check=False, timeout=10)
    try:
        assigned = {item["local"] for link in json.loads(addresses.stdout)
                    for item in link.get("addr_info", []) if item.get("family") == "inet"}
    except (json.JSONDecodeError, TypeError, KeyError):
        assigned = set()
    if addresses.returncode or (str(net["publish_ip"]) not in assigned and net["publish_ip"] != "127.0.0.1"):
        raise RuntimeError(f"publish_ip is not assigned on host: {net['publish_ip']}")
    listening = subprocess.run([ss, "-H", "-ltn"], text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               check=False, timeout=10)
    if listening.returncode:
        raise RuntimeError("unable to inspect listening TCP ports")
    ports = {int(item["publish_port"]) for item in services}
    for line in listening.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        endpoint = fields[-2].rsplit(":", 1) if len(fields) >= 2 else []
        if len(endpoint) == 2 and endpoint[1].isdigit() and int(endpoint[1]) in ports:
            raise RuntimeError(f"published TCP port already listening: {endpoint[1]}")


def _registry_module():
    return _module("publish_registry", HERE.parent / "assets/runtime/instance_registry.py")


def _registry_guard(host_root: Path):
    if os.name != "posix":
        return contextlib.nullcontext()
    registry = _registry_module()
    directory = registry.registry_dir(host_root, create=False)
    return registry._lock(directory)


def _assert_registry_candidate(data: dict[str, Any]) -> None:
    if os.name != "posix":
        return
    registry = _registry_module()
    host_root = Path(data.get("host_root") or data["root"])
    directory = registry.registry_dir(host_root, create=False)
    candidate = registry._metadata(data)
    for path, existing in registry._records(directory):
        if existing.get("name") == data["name"]:
            continue
        reason = registry._conflict(candidate, existing)
        if reason:
            raise RuntimeError(reason)


def _prepare_services(profile_path: Path, services: list[dict[str, Any]]) -> dict[str, Any]:
    old = profile.load(profile_path, new=False)
    desired = copy.deepcopy(old)
    desired["network"]["published_services"] = services
    # The profile validator is the single schema authority.  Only the network
    # service list may differ from the loaded profile.
    profile.validate(desired, new=False)
    for key, value in old.items():
        if key != "network" and desired.get(key) != value:
            raise RuntimeError(f"service change altered immutable profile field: {key}")
    for key, value in old["network"].items():
        if key != "published_services" and desired["network"].get(key) != value:
            raise RuntimeError(f"service change altered network field: {key}")
    evidence = upgrade.inspect(profile_path, apply=False)
    store = evidence["store"]
    rendered_old = upgrade.render_runtime(old, store["uuid"], store["inode"])
    rendered_new = upgrade.render_runtime(desired, store["uuid"], store["inode"])
    root = Path(old["root"])
    _assert_no_active_transition(root)
    _assert_registry_candidate(desired)
    _assert_host_ports(desired, _policy(desired["network"]))
    changes: dict[Path, bytes] = {}
    runtime_names = {
        Path("scripts/lib") / f"{old['name']}_network.py",
        Path("scripts/lib/maintenance.py"), Path("scripts/lib/service_policy.py"),
        Path("scripts/lib/instance_registry.py"),
    }
    runtime_changed: dict[Path, bytes] = {}
    for relative in runtime_names:
        content = rendered_new.get(relative)
        if content is None:
            continue
        target = root / relative
        _safe(target, allow_missing=True)
        if not target.exists() or target.read_bytes() != content:
            changes[target] = content
            runtime_changed[relative] = content
    instance = root / "instance.json"
    _safe(instance)
    saved = _json(instance)
    saved["network"] = copy.deepcopy(desired["network"])
    changes[instance] = _bytes(saved)
    registry = Path(old.get("host_root") or root) / "registry" / f"{old['name']}.json"
    if registry.exists():
        _safe(registry)
        row = _json(registry)
        row["published_services"] = _policy(desired["network"])
        changes[registry] = _bytes(row)
    if profile_path.resolve() != instance.resolve():
        _safe(profile_path)
        changes[profile_path] = _bytes(desired)
    manifest = root / "vol/state/private/runtime-files.json"
    if manifest.exists():
        _safe(manifest)
        manifest_data = _json(manifest)
        manifest_data["name"] = old["name"]
        manifest_data["uuid"] = old["uuid"]
        hashes = manifest_data.setdefault("sha256", {})
        for relative, content in runtime_changed.items():
            hashes[relative.as_posix()] = hashlib.sha256(content).hexdigest()
        changes[manifest] = _bytes(manifest_data)
    before = {
        path: ((path.read_bytes() if path.exists() else None),
               (stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600))
        for path in changes
    }
    return {"old": old, "desired": desired, "evidence": evidence,
            "changes": changes, "before": before, "rendered": rendered_new,
            "rendered_old": rendered_old}


def prepare(profile_path: Path, services_path: Path) -> dict[str, Any]:
    return _prepare_services(profile_path, _service_input(services_path))


def _unchanged(plan: dict[str, Any]) -> None:
    for path, (content, _mode) in plan["before"].items():
        if content is None:
            if path.exists() or path.is_symlink():
                raise RuntimeError(f"new managed file appeared concurrently: {path}")
            continue
        _safe(path)
        if path.read_bytes() != content:
            raise RuntimeError(f"managed file changed concurrently: {path}")


def _backup(plan: dict[str, Any], payloads: dict[str, str] | None = None,
            shared_output_jump_before: bool | None = None,
            shared_output_jump_rule: list[str] | None = None) -> Path:
    root = Path(plan["old"]["root"])
    directory = root / "vol/state/private"
    directory.mkdir(parents=False, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="published-services-", dir=directory))
    os.chmod(backup, 0o700)
    index = {}
    for number, (path, (content, mode)) in enumerate(plan["before"].items()):
        target = backup / f"{number:03d}.before"
        target.write_bytes(content if content is not None else b"")
        os.chmod(target, 0o600)
        after = plan["changes"].get(path)
        index[str(path)] = {"file": target.name, "mode": mode, "exists": content is not None,
                            "sha256": hashlib.sha256(content).hexdigest() if content is not None else None,
                            "after_sha256": hashlib.sha256(after).hexdigest() if after is not None else None}
    (backup / "files.json").write_bytes(_bytes(index))
    os.chmod(backup / "files.json", 0o600)
    metadata = {"root": str(root), "name": plan["old"]["name"], "uuid": plan["old"]["uuid"],
                "shared_output_jump_before": shared_output_jump_before,
                "shared_output_jump_rule": shared_output_jump_rule}
    (backup / "metadata.json").write_bytes(_bytes(metadata))
    os.chmod(backup / "metadata.json", 0o600)
    if payloads:
        rule_hashes = {}
        for key in ("old_filter", "old_nat", "new_filter_gated", "new_nat_gated", "new_filter", "new_nat"):
            data = payloads[key].encode()
            target = backup / f"{key}.rules"
            target.write_bytes(data)
            os.chmod(target, 0o600)
            rule_hashes[target.name] = hashlib.sha256(data).hexdigest()
        metadata["rules_sha256"] = rule_hashes
        (backup / "metadata.json").write_bytes(_bytes(metadata))
        os.chmod(backup / "metadata.json", 0o600)
    return backup


def _restore(backup: Path) -> None:
    index = _json(backup / "files.json")
    for raw, item in index.items():
        target = Path(raw)
        if not item.get("exists", True):
            if target.exists() or target.is_symlink():
                _safe(target)
                target.unlink()
            continue
        data = (backup / item["file"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise RuntimeError(f"backup hash mismatch: {target}")
        _atomic(data, target, int(item["mode"]))


def _load_candidate(rendered: dict[Path, bytes], root: Path, relative: Path):
    """Load a rendered helper without importing the installed runtime."""
    maintenance_source = rendered.get(Path("scripts/lib/maintenance.py"))
    if maintenance_source is None:
        maintenance_source = (HERE.parent / "assets/runtime/maintenance.py").read_bytes()
    saved = sys.modules.get("maintenance")
    maintenance_module = types.ModuleType("maintenance")
    maintenance_module.__file__ = str(root / "scripts/lib/maintenance.py")
    exec(compile(maintenance_source, maintenance_module.__file__, "exec"), maintenance_module.__dict__)
    module = types.ModuleType(f"candidate_{relative.stem}")
    module.__file__ = str(root / relative)
    sys.modules["maintenance"] = maintenance_module
    try:
        exec(compile(rendered[relative], module.__file__, "exec"), module.__dict__)
    finally:
        if saved is None:
            sys.modules.pop("maintenance", None)
        else:
            sys.modules["maintenance"] = saved
    return module


def _quote(words: list[str]) -> str:
    rendered = []
    for word in words:
        if any(char.isspace() or char in "\\\"'" for char in word):
            rendered.append(json.dumps(word, ensure_ascii=False))
        else:
            rendered.append(word)
    return " ".join(rendered)


def _table_payload(module: Any, table: str, *, gated: bool, cleanup_gate: bool = False) -> str:
    """Render only this VM's IPv4 private chains; never restore a whole table."""
    previous = module.maintenance_active
    module.maintenance_active = (lambda: gated)
    try:
        specs = list(module.private_chain_specs())
    finally:
        module.maintenance_active = previous
    rows: list[str] = [f"*{table}"]
    selected = [(chain, rules) for v6, current, chain, rules in specs
                if not v6 and current == table]
    gate = module.maintenance_gate() if table == "filter" and (gated or cleanup_gate) else None
    declarations = [chain for chain, _ in selected]
    if gate is not None:
        declarations.append(gate.chain)
    for chain in declarations:
        rows.append(f":{chain} -")
    for chain, rules in selected:
        rows.append(f"-F {chain}")
        for rule in rules:
            rows.append(_quote(["-A", chain, *rule]))
    if gate is not None and gated:
        rows.append(f"-F {gate.chain}")
        for rule in gate.rules():
            rows.append(_quote(["-A", gate.chain, *rule]))
    elif gate is not None:
        rows.extend((f"-F {gate.chain}", f"-X {gate.chain}"))
    rows.append("COMMIT")
    return "\n".join(rows) + "\n"


def _transaction_payloads(plan: dict[str, Any]) -> dict[str, str]:
    root = Path(plan["old"]["root"])
    relative = Path("scripts/lib") / f"{plan['old']['name']}_network.py"
    old_module = _load_candidate(plan["rendered_old"], root, relative)
    new_module = _load_candidate(plan["rendered"], root, relative)
    # Build stable rules before the marker exists.  Gating is then added only
    # to the new candidate while the marker is active.
    return {
        "old_filter": _table_payload(old_module, "filter", gated=False),
        "old_nat": _table_payload(old_module, "nat", gated=False),
        "new_filter_gated": _table_payload(new_module, "filter", gated=True),
        "new_nat_gated": _table_payload(new_module, "nat", gated=False),
        "new_filter": _table_payload(new_module, "filter", gated=False, cleanup_gate=True),
        "new_nat": _table_payload(new_module, "nat", gated=False),
    }


def _capture_private_rules(plan: dict[str, Any]) -> dict[str, str]:
    """Capture the installed VM chains; rollback must not re-render old policy."""
    if os.name != "posix":
        return {"old_filter": "*filter\nCOMMIT\n", "old_nat": "*nat\nCOMMIT\n"}
    root = Path(plan["old"]["root"])
    relative = Path("scripts/lib") / f"{plan['old']['name']}_network.py"
    module = _load_candidate(plan["rendered_old"], root, relative)
    names = {chain for _v6, table, chain in module.chain_rules() if not _v6}
    names.add(f"{plan['old']['name'].upper()}_MAINT")
    result: dict[str, str] = {}
    for table in ("filter", "nat"):
        binary = shutil.which("iptables-save")
        if not binary:
            raise RuntimeError("iptables-save is unavailable")
        saved = subprocess.run([binary, "-t", table], text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               check=False, timeout=15)
        if saved.returncode:
            raise RuntimeError(f"unable to snapshot installed {table} rules")
        selected = []
        seen = set()
        for line in saved.stdout.splitlines():
            if line.startswith(":"):
                chain = line[1:].split()[0]
                if chain in names:
                    selected.append(line)
                    seen.add(chain)
            elif line.startswith("-A "):
                chain = line.split()[1]
                if chain in names:
                    selected.append(line)
        required = {chain for _v6, current, chain in module.chain_rules() if not _v6 and current == table}
        if not required.issubset(seen):
            raise RuntimeError(f"installed {table} snapshot is missing private chains")
        rows = [f"*{table}"]
        declarations = [line for line in selected if line.startswith(":")]
        rules = [line for line in selected if line.startswith("-A ")]
        rows.extend(declarations)
        rows.extend(f"-F {line[1:].split()[0]}" for line in declarations)
        rows.extend(rules)
        gate = f"{plan['old']['name'].upper()}_MAINT"
        if table == "filter" and gate not in seen:
            # The transaction creates this chain temporarily.  Restore its
            # original absence as well as the original private rules.
            rows.insert(1, f":{gate} - [0:0]")
            rows.extend([f"-F {gate}", f"-X {gate}"])
        rows.append("COMMIT")
        result[f"old_{table}"] = "\n".join(rows) + "\n"
    return result


def _shared_output_jump(plan: dict[str, Any]) -> tuple[list[str], bool]:
    """Return the exact shared OUTPUT jump required by the current helper."""
    if os.name != "posix":
        return ["-d", "0.0.0.0", "-j", f"{plan['old']['name'].upper()}_OUTPUT"], False
    root = Path(plan["old"]["root"])
    relative = Path("scripts/lib") / f"{plan['old']['name']}_network.py"
    module = _load_candidate(plan["rendered"], root, relative)
    rule = ["-d", module.GUEST_IP, "-j", module.FILTER4["output"]]
    binary = shutil.which("iptables")
    if not binary:
        raise RuntimeError("iptables is unavailable")
    result = subprocess.run([binary, "-t", "filter", "-C", "OUTPUT", *rule],
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            check=False, timeout=15)
    return rule, result.returncode == 0


def _ensure_shared_output_jump(plan: dict[str, Any]) -> None:
    rule, present = _shared_output_jump(plan)
    if os.name != "posix" or present:
        return
    binary = shutil.which("iptables")
    result = subprocess.run([binary, "-t", "filter", "-I", "OUTPUT", "1", *rule],
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            check=False, timeout=15)
    if result.returncode:
        raise RuntimeError("unable to install the dedicated shared OUTPUT jump")


def _restore_shared_output_jump(plan: dict[str, Any], backup: Path) -> None:
    metadata = _json(backup / "metadata.json")
    if metadata.get("shared_output_jump_before") is not False or os.name != "posix":
        return
    rule = metadata.get("shared_output_jump_rule")
    if not isinstance(rule, list) or not all(isinstance(item, str) for item in rule):
        raise RuntimeError("backup has no safe shared OUTPUT jump fingerprint")
    binary = shutil.which("iptables")
    check = subprocess.run([binary, "-t", "filter", "-C", "OUTPUT", *rule],
                           text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           check=False, timeout=15)
    if check.returncode == 0:
        removed = subprocess.run([binary, "-t", "filter", "-D", "OUTPUT", *rule],
                                 text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 check=False, timeout=15)
        if removed.returncode:
            raise RuntimeError("unable to remove the transaction-owned shared OUTPUT jump")


def _restore_payload(payload: str) -> None:
    binary = shutil.which("iptables-nft-restore") or shutil.which("iptables-restore")
    if not binary:
        raise RuntimeError("iptables-nft-restore is unavailable")
    command = [binary, "--noflush", "-w", "5"]
    _test_restore_payload(payload, binary=binary, command=command)
    result = subprocess.run(command, input=payload, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=15)
    if result.returncode:
        raise RuntimeError(f"iptables restore failed: {(result.stderr or result.stdout).strip()}")


def _test_restore_payload(payload: str, *, binary: str | None = None, command: list[str] | None = None) -> None:
    if os.name != "posix":
        return
    binary = binary or shutil.which("iptables-nft-restore") or shutil.which("iptables-restore")
    if not binary:
        raise RuntimeError("iptables-nft-restore is unavailable")
    command = command or [binary, "--noflush", "-w", "5"]
    if "\x00" in payload or "\r" in payload:
        raise RuntimeError("unsafe iptables restore payload")
    test = subprocess.run([*command, "--test"], input=payload, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=15)
    if test.returncode:
        raise RuntimeError(f"iptables restore test failed: {(test.stderr or test.stdout).strip()}")


def _install_pair(payloads: dict[str, str], prefix: str) -> None:
    _restore_payload(payloads[f"{prefix}_filter"])
    _restore_payload(payloads[f"{prefix}_nat"])


def _marker(plan: dict[str, Any], phase: str, backup: Path | None = None) -> Path:
    path = Path(plan["old"]["root"]) / "vol/state/private/network-transition.json"
    data = {"name": plan["old"]["name"], "uuid": plan["old"]["uuid"], "phase": phase,
            "operation": "published-services", "services": plan["desired"]["network"].get("published_services", [])}
    if backup is not None:
        data["backup"] = str(backup)
        data["expected_after"] = {str(path): hashlib.sha256(content).hexdigest()
                                   for path, content in plan["changes"].items()}
    _atomic(_bytes(data), path, 0o600)
    return path


def _remove_marker(path: Path) -> None:
    if path.exists() or path.is_symlink():
        _safe(path)
        path.unlink()


def apply_plan(plan: dict[str, Any]) -> dict[str, Any]:
    name = plan["old"]["name"]
    payloads = _transaction_payloads(plan)
    marker = Path(plan["old"]["root"]) / "vol/state/private/network-transition.json"
    backup: Path | None = None
    with upgrade.lifecycle_lock(name), _registry_guard(Path(plan["old"].get("host_root") or plan["old"]["root"])):
        try:
            _unchanged(plan)
            if plan["evidence"].get("vm_state") not in {"running", "paused", "shut off", "shutoff"}:
                raise RuntimeError(f"unsupported VM state: {plan['evidence'].get('vm_state')}")
            _assert_no_active_transition(Path(plan["old"]["root"]))
            _assert_registry_candidate(plan["desired"])
            _assert_host_ports(plan["desired"], _policy(plan["desired"]["network"]))
            domain_before = (upgrade.domain_facts(plan["old"], Path(plan["old"]["root"]))
                             if os.name == "posix" else None)
            _assert_auto_port_still_free(plan)
            old_helper = Path(plan["old"]["root"]) / "scripts/lib" / f"{name}_network.py"
            old_check = upgrade.run(["python3", str(old_helper), "verify", "--backend", "iptables-nft"])
            if old_check.returncode:
                raise RuntimeError("existing network helper verification failed")
            payloads.update(_capture_private_rules(plan))
            jump_rule, jump_before = _shared_output_jump(plan)
            # Validate every candidate restore while no marker exists.  A
            # syntax/semantic failure cannot leave maintenance state behind.
            for key in ("new_filter_gated", "new_nat_gated", "new_filter", "new_nat"):
                _test_restore_payload(payloads[key])
            backup = _backup(plan, payloads, jump_before, jump_rule)
            _marker(plan, "applying", backup)
            # Install the gate before changing NAT so no new flow can enter
            # during the transition window.
            _restore_payload(payloads["new_filter_gated"])
            _ensure_shared_output_jump(plan)
            _restore_payload(payloads["new_nat_gated"])
            for path, content in plan["changes"].items():
                mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
                _atomic(content, path, mode)
            helper = old_helper
            result = upgrade.run(["python3", str(helper), "verify", "--backend", "iptables-nft"])
            if result.returncode:
                raise RuntimeError("network helper verification failed after service update: " +
                                   (result.stderr or result.stdout or "no diagnostic output").strip())
            _marker(plan, "releasing", backup)
            _install_pair(payloads, "new")
            _remove_marker(marker)
            result = upgrade.run(["python3", str(helper), "verify", "--backend", "iptables-nft"])
            if result.returncode:
                raise RuntimeError("network helper verification failed after gate release: " +
                                   (result.stderr or result.stdout or "no diagnostic output").strip())
            if domain_before is not None:
                domain_after = upgrade.domain_facts(plan["old"], Path(plan["old"]["root"]))
                if domain_after != domain_before:
                    raise RuntimeError("VM state or libvirt XML changed during service transaction")
            return {"status": "applied", "backup": str(backup),
                    "services": plan["desired"]["network"].get("published_services", [])}
        except Exception as error:
            if backup is None:
                raise
            try:
                if not marker.exists():
                    _marker(plan, "restoring", backup)
                # Keep the gate and the new NAT in place while files are
                # restored; old stable filter is installed only at the end.
                _restore_payload(payloads["new_filter_gated"])
                _restore_payload(payloads["new_nat_gated"])
                _restore(backup)
                _restore_payload(payloads["old_nat"])
                _restore_payload(payloads["old_filter"])
                _restore_shared_output_jump(plan, backup)
                _remove_marker(marker)
                helper = Path(plan["old"]["root"]) / "scripts/lib" / f"{name}_network.py"
                result = upgrade.run(["python3", str(helper), "verify", "--backend", "iptables-nft"])
                if result.returncode:
                    raise RuntimeError("network verification failed after rollback")
            except Exception as rollback_error:
                raise RuntimeError(f"service update blocked; rollback failed; backup {backup}: {rollback_error}") from error
            raise RuntimeError(f"service update failed and was restored; backup {backup}: {error}") from error


def _validate_rule_file(path: Path, name: str, table: str) -> None:
    import shlex
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != f"*{table}" or lines[-1] != "COMMIT":
        raise RuntimeError(f"invalid {table} rule snapshot")
    chains = {line[1:].split()[0] for line in lines[1:-1] if line.startswith(":")}
    suffixes = {"INPUT", "FWD_FROM", "FWD_TO", "OUTPUT", "MAINT"} if table == "filter" else {"DNAT", "DNAT_OUT", "SNAT"}
    allowed = {name.upper() + "_" + suffix for suffix in suffixes}
    if not chains or not chains <= allowed:
        raise RuntimeError("rule snapshot contains a foreign chain")
    for line in lines[1:-1]:
        if line.startswith(":"):
            continue
        if line.startswith(("-A ", "-F ", "-X ")):
            fields = shlex.split(line)
            if len(fields) < 2 or fields[1] not in chains:
                raise RuntimeError("rule snapshot mutates a shared chain")
            if fields[0] in {"-F", "-X"} and len(fields) != 2:
                raise RuntimeError("invalid private-chain cleanup rule")
            if fields[0] == "-A" and "-j" in fields:
                target = fields[fields.index("-j") + 1]
                if target not in {"ACCEPT", "DROP", "LOG", "RETURN", "DNAT", "SNAT"} | allowed:
                    raise RuntimeError("rule snapshot jumps outside the instance")
        else:
            raise RuntimeError("rule snapshot contains an unsupported operation")


def _validate_backup(path: Path, profile_path: Path, *, allow_partial: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    if path.is_symlink() or not path.is_dir():
        raise ValueError("backup directory is missing or symlinked")
    metadata = _json(path / "metadata.json")
    _safe(path / "metadata.json")
    _safe(path / "files.json")
    current = profile.load(profile_path, new=False)
    root = Path(current["root"]).resolve()
    if metadata.get("root") != str(root) or metadata.get("name") != current["name"] or metadata.get("uuid") != current["uuid"]:
        raise RuntimeError("backup is not bound to the supplied profile")
    private = (root / "vol/state/private").resolve()
    if path.resolve().parent != private:
        raise RuntimeError("backup is outside the instance private state directory")
    index = _json(path / "files.json")
    allowed = {profile_path.resolve(), (root / "instance.json").resolve(),
               (root / "vol/state/private/runtime-files.json").resolve(),
               (Path(current.get("host_root") or root) / "registry" / f"{current['name']}.json").resolve()}
    allowed.update((root / "scripts/lib" / name).resolve() for name in
                   (f"{current['name']}_network.py", "maintenance.py", "service_policy.py", "instance_registry.py"))
    for raw, item in index.items():
        target = Path(raw)
        if target.resolve() not in allowed:
            raise RuntimeError(f"backup target is outside exact allowlist: {target}")
        _safe(target, allow_missing=not item.get("exists", True))
        member = path / item["file"]
        _safe(member)
        if member.resolve().parent != path.resolve():
            raise RuntimeError("backup member is unsafe")
        current_hash = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
        before_hash = item.get("sha256")
        after_hash = item.get("after_sha256")
        before_ok = (current_hash == before_hash) if item.get("exists", True) else current_hash is None
        after_ok = bool(after_hash) and current_hash == after_hash
        if (allow_partial and not (before_ok or after_ok)) or (not allow_partial and not after_ok):
            raise RuntimeError(f"current target fingerprint changed: {target}")
    rule_hashes = metadata.get("rules_sha256") or {}
    required_rules = {key + ".rules" for key in ("old_filter", "old_nat", "new_filter", "new_nat", "new_filter_gated", "new_nat_gated")}
    if set(rule_hashes) != required_rules:
        raise RuntimeError("backup private rule fingerprints are incomplete")
    for filename, digest in rule_hashes.items():
        member = path / filename
        _safe(member)
        if hashlib.sha256(member.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"rule snapshot hash mismatch: {filename}")
        if filename.endswith("_filter.rules"):
            _validate_rule_file(member, current["name"], "filter")
        elif filename.endswith("_nat.rules"):
            _validate_rule_file(member, current["name"], "nat")
    expected_jump = ["-d", current["network"]["guest_ip"], "-j", current["name"].upper() + "_OUTPUT"]
    if metadata.get("shared_output_jump_rule") not in (None, expected_jump):
        raise RuntimeError("backup shared OUTPUT jump is not the current instance jump")
    return current, index


def rollback(path: Path, profile_path: Path) -> dict[str, Any]:
    current_profile = profile.load(profile_path, new=False)
    marker = Path(current_profile["root"]) / "vol/state/private/network-transition.json"
    marker_data = _json(marker) if marker.exists() else {}
    allow_partial = marker_data.get("phase") not in (None, "complete")
    if allow_partial and (marker_data.get("uuid") != current_profile["uuid"] or
                          Path(marker_data.get("backup", "")).resolve() != path.resolve()):
        raise RuntimeError("active transaction marker does not match rollback backup")
    current, index = _validate_backup(path, profile_path, allow_partial=allow_partial)
    target_names = [Path(item) for item in index]
    name = next((p.name.removesuffix("_network.py") for p in target_names
                 if p.name.endswith("_network.py")), None)
    if not name:
        # infer from the instance.json target
        instance = next((p for p in target_names if p.name == "instance.json"), None)
        if instance is None:
            raise ValueError("backup has no instance identity")
        name = current["name"]
    if name != current["name"]:
        raise RuntimeError("backup target name does not match profile")
    with upgrade.lifecycle_lock(name), _registry_guard(Path(current.get("host_root") or current["root"])):
        _validate_backup(path, profile_path, allow_partial=allow_partial)
        root = Path(current["root"])
        old_filter = path / "old_filter.rules"
        old_nat = path / "old_nat.rules"
        if not old_filter.is_file() or not old_nat.is_file():
            raise RuntimeError("backup has no private firewall rule snapshots")
        gated_filter = path / "new_filter_gated.rules"
        gated_nat = path / "new_nat_gated.rules"
        if not gated_filter.is_file() or not gated_nat.is_file():
            raise RuntimeError("backup has no maintenance gate snapshots")
        _restore_payload(gated_filter.read_text(encoding="utf-8"))
        _restore_payload(gated_nat.read_text(encoding="utf-8"))
        _restore_payload(old_nat.read_text(encoding="utf-8"))
        _restore(path)
        _restore_payload(old_filter.read_text(encoding="utf-8"))
        _restore_shared_output_jump({}, path)
        marker = root / "vol/state/private/network-transition.json"
        _remove_marker(marker)
        helper = root / "scripts/lib" / f"{name}_network.py"
        result = upgrade.run(["python3", str(helper), "verify", "--backend", "iptables-nft"])
        if result.returncode:
            raise RuntimeError("network verification failed after rollback")
    return {"status": "rolled_back", "backup": str(path), "target": name}


def _listening_ports() -> set[int]:
    if os.name != "posix":
        return set()
    ss = shutil.which("ss")
    if not ss:
        raise RuntimeError("ss is required for automatic published-port allocation")
    result = subprocess.run([ss, "-H", "-ltn"], text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False, timeout=10)
    if result.returncode:
        raise RuntimeError("unable to inspect listening TCP ports")
    ports = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        endpoint = fields[-2].rsplit(":", 1) if len(fields) >= 2 else []
        if len(endpoint) == 2 and endpoint[1].isdigit():
            ports.add(int(endpoint[1]))
    return ports


def _reserved_ports(data: dict[str, Any]) -> set[int]:
    ports = {data["network"]["rdp_port"], data["network"]["winrm_port"]}
    ports.update(item["publish_port"] for item in _policy(data["network"]))
    if os.name == "posix":
        registry = _registry_module()
        directory = registry.registry_dir(Path(data.get("host_root") or data["root"]), create=False)
        for _path, item in registry._records(directory):
            ports.update({item.get("rdp_port"), item.get("winrm_port")})
            ports.update(service.get("publish_port") for service in item.get("published_services", []))
    ports.update(_listening_ports())
    return {port for port in ports if isinstance(port, int)}


def _resolve_cli_services(profile_path: Path, add_id: str | None, guest_port: int | None,
                          publish_port: int | None, remove_id: str | None) -> tuple[list[dict[str, Any]], int | None]:
    data = profile.load(profile_path, new=False)
    current = _policy(data["network"])
    if remove_id:
        found = [item for item in current if item["id"] == remove_id]
        if not found:
            raise ValueError(f"published service does not exist: {remove_id}")
        return [item for item in current if item["id"] != remove_id], None
    assert add_id is not None and guest_port is not None
    if any(item["id"] == add_id for item in current):
        if publish_port is None:
            publish_port = next(item["publish_port"] for item in current if item["id"] == add_id)
        current = [item for item in current if item["id"] != add_id]
    auto = publish_port is None
    if auto:
        reserved = _reserved_ports(data)
        publish_port = next((port for port in range(18000, 19000) if port not in reserved), None)
        if publish_port is None:
            raise RuntimeError("no free published port in 18000..18999")
    service = {"id": add_id, "protocol": "tcp", "publish_ip": "127.0.0.1",
               "publish_port": publish_port, "guest_port": guest_port,
               "allowed_sources": ["127.0.0.1/32"], "access": "host-loopback",
               "snat_to_bridge": True, "allow_during_maintenance": False}
    current.append(service)
    return _policy({**data["network"], "published_services": current}), publish_port if auto else None


def _assert_auto_port_still_free(plan: dict[str, Any]) -> None:
    port = plan.get("auto_publish_port")
    if port is None:
        return
    old_services = {item["id"]: item for item in _policy(plan["old"]["network"])}
    selected = next((item for item in plan["desired"]["network"].get("published_services", [])
                    if item["publish_port"] == port), None)
    if selected and old_services.get(selected["id"], {}).get("publish_port") == port:
        return
    if port in _reserved_ports(plan["old"]):
        raise RuntimeError(f"automatically selected published port lost its reservation: {port}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--services", type=Path)
    group.add_argument("--add-service", metavar="ID")
    group.add_argument("--remove-service", metavar="ID")
    parser.add_argument("--guest-port", type=int)
    parser.add_argument("--publish-port", type=int)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", type=Path)
    args = parser.parse_args()
    try:
        if args.rollback:
            if not args.profile:
                raise ValueError("--profile is required with --rollback")
            print(json.dumps(rollback(args.rollback.absolute(), args.profile.absolute()), ensure_ascii=False))
            return 0
        if not args.profile:
            raise ValueError("--profile is required")
        if args.add_service or args.remove_service:
            if args.add_service and args.guest_port is None:
                raise ValueError("--guest-port is required with --add-service")
            if args.remove_service and (args.guest_port is not None or args.publish_port is not None):
                raise ValueError("port options are only valid with --add-service")
            services, auto_port = _resolve_cli_services(args.profile.absolute(), args.add_service,
                                                        args.guest_port, args.publish_port, args.remove_service)
            plan = _prepare_services(args.profile.absolute(), services)
            plan["auto_publish_port"] = auto_port
        elif args.services:
            if args.guest_port is not None or args.publish_port is not None:
                raise ValueError("port options require --add-service")
            plan = prepare(args.profile.absolute(), args.services.absolute())
        else:
            raise ValueError("one of --services, --add-service, or --remove-service is required")
        if not args.apply:
            print(json.dumps({"status": "ready", "mode": "check", "vm_state": plan["evidence"]["vm_state"],
                              "changed_files": [str(path) for path in plan["changes"]],
                              "services": plan["desired"]["network"].get("published_services", []),
                              "apply_required": True}, ensure_ascii=False))
            return 0
        print(json.dumps(apply_plan(plan), ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        print(json.dumps({"status": "blocked", "reason": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
