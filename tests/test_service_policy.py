import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


policy = load(ROOT / "skill/windows-vm-sandbox/assets/runtime/service_policy.py", "service_policy_test")
registry = load(ROOT / "skill/windows-vm-sandbox/assets/runtime/instance_registry.py", "instance_registry_test")
destroy = load(ROOT / "skill/windows-vm-sandbox/scripts/destroy.py", "destroy_service_policy_test")


class ServicePolicyTests(unittest.TestCase):
    def setUp(self):
        self.network = {
            "subnet": "10.20.0.0/30", "guest_ip": "10.20.0.2",
            "publish_ip": "192.0.2.11", "client_cidr": "198.51.100.0/24",
            "rdp_port": 23001, "winrm_port": 24001,
        }

    def test_missing_or_empty_services_preserve_legacy_minimal_network(self):
        minimal = {"published_services": []}
        self.assertEqual(policy.normalize(minimal), [])
        self.assertEqual(policy.normalize({}), [])

    def test_loopback_defaults_are_canonical(self):
        self.network["published_services"] = [{"id": "cad-mcp", "publish_port": 18000, "guest_port": 8000}]
        self.assertEqual(policy.normalize(self.network), [{
            "id": "cad-mcp", "protocol": "tcp", "publish_ip": "127.0.0.1",
            "publish_port": 18000, "guest_port": 8000,
            "allowed_sources": ["127.0.0.1/32"], "access": "host-loopback",
            "snat_to_bridge": True, "allow_during_maintenance": False,
        }])

    def test_network_service_must_be_bounded_and_target_current_publish_ip(self):
        self.network["published_services"] = [{"id": "mcp", "access": "network",
            "publish_ip": "192.0.2.10", "publish_port": 18000, "guest_port": 8000,
            "allowed_sources": ["0.0.0.0/0"]}]
        with self.assertRaises(ValueError):
            policy.normalize(self.network)

    def test_two_extra_services_cannot_share_published_port(self):
        self.network["published_services"] = [
            {"id": "mcp-one", "publish_port": 18000, "guest_port": 8000},
            {"id": "mcp-two", "publish_port": 18000, "guest_port": 8001},
        ]
        with self.assertRaisesRegex(ValueError, "conflicts"):
            policy.normalize(self.network)

    def test_registry_metadata_contains_services_and_legacy_rows_remain_valid(self):
        data = {
            "name": "alpha-vm", "uuid": "11111111-1111-4111-8111-111111111111", "root": "/srv/alpha",
            "network": {**self.network, "bridge": "bralpha", "published_services": [{"id": "mcp", "publish_port": 18000, "guest_port": 8000}]},
            "windows": {"admin_user": "vmadmin"}, "resources": {},
        }
        item = registry._metadata(data)
        self.assertEqual(item["published_services"][0]["id"], "mcp")
        legacy = dict(item); legacy.pop("published_services")
        registry._valid_metadata(legacy)

    def test_registry_rejects_noncanonical_or_dangerous_services(self):
        data = {
            "name": "alpha-vm", "uuid": "11111111-1111-4111-8111-111111111111", "root": "/srv/alpha",
            "network": {**self.network, "bridge": "bralpha", "published_services": [{
                "id": "mcp", "publish_port": 18000, "guest_port": 8000,
            }]},
            "windows": {"admin_user": "vmadmin"}, "resources": {},
        }
        canonical = registry._metadata(data)
        for replacement in (None, "18000", True, 65536):
            broken = dict(canonical)
            broken["published_services"] = [dict(canonical["published_services"][0], publish_port=replacement)]
            with self.assertRaises(ValueError):
                registry._valid_metadata(broken)
        cases = [
            [{"id": "mcp", "publish_ip": "127.0.0.1", "publish_port": 18000, "guest_port": 8000,
              "allowed_sources": ["127.0.0.1/32"], "access": "host-loopback", "protocol": "tcp",
              "snat_to_bridge": True, "allow_during_maintenance": False},
             {"id": "mcp", "publish_ip": "127.0.0.1", "publish_port": 18001, "guest_port": 8001,
              "allowed_sources": ["127.0.0.1/32"], "access": "host-loopback", "protocol": "tcp",
              "snat_to_bridge": True, "allow_during_maintenance": False}],
            [dict(canonical["published_services"][0], guest_port=8001),
             dict(canonical["published_services"][0], id="other", guest_port=8002)],
            [dict(canonical["published_services"][0], allowed_sources=["0.0.0.0/0"])],
            [dict(canonical["published_services"][0], guest_ip="10.20.0.2")],
        ]
        for services in cases:
            broken = dict(canonical, published_services=services)
            with self.assertRaises(ValueError):
                registry._valid_metadata(broken)

    def test_registry_rejects_extra_port_collision_across_vms(self):
        first = {
            "name": "alpha-vm", "uuid": "11111111-1111-4111-8111-111111111111", "root": "/srv/alpha",
            "network": {**self.network, "bridge": "bralpha", "published_services": [{"id": "mcp", "publish_port": 18000, "guest_port": 8000}]},
            "windows": {"admin_user": "vmadmin"}, "resources": {},
        }
        second = {
            **first, "name": "beta-vm", "uuid": "22222222-2222-4222-8222-222222222222", "root": "/srv/beta",
            "network": {**self.network, "bridge": "brbeta", "subnet": "10.21.0.0/30", "bridge_ip": "10.21.0.1", "guest_ip": "10.21.0.2",
                         "published_services": [{"id": "other", "publish_port": 18000, "guest_port": 8001}]},
        }
        reason = registry._conflict(registry._metadata(second), registry._metadata(first))
        self.assertIn("published port 18000", reason)

    def test_destroy_rejects_changed_services_before_mutation(self):
        base = Path(tempfile.mkdtemp(dir=ROOT))
        self.addCleanup(lambda: shutil.rmtree(base, ignore_errors=True))
        host = base / "host"; root = host / "instances" / "alpha-vm"
        (root / "vol").mkdir(parents=True)
        profile = {
            "name": "alpha-vm", "uuid": "11111111-1111-4111-8111-111111111111",
            "root": str(root), "host_root": str(host), "network": {**self.network, "bridge": "bralpha",
                "published_services": [{"id": "mcp", "publish_port": 18000, "guest_port": 8000}]},
        }
        actual = dict(profile); actual["network"] = dict(profile["network"])
        actual["network"]["published_services"] = []
        actual["windows"] = {"admin_user": "vmadmin"}; actual["resources"] = {}
        (root / "instance.json").write_text(json.dumps(actual), encoding="utf-8")
        (host / "registry").mkdir(parents=True)
        registry_row = registry._metadata(actual)
        (host / "registry" / "alpha-vm.json").write_text(json.dumps(registry_row), encoding="utf-8")
        before = sorted(str(p) for p in host.rglob("*"))
        with self.assertRaisesRegex(destroy.DestroyError, "published service identity"):
            destroy.validate_identity(profile, {"domain_xml": None})
        self.assertEqual(sorted(str(p) for p in host.rglob("*")), before)


if __name__ == "__main__":
    unittest.main()
