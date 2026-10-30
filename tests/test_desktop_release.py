import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

source = Path(__file__).resolve().parents[1] / 'desktop/scripts/prepare_release.py'
spec = importlib.util.spec_from_file_location('desktop_release', source)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def make_artifacts(self, root):
        for name in ('virtops-desktop.exe', 'app-setup.exe', 'app.msi', 'app.deb', 'app.AppImage'):
            (root / name).write_bytes(name.encode())

    def test_missing_or_duplicate_platform_installer_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_artifacts(root)
            self.assertEqual(len(release.release_files(root)), 5)
            (root / 'app.msi').unlink()
            with self.assertRaisesRegex(ValueError, 'msi'):
                release.release_files(root)
            (root / 'app.msi').write_bytes(b'installer')
            (root / 'duplicate-setup.exe').write_bytes(b'installer')
            with self.assertRaisesRegex(ValueError, 'setup.exe'):
                release.release_files(root)

    def test_setup_cannot_replace_missing_portable_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_artifacts(root)
            (root / 'virtops-desktop.exe').unlink()
            with self.assertRaisesRegex(ValueError, 'virtops-desktop.exe'):
                release.release_files(root)

    def test_prepare_keeps_portable_and_setup_payloads_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = root / 'artifacts'
            artifacts.mkdir()
            self.make_artifacts(artifacts)
            output = root / 'release'
            with patch.dict('os.environ', {
                'GITHUB_SHA': 'a' * 40, 'GITHUB_REPOSITORY': 'test/virtops',
                'GITHUB_RUN_ID': '1', 'GITHUB_OUTPUT': str(root / 'github-output'),
            }):
                release.prepare(artifacts, output)
            portable = next(output.glob('*-portable.exe'))
            setup = next(output.glob('*-setup.exe'))
            self.assertEqual(portable.read_bytes(), b'virtops-desktop.exe')
            self.assertEqual(setup.read_bytes(), b'app-setup.exe')
            expected = json.loads((output / 'expected-assets.json').read_text())
            self.assertEqual(len(expected), 6)
            self.assertIn(portable.name, (output / 'SHA256SUMS.txt').read_text())

    def test_upload_verification_rejects_changed_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = [{'name': 'app.exe', 'size': 4, 'sha256': 'a' * 64}]
            uploaded = {'assets': [{'name': 'app.exe', 'size': 4, 'state': 'uploaded', 'digest': 'sha256:' + 'a' * 64}]}
            (root / 'expected-assets.json').write_text(json.dumps(expected))
            (root / 'server-release.json').write_text(json.dumps(uploaded))
            release.verify(root)
            uploaded['assets'][0]['digest'] = 'sha256:' + 'b' * 64
            (root / 'server-release.json').write_text(json.dumps(uploaded))
            with self.assertRaisesRegex(ValueError, 'hash'):
                release.verify(root)


if __name__ == '__main__':
    unittest.main()
