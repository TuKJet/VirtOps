"""Root-only, namespace-contained online transaction acceptance test.

Run with ``sudo unshare --net --mount --fork python3
tests/linux_service_transaction.py``.  The fixture is the disposable bridge
and guest HTTP server from linux_published_services; no libvirt VM is used.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys_path = [str(ROOT / "tests"), str(ROOT / "skill/windows-vm-sandbox/scripts")]
import sys
sys.path[:0] = sys_path

from linux_published_services import PublishedServiceLinuxTests  # noqa: E402
from test_profile import profile_for  # noqa: E402

SCRIPT = ROOT / "skill/windows-vm-sandbox/scripts/publish_services.py"
spec = importlib.util.spec_from_file_location("publish_services_linux", SCRIPT)
publish = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(publish)


def _run(args):
    result = subprocess.run(args, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result


@unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0, "requires Linux root")
class LinuxServiceTransactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.stat("/proc/self/ns/net").st_ino == os.stat("/proc/1/ns/net").st_ino:
            raise unittest.SkipTest("must run inside an outer network namespace")
        PublishedServiceLinuxTests.setUpClass()
        cls.lab = PublishedServiceLinuxTests
        cls.work = ROOT / ".validation" / "linux-service-transaction"
        if cls.work.exists():
            shutil.rmtree(cls.work)
        cls.work.mkdir(parents=True)
        os.chmod(cls.work, 0o700)
        cls.instance_root = cls.work / "alpha"
        (cls.instance_root / "scripts/lib").mkdir(parents=True)
        (cls.instance_root / "vol/state/private").mkdir(parents=True)
        (cls.work / "registry").mkdir()
        cls.profile = profile_for("alpha", 1)
        cls.profile.update({"root": str(cls.instance_root), "host_root": str(cls.work)})
        cls.profile["network"].update({
            "bridge": "winbr01", "subnet": "172.30.61.0/30", "bridge_ip": "172.30.61.1",
            "guest_ip": "172.30.61.2", "publish_ip": "192.0.2.5", "client_cidr": "198.51.100.0/24",
            "published_services": [{"id": "cad-mcp", "publish_port": 18000, "guest_port": 8000,
                                     "access": "host-loopback", "allowed_sources": ["127.0.0.1/32"]}],
        })
        cls.profile_path = cls.instance_root / "profile.json"
        cls.profile_path.write_text(json.dumps(cls.profile), encoding="utf-8")
        cls.services_path = cls.work / "services.json"
        cls.services_path.write_text(json.dumps([{"id": "cad-mcp", "publish_port": 18002,
                                                   "guest_port": 8000, "access": "host-loopback",
                                                   "allowed_sources": ["127.0.0.1/32"]}]), encoding="utf-8")
        from render import runtime_files
        old_render = runtime_files(cls.profile, "22222222-2222-4222-8222-000000000001", 42)
        for relative, text in old_render.items():
            if relative.startswith("scripts/lib/") and relative.rsplit("/", 1)[1] in {
                "alpha_network.py", "maintenance.py", "service_policy.py", "instance_registry.py",
            }:
                target = cls.instance_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
                os.chmod(target, 0o700 if target.suffix == ".py" else 0o600)
        (cls.instance_root / "instance.json").write_text(json.dumps(cls.profile), encoding="utf-8")
        registry = cls.work / "registry/alpha.json"
        registry.write_text(json.dumps(publish._registry_module()._metadata(cls.profile)), encoding="utf-8")
        os.chmod(registry, 0o600)
        manifest = {"name": "alpha", "uuid": cls.profile["uuid"], "sha256": {}}
        for relative, text in old_render.items():
            manifest["sha256"][relative] = hashlib.sha256(text.encode()).hexdigest()
        (cls.instance_root / "vol/state/private/runtime-files.json").write_text(json.dumps(manifest), encoding="utf-8")
        cls.beta_filter, cls.beta_nat = "ALPHA_TEST_BETA_FILTER", "ALPHA_TEST_BETA_NAT"
        _run(["iptables", "-N", cls.beta_filter])
        _run(["iptables", "-A", cls.beta_filter, "-s", "198.51.100.0/24", "-j", "RETURN"])
        _run(["iptables", "-t", "nat", "-N", cls.beta_nat])
        _run(["iptables", "-t", "nat", "-A", cls.beta_nat, "-p", "tcp", "--dport", "19000", "-j", "RETURN"])
        cls.beta_before = cls._beta_rules()

    @classmethod
    def tearDownClass(cls):
        for command in (("iptables", "-F", getattr(cls, "beta_filter", "ALPHA_TEST_BETA_FILTER")),
                        ("iptables", "-X", getattr(cls, "beta_filter", "ALPHA_TEST_BETA_FILTER")),
                        ("iptables", "-t", "nat", "-F", getattr(cls, "beta_nat", "ALPHA_TEST_BETA_NAT")),
                        ("iptables", "-t", "nat", "-X", getattr(cls, "beta_nat", "ALPHA_TEST_BETA_NAT"))):
            subprocess.run(command, check=False, capture_output=True)
        if getattr(cls, "work", None) and cls.work.exists():
            shutil.rmtree(cls.work)
        PublishedServiceLinuxTests.tearDownClass()

    @classmethod
    def _beta_rules(cls):
        return (
            [x for x in _run(["iptables-save", "-t", "filter"]).stdout.splitlines() if cls.beta_filter in x],
            [x for x in _run(["iptables-save", "-t", "nat"]).stdout.splitlines() if cls.beta_nat in x],
        )

    def _plan(self):
        evidence = {"store": {"uuid": "22222222-2222-4222-8222-000000000001", "inode": 42},
                    "vm_state": "running"}
        lock_dir = self.work / "locks"
        lock_dir.mkdir(exist_ok=True)
        original_lock = publish.upgrade.lifecycle_lock
        def private_lock(name):
            return original_lock(name, lock_dir=lock_dir)
        inspect = patch.object(publish.upgrade, "inspect", return_value=evidence)
        domain = patch.object(publish.upgrade, "domain_facts", return_value=("running", "stable-xml"))
        lock = patch.object(publish.upgrade, "lifecycle_lock", side_effect=private_lock)
        return inspect, domain, lock

    def _get(self, port):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(f"http://127.0.0.1:{port}/", timeout=3).status

    def test_apply_switches_port_and_invalidates_old_session_then_manual_rollback(self):
        inspect, domain, lock = self._plan()
        with inspect, domain, lock:
            plan = publish.prepare(self.profile_path, self.services_path)
            connection = __import__("http.client").client.HTTPConnection("127.0.0.1", 18000, timeout=3)
            connection.connect()
            self.assertEqual(self._get(18000), 200)
            result = publish.apply_plan(plan)
            self.assertEqual(result["status"], "applied")
            with self.assertRaises((urllib.error.URLError, OSError, TimeoutError)):
                connection.request("GET", "/"); connection.getresponse()
            connection.close()
            self.assertEqual(self._get(18002), 200)
            publish.rollback(Path(result["backup"]), self.profile_path)
            self.assertEqual(self._get(18000), 200)
            with self.assertRaises((urllib.error.URLError, OSError, TimeoutError)):
                self._get(18002)
        self.assertEqual(self._beta_rules(), self.beta_before)

    def test_write_failure_rolls_back_files_and_kernel_rules(self):
        inspect, domain, lock = self._plan()
        with inspect, domain, lock:
            plan = publish.prepare(self.profile_path, self.services_path)
            original = publish._atomic
            failed = {"value": False}
            registry = self.work / "registry/alpha.json"
            def fail_once(data, target, mode):
                if target in {self.instance_root / "instance.json", registry} and not failed["value"]:
                    failed["value"] = True
                    raise RuntimeError("injected file write failure")
                return original(data, target, mode)
            with patch.object(publish, "_atomic", side_effect=fail_once):
                with self.assertRaisesRegex(RuntimeError, "restored"):
                    publish.apply_plan(plan)
            self.assertEqual(self._get(18000), 200)
            with self.assertRaises((urllib.error.URLError, OSError, TimeoutError)):
                self._get(18002)
        self.assertEqual(self._beta_rules(), self.beta_before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
