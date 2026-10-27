import copy
import contextlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "skill/windows-vm-sandbox/scripts/publish_services.py"
spec = importlib.util.spec_from_file_location("publish_services", SCRIPT)
publish = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(publish)


def profile(root):
    return {
        "schema_version": 1, "name": "winvm01", "uuid": "u",
        "root": str(root), "host_root": str(root),
        "network": {"bridge": "winbr01", "subnet": "172.30.61.0/30",
                     "bridge_ip": "172.30.61.1", "guest_ip": "172.30.61.2",
                     "publish_ip": "192.168.20.5", "client_cidr": "192.168.20.0/24",
                     "rdp_port": 13389, "winrm_port": 15986},
        "resources": {"memory_gib": 64, "host_limit_gib": 72, "vcpus": 96,
                       "storage_gib": 150, "cpu_set": "0-95"},
        "windows": {"admin_user": "kaijie", "computer_name": "WINVM01"},
        "credentials": {"mode": "existing"},
        "limits": {"root_free_gib": 1, "start_free_gib": 136,
                    "resume_free_gib": 1, "watch_free_gib": 1},
    }


class PublishServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.temp.name)
        (self.root / "scripts/lib").mkdir(parents=True)
        (self.root / "vol/state/private").mkdir(parents=True)
        (self.root / "registry").mkdir()
        self.old = profile(self.root)
        self.profile_path = self.root / "profile.json"
        self.profile_path.write_text(json.dumps(self.old), encoding="utf-8")
        self.instance = self.root / "instance.json"
        self.instance.write_text(json.dumps(self.old), encoding="utf-8")
        self.registry = self.root / "registry/winvm01.json"
        self.registry.write_text(json.dumps({"name": "winvm01"}), encoding="utf-8")
        self.helper = self.root / "scripts/lib/winvm01_network.py"
        self.helper.write_bytes(b"old-network")
        self.services = self.root / "services.json"
        self.services.write_text(json.dumps([{
            "id": "cad-mcp", "protocol": "tcp", "publish_ip": "127.0.0.1",
            "publish_port": 18000, "guest_port": 8000,
            "allowed_sources": ["127.0.0.1/32"], "access": "host-loopback",
            "snat_to_bridge": True, "allow_during_maintenance": False,
        }]), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def plan(self):
        desired = copy.deepcopy(self.old)
        desired["network"]["published_services"] = json.loads(self.services.read_text())
        new_helper = b"new-network"
        rendered_old = {Path("scripts/lib/winvm01_network.py"): b"old-network"}
        rendered_new = {Path("scripts/lib/winvm01_network.py"): new_helper}
        evidence = {"store": {"uuid": "store", "inode": 1}, "vm_state": "running"}
        return desired, rendered_old, rendered_new, evidence

    def patch_prepare(self):
        desired, old_render, new_render, evidence = self.plan()
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(publish.profile, "load", return_value=self.old))
        stack.enter_context(patch.object(publish.profile, "validate"))
        stack.enter_context(patch.object(publish.upgrade, "inspect", return_value=evidence))
        stack.enter_context(patch.object(publish.upgrade, "render_runtime",
                                         side_effect=[old_render, new_render]))
        return stack

    def test_prepare_contains_only_service_and_generated_runtime_changes(self):
        with self.patch_prepare():
            with patch.object(publish, "_policy", return_value=[{"id": "cad-mcp", "publish_port": 18000}]):
                plan = publish.prepare(self.profile_path, self.services)
        self.assertIn(self.helper, plan["changes"])
        self.assertIn(self.instance, plan["changes"])
        self.assertIn(self.profile_path, plan["changes"])
        self.assertEqual(plan["old"]["network"].get("published_services"), None)

    def test_default_main_is_read_only(self):
        with self.patch_prepare(), patch.object(publish, "_policy", return_value=[]):
            # Exercise the pure prepare path: no target bytes are changed.
            before = self.profile_path.read_bytes(), self.instance.read_bytes(), self.helper.read_bytes()
            publish.prepare(self.profile_path, self.services)
            self.assertEqual(before, (self.profile_path.read_bytes(), self.instance.read_bytes(), self.helper.read_bytes()))

    def test_scope_rejects_change_to_immutable_network_field(self):
        def mutate(value, new=False):
            value["network"]["guest_ip"] = "172.30.61.3"
        with self.patch_prepare(), patch.object(publish.profile, "validate", side_effect=mutate):
            with self.assertRaisesRegex(RuntimeError, "network field"):
                publish.prepare(self.profile_path, self.services)

    def test_failed_verify_restores_all_changed_files(self):
        with self.patch_prepare(), patch.object(publish, "_policy", return_value=[]):
            plan = publish.prepare(self.profile_path, self.services)
        responses = [Mock(returncode=0, stdout="", stderr=""), Mock(returncode=1, stdout="", stderr="bad"), Mock(returncode=0, stdout="", stderr="")]
        with patch.object(publish.upgrade, "lifecycle_lock", return_value=contextlib.nullcontext()), \
             patch.object(publish.upgrade, "run", side_effect=responses), \
             patch.object(publish, "_transaction_payloads", return_value={
                 "old_filter": "*filter\nCOMMIT\n", "old_nat": "*nat\nCOMMIT\n",
                 "new_filter_gated": "*filter\nCOMMIT\n", "new_nat_gated": "*nat\nCOMMIT\n",
                 "new_filter": "*filter\nCOMMIT\n", "new_nat": "*nat\nCOMMIT\n",
             }), \
             patch.object(publish, "_restore_payload"):
            with self.assertRaisesRegex(RuntimeError, "restored"):
                publish.apply_plan(plan)
        self.assertEqual(self.helper.read_bytes(), b"old-network")
        self.assertEqual(json.loads(self.instance.read_text()), self.old)

    def test_backup_restore_is_scoped_to_recorded_files(self):
        with self.patch_prepare(), patch.object(publish, "_policy", return_value=[]):
            plan = publish.prepare(self.profile_path, self.services)
        backup = publish._backup(plan)
        unrelated = self.root / "unrelated.txt"
        unrelated.write_text("keep", encoding="utf-8")
        publish._atomic(b"changed", self.helper, 0o600)
        publish._restore(backup)
        self.assertEqual(self.helper.read_bytes(), b"old-network")
        self.assertEqual(unrelated.read_text(), "keep")

    def test_online_transaction_installs_gate_then_stable_under_one_lock(self):
        with self.patch_prepare(), patch.object(publish, "_policy", return_value=[]):
            plan = publish.prepare(self.profile_path, self.services)
        payloads = {key: f"{key}\n" for key in (
            "old_filter", "old_nat", "new_filter_gated", "new_nat_gated", "new_filter", "new_nat")}
        calls = []
        failed = {"value": False}
        lock = Mock()
        lock.__enter__ = Mock(side_effect=lambda: calls.append("lock-enter") or lock)
        lock.__exit__ = Mock(side_effect=lambda *args: calls.append("lock-exit") or False)
        with patch.object(publish, "_transaction_payloads", return_value=payloads), \
             patch.object(publish.upgrade, "lifecycle_lock", return_value=lock), \
             patch.object(publish.upgrade, "run", return_value=Mock(returncode=0)), \
             patch.object(publish, "_restore_payload", side_effect=lambda value: calls.append(value)):
            result = publish.apply_plan(plan)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(calls.count("lock-enter"), 1)
        self.assertEqual(calls.count("lock-exit"), 1)
        self.assertLess(calls.index("new_filter_gated\n"), calls.index("new_filter\n"))
        self.assertLess(calls.index("new_nat_gated\n"), calls.index("new_nat\n"))

    def test_restore_failure_keeps_same_lock_and_restores_old_rules(self):
        with self.patch_prepare(), patch.object(publish, "_policy", return_value=[]):
            plan = publish.prepare(self.profile_path, self.services)
        payloads = {key: key for key in (
            "old_filter", "old_nat", "new_filter_gated", "new_nat_gated", "new_filter", "new_nat")}
        calls = []
        failed = {"value": False}
        lock = Mock()
        lock.__enter__ = Mock(side_effect=lambda: calls.append("enter") or lock)
        lock.__exit__ = Mock(side_effect=lambda *args: calls.append("exit") or False)
        def fake_restore(value):
            calls.append(value)
            if value == "new_filter_gated" and not failed["value"]:
                failed["value"] = True
                raise RuntimeError("injected firewall failure")
        with patch.object(publish, "_transaction_payloads", return_value=payloads), \
             patch.object(publish.upgrade, "lifecycle_lock", return_value=lock), \
             patch.object(publish.upgrade, "run", return_value=Mock(returncode=0)), \
             patch.object(publish, "_restore_payload", side_effect=fake_restore):
            with self.assertRaisesRegex(RuntimeError, "restored"):
                publish.apply_plan(plan)
        self.assertEqual(calls.count("enter"), 1)
        self.assertIn("*filter\nCOMMIT\n", calls)

    def test_cli_add_service_auto_allocates_first_free_port(self):
        with patch.object(publish.profile, "load", return_value=self.old), \
             patch.object(publish, "_policy", side_effect=lambda network: network.get("published_services", [])), \
             patch.object(publish, "_reserved_ports", return_value={13389, 15986, 18000}):
            services, port = publish._resolve_cli_services(self.profile_path, "cad-mcp", 8000, None, None)
        self.assertEqual(port, 18001)
        self.assertEqual(services[0]["publish_port"], 18001)
        self.assertEqual(services[0]["publish_ip"], "127.0.0.1")
        self.assertFalse(services[0]["allow_during_maintenance"])

    def test_cli_add_existing_id_reuses_port_without_auto_allocation(self):
        existing = copy.deepcopy(self.old)
        existing["network"]["published_services"] = [{
            "id": "cad-mcp", "protocol": "tcp", "publish_ip": "127.0.0.1",
            "publish_port": 18042, "guest_port": 8000,
            "allowed_sources": ["127.0.0.1/32"], "access": "host-loopback",
            "snat_to_bridge": True, "allow_during_maintenance": False,
        }]
        with patch.object(publish.profile, "load", return_value=existing), \
             patch.object(publish, "_policy", side_effect=lambda network: network.get("published_services", [])):
            services, port = publish._resolve_cli_services(self.profile_path, "cad-mcp", 9000, None, None)
        self.assertIsNone(port)
        self.assertEqual(services[0]["publish_port"], 18042)
        self.assertEqual(services[0]["guest_port"], 9000)

    def test_restore_rule_quote_is_iptables_compatible(self):
        line = publish._quote(["-A", "ALPHA_OUTPUT", "-j", "LOG", "--log-prefix", "ALPHA DROP output "])
        self.assertIn('"ALPHA DROP output "', line)
        self.assertNotIn("'", line)
        self.assertNotIn("\x00", line)

    def test_rule_restore_rejects_shared_operations_and_foreign_prefix_chains(self):
        path = self.root / 'snapshot.rules'
        for unsafe in [
            '*filter\n:WINVM01_OUTPUT - [0:0]\n-P OUTPUT ACCEPT\nCOMMIT\n',
            '*filter\n:WINVM01_OTHER_OUTPUT - [0:0]\n-F WINVM01_OTHER_OUTPUT\nCOMMIT\n',
            '*filter\n:WINVM01_OUTPUT - [0:0]\n-A WINVM01_OUTPUT -j DOCKER\nCOMMIT\n',
        ]:
            path.write_text(unsafe, encoding='utf-8')
            with self.subTest(unsafe=unsafe), self.assertRaises(RuntimeError):
                publish._validate_rule_file(path, 'winvm01', 'filter')

    def test_ss_parser_uses_local_endpoint_for_ipv4_and_ipv6(self):
        output = ("LISTEN 0 128 127.0.0.1:18000 0.0.0.0:*\n"
                  "LISTEN 0 128 [::1]:18002 [::]:*\n")
        result = Mock(returncode=0, stdout=output, stderr="")
        with patch.object(publish.os, "name", "posix"), patch.object(publish.shutil, "which", return_value="ss"), \
             patch.object(publish.subprocess, "run", return_value=result):
            self.assertEqual(publish._listening_ports(), {18000, 18002})

    def test_host_ip_json_check_does_not_accept_prefix_match(self):
        self.old["network"]["publish_ip"] = "192.0.2.10"
        result = Mock(returncode=0, stdout=json.dumps([{"addr_info": [{"family": "inet", "local": "192.0.2.1"}]}]), stderr="")
        with patch.object(publish.os, "name", "posix"), patch.object(publish.shutil, "which", side_effect=lambda name: name), \
             patch.object(publish.subprocess, "run", side_effect=[result, Mock(returncode=0, stdout="", stderr="")]):
            with self.assertRaisesRegex(RuntimeError, "not assigned"):
                publish._assert_host_ports(self.old, [])


if __name__ == "__main__":
    unittest.main()
