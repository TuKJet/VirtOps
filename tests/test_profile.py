import copy
import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill/windows-vm-sandbox/scripts"
sys.path.insert(0, str(SCRIPTS))
from profile import validate  # noqa: E402


def profile_for(name="alpha", number=1):
    """Build a complete new-VM profile without touching a host."""
    return {
        "schema_version": 1,
        "gpu": {"mode":"none","pci_devices":[],"dedicated":False},
        "name": name,
        "uuid": f"11111111-1111-4111-8111-{number:012d}",
        "labels": {"owner": "test-user", "purpose": "test"},
        "host_root": "/srv",
        "root": f"/srv/{name}",
        "ssh": {"host": "192.0.2.10", "user": "operator", "port": 22, "sudo": True, "bind_address": None},
        "resources": {"memory_gib": 8, "host_limit_gib": 10, "vcpus": 4, "storage_gib": 64, "cpu_set": "0-3"},
        "network": {
            "bridge": f"br{name}",
            "subnet": f"10.{20 + number}.0.0/30",
            "bridge_ip": f"10.{20 + number}.0.1",
            "guest_ip": f"10.{20 + number}.0.2",
            "publish_ip": f"192.0.2.{100 + number}",
            "client_cidr": "198.51.100.0/24",
            "rdp_port": 23000 + number,
            "winrm_port": 24000 + number,
        },
        "windows": {"computer_name": name.upper(), "admin_user": "vmadmin", "language": "en-US", "image_index": 4},
        "media": {"windows_iso": "/srv/media/windows.iso", "virtio_iso": "/srv/media/virtio.iso"},
        "credentials": {"mode": "generate", "secret_file": None, "local_clixml": None},
        "winrm_thumbprint": None,
        "limits": {"root_free_gib": 20, "start_free_gib": 12, "resume_free_gib": 8, "watch_free_gib": 4},
    }


class ProfileValidationTests(unittest.TestCase):
    def test_base_instance_requires_no_installer_iso_or_index(self):
        data=profile_for();data['root']='/srv/instances/alpha'
        data['image']={'kind':'base','manifest':'/srv/images/win11/manifest.json'}
        data['media']['windows_iso']=None;data['windows']['image_index']=None
        self.assertIs(validate(data,new=True),data)
        data['root']='/srv/alpha'
        with self.assertRaisesRegex(ValueError,'instances/name'):validate(data,new=True)

    def test_new_profile_rejects_missing_required_input(self):
        data = profile_for()
        data["network"]["bridge"] = None
        with self.assertRaisesRegex(ValueError, "network.bridge"):
            validate(data, new=True)

    def test_profile_rejects_secret_values(self):
        data = profile_for()
        data["credentials"]["password"] = "do-not-store"
        with self.assertRaisesRegex(ValueError, "credential paths"):
            validate(data, new=True)

    def test_profile_rejects_overlapping_dedicated_subnet(self):
        data = profile_for()
        data["network"]["client_cidr"] = data["network"]["subnet"]
        with self.assertRaisesRegex(ValueError, "overlap"):
            validate(data, new=True)

    def test_profile_rejects_equal_ports(self):
        data = profile_for()
        data["network"]["winrm_port"] = data["network"]["rdp_port"]
        with self.assertRaisesRegex(ValueError, "must differ"):
            validate(data, new=True)

    def test_profile_accepts_an_optional_mtu_and_rejects_out_of_range(self):
        data = profile_for()
        self.assertIs(validate(data, new=True), data)
        data["network"]["mtu"] = 1360
        self.assertIs(validate(data, new=True), data)
        for bad in (1279, 9001, 1500.0, "1500", True):
            data["network"]["mtu"] = bad
            with self.assertRaisesRegex(ValueError, "network.mtu"):
                validate(data, new=True)

    def test_profile_rejects_zero_uuid(self):
        data = profile_for()
        data["uuid"] = "00000000-0000-0000-0000-000000000000"
        with self.assertRaisesRegex(ValueError, "nonzero UUID"):
            validate(data, new=True)

    def test_profile_requires_host_overhead(self):
        data = profile_for()
        data["resources"]["host_limit_gib"] = data["resources"]["memory_gib"] + 1
        with self.assertRaisesRegex(ValueError, "overhead"):
            validate(data, new=True)

    def test_new_profile_requires_instance_root_below_host_root(self):
        data = profile_for()
        data['root'] = '/other/alpha'
        with self.assertRaisesRegex(ValueError, 'inside host_root'):
            validate(data, new=True)

    def test_new_profile_requires_labels(self):
        data = profile_for()
        data['labels']['owner'] = None
        with self.assertRaisesRegex(ValueError, 'labels.owner'):
            validate(data, new=True)


if __name__ == "__main__":
    unittest.main()
