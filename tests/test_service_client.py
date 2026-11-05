import importlib.util
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skill/windows-vm-sandbox/scripts'))
import service


class FakeOperations:
    def __init__(self, fail=None, created=True, listener=True):
        self.fail, self.created, self.listener = fail, created, listener
        self.calls = []
        self.host_kwargs = []
        self.plan_extra = {}
        self.service = {'id': 'tcp-8001', 'guest_port': 8001, 'publish_port': 18001,
                        'publish_ip': '127.0.0.1', 'access': 'host-loopback'}

    def host(self, action, **kwargs):
        self.calls.append('host:' + action)
        self.host_kwargs.append((action, kwargs))
        if self.fail == 'host:' + action: raise RuntimeError('host failure')
        if action in {'plan', 'state'}:
            if action == 'plan' and kwargs.get('publish_port'):
                self.service['publish_port'] = kwargs['publish_port']
            return {'service': self.service, 'expected_before': 'fingerprint',
                    'guest_ip': '172.30.61.2', 'bridge_ip': '172.30.61.1', **self.plan_extra}
        return {'backup': '/private/backup'} if action == 'apply' else {'status': 'ok'}

    def guest(self, action, *args):
        self.calls.append('guest:' + action)
        if self.fail == 'guest:' + action: raise RuntimeError('guest failure')
        return {'created': self.created, 'listener': self.listener}

    def tunnel(self, action, *args):
        self.calls.append('tunnel:' + action)
        if self.fail == 'tunnel:' + action: raise RuntimeError('tunnel failure')
        return {'created': True, 'local_port': 18001}


