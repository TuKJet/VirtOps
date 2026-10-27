import sys
import os
import json
import tempfile
from pathlib import Path
import unittest

sys.path[:0] = ["skill/windows-vm-sandbox/scripts", "tests", "skill/windows-vm-sandbox/assets/runtime"]

from profile import validate  # noqa: E402
from render import runtime_files  # noqa: E402
from test_profile import profile_for  # noqa: E402


def rendered_network(service):
    profile = profile_for("alpha", 1)
    profile["network"]["published_services"] = [service]
    validate(profile, new=False)
    source = runtime_files(profile, "22222222-2222-4222-8222-000000000001", 42)[
        "scripts/lib/alpha_network.py"
    ]
    namespace = {"__name__": "alpha_network", "__file__": "/tmp/alpha/scripts/lib/alpha_network.py"}
    exec(compile(source, "alpha_network.py", "exec"), namespace)
    return namespace


class PublishedServiceRuleTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'posix' and os.geteuid() == 0, 'requires root registry ownership')
    def test_public_readonly_registry_recognizes_other_instance_guards(self):
        network = rendered_network({'id': 'mcp', 'publish_port': 18000, 'guest_port': 8000})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            own = root / 'alpha'
            own.mkdir()
            (root / 'registry').mkdir()
            (own / 'instance.json').write_text(json.dumps({'name': 'alpha', 'host_root': str(root)}))
            row = root / 'registry/beta.json'
            row.write_text(json.dumps({'name': 'beta', 'uuid': 'beta-id',
                                       'root': str(root / 'beta'), 'bridge': 'winbr02'}))
            row.chmod(0o644)
            network['__file__'] = str(own / 'scripts/lib/alpha_network.py')
            allowed = network['_catalog_guard_jumps']()[(False, 'filter', 'INPUT')]
            self.assertIn(('-i', 'winbr02', '-j', 'BETA_INPUT'), allowed)
            row.chmod(0o666)
            allowed = network['_catalog_guard_jumps']()[(False, 'filter', 'INPUT')]
            self.assertNotIn(('-i', 'winbr02', '-j', 'BETA_INPUT'), allowed)

    def test_loopback_service_is_output_only_and_snat_bound(self):
        network = rendered_network({
            "id": "cad-mcp", "publish_port": 18000, "guest_port": 8000,
            "access": "host-loopback", "allowed_sources": ["127.0.0.1/32"],
        })
        output = network["filter4_specs"]()["ALPHA_OUTPUT"]
        prerouting = network["nat_specs"]()["ALPHA_DNAT"]
        postrouting = network["nat_specs"]()["ALPHA_SNAT"]
        self.assertTrue(any("18000" in " ".join(rule) and "8000" in " ".join(rule) for rule in output))
        self.assertFalse(any("18000" in " ".join(rule) for rule in prerouting))
        self.assertTrue(any("--ctorigdstport" in rule and "18000" in rule for rule in postrouting))

    def test_network_service_has_source_bounded_prerouting_and_no_loopback_output(self):
        network = rendered_network({
            "id": "cad-mcp", "publish_ip": "192.0.2.101", "publish_port": 18000,
            "guest_port": 8000, "access": "network", "allowed_sources": ["198.51.100.0/24"],
        })
        output = network["filter4_specs"]()["ALPHA_OUTPUT"]
        prerouting = network["nat_specs"]()["ALPHA_DNAT"]
        self.assertFalse(any("18000" in " ".join(rule) for rule in output))
        self.assertTrue(any("198.51.100.0/24" in rule and "18000" in rule for rule in prerouting))

    def test_extra_service_is_not_maintenance_exception_by_default(self):
        network = rendered_network({
            "id": "cad-mcp", "publish_port": 18000, "guest_port": 8000,
            "access": "host-loopback", "allowed_sources": ["127.0.0.1/32"],
        })
        self.assertNotIn((18000, 8000), network["maintenance_gate"]().published)

    def test_established_rules_are_bound_to_current_declared_tuples(self):
        network = rendered_network({
            "id": "cad-mcp", "publish_port": 18000, "guest_port": 8000,
            "access": "host-loopback", "allowed_sources": ["127.0.0.1/32"],
        })
        specs = network["filter4_specs"]()
        output = specs["ALPHA_OUTPUT"]
        replies = specs["ALPHA_INPUT"]
        self.assertFalse(any(
            rule == ["-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"]
            for rules in specs.values() for rule in rules
        ))
        self.assertTrue(any("--ctdir" in rule and "ORIGINAL" in rule and "18000" in rule for rule in output))
        self.assertTrue(any("--ctdir" in rule and "REPLY" in rule and "18000" in rule
                            and "--ctreplsrcport" in rule and "8000" in rule for rule in replies))


if __name__ == "__main__":
    unittest.main()
