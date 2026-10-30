import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skill/windows-vm-sandbox/scripts'))
import service


class FakeOperations:
    def __init__(self, fail=None, created=True, listener=True):
        self.fail, self.created, self.listener = fail, created, listener
        self.calls = []
        self.service = {'id': 'tcp-8001', 'guest_port': 8001, 'publish_port': 18001}

    def host(self, action, **kwargs):
        self.calls.append('host:' + action)
        if self.fail == 'host:' + action: raise RuntimeError('host failure')
        if action in {'plan', 'state'}:
            return {'service': self.service, 'expected_before': 'fingerprint',
                    'guest_ip': '172.30.61.2', 'bridge_ip': '172.30.61.1'}
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
