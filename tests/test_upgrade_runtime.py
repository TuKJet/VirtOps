import hashlib
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill/windows-vm-sandbox/scripts/upgrade_runtime.py"
SPEC = importlib.util.spec_from_file_location("upgrade_runtime", SCRIPT)
upgrade = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(upgrade)


class UpgradeRuntimeTests(unittest.TestCase):
    def test_packaged_runtime_matches_provenance_manifest(self):
        _, sources = upgrade.validate_source()
        self.assertEqual(set(sources), upgrade.RUNTIME_FILES)

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(dir=ROOT))
        self.root = self.temp / "vm"
        (self.root / "scripts/lib").mkdir(parents=True)
        self.source = Path(self.temp.name) / "source.sh"
        self.source.write_text("#!/bin/sh\nnew\n", encoding="utf-8")
        self.old = self.root / "scripts/start.sh"
        self.old.write_text("#!/bin/sh\nold\n", encoding="utf-8")
        self.profile = {
            "name": "winvm01", "uuid": "00000000-0000-4000-8000-000000000001",
            "root": str(self.root), "host_root": str(self.root),
            "windows": {"admin_user": "admin"},
            "resources": {"storage_gib": 1, "cpu_order": [0, 1], "threads_per_core": 2, "amd": False},
        }

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def patch_checks(self, expected):
        return mock.patch.multiple(
            upgrade,
            load_profile=mock.Mock(return_value=self.profile),
            manifest_hashes=mock.Mock(return_value=expected),
            validate_source=mock.Mock(return_value=(expected, {})),
            render_runtime=mock.Mock(return_value={Path("scripts/start.sh"): self.source.read_bytes()}),
            store_identity=mock.Mock(return_value={"uuid": "store", "inode": 7}),
            domain_facts=mock.Mock(return_value=("running", "<domain/>")),
            validate_network_helper=mock.Mock(),
            validate_instance_config=mock.Mock(),
            check_owner_state=mock.Mock(),
            check_registry=mock.Mock(),
            ensure_owner=mock.Mock(),
            ensure_instance=mock.Mock(),
            register_catalog=mock.Mock(),
            post_apply_verify=mock.Mock(),
            installed_hashes=mock.Mock(return_value={}),
            save_installed_hashes=mock.Mock(),
        )

    def test_check_is_default_and_does_not_modify_files(self):
        expected = {"start.sh": {upgrade.sha256(self.old)}}
        with self.patch_checks(expected):
            result = upgrade.inspect(Path("profile.json"), apply=False, lock_dir=Path(self.temp.name) / "lock")
        self.assertEqual(result["status"], "ready")
        self.assertIn("old", self.old.read_text(encoding="utf-8"))

    def test_apply_replaces_only_managed_file_and_preserves_unknown(self):
        expected = {"start.sh": {upgrade.sha256(self.old)}}
        unknown = self.root / "scripts/lib/operator-note.txt"
        unknown.write_text("keep", encoding="utf-8")
        with self.patch_checks(expected):
            result = upgrade.inspect(Path("profile.json"), apply=True, lock_dir=Path(self.temp.name) / "lock")
        self.assertEqual(result["status"], "applied")
        self.assertIn("new", self.old.read_text(encoding="utf-8"))
        self.assertEqual(unknown.read_text(encoding="utf-8"), "keep")
        self.assertFalse(result["restarted"])
        self.assertFalse(result["network_changed"])

    def test_network_runtime_change_requires_service_transaction_before_any_write(self):
        relative = Path('scripts/lib/winvm01_network.py')
        network = self.root / relative
        network.write_bytes(b'old-network\n')
        expected = {'start.sh': {upgrade.sha256(self.old)},
                    'winvm01_network.py': {upgrade.sha256(network)}}
        with self.patch_checks(expected), mock.patch.object(upgrade, 'render_runtime', return_value={
                Path('scripts/start.sh'): self.source.read_bytes(), relative: b'new-network\n'}):
            result = upgrade.inspect(Path('profile.json'), apply=False)
            self.assertTrue(result['network_update_required'])
            with self.assertRaisesRegex(RuntimeError, 'publish_services.py transaction'):
                upgrade.inspect(Path('profile.json'), apply=True)
        self.assertEqual(network.read_bytes(), b'old-network\n')
        self.assertIn('old', self.old.read_text())

    def test_network_change_while_waiting_for_lock_blocks_all_writes(self):
        relative = Path('scripts/lib/winvm01_network.py')
        network = self.root / relative
        network.write_bytes(b'current-network\n')
        concurrent = b'concurrent-managed-network\n'
        expected = {'start.sh': {upgrade.sha256(self.old)},
                    'winvm01_network.py': {hashlib.sha256(concurrent).hexdigest()}}

        @contextlib.contextmanager
        def changed_before_lock(*args):
            network.write_bytes(concurrent)
            yield

        with self.patch_checks(expected), mock.patch.object(upgrade, 'render_runtime', return_value={
                Path('scripts/start.sh'): self.source.read_bytes(), relative: b'current-network\n'}), \
                mock.patch.object(upgrade, 'lifecycle_lock', changed_before_lock), \
                mock.patch.object(upgrade, 'atomic_copy') as copy:
            with self.assertRaisesRegex(RuntimeError, 'before the lifecycle lock'):
                upgrade.inspect(Path('profile.json'), apply=True)
            copy.assert_not_called()
        self.assertEqual(network.read_bytes(), concurrent)
        self.assertIn('old', self.old.read_text())

    def test_changed_active_collector_is_restarted(self):
        collector = Path("scripts/lib/winvm01_log_collector.py")
        new = b"collector-new\n"
        target = self.root / collector
        target.write_bytes(b"collector-old\n")
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return mock.Mock(returncode=0, stdout="active\n", stderr="")

        with mock.patch.object(upgrade, "run", side_effect=fake_run):
            self.assertTrue(upgrade.restart_collector_if_needed(self.profile, True))
        self.assertEqual(calls, [
            ["systemctl", "is-active", "--quiet", "winvm01-log-collector.service"],
            ["systemctl", "restart", "winvm01-log-collector.service"],
            ["systemctl", "is-active", "--quiet", "winvm01-log-collector.service"],
        ])

    def test_unchanged_collector_is_not_restarted(self):
        collector = Path("scripts/lib/winvm01_log_collector.py")
        content = b"collector-same\n"
        target = self.root / collector
        target.write_bytes(content)
        with mock.patch.object(upgrade, "run") as run:
            self.assertFalse(upgrade.restart_collector_if_needed(self.profile, False))
        run.assert_not_called()

    def test_inactive_collector_is_not_started(self):
        collector = Path("scripts/lib/winvm01_log_collector.py")
        content = b"collector-new\n"
        (self.root / collector).write_bytes(b"collector-old\n")
        with mock.patch.object(upgrade, "run", return_value=mock.Mock(returncode=3, stdout="inactive\n")) as run:
            self.assertFalse(upgrade.restart_collector_if_needed(self.profile, True))
        self.assertEqual(run.call_count, 1)

    def test_collector_restart_failure_blocks_upgrade(self):
        collector = Path("scripts/lib/winvm01_log_collector.py")
        content = b"collector-new\n"
        (self.root / collector).write_bytes(b"collector-old\n")
        responses = [mock.Mock(returncode=0, stdout="active\n"), mock.Mock(returncode=1, stdout="")]
        with mock.patch.object(upgrade, "run", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, "restart failed"):
                upgrade.restart_collector_if_needed(self.profile, True)

    def test_apply_reports_collector_restart_when_rendered_content_changes(self):
        collector = Path("scripts/lib/winvm01_log_collector.py")
        old = b"collector-old\n"
        new = b"collector-new\n"
        target = self.root / collector
        target.write_bytes(old)
        old_hash = hashlib.sha256(old).hexdigest()
        expected = {"start.sh": {upgrade.sha256(self.old)}, collector.as_posix(): {old_hash}}
        with self.patch_checks(expected), \
             mock.patch.object(upgrade, "render_runtime", return_value={
                 Path("scripts/start.sh"): self.source.read_bytes(), collector: new}), \
             mock.patch.object(upgrade, "installed_hashes", return_value={collector.as_posix(): old_hash}), \
             mock.patch.object(upgrade, "run", side_effect=[
                 mock.Mock(returncode=0, stdout="active\n"),
                 mock.Mock(returncode=0, stdout=""),
                 mock.Mock(returncode=0, stdout="active\n"),
             ]):
            result = upgrade.inspect(Path("profile.json"), apply=True,
                                     lock_dir=Path(self.temp.name) / "lock")
        self.assertTrue(result["collector_restarted"])
        self.assertFalse(result["restarted"])
        self.assertFalse(result["network_changed"])

    def test_failed_restart_is_retried_when_manifest_was_not_saved(self):
        collector = Path("scripts/lib/winvm01_log_collector.py")
        old = b"collector-old\n"
        new = b"collector-new\n"
        target = self.root / collector
        target.write_bytes(old)
        old_hash = hashlib.sha256(old).hexdigest()
        expected = {"start.sh": {upgrade.sha256(self.old)}, collector.as_posix(): {old_hash}}
        rendered = {Path("scripts/start.sh"): self.source.read_bytes(), collector: new}
        with self.patch_checks(expected), \
             mock.patch.object(upgrade, "render_runtime", return_value=rendered), \
             mock.patch.object(upgrade, "installed_hashes", return_value={collector.as_posix(): old_hash}), \
             mock.patch.object(upgrade, "run", side_effect=[
                 mock.Mock(returncode=0, stdout="active\n"),
                 mock.Mock(returncode=1, stdout=""),
             ]):
            with self.assertRaisesRegex(RuntimeError, "restart failed"):
                upgrade.inspect(Path("profile.json"), apply=True,
                                lock_dir=Path(self.temp.name) / "lock")
        self.assertEqual(target.read_bytes(), new)
        responses = [
            mock.Mock(returncode=0, stdout="active\n"),
            mock.Mock(returncode=0, stdout=""),
            mock.Mock(returncode=0, stdout="active\n"),
        ]
        with self.patch_checks(expected), \
             mock.patch.object(upgrade, "render_runtime", return_value=rendered), \
             mock.patch.object(upgrade, "installed_hashes", return_value={collector.as_posix(): old_hash}), \
             mock.patch.object(upgrade, "run", side_effect=responses):
            result = upgrade.inspect(Path("profile.json"), apply=True,
                                     lock_dir=Path(self.temp.name) / "lock")
        self.assertTrue(result["collector_restarted"])

    def test_external_edit_is_fail_closed(self):
        expected = {"start.sh": {hashlib.sha256(b"approved").hexdigest()}}
        with self.patch_checks(expected):
            with self.assertRaisesRegex(RuntimeError, "external edits detected"):
                upgrade.inspect(Path("profile.json"), apply=False, lock_dir=Path(self.temp.name) / "lock")

    def test_owner_json_carries_existing_password_for_reset(self):
        private = self.root / "vol/state/private"
        private.mkdir(parents=True)
        (private / "credentials.json").write_text(json.dumps({"admin": "A-strong-password-1"}), encoding="utf-8")
        profile = dict(self.profile)
        profile["windows"] = {"admin_user": "admin"}
        upgrade.ensure_owner(self.root, profile)
        state = json.loads((private / "owner.json").read_text(encoding="utf-8"))
        self.assertEqual(state["admin_user"], "admin")
        self.assertEqual(state["password"], "A-strong-password-1")

    def test_atomic_copy_writes_bytes_and_uses_target_suffix(self):
        target = self.root / "scripts/lib/example.py"
        upgrade.atomic_copy(b"print('ok')\n", target)
        self.assertEqual(target.read_bytes(), b"print('ok')\n")
        if os.name == "posix":
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    def test_previous_managed_runtime_is_accepted_by_private_manifest(self):
        with self.patch_checks({}):
            with mock.patch.object(upgrade, 'installed_hashes', return_value={'scripts/start.sh': upgrade.sha256(self.old)}):
                result = upgrade.inspect(Path('profile.json'), apply=False)
        self.assertEqual(result['status'], 'ready')

    def test_saved_runtime_manifest_binds_identity_and_hashes(self):
        upgrade.save_installed_hashes(self.root, self.profile, {Path('scripts/start.sh'): b'new'})
        path = self.root / 'vol/state/private/runtime-files.json'
        data = json.loads(path.read_text())
        self.assertEqual(data['uuid'], self.profile['uuid'])
        self.assertEqual(data['sha256']['scripts/start.sh'], hashlib.sha256(b'new').hexdigest())
        if os.name == 'posix':
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_network_helper_must_match_profile(self):
        helper = self.root / "scripts/lib/winvm01_network.py"
        helper.write_text(
            "BRIDGE='br0'\nVM_NAME='winvm01'\nBRIDGE_IP='10.0.0.1'\n"
            "GUEST_IP='10.0.0.2'\nPUBLISH_IP='192.0.2.1'\nCLIENT_NET='192.0.2.0/24'\n"
            "PUBLISHED=((13389,3389),(15986,5986))\n", encoding="utf-8")
        profile = dict(self.profile)
        profile["network"] = {"bridge": "br0", "subnet": "10.0.0.0/30", "bridge_ip": "10.0.0.1", "guest_ip": "10.0.0.2",
                               "publish_ip": "192.0.2.1", "client_cidr": "192.0.2.0/24",
                               "rdp_port": 13389, "winrm_port": 15986}
        upgrade.validate_network_helper(self.root, profile)
        helper.write_text(helper.read_text(encoding="utf-8").replace("13389", "13390"), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "PUBLISHED"):
            upgrade.validate_network_helper(self.root, profile)

    def test_network_helper_rejects_unexpected_services_for_empty_profile(self):
        helper = self.root / "scripts/lib/winvm01_network.py"
        helper.write_text(
            "BRIDGE='br0'\nVM_NAME='winvm01'\nBRIDGE_IP='10.0.0.1'\n"
            "GUEST_IP='10.0.0.2'\nPUBLISH_IP='192.0.2.1'\nCLIENT_NET='192.0.2.0/24'\n"
            "PUBLISHED=((13389,3389),(15986,5986))\n"
            "PUBLISHED_SERVICES=({'id':'mcp'},)\n", encoding="utf-8")
        profile = dict(self.profile)
        profile["network"] = {"bridge": "br0", "subnet": "10.0.0.0/30", "bridge_ip": "10.0.0.1", "guest_ip": "10.0.0.2",
                               "publish_ip": "192.0.2.1", "client_cidr": "192.0.2.0/24",
                               "rdp_port": 13389, "winrm_port": 15986}
        with self.assertRaisesRegex(RuntimeError, "PUBLISHED_SERVICES"):
            upgrade.validate_network_helper(self.root, profile)

    def test_network_helper_requires_services_for_nonempty_profile(self):
        helper = self.root / "scripts/lib/winvm01_network.py"
        helper.write_text(
            "BRIDGE='br0'\nVM_NAME='winvm01'\nBRIDGE_IP='10.0.0.1'\n"
            "GUEST_IP='10.0.0.2'\nPUBLISH_IP='192.0.2.1'\nCLIENT_NET='192.0.2.0/24'\n"
            "PUBLISHED=((13389,3389),(15986,5986))\n", encoding="utf-8")
        profile = dict(self.profile)
        profile["network"] = {"bridge": "br0", "subnet": "10.0.0.0/30", "bridge_ip": "10.0.0.1", "guest_ip": "10.0.0.2",
                               "publish_ip": "192.0.2.1", "client_cidr": "192.0.2.0/24",
                               "rdp_port": 13389, "winrm_port": 15986,
                               "published_services": [{"id": "mcp", "publish_port": 18000, "guest_port": 8000}]}
        with self.assertRaisesRegex(RuntimeError, "PUBLISHED_SERVICES"):
            upgrade.validate_network_helper(self.root, profile)

    def test_registry_services_are_canonical_and_legacy_empty_is_compatible(self):
        profile = dict(self.profile)
        profile["host_root"] = str(self.temp)
        profile["root"] = str(self.root)
        profile["network"] = {"bridge": "br0", "subnet": "10.0.0.0/30", "bridge_ip": "10.0.0.1",
                               "guest_ip": "10.0.0.2", "publish_ip": "192.0.2.1",
                               "client_cidr": "192.0.2.0/24", "rdp_port": 13389, "winrm_port": 15986}
        registry = self.temp / "registry"
        registry.mkdir()
        row = upgrade.registry_module()._metadata(profile)
        row.pop("published_services")
        (registry / "winvm01.json").write_text(json.dumps(row), encoding="utf-8")
        upgrade.check_registry(profile, self.root)

        profile["network"]["published_services"] = [{"id": "mcp", "publish_port": 18000, "guest_port": 8000}]
        with self.assertRaisesRegex(RuntimeError, "published_services"):
            upgrade.check_registry(profile, self.root)

        row = upgrade.registry_module()._metadata(profile)
        row["published_services"][0]["publish_port"] = 18001
        (registry / "winvm01.json").write_text(json.dumps(row), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "published_services"):
            upgrade.check_registry(profile, self.root)

    @unittest.skipUnless(os.name == "posix", "Linux privilege gate")
    def test_apply_requires_root(self):
        with mock.patch.object(upgrade.os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(RuntimeError, "requires Linux root"):
                upgrade.inspect(Path("profile.json"), apply=False)


if __name__ == "__main__":
    unittest.main()
