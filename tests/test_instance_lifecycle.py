from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill/windows-vm-sandbox/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def profile(root: Path, name: str, ident: str):
    return {
        "schema_version": 1, "name": name, "uuid": ident, "root": str(root), "host_root": str(root.parent.parent),
        "labels": {"owner": "owner", "purpose": "test"}, "template_builder": False,
        "image": {"kind": "base", "manifest": "/srv/images/win11-pro/manifest.json"},
        "resources": {"memory_gib": 8, "host_limit_gib": 10, "vcpus": 4, "storage_gib": 64, "cpu_set": "0-3", "cpu_order": [0, 1, 2, 3], "threads_per_core": 1, "amd": False},
        "limits": {"root_free_gib": 32, "start_free_gib": 12, "resume_free_gib": 16, "watch_free_gib": 16},
        "network": {"bridge": name + "br", "subnet": "172.31.40.0/30", "bridge_ip": "172.31.40.1", "guest_ip": "172.31.40.2", "publish_ip": "192.0.2.10", "client_cidr": "192.0.2.0/24", "rdp_port": 23389, "winrm_port": 25986},
        "windows": {"computer_name": name.upper(), "admin_user": "alice", "language": "zh-CN", "image_index": 3},
        "media": {"windows_iso": None, "virtio_iso": "/srv/virtio.iso", "seed_iso": None},
        "credentials": {"mode": "generate", "secret_file": None, "local_clixml": None}, "gpu": {"mode": "none", "pci_devices": [], "dedicated": False},
    }


