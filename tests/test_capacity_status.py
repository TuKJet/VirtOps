from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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


guest_status = load(SCRIPTS / "guest_status.py", "guest_status_under_test")
instance = load(SCRIPTS / "instance.py", "instance_under_test")
prepare = load(SCRIPTS / "prepare_host.py", "prepare_host_under_test")

GIB = 1024 ** 3


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


class GuestStatusClassificationTests(unittest.TestCase):
    """A Windows install takes tens of minutes and the guest agent only answers
    at the end of it.  Reporting that as a failure invites a reinstall."""

    def test_running_domain_without_guest_agent_is_not_ready_not_failed(self):
        with mock.patch.object(guest_status, "domain_state", return_value="running"):
            status, hint = guest_status.classify({"name": "winvm02"}, RuntimeError("Guest agent unavailable"))
        self.assertEqual(status, "not_ready")
        self.assertIn("never reinstall", hint)

    def test_stopped_domain_is_reported_as_not_running(self):
        with mock.patch.object(guest_status, "domain_state", return_value="shut off"):
            status, _ = guest_status.classify({"name": "winvm02"}, RuntimeError("Guest agent unavailable"))
        self.assertEqual(status, "not_running")

    def test_paused_domain_is_not_reported_as_waiting(self):
        with mock.patch.object(guest_status, "domain_state", return_value="paused"):
            status, _ = guest_status.classify({"name": "winvm02"}, RuntimeError("Guest agent unavailable"))
        self.assertEqual(status, "not_running")

    def test_undefined_domain_is_reported_separately(self):
        with mock.patch.object(guest_status, "domain_state", return_value=None):
            status, _ = guest_status.classify({"name": "winvm02"}, RuntimeError("no such domain"))
        self.assertEqual(status, "unknown_instance")

    def test_integrity_failure_is_blocked_not_transient(self):
        with mock.patch.object(guest_status, "domain_state", return_value="running"):
            status, hint = guest_status.classify({"name": "winvm02"}, guest_status.IntegrityError("VM UUID differs from profile"))
        self.assertEqual(status, "blocked")
        self.assertIn("integrity", hint)

    def test_unreadable_profile_is_blocked(self):
        status, _ = guest_status.classify({}, RuntimeError("profile missing"))
        self.assertEqual(status, "blocked")

    def test_integrity_error_is_a_runtime_error(self):
        self.assertTrue(issubclass(guest_status.IntegrityError, RuntimeError))


class BlockedRunReportingTests(unittest.TestCase):
    """A blocked create keeps the instance; the report must say how to resume."""

    def test_blocked_payload_carries_journal_stage_and_resume_command(self):
        error = instance.Blocked("guest readiness timed out", journal="/home/vm_run/operations/create-1.json",
                                 stage="booting", target="winvm02")
        with mock.patch.object(instance, "domstate", return_value="running"):
            payload = instance.blocked_payload(error, "/home/vm_run/profiles/winvm02.json")
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["journal"], "/home/vm_run/operations/create-1.json")
        self.assertEqual(payload["stage"], "booting")
        self.assertEqual(payload["domstate"], "running")
        self.assertIn("--resume /home/vm_run/operations/create-1.json", payload["resume"])
        self.assertIn("preserved", payload["note"])

    def test_blocked_payload_without_journal_stays_minimal(self):
        payload = instance.blocked_payload(RuntimeError("missing media"))
        self.assertEqual(payload, {"status": "blocked", "reason": "missing media"})

    def test_blocked_preserves_the_original_reason(self):
        error = instance.Blocked("guest readiness timed out", journal="/j.json")
        self.assertIn("guest readiness timed out", str(error))
        self.assertEqual(error.journal, "/j.json")
        self.assertIsNone(error.stage)


class GuardThresholdTests(unittest.TestCase):
    """The threshold that matters is the one compiled into the running guard."""

    def test_threshold_is_read_from_the_installed_guard(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "scripts/lib").mkdir(parents=True)
            (root / "scripts/lib/vmctl.py").write_text(
                "GIB = 1024**3\nSTART_ROOT_FREE_MIN = 250 * GIB\n", encoding="utf-8")
            self.assertEqual(prepare.guard_root_free_threshold(root), 250 * GIB)

    def test_threshold_is_read_from_a_literal_byte_value(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "scripts/lib").mkdir(parents=True)
            (root / "scripts/lib/vmctl.py").write_text(
                "START_ROOT_FREE_MIN = 85899345920\n", encoding="utf-8")
            self.assertEqual(prepare.guard_root_free_threshold(root), 85899345920)

    def test_missing_guard_has_no_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(prepare.guard_root_free_threshold(Path(td)))

    def test_recorded_threshold_falls_back_to_the_instance_profile(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "instance.json").write_text(json.dumps({"limits": {"root_free_gib": 80}}), encoding="utf-8")
            self.assertEqual(prepare.recorded_root_free_threshold(root), 80 * GIB)


