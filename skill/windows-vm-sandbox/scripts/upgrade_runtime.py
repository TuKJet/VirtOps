"""Safely upgrade the managed runtime of an existing VM.

The default is a read-only check.  ``--apply`` only replaces the reviewed
wrapper/library files after the profile, store, libvirt XML and provenance
checks pass.  It never changes the VM definition, network rules or systemd
units and never removes unknown files.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
try:
    import fcntl
except ImportError:  # pragma: no cover - local Windows tests only
    fcntl = None
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import xml.etree.ElementTree as ET


HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
ASSETS = SKILL / "assets"
RUNTIME = ASSETS / "runtime"
MANIFEST = RUNTIME / "source-manifest.json"
BASIC = {"start.sh", "stop.sh", "status.sh", "logs.sh", "counters.sh", "reset.sh", "delete.sh", "recreate.sh"}
RUNTIME_FILES = frozenset({
    "vm.sh", "vmctl.py", "start.sh", "stop.sh", "status.sh", "logs.sh", "counters.sh",
    "reset.sh", "delete.sh", "recreate.sh", "winvm01_bridge.py", "winvm01_network.py",
    "winvm01_log_collector.py", "winvm01_store_guard.py", "winvm01_qga.py",
    "winvm01_recovery.py", "winvm01_c_reset.py", "winvm01_reset_context.ps1",
    "winvm01_reset_postboot.ps1", "owner_apply.ps1", "owner_verify.ps1",
    "owner_environment.py", "maintenance.py", "instance_registry.py", "admission.py",
    "service_policy.py",
})


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_profile(path: Path) -> dict:
    spec = importlib.util.spec_from_file_location("virtops_profile", HERE / "profile.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module.load(path, new=False)


def complete_cpu_profile(profile: dict) -> dict:
    resources = profile.get("resources", {})
    if all(resources.get(key) is not None for key in ("cpu_order", "threads_per_core", "amd")):
        return profile
    prep_path = HERE / "prepare_host.py"
    spec = importlib.util.spec_from_file_location("virtops_prepare_host", prep_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    result = subprocess.run(["lscpu", "-p=CPU,CORE,SOCKET,ONLINE"], text=True,
                            capture_output=True, check=False, timeout=30)
    if result.returncode:
        raise RuntimeError("cannot derive CPU topology with lscpu")
    return module.select_cpu(profile, result.stdout)


def run(args: list[str], *, check=False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=check, timeout=30)


def manifest_hashes() -> dict[str, set[str]]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    hashes: dict[str, set[str]] = {}
    for key in ("sha256", "upstream_sha256", "rendered_sha256"):
        section = data.get(key, {})
        if isinstance(section, dict):
            for name, value in section.items():
                if isinstance(value, str) and len(value) == 64:
                    hashes.setdefault(name, set()).add(value)
    return hashes


def render_runtime(profile: dict, store_uuid: str, inode: int) -> dict[Path, bytes]:
    spec = importlib.util.spec_from_file_location("virtops_render", HERE / "render.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    rendered = module.runtime_files(profile, store_uuid, inode)
    return {Path(relative): text.encode("utf-8") for relative, text in rendered.items()}


def secure_ancestors(path: Path) -> None:
    if os.name != "posix":
        return
    current = path.absolute()
    for item in [current, *current.parents]:
        st = item.stat()
        if item.is_symlink() or st.st_uid != 0 or st.st_mode & 0o022:
            raise RuntimeError(f"unsafe root ancestor: {item}")


def validate_source() -> tuple[dict[str, set[str]], dict[str, Path]]:
    expected = manifest_hashes()
    result = {}
    for name in sorted(RUNTIME_FILES):
        source = RUNTIME / name
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(f"runtime source missing or symlink: {name}")
        allowed = expected.get(name, set())
        if not allowed:
            raise RuntimeError(f"runtime provenance missing for {name}; update source-manifest before applying")
        if sha256(source) not in allowed:
            raise RuntimeError(f"runtime source is not one of the manifest hashes: {name}")
        result[name] = source
    return expected, result


def read_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"required state file missing or symlink: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def check_owner_state(root: Path, profile: dict) -> None:
    private = root / "vol/state/private"
    if private.is_symlink() or not private.is_dir():
        raise RuntimeError("private state directory is missing or symlinked")
    if os.name == "posix":
        for item in (private, root / "vol/state"):
            st = item.stat()
            forbidden = 0o077 if item == private else 0o022
            if item.is_symlink() or st.st_uid != 0 or st.st_mode & forbidden:
                raise RuntimeError("unsafe private state ownership or permissions")
    credentials = private / "credentials.json"
    if credentials.is_symlink() or not credentials.is_file():
        raise RuntimeError("existing root-only credentials.json is required")
    cst = credentials.stat()
    if cst.st_uid != 0 or cst.st_mode & 0o077:
        raise RuntimeError("credentials.json must be root-owned mode 0600")
    payload = read_json(credentials)
    user = profile["windows"]["admin_user"]
    if user not in payload or not isinstance(payload[user], str):
        raise RuntimeError("credentials.json does not contain the profile administrator")
    owner = private / "owner.json"
    if owner.exists():
        state = read_json(owner)
        if state.get("name") != profile["name"] or state.get("uuid") != profile["uuid"]:
            raise RuntimeError("existing owner.json belongs to another VM")
        if state.get("admin_user") != user or state.get("password") != payload[user]:
            raise RuntimeError("owner.json administrator or password differs from credentials.json")


def store_identity(root: Path, profile: dict) -> dict:
    store = root / ".storage" / f"{profile['name']}.store"
    if store.is_symlink() or not store.is_file():
        raise RuntimeError("bounded store is missing or symlinked")
    st = store.stat()
    if st.st_uid != 0 or st.st_mode & 0o077:
        raise RuntimeError("bounded store must be root-owned and private")
    expected_size = profile["resources"]["storage_gib"] * 1024**3
    if st.st_size != expected_size:
        raise RuntimeError("bounded store size does not match profile storage budget")
    if os.name == "posix" and st.st_blocks * 512 < st.st_size:
        raise RuntimeError("bounded store is sparse or not fully allocated")
    volume = root / "vol"
    if not volume.is_dir() or volume.is_symlink():
        raise RuntimeError("bounded store mountpoint is missing or symlinked")
    mounted = run(["findmnt", "-rn", "--mountpoint", str(volume), "-o", "UUID"])
    actual_uuid = mounted.stdout.strip()
    if mounted.returncode or not actual_uuid:
        raise RuntimeError("unable to read bounded store filesystem UUID")
    source = run(["findmnt", "-rn", "--mountpoint", str(volume), "-o", "SOURCE"])
    loop = source.stdout.strip()
    if source.returncode or not loop.startswith("/dev/loop"):
        raise RuntimeError("bounded store must be mounted through one loop device")
    backing = run(["losetup", "--list", "--json", "--output", "NAME,BACK-FILE"])
    loops = json.loads(backing.stdout).get('loopdevices', []) if not backing.returncode else []
    matched = [entry for entry in loops if entry.get('name') == loop]
    if len(matched) != 1 or Path(matched[0].get('back-file', '')).resolve() != store.resolve():
        raise RuntimeError("loop device backing file does not match bounded store")
    identity = profile.get("store_identity") or {}
    instance = root / "instance.json"
    if instance.exists():
        saved = read_json(instance)
        if saved.get("name") != profile["name"] or saved.get("uuid") != profile["uuid"] or saved.get("root") != str(root):
            raise RuntimeError("instance.json does not match the profile")
        identity = saved.get("store_identity") or {}
        if int(identity.get("inode", -1)) != st.st_ino or identity.get("uuid") != actual_uuid:
            raise RuntimeError("store identity does not match instance.json")
    return {"uuid": actual_uuid, "inode": st.st_ino}


def domain_facts(profile: dict, root: Path) -> tuple[str, str]:
    result = run(["virsh", "--readonly", "-c", "qemu:///system", "dumpxml", profile["name"]])
    if result.returncode:
        raise RuntimeError("unable to read libvirt XML in read-only mode")
    xml = ET.fromstring(result.stdout)
    if xml.findtext("name") != profile["name"] or xml.findtext("uuid") != profile["uuid"]:
        raise RuntimeError("libvirt domain name/UUID does not match profile")
    resources = profile["resources"]
    expected_kib = resources["memory_gib"] * 1024 * 1024
    for tag in ("memory", "currentMemory"):
        node = xml.find(tag)
        if node is None or node.get("unit", "KiB") != "KiB" or int(node.text or 0) != expected_kib:
            raise RuntimeError(f"libvirt {tag} does not match profile memory")
    if int(xml.findtext("./memtune/hard_limit", "0")) != resources["host_limit_gib"] * 1024 * 1024:
        raise RuntimeError("libvirt hard_limit does not match profile host limit")
    if int(xml.findtext("vcpu", "0")) != resources["vcpus"]:
        raise RuntimeError("libvirt vCPU count does not match profile")
    disk = next((node for node in xml.findall("./devices/disk") if node.get("device") == "disk"), None)
    expected_disk = str(root / "vol/current.qcow2")
    if disk is None or disk.find("source") is None or disk.find("source").get("file") != expected_disk:
        raise RuntimeError("libvirt system disk does not match the profile root")
    nvram = xml.find("./os/nvram")
    expected_nvram = str(root / "vol/state" / f"{profile['name']}_VARS.fd")
    if nvram is None or nvram.text != expected_nvram:
        raise RuntimeError("libvirt NVRAM path does not match the profile")
    if xml.find("./devices/tpm") is None:
        raise RuntimeError("libvirt TPM device is missing")
    interface = xml.find("./devices/interface/source")
    if interface is None or interface.get("bridge") != profile["network"]["bridge"]:
        raise RuntimeError("libvirt network bridge does not match profile")
    pins = xml.findall("./cputune/vcpupin")
    actual_cpus = set()
    for pin in pins:
        for part in (pin.get("cpuset") or "").split(","):
            bounds = [int(n) for n in part.split("-")]
            actual_cpus.update(range(bounds[0], bounds[-1] + 1) if len(bounds) == 2 else bounds)
    cpu_text = resources.get("cpu_set")
    if cpu_text:
        expected_cpus = set()
        for part in cpu_text.split(","):
            bounds = [int(n) for n in part.split("-")]
            expected_cpus.update(range(bounds[0], bounds[-1] + 1) if len(bounds) == 2 else bounds)
        if actual_cpus != expected_cpus:
            raise RuntimeError("libvirt CPU pin set does not match profile")
    quota = xml.findtext("./cputune/global_quota")
    if quota is None or int(quota) != resources["vcpus"] * 100000:
        raise RuntimeError("libvirt CPU quota does not match profile")
    state = run(["virsh", "--readonly", "-c", "qemu:///system", "domstate", profile["name"]])
    if state.returncode:
        raise RuntimeError("unable to read VM state")
    return state.stdout.strip(), result.stdout


def validate_network_helper(root: Path, profile: dict) -> None:
    helper = root / "scripts/lib" / f"{profile['name']}_network.py"
    if not helper.is_file() or helper.is_symlink():
        raise RuntimeError("network helper is missing or symlinked")
    values = {}
    tree = ast.parse(helper.read_text(encoding="utf-8"), filename=str(helper))
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in {"BRIDGE", "VM_NAME", "BRIDGE_IP", "GUEST_IP", "PUBLISH_IP", "CLIENT_NET", "PUBLISHED", "PUBLISHED_SERVICES"}:
                try:
                    values[node.targets[0].id] = ast.literal_eval(node.value)
                except (ValueError, SyntaxError) as exc:
                    raise RuntimeError("network helper constants are not literal values") from exc
    expected = {"BRIDGE": profile["network"]["bridge"], "VM_NAME": profile["name"],
                "BRIDGE_IP": profile["network"]["bridge_ip"], "GUEST_IP": profile["network"]["guest_ip"],
                "PUBLISH_IP": profile["network"]["publish_ip"], "CLIENT_NET": profile["network"]["client_cidr"],
                "PUBLISHED": ((profile["network"]["rdp_port"], 3389), (profile["network"]["winrm_port"], 5986))}
    # Always derive the canonical value.  A profile with no extras must reject
    # a helper that quietly publishes services, while an old helper remains
    # compatible only when the profile itself has no extras.
    profile_spec = importlib.util.spec_from_file_location("virtops_profile_services", HERE / "profile.py")
    profile_module = importlib.util.module_from_spec(profile_spec)
    assert profile_spec and profile_spec.loader
    profile_spec.loader.exec_module(profile_module)
    expected_services = tuple(profile_module.normalize_services(profile["network"]))
    expected["PUBLISHED_SERVICES"] = expected_services
    for key, value in expected.items():
        if key == "PUBLISHED_SERVICES" and key not in values and not expected_services:
            # Rendered helpers from before optional services were introduced
            # have no constant at all; preserve that empty-profile upgrade
            # compatibility without allowing a non-empty omission.
            continue
        if values.get(key) != value:
            raise RuntimeError(f"network helper constant does not match profile: {key}")


def validate_instance_config(root: Path, profile: dict) -> None:
    instance = root / "instance.json"
    if not instance.exists():
        return
    saved = read_json(instance)
    for key in ("name", "uuid", "root", "host_root", "resources", "network", "windows"):
        if key == "windows":
            if saved.get(key, {}).get("admin_user") != profile["windows"]["admin_user"]:
                raise RuntimeError("instance.json administrator differs from profile")
            continue
        if saved.get(key) != profile.get(key):
            raise RuntimeError(f"instance.json immutable field differs from profile: {key}")


def registry_module():
    registry = RUNTIME / "instance_registry.py"
    if not registry.is_file():
        raise RuntimeError("instance registry helper is unavailable")
    spec = importlib.util.spec_from_file_location("virtops_registry", registry)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def check_registry(profile: dict, root: Path) -> None:
    module = registry_module()
    host_root = Path(profile.get("host_root") or root)
    path = host_root / "registry" / f"{profile['name']}.json"
    if path.is_symlink():
        raise RuntimeError("registry entry is a symlink")
    if not path.exists():
        return
    existing = read_json(path)
    expected = module._metadata(profile)
    for key in ("name", "uuid", "root", "bridge", "subnet", "publish_ip", "rdp_port", "winrm_port"):
        if existing.get(key) != expected.get(key):
            raise RuntimeError(f"registry entry does not match profile: {key}")
    expected_services = expected.get("published_services", [])
    if "published_services" not in existing and not expected_services:
        # Older registry rows predate optional services and are valid only for
        # a profile that also declares no extras.
        return
    if existing.get("published_services") != expected_services:
        raise RuntimeError("registry entry does not match profile: published_services")


def register_catalog(profile: dict) -> None:
    module = registry_module()
    root = Path(profile["root"])
    host_root = Path(profile.get("host_root") or root)
    module.reserve(profile, allow_legacy=(root == host_root))


def ensure_owner(root: Path, profile: dict) -> None:
    private = root / "vol/state/private"
    owner = private / "owner.json"
    if owner.exists():
        check_owner_state(root, profile)
        return
    credentials = read_json(private / "credentials.json")
    user = profile["windows"]["admin_user"]
    if user not in credentials or not isinstance(credentials[user], str):
        raise RuntimeError("credentials.json does not contain the profile administrator")
    payload = {"schema_version": 1, "name": profile["name"], "uuid": profile["uuid"],
               "admin_user": user, "password": credentials[user], "data_policy": "preserve"}
    temporary = owner.with_name("." + owner.name + ".upgrade")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        if os.name == "posix": os.chown(temporary, 0, 0)
        os.replace(temporary, owner)
    finally:
        Path(temporary).unlink(missing_ok=True)


def ensure_instance(root: Path, profile: dict, store: dict) -> None:
    instance = root / "instance.json"
    if instance.exists():
        instance.chmod(0o600)
        return
    payload = dict(profile)
    payload["store_identity"] = store
    temporary = instance.with_name("." + instance.name + ".upgrade")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        if os.name == "posix": os.chown(temporary, 0, 0)
        os.replace(temporary, instance)
    finally:
        Path(temporary).unlink(missing_ok=True)


def installed_hashes(root: Path, profile: dict) -> dict:
    path = root / 'vol/state/private/runtime-files.json'
    if not path.exists():
        return {}
    st = path.stat()
    if path.is_symlink() or st.st_uid != 0 or st.st_mode & 0o077:
        raise RuntimeError('unsafe installed runtime manifest')
    data = read_json(path)
    if data.get('name') != profile['name'] or data.get('uuid') != profile['uuid']:
        raise RuntimeError('installed runtime manifest identity mismatch')
    return data['sha256']


def save_installed_hashes(root: Path, profile: dict, rendered: dict) -> None:
    payload = {'schema_version': 1, 'name': profile['name'], 'uuid': profile['uuid'],
               'sha256': {path.as_posix(): hashlib.sha256(content).hexdigest() for path, content in rendered.items()}}
    atomic_copy((json.dumps(payload, sort_keys=True)+'\n').encode(), root/'vol/state/private/runtime-files.json', mode=0o600)


def post_apply_verify(root: Path, profile: dict) -> None:
    network = root / "scripts/lib" / f"{profile['name']}_network.py"
    if network.is_file():
        result = run(["python3", str(network), "verify", "--backend", "iptables-nft"])
        if result.returncode:
            raise RuntimeError("network guard read-only verification failed")
    controller = root / "scripts/lib/vmctl.py"
    result = run(["python3", str(controller), "status"])
    if result.returncode:
        raise RuntimeError("vmctl read-only status verification failed")


def restart_collector_if_needed(profile: dict, content_changed: bool) -> bool:
    """Reload a changed collector only when its existing unit is active."""
    if not content_changed:
        return False
    service = f"{profile['name']}-log-collector.service"
    active = run(["systemctl", "is-active", "--quiet", service])
    if active.returncode == 3:
        return False
    if active.returncode:
        raise RuntimeError(f"unable to determine log collector service state: {service}")
    restarted = run(["systemctl", "restart", service])
    if restarted.returncode:
        raise RuntimeError(f"log collector service restart failed: {service}")
    active_after = run(["systemctl", "is-active", "--quiet", service])
    if active_after.returncode:
        raise RuntimeError(f"log collector service is not active after restart: {service}")
    return True


@contextlib.contextmanager
def lifecycle_lock(name: str, lock_dir: Path = Path("/run/lock")):
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"{name}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def atomic_copy(source: bytes, target: Path, *, mode=None) -> None:
    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if target.is_symlink():
        raise RuntimeError(f"managed target is a symlink: {target}")
    if target.exists() and not target.is_file():
        raise RuntimeError(f"managed target is not a regular file: {target}")
    mode = mode if mode is not None else (0o755 if target.name in BASIC or target.suffix in {".py", ".sh"} else 0o644)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(source)
            output.flush(); os.fsync(output.fileno())
        os.chmod(temporary, mode)
        if os.name == "posix":
            os.chown(temporary, 0, 0)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def inspect(profile_path: Path, *, apply=False, lock_dir=Path("/run/lock")) -> dict:
    if os.name == "posix" and os.geteuid() != 0:
        raise RuntimeError("runtime upgrade requires Linux root")
    profile = complete_cpu_profile(load_profile(profile_path))
    root = Path(profile["root"])
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("profile root must be an existing non-symlink directory")
    secure_ancestors(root)
    manifest, _sources = validate_source()
    store = store_identity(root, profile)
    rendered = render_runtime(profile, store["uuid"], store["inode"])
    state, _xml = domain_facts(profile, root)
    validate_network_helper(root, profile)
    validate_instance_config(root, profile)
    check_owner_state(root, profile)
    check_registry(profile, root)
    installed = installed_hashes(root, profile)
    destination = {relative: root / relative for relative in rendered}
    mismatches = []
    for relative, target in destination.items():
        if target.parent.exists():
            secure_ancestors(target.parent)
        if target.exists() and (target.is_symlink() or not target.is_file()):
            raise RuntimeError(f"managed destination is unsafe: {target}")
        source_name = relative.name
        allowed = set(manifest.get(source_name, set()))
        allowed.add(hashlib.sha256(rendered[relative]).hexdigest())
        if relative.as_posix() in installed:
            allowed.add(installed[relative.as_posix()])
        if target.exists() and sha256(target) not in allowed:
            mismatches.append(str(relative))
    if mismatches:
        raise RuntimeError("external edits detected: " + ", ".join(sorted(mismatches)))
    network_relative = Path('scripts/lib') / f"{profile['name']}_network.py"
    network_update_required = (network_relative in rendered and
                               destination[network_relative].read_bytes() != rendered[network_relative])
    if not apply:
        return {"status": "ready", "mode": "check", "vm_state": state, "store": store,
                "files": len(rendered), "apply_required": True,
                "network_update_required": network_update_required,
                "restarted": False, "network_changed": False, "collector_restarted": False}
    if network_update_required:
        raise RuntimeError('network runtime update requires the guarded publish_services.py transaction; '
                           'ordinary runtime upgrade cannot change the guard expectation without its rules')
    if state not in {"running", "paused"}:
        raise RuntimeError("apply requires the existing VM to remain running or paused")
    with lifecycle_lock(profile["name"], lock_dir):
        # Re-run all checks while holding the instance lifecycle lock.
        locked_check = inspect(profile_path, apply=False, lock_dir=lock_dir)
        if locked_check["network_update_required"]:
            raise RuntimeError('network runtime update requires the guarded publish_services.py transaction; '
                               'network runtime changed before the lifecycle lock was acquired')
        collector_relative = Path("scripts/lib") / f"{profile['name']}_log_collector.py"
        collector_target = root / collector_relative
        collector_content_changed = False
        if collector_relative in rendered:
            desired_collector_hash = hashlib.sha256(rendered[collector_relative]).hexdigest()
            installed_collector_hash = installed.get(collector_relative.as_posix())
            collector_content_changed = (
                not collector_target.exists() or collector_target.is_symlink() or
                not collector_target.is_file() or collector_target.read_bytes() != rendered[collector_relative] or
                (installed_collector_hash is not None and installed_collector_hash != desired_collector_hash)
            )
        for relative, source in rendered.items():
            atomic_copy(source, destination[relative])
        ensure_instance(root, profile, store)
        ensure_owner(root, profile)
        register_catalog(profile)
        after, _ = domain_facts(profile, root)
        if after != state:
            raise RuntimeError("VM state changed during runtime replacement")
        post_apply_verify(root, profile)
        collector_restarted = restart_collector_if_needed(profile, collector_content_changed)
        save_installed_hashes(root, profile, rendered)
    return {"status": "applied", "mode": "apply", "vm_state": state, "store": store,
            "files": len(rendered), "restarted": False, "network_changed": False,
            "collector_restarted": collector_restarted}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="perform the checked atomic replacement")
    args = parser.parse_args()
    try:
        print(json.dumps(inspect(args.profile, apply=args.apply), ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, ET.ParseError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
