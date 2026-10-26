from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RENDER_PATH = ROOT / 'skill/windows-vm-sandbox/scripts/render.py'
PREPARE_PATH = ROOT / 'skill/windows-vm-sandbox/scripts/prepare_host.py'


def load_render():
    spec = importlib.util.spec_from_file_location('virtops_render', RENDER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_prepare():
    """Load the generator with its sibling scripts ahead of stdlib modules."""
    scripts = str(PREPARE_PATH.parent)
    old_path = list(__import__('sys').path)
    __import__('sys').path.insert(0, scripts)
    try:
        spec = importlib.util.spec_from_file_location('virtops_prepare_test', PREPARE_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        __import__('sys').path[:] = old_path


class EntryPointTests(unittest.TestCase):
    def test_renderer_uses_allowlist_and_keeps_neutral_owner_helper(self):
        render = load_render()
        data = {
            'name': 'alpha-vm',
            'uuid': '11111111-1111-4111-8111-111111111111',
            'root': '/srv/virtops/alpha-vm',
            'network': {
                'subnet': '172.31.40.0/30', 'bridge_ip': '172.31.40.1',
                'guest_ip': '172.31.40.2', 'publish_ip': '192.168.50.10',
                'client_cidr': '192.168.50.0/24', 'bridge': 'vopsbr0',
                'rdp_port': 23389, 'winrm_port': 25986,
            },
            'resources': {
                'storage_gib': 80, 'memory_gib': 16, 'host_limit_gib': 20,
                'vcpus': 4, 'cpu_set': '0-3', 'cpu_order': [0, 1, 2, 3],
                'threads_per_core': 1, 'amd': False,
            },
            'limits': {'start_free_gib': 24, 'root_free_gib': 32,
                       'resume_free_gib': 16, 'watch_free_gib': 8},
            'gpu': {'mode': 'none', 'pci_devices': []},
            'media': {'windows_iso': '/srv/iso/windows.iso',
                      'virtio_iso': '/srv/iso/virtio.iso'},
            'windows': {'computer_name': 'ALPHA-VM', 'admin_user': 'alice'},
        }
        outputs = render.runtime_files(data, '22222222-2222-4222-8222-222222222222', 42)
        self.assertIn('scripts/start.sh', outputs)
        self.assertIn('scripts/lib/owner_apply.ps1', outputs)
        self.assertNotIn('/home/vm_run', outputs['scripts/start.sh'])
        self.assertIn('/srv/virtops/alpha-vm', outputs['scripts/lib/vmctl.py'])
        owner = (ROOT / 'skill/windows-vm-sandbox/assets/runtime/owner_apply.ps1').read_text(encoding='utf-8-sig')
        self.assertEqual(outputs['scripts/lib/owner_apply.ps1'], owner)
        self.assertNotIn('source-manifest.json', outputs)

    @unittest.skipUnless(os.name == 'posix' and os.geteuid() == 0,
                         'requires a root-owned temporary Linux instance tree')
    def test_wrapper_resolves_non_default_root_and_forwards_reset_options(self):
        runtime = ROOT / 'skill/windows-vm-sandbox/assets/runtime'
        with tempfile.TemporaryDirectory(prefix='virtops-entrypoint-') as temp:
            instance = Path(temp) / 'different-root' / 'vm-two'
            lib = instance / 'scripts' / 'lib'
            scripts = instance / 'scripts'
            lib.mkdir(parents=True)
            for parent in [Path(temp), Path(temp) / 'different-root', instance, scripts, lib]:
                parent.chmod(0o755)
            shutil.copy2(runtime / 'vm.sh', lib / 'vm.sh')
            shutil.copy2(runtime / 'reset.sh', scripts / 'reset.sh')
            for path in [lib / 'vm.sh', scripts / 'reset.sh']:
                path.chmod(0o755)
            backend = lib / 'vmctl.py'
            backend.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$VIRTOPS_CAPTURE"\n', encoding='utf-8')
            backend.chmod(0o755)
            capture = Path(temp) / 'capture.txt'
            stub_dir = Path(temp) / 'bin'
            stub_dir.mkdir()
            sudo = stub_dir / 'sudo'
            sudo.write_text('#!/bin/sh\nif [ "$1" = "--" ]; then shift; fi\nexec "$@"\n', encoding='utf-8')
            sudo.chmod(0o755)
            env = dict(os.environ)
            env['PATH'] = str(stub_dir) + os.pathsep + env.get('PATH', '')
            env['VIRTOPS_CAPTURE'] = str(capture)
            result = subprocess.run(
                [str(scripts / 'reset.sh'), '--discard',
                 '--owner-profile', '/etc/virtops/owner.json', '--discard-data'],
                env=env, text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(capture.read_text(encoding='utf-8').splitlines(), [
                'reset', '--discard', '--owner-profile', '/etc/virtops/owner.json', '--discard-data'
            ])

    @unittest.skipUnless(os.name == 'posix' and shutil.which('bash'),
                         'requires bash for shell syntax validation')
    def test_all_shell_entrypoints_parse(self):
        runtime = ROOT / 'skill/windows-vm-sandbox/assets/runtime'
        for path in runtime.glob('*.sh'):
            result = subprocess.run(['bash', '-n', str(path)], text=True,
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, f'{path}: {result.stderr}')

    @unittest.skipUnless(os.name == 'posix' and os.geteuid() == 0 and shutil.which('sh'),
                         'requires a root-owned temporary Linux host catalog')
    def test_shared_status_wrapper_runs_without_sudo(self):
        """The fresh multi-VM catalog is usable before any VM-specific backend exists."""
        prepare = load_prepare()
        with tempfile.TemporaryDirectory(prefix='virtops-catalog-', dir=ROOT) as temp:
            host = Path(temp) / 'host'
            lib = host / 'scripts' / 'lib'
            registry = host / 'registry'
            lib.mkdir(parents=True)
            registry.mkdir()
            for path in [Path(temp), host, host / 'scripts', lib, registry]:
                path.chmod(0o755)
            shutil.copy2(ROOT / 'skill/windows-vm-sandbox/assets/runtime/instance_registry.py',
                         lib / 'instance_registry.py')
            (lib / 'instance_registry.py').chmod(0o755)
            (registry / 'catalog-vm.json').write_text(
                '{"schema_version":1,"name":"catalog-vm",'
                '"uuid":"11111111-1111-4111-8111-111111111111",'
                '"root":"/srv/catalog-vm","bridge":"vopsbr0",'
                '"subnet":"172.31.40.0/30","publish_ip":"192.168.50.10",'
                '"rdp_port":23389,"winrm_port":25986,"owner":"alice",'
                '"purpose":"test","admin_user":"alice",'
                '"resources":{"memory_gib":16,"host_limit_gib":20,'
                '"vcpus":4,"storage_gib":80,"cpu_set":"0-3"},'
                '"start_free_gib":24}\n', encoding='utf-8')
            status = host / 'scripts' / 'status.sh'
            status.write_text(prepare.shared_status_wrapper(), encoding='utf-8')
            status.chmod(0o755)
            stub_bin = Path(temp) / 'bin'
            stub_bin.mkdir()
            virsh = stub_bin / 'virsh'
            virsh.write_text('#!/bin/sh\nprintf "shut off\\n"\n', encoding='utf-8')
            virsh.chmod(0o755)
            env = dict(os.environ)
            env['PATH'] = os.pathsep.join([str(stub_bin), '/usr/bin', '/bin'])
            for args in ([], ['--all']):
                result = subprocess.run([str(status), *args], env=env,
                                        text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('"name": "catalog-vm"', result.stdout)
                self.assertIn('"runtime_state": "shut off"', result.stdout)
                self.assertNotIn('sudo', result.stdout.lower())


class BootKeyTests(unittest.TestCase):
    """OVMF asks for a keypress before it boots removable installation media."""

    def setUp(self):
        self.prepare = load_prepare()

    def test_boot_key_is_only_needed_for_installation_media(self):
        data = {"media": {"windows_iso": None, "virtio_iso": "/srv/virtio.iso"}}
        self.assertFalse(self.prepare.needs_boot_key(data))
        data["media"]["windows_iso"] = "/srv/windows.iso"
        self.assertTrue(self.prepare.needs_boot_key(data))

    def test_boot_key_press_is_bounded_and_tolerates_a_missing_domain(self):
        calls = []
        with mock.patch.object(self.prepare, "run", side_effect=lambda argv, **kwargs: calls.append(argv)):
            self.prepare.press_boot_key("alpha", seconds=0.2, interval=0.01)
        self.assertTrue(calls)
        for argv in calls:
            self.assertEqual(argv[:3], ["virsh", "send-key", "alpha"])


if __name__ == '__main__':
    unittest.main()