class CapacityPlanTests(unittest.TestCase):
    """Allocating a store can push a neighbour below its guard threshold."""

    def host(self, td: str):
        host_root = Path(td) / "host"
        (host_root / "registry").mkdir(parents=True)
        neighbour = host_root / "instances" / "winvm01"
        (neighbour / "scripts/lib").mkdir(parents=True)
        (neighbour / "scripts/lib/vmctl.py").write_text(
            "GIB = 1024**3\nSTART_ROOT_FREE_MIN = %d * GIB\n" % 250, encoding="utf-8")
        (host_root / "registry" / "winvm01.json").write_text(json.dumps({
            "schema_version": 1, "name": "winvm01", "uuid": "11111111-1111-4111-8111-111111111111",
            "root": str(neighbour), "bridge": "winbr01", "subnet": "172.30.61.0/30",
            "publish_ip": "192.168.25.5", "rdp_port": 13389, "winrm_port": 15986,
            "owner": "other", "purpose": "colleague", "admin_user": "kaijie",
            "resources": {"memory_gib": 64, "host_limit_gib": 72, "vcpus": 96, "storage_gib": 150, "cpu_set": "0-95"},
            "start_free_gib": 136,
        }), encoding="utf-8")
        return host_root

    def plan(self, host_root: Path, *, root_free_gib: int, storage_gib: int, same_filesystem: bool = True):
        data = profile(host_root / "instances" / "winvm02", "winvm02", "22222222-2222-4222-8222-222222222222")
        data["host_root"] = str(host_root)
        data["resources"]["storage_gib"] = storage_gib
        return prepare.capacity_plan(data, root_free=root_free_gib * GIB, same_filesystem=same_filesystem)

    def test_neighbour_below_its_threshold_is_reported_as_at_risk(self):
        with tempfile.TemporaryDirectory() as td:
            host_root = self.host(td)
            plan = self.plan(host_root, root_free_gib=350, storage_gib=200)
            self.assertEqual(plan["instances_at_risk"], ["winvm01"])
            self.assertEqual(plan["projected_root_free_gib"], 150.0)
            entry = plan["existing_instances"][0]
            self.assertEqual(entry["threshold_source"], "installed-guard")
            self.assertEqual(entry["root_free_threshold_gib"], 250.0)

    def test_neighbour_above_its_threshold_is_not_at_risk(self):
        with tempfile.TemporaryDirectory() as td:
            host_root = self.host(td)
            (host_root / "instances/winvm01/scripts/lib/vmctl.py").write_text(
                "GIB = 1024**3\nSTART_ROOT_FREE_MIN = 80 * GIB\n", encoding="utf-8")
            plan = self.plan(host_root, root_free_gib=350, storage_gib=200)
            self.assertEqual(plan["instances_at_risk"], [])
            self.assertIsNone(prepare.capacity_conflict(plan))

    def test_separate_filesystem_does_not_flag_neighbours(self):
        with tempfile.TemporaryDirectory() as td:
            host_root = self.host(td)
            plan = self.plan(host_root, root_free_gib=350, storage_gib=200, same_filesystem=False)
            self.assertEqual(plan["instances_at_risk"], [])
            self.assertEqual(plan["projection_scope"], "separate-filesystem")

    def test_conflict_message_names_the_affected_instance(self):
        with tempfile.TemporaryDirectory() as td:
            host_root = self.host(td)
            plan = self.plan(host_root, root_free_gib=350, storage_gib=200)
            message = prepare.capacity_conflict(plan)
            self.assertIsNotNone(message)
            self.assertIn("NEEDS_USER_INPUT", message)
            self.assertIn("winvm01", message)
            self.assertIn("250", message)
            self.assertIn("Never pause a neighbour automatically", message)

    def test_chain_reports_the_store_and_the_order(self):
        with tempfile.TemporaryDirectory() as td:
            host_root = self.host(td)
            plan = self.plan(host_root, root_free_gib=350, storage_gib=200)
            chain = plan["chain"]
            self.assertEqual(chain["store_allocation_gib"], 200)
            self.assertIn("destroy builder", chain["order"])
            self.assertIn("fully preallocated", chain["note"])


class DocumentationLinkTests(unittest.TestCase):
    """A reference that is not linked from anywhere is not discoverable."""

    def root(self):
        return ROOT / "skill/windows-vm-sandbox"

    def test_every_relative_link_resolves(self):
        root = self.root()
        broken = []
        documents = list((root / "references").glob("*.md")) + [root / "SKILL.md"]
        for path in documents:
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"\]\((?!https?://)([^)#]+\.md)(?:#[^)]*)?\)", text):
                if not (path.parent / match.group(1)).resolve().is_file():
                    broken.append(f"{path.relative_to(root)} -> {match.group(1)}")
        self.assertEqual(broken, [])

    def test_lifecycle_reference_exists_and_is_reachable(self):
        root = self.root()
        self.assertTrue((root / "references/instance-lifecycle.md").is_file())
        self.assertIn("references/instance-lifecycle.md", (root / "SKILL.md").read_text(encoding="utf-8"))

    def test_capacity_and_status_semantics_are_documented(self):
        root = self.root()
        for name in ("profile.md", "operate.md", "current-instance.md", "initialize.md",
                     "troubleshooting.md", "installation-pitfalls.md"):
            text = (root / "references" / name).read_text(encoding="utf-8")
            self.assertNotIn("运行中 watch 只做告警，不暂停 VM", text)


if __name__ == "__main__":
    unittest.main()
