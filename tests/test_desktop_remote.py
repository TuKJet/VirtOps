import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

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

    def test_network_service_endpoint_is_the_declared_server_address(self):
        self.assertEqual(
            rpc.service_endpoint({
                'access': 'network', 'publish_ip': '192.168.25.5',
                'publish_port': 18002,
            }),
            '192.168.25.5:18002',
        )

    def test_service_endpoint_rejects_untrusted_address_text(self):
        with self.assertRaisesRegex(ValueError, 'address'):
            rpc.service_endpoint({
                'access': 'network', 'publish_ip': '192.168.25.5\\r\\nHost: evil',
                'publish_port': 18002,
            })

    def test_network_publish_returns_direct_endpoint_and_host_probe_scope(self):
        data = {
            'name': 'winvm02', 'uuid': '17028aba-61b4-4d43-b666-df88f120bd7f',
            'root': '/home/vm_run/instances/winvm02',
            'network': {'guest_ip': '172.30.62.2', 'bridge_ip': '172.30.62.1'},
        }
        service = {
            'id': 'tcp-8001', 'guest_port': 8001, 'publish_ip': '192.168.25.5',
            'publish_port': 18002, 'access': 'network',
        }
        adapter = Mock()
        adapter.main.side_effect = [
            {'status': 'ready', 'service': service, 'expected_before': 'old'},
            {'status': 'applied', 'backup': '/var/lib/virtops/backup'},
        ]
        qga = Mock()
        qga.powershell.return_value = '{"status":"ok","created":true,"present":true,"listener":true}'
        with patch.object(rpc, 'module', return_value=adapter), \
                patch.object(rpc, 'qga_for', return_value=qga), \
                patch.object(rpc.socket, 'create_connection') as connect:
            connect.return_value.__enter__.return_value = Mock()
            result = rpc.services(data, Path('/home/vm_run/instances/winvm02'), Path('/opt/virtops'),
                                  'publish', {'guest_port': 8001, 'service_id': 'tcp-8001',
                                              'access': 'network', 'publish_port': 18002})
        self.assertEqual(result['endpoint'], '192.168.25.5:18002')
        self.assertTrue(result['host_to_guest_tcp'])
        self.assertNotIn('local_target', result)
        self.assertEqual(adapter.main.call_args_list[0].args[0]['access'], 'network')


if __name__ == '__main__':
    unittest.main()
