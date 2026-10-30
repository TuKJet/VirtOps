import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

path = Path(__file__).resolve().parents[1] / 'desktop/engine/remote.py'
spec = importlib.util.spec_from_file_location('desktop_rpc', path)
rpc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rpc)


class DesktopRemoteTests(unittest.TestCase):
    def test_arbitrary_commands_are_rejected_before_host_access(self):
        with patch.object(rpc, 'trusted_context') as context:
            for action in ['shell', 'exec', 'reset', 'delete', '__import__']:
                with self.assertRaisesRegex(ValueError, 'Unsupported'):
                    rpc.dispatch({'action': action, 'profile': {}})
            context.assert_not_called()

    def test_guest_file_browser_rejects_network_and_traversal_paths(self):
        for path in [r'\\server\share', r'\\?\C:\Windows', r'C:\folder\..\secret', 'C:relative', '../x', 'C:\bad\nname']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                rpc.windows_path(path)
        self.assertEqual(rpc.windows_path(r'C:\工程文件'), r'C:\工程文件')

    def test_printing_command_failure_is_not_success(self):
        def failed():
            print(json.dumps({'status': 'blocked', 'reason': 'wrong VM UUID'}))
            return 2
        with self.assertRaisesRegex(RuntimeError, 'wrong VM UUID'):
            rpc.call_printing(failed)

    def test_plaintext_lifecycle_result_remains_bounded(self):
        def output():
            print('x' * 50000)
        result = rpc.call_printing(output)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['output']), 32768)

    def test_resource_values_reject_boolean_before_apply(self):
        context = ({'resources': {'vcpus': 4}}, Path('/vm'), Path('/skill'), None)
        with patch.object(rpc, 'trusted_context', return_value=context):
            with self.assertRaisesRegex(ValueError, 'positive integers'):
                rpc.dispatch({'action': 'configure', 'profile': {}, 'parameters': {'vcpus': True}})


if __name__ == '__main__':
    unittest.main()
