import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

source = Path(__file__).resolve().parents[1] / 'desktop/scripts/prepare_release.py'
spec = importlib.util.spec_from_file_location('desktop_release', source)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def test_missing_or_duplicate_platform_installer_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for suffix in release.FORMATS:
                (root / ('installer' + suffix)).write_bytes(b'installer')
            self.assertEqual(len(release.installer_set(root)), 4)
            (root / 'installer.msi').unlink()
            with self.assertRaisesRegex(ValueError, 'msi'):
                release.installer_set(root)
            (root / 'installer.msi').write_bytes(b'installer')
            (root / 'duplicate.exe').write_bytes(b'installer')
            with self.assertRaisesRegex(ValueError, 'exe'):
                release.installer_set(root)

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