class ServiceWorkflowTests(unittest.TestCase):
    def test_preflight_does_not_mutate(self):
        ops = FakeOperations()
        service.publish(ops, guest_port=8001)
        self.assertEqual(ops.calls, ['host:plan', 'guest:inspect'])

    def test_no_listener_can_prepare_port_without_claiming_service_ready(self):
        ops = FakeOperations(listener=False)
        result = service.publish(ops, guest_port=8001, apply=True)
        self.assertEqual(result['status'], 'configured')
        self.assertNotIn('host:probe', ops.calls)

    def test_success_order(self):
        ops = FakeOperations()
        result = service.publish(ops, guest_port=8001, apply=True)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(ops.calls, ['host:plan', 'guest:inspect', 'guest:ensure', 'host:apply', 'host:probe', 'tunnel:start'])

    def test_network_service_uses_server_endpoint_without_tunnel(self):
        ops = FakeOperations()
        ops.service.update(access='network', publish_ip='192.168.25.5')
        with mock.patch.object(service, 'tcp_probe', return_value=True) as probe:
            result = service.publish(ops, guest_port=8001, apply=True)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['endpoint'], '192.168.25.5:18001')
        self.assertNotIn('tunnel:start', ops.calls)
        probe.assert_called_once_with('192.168.25.5', 18001)

    def test_network_preflight_exposes_server_endpoint(self):
        ops = FakeOperations()
        ops.service.update(access='network', publish_ip='192.168.25.5')
        result = service.publish(ops, guest_port=8001)
        self.assertEqual(result['endpoint'], '192.168.25.5:18001')
        self.assertNotIn('tunnel:start', ops.calls)

    def test_network_failed_tcp_probe_stays_configured(self):
        ops = FakeOperations()
        ops.service.update(access='network', publish_ip='192.168.25.5')
        with mock.patch.object(service, 'tcp_probe', return_value=False):
            result = service.publish(ops, guest_port=8001, apply=True)
        self.assertEqual(result['status'], 'configured')
        self.assertFalse(result['network_probe'])
        self.assertIn('could not reach', result['note'])

    def test_network_options_are_sent_to_plan_and_apply(self):
        ops = FakeOperations()
        ops.service.update(access='network', publish_ip='192.168.25.5')
        service.publish(ops, guest_port=8001, service_id='mcp', access='network',
                        publish_port=18009, allowed_sources=['192.168.25.0/24'],
                        apply=True)
        for action in ('plan', 'apply'):
            kwargs = next(item for name, item in ops.host_kwargs if name == action)
            self.assertEqual(kwargs['access'], 'network')
            self.assertEqual(kwargs['publish_port'], 18009)
            self.assertEqual(kwargs['allowed_sources'], ['192.168.25.0/24'])

    def test_network_local_port_is_rejected(self):
        ops = FakeOperations()
        ops.service.update(access='network', publish_ip='192.168.25.5')
        with self.assertRaisesRegex(ValueError, 'local-port'):
            service.publish(ops, guest_port=8001, local_port=18099)
        self.assertEqual(ops.calls, ['host:plan', 'guest:inspect'])

    def test_network_migration_cleanup_failure_is_warning_after_commit(self):
        ops = FakeOperations(fail='tunnel:stop')
        ops.service.update(access='network', publish_ip='192.168.25.5')
        ops.plan_extra['previous_service'] = {
            'id': 'tcp-8001', 'publish_port': 18001, 'access': 'host-loopback'
        }
        with mock.patch.object(service, 'tcp_probe', return_value=True):
            result = service.publish(ops, guest_port=8001, apply=True)
        self.assertEqual(result['status'], 'ready')
        self.assertIn('warnings', result)
        self.assertIn('old loopback tunnel cleanup failed', result['warnings'][0])
        self.assertNotIn('host:rollback', ops.calls)

    def test_network_service_without_guest_listener_stays_configured(self):
        ops = FakeOperations(listener=False)
        ops.service.update(access='network', publish_ip='192.168.25.5')
        with mock.patch.object(service, 'tcp_probe') as probe:
            result = service.publish(ops, guest_port=8001, apply=True)
        self.assertEqual(result['status'], 'configured')
        probe.assert_not_called()
        self.assertNotIn('tunnel:start', ops.calls)

    def test_network_remove_does_not_require_tunnel_cleanup(self):
        ops = FakeOperations()
        ops.service.update(access='network', publish_ip='192.168.25.5')
        result = service.remove(ops, service_id='tcp-8001', apply=True)
        self.assertEqual(result['status'], 'removed')
        self.assertNotIn('tunnel:stop', ops.calls)

    def test_remove_rejects_publish_options(self):
        with self.assertRaisesRegex(ValueError, 'only valid for publish'):
            service.validate_cli_options('remove', access='network')

    def test_tunnel_failure_rolls_back_host_before_new_guest_rule(self):
        ops = FakeOperations(fail='tunnel:start')
        with self.assertRaisesRegex(RuntimeError, 'changes restored'):
            service.publish(ops, guest_port=8001, apply=True)
        self.assertEqual(ops.calls[-2:], ['host:rollback', 'guest:remove'])

    def test_failure_preserves_preexisting_guest_rule(self):
        ops = FakeOperations(fail='tunnel:start', created=False)
        with self.assertRaises(RuntimeError): service.publish(ops, guest_port=8001, apply=True)
        self.assertNotIn('guest:remove', ops.calls)

    def test_unknown_host_outcome_is_journalled_without_unsafe_cleanup(self):
        ops = FakeOperations(fail='host:apply')
        records = []
        with self.assertRaisesRegex(RuntimeError, 'reconciliation'):
            service.publish(ops, guest_port=8001, apply=True, record=lambda **kw: records.append(kw))
        self.assertEqual(records[-1]['phase'], 'reconcile_required')
        self.assertNotIn('guest:remove', ops.calls)

    def test_revocation_closes_host_before_tunnel_and_guest(self):
        ops = FakeOperations()
        service.remove(ops, service_id='tcp-8001', apply=True)
        self.assertEqual(ops.calls, ['host:state', 'host:remove', 'tunnel:stop', 'guest:remove'])

    def test_revocation_cleanup_failure_never_reopens_host(self):
        ops = FakeOperations(fail='tunnel:stop')
        with self.assertRaises(RuntimeError): service.remove(ops, service_id='tcp-8001', apply=True)
        self.assertNotIn('host:rollback', ops.calls)

    def test_revocation_retry_only_finishes_cleanup(self):
        ops = FakeOperations()
        original = ops.host
        plan = original('state')
        ops.calls.clear()
        def host(action, **kwargs):
            result = original(action, **kwargs)
            return dict(result, service=None) if action == 'state' else result
        ops.host = host
        ops.recovery_plan = lambda service_id: plan
        result = service.remove(ops, service_id='tcp-8001', apply=True)
        self.assertEqual(result['status'], 'removed')
        self.assertEqual(ops.calls, ['host:state', 'tunnel:stop', 'guest:remove'])

    def test_revocation_retry_refuses_changed_guest_address(self):
        ops = FakeOperations()
        ops.host = lambda *a, **kw: {'service': None, 'guest_ip': '172.30.62.2', 'bridge_ip': '172.30.62.1'}
        ops.recovery_plan = lambda service_id: {'service': ops.service, 'guest_ip': '172.30.61.2', 'bridge_ip': '172.30.61.1'}
        with self.assertRaisesRegex(RuntimeError, 'addresses changed'):
            service.remove(ops, service_id='tcp-8001', apply=True)
        self.assertFalse(ops.calls)


if __name__ == '__main__': unittest.main()