class RenderIsolationTests(unittest.TestCase):
    def setUp(self):
        self.render = load(SCRIPTS / "render.py", f"render_{id(self)}")

    def test_base_seed_has_no_installer_wipe_and_domain_boots_disk_without_windows_iso(self):
        with tempfile.TemporaryDirectory() as td:
            p = profile(Path("/srv/instances/a"), "alpha", "11111111-1111-4111-8111-111111111111")
            p["media"]["virtio_iso"] = "/srv/virtio.iso"
            seed = self.render.seed_files(p, "Safe!Password123")
            root = ET.fromstring(seed["Autounattend.xml"])
            ns = {"u": "urn:schemas-microsoft-com:unattend"}
            self.assertFalse(root.findall("u:settings[@pass='windowsPE']", ns))
            self.assertNotIn("WillWipeDisk", seed["Autounattend.xml"])
            self.assertNotIn("ImageInstall", seed["Autounattend.xml"])
            domain = ET.fromstring(self.render.domain_xml(p, installing=True))
            self.assertEqual(domain.find("./devices/disk[@device='disk']/boot").get("order"), "1")
            cdroms = domain.findall("./devices/disk[@device='cdrom']/source")
            self.assertEqual([node.get("file") for node in cdroms], ["/srv/virtio.iso", "\\srv\\instances\\a/vol/state/autounattend.iso"])
            self.assertNotIn("windows_iso", " ".join(node.get("file") or "" for node in cdroms))

    def test_two_profiles_get_distinct_identity_paths_macs_and_runtime_placeholders(self):
        with tempfile.TemporaryDirectory() as td:
            a = profile(Path("/srv/instances/alpha"), "alpha", "11111111-1111-4111-8111-111111111111")
            b = profile(Path("/srv/instances/bravo"), "bravo", "22222222-2222-4222-8222-222222222222")
            xml_a = ET.fromstring(self.render.domain_xml(a, installing=False)); xml_b = ET.fromstring(self.render.domain_xml(b, installing=False))
            self.assertNotEqual(xml_a.findtext("uuid"), xml_b.findtext("uuid"))
            self.assertNotEqual(xml_a.find("./devices/interface/mac").get("address"), xml_b.find("./devices/interface/mac").get("address"))
            self.assertIn(a["root"], ET.tostring(xml_a, encoding="unicode")); self.assertIn(b["root"], ET.tostring(xml_b, encoding="unicode"))
            out_a = self.render.runtime_files(a, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", 123)
            out_b = self.render.runtime_files(b, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", 456)
            self.assertNotEqual(out_a["scripts/lib/vmctl.py"], out_b["scripts/lib/vmctl.py"])
            for output in (out_a, out_b):
                for text in output.values():
                    self.assertNotIn("8cb9f0f6-3d77-4c1c-8d0b-5ab70ac5f201", text)
                    self.assertNotIn("/home/vm_run", text)
                    self.assertNotIn("__VM_NAME__", text)


class InstanceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.instance = load(SCRIPTS / "instance.py", f"instance_{id(self)}")

    def test_replacement_uses_new_uuid_and_fresh_credentials_without_password_inheritance(self):
        with tempfile.TemporaryDirectory() as td:
            old = profile(Path(td) / "instances/alpha", "alpha", "11111111-1111-4111-8111-111111111111")
            with mock.patch.object(self.instance, "validate"):
                replacement = self.instance.replacement_profile(old)
            self.assertNotEqual(replacement["uuid"], old["uuid"])
            self.assertEqual(replacement["credentials"]["mode"], "generate")
            self.assertNotIn("password", replacement)
            self.assertIsNone(replacement.get("winrm_thumbprint"))

    def test_create_readiness_failure_preserves_allocated_instance_and_journal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "instances/alpha"; data = profile(root, "alpha", "11111111-1111-4111-8111-111111111111")
            host = root.parent.parent; host.mkdir(parents=True, exist_ok=True)
            def preflight(value): return value
            def storage(value):
                root.mkdir(parents=True, exist_ok=True); (root / "allocation.marker").write_text("allocated")
            with mock.patch.object(self.instance, "validate"), mock.patch.object(self.instance.prepare, "preflight", side_effect=preflight), mock.patch.object(self.instance.prepare, "reserve_registry"), mock.patch.object(self.instance.prepare, "install_host_catalog"), mock.patch.object(self.instance.prepare, "storage", side_effect=storage), mock.patch.object(self.instance.prepare, "seed"), mock.patch.object(self.instance.prepare, "activate"), mock.patch.object(self.instance, "trusted", side_effect=lambda p: Path(p)), mock.patch.object(self.instance, "ready", side_effect=RuntimeError("guest readiness timed out")):
                with self.assertRaisesRegex(RuntimeError, "guest readiness timed out"):
                    self.instance.create(data, apply=True, timeout=30)
            self.assertTrue((root / "allocation.marker").exists())
            self.assertEqual(len(list((host / "operations").glob("create-*.json"))), 1)

    def test_rebuild_validates_template_before_delete(self):
        with tempfile.TemporaryDirectory() as td:
            old = profile(Path(td) / "instances/alpha", "alpha", "11111111-1111-4111-8111-111111111111")
            replacement = dict(old); replacement["uuid"] = "22222222-2222-4222-8222-222222222222"
            fake_destroy = types.ModuleType("destroy")
            fake_destroy.destroy = mock.Mock(side_effect=AssertionError("delete must not run"))
            fake_destroy.validate_identity = mock.Mock(); fake_destroy.inventory = mock.Mock()
            with mock.patch.dict(sys.modules, {"destroy": fake_destroy}), mock.patch.object(self.instance, "inspect_manifest", side_effect=ValueError("bad template hash")), mock.patch.object(self.instance, "replacement_profile", return_value=replacement):
                with self.assertRaisesRegex(ValueError, "bad template hash"):
                    self.instance.rebuild(old, replacement=replacement, apply=True, discard=True)
            fake_destroy.destroy.assert_not_called()

    def test_create_success_reports_no_c_wim_recovery_and_base_runtime_excludes_reset_helpers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "instances/alpha"; data = profile(root, "alpha", "11111111-1111-4111-8111-111111111111"); root.mkdir(parents=True)
            host = root.parent.parent; (host / "operations").mkdir(parents=True)
            data["host_root"] = str(host)
            def storage(value):
                root.mkdir(parents=True, exist_ok=True)
                (root / "instance.json").write_text(json.dumps(value), encoding="utf-8")
            with mock.patch.object(self.instance, "validate"), mock.patch.object(self.instance.prepare, "preflight", side_effect=lambda value: value), mock.patch.object(self.instance.prepare, "reserve_registry"), mock.patch.object(self.instance.prepare, "install_host_catalog"), mock.patch.object(self.instance.prepare, "storage", side_effect=storage), mock.patch.object(self.instance.prepare, "seed"), mock.patch.object(self.instance.prepare, "activate"), mock.patch.object(self.instance.prepare, "finish"), mock.patch.object(self.instance, "ready", return_value={"provision_success": True, "computer_name": "ALPHA", "winrm": "Running", "rdp": "Running", "enabled_users": ["alice"], "logical_cpus": 4, "memory_bytes": 8 * 1024**3}), mock.patch.object(self.instance, "authenticate", return_value={"thumbprint": "abc", "upload_sha256": "hash", "execution": {"is_admin": True, "exit_code": 0}, "rdp_service_running": True}), mock.patch.object(self.instance, "authenticate_after_restart", return_value={"thumbprint": "abc"}), mock.patch.object(self.instance, "trusted", side_effect=lambda p: Path(p)):
                result = self.instance.create(data, apply=True, timeout=30)
            self.assertFalse(result["c_recovery_baseline_created"])
            linux_data = {**data, 'root': '/srv/virtops/instances/alpha'}
            rendered = self.instance.prepare.runtime_files(linux_data, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", 123)
            self.assertFalse(any(name.endswith(("reset.sh", "winvm01_c_reset.py", "winvm01_recovery.py")) for name in rendered))


class ResumeRequestTests(unittest.TestCase):
    """A create journal stores the preflight-normalized profile."""

    def setUp(self):
        self.instance = load(SCRIPTS / "instance.py", f"instance_{id(self)}")

    def request(self, cpu_set="0-3"):
        data = profile(Path("/srv/instances/alpha"), "alpha", str(uuid.uuid4()))
        data["resources"]["cpu_set"] = cpu_set
        for key in ("cpu_order", "threads_per_core", "amd"):
            data["resources"].pop(key, None)
        return data

    def normalized(self, data):
        import copy
        result = copy.deepcopy(data)
        result["resources"].update(
            cpu_set=",".join(str(n) for n in range(0, 4)),
            cpu_order=[0, 1, 2, 3], threads_per_core=1, amd=False)
        return result

    def test_resume_accepts_the_normalized_profile_of_the_same_request(self):
        data = self.request()
        self.assertTrue(self.instance.same_request(self.normalized(data), data))

    def test_the_normalized_profile_really_differs_from_the_requested_one(self):
        # The preflight rewrites the resources block, so the previous
        # field-by-field comparison rejected every resume of the same request.
        data = self.request()
        normalized = self.normalized(data)
        self.assertNotEqual(normalized["resources"], data["resources"])
        self.assertTrue(any(normalized["resources"].get(key) != data["resources"].get(key)
                            for key in normalized["resources"]))
        self.assertTrue(self.instance.same_request(normalized, data))

    def test_resume_rejects_a_different_instance(self):
        data = self.request()
        other = self.normalized(data)
        other["uuid"] = str(uuid.uuid4())
        self.assertFalse(self.instance.same_request(other, data))

    def test_resume_rejects_a_different_cpu_selection(self):
        data = self.request()
        other = self.normalized(data)
        other["resources"]["cpu_set"] = "8-11"
        self.assertFalse(self.instance.same_request(other, data))


if __name__ == "__main__":
    unittest.main()
