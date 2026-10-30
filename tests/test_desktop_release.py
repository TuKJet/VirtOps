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
        for name in ('virtops-desktop.exe', 'app.AppImage'):
            (root / name).write_bytes(name.encode())

    def test_missing_or_duplicate_platform_client_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_artifacts(root)
            self.assertEqual(len(release.release_files(root)), 2)
            (root / 'app.AppImage').unlink()
            with self.assertRaisesRegex(ValueError, 'AppImage'):
                release.release_files(root)
            (root / 'app.AppImage').write_bytes(b'client')
            (root / 'duplicate.AppImage').write_bytes(b'client')
            with self.assertRaisesRegex(ValueError, 'AppImage'):
                release.release_files(root)

    def test_setup_cannot_replace_missing_portable_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_artifacts(root)
            (root / 'virtops-desktop.exe').unlink()
            (root / 'app-setup.exe').write_bytes(b'installer')
            with self.assertRaisesRegex(ValueError, 'virtops-desktop.exe'):
                release.release_files(root)

    def test_installers_cannot_be_added_to_standalone_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_artifacts(root)
            for name in ('app-setup.exe', 'app.msi', 'app.deb'):
                with self.subTest(name=name):
                    extra = root / name
                    extra.write_bytes(b'installer')
                    with self.assertRaisesRegex(ValueError, 'Unexpected release files'):
                        release.release_files(root)
                    extra.unlink()

    def test_prepare_contains_only_two_clients_and_checksum(self):
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
            self.assertEqual(portable.read_bytes(), b'virtops-desktop.exe')
            self.assertEqual(next(output.glob('*.AppImage')).read_bytes(), b'app.AppImage')
            expected = json.loads((output / 'expected-assets.json').read_text())
            self.assertEqual({item['name'] for item in expected}, {
                portable.name, next(output.glob('*.AppImage')).name, 'SHA256SUMS.txt',
            })
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
