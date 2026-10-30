"""Trusted SSH stdin adapter for the Windows service workflow (REQUEST is data)."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import socket


def fingerprint(services):
    return hashlib.sha256(json.dumps(services, sort_keys=True).encode()).hexdigest()


def main(request):
    root = Path(request['root'])
    package = Path(request['host_scripts'])
    for path in (root, package):
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError('remote paths must be absolute')
        for parent in [path, *path.parents]:
            if parent.is_symlink() or parent.stat().st_uid != 0 or parent.stat().st_mode & 0o022:
                raise ValueError('unsafe remote path ownership')
    script = package / 'publish_services.py'
    if script.is_symlink() or script.stat().st_uid != 0 or script.stat().st_mode & 0o022:
        raise ValueError('unsafe service publisher')
    sys.path.insert(0, str(package))
    spec = importlib.util.spec_from_file_location('virtops_publish', script)
    publisher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publisher)
    profile_path = root / 'instance.json'
    publisher._safe(profile_path)
    data = publisher.profile.load(profile_path, new=False)
    if any(data.get(key) != request[key] for key in ('name', 'uuid', 'root')):
        raise ValueError('authoritative VM identity differs from client profile')
    current = publisher._policy(data['network'])
    action = request['action']
    if action == 'rollback':
        return publisher.rollback(Path(request['backup']), profile_path)
    if action in {'state', 'probe'}:
        service = next((s for s in current if s['id'] == request['service_id']), None)
        if service is None and action == 'probe':
            raise ValueError('service is not published')
        result = {'status': 'ready' if service else 'absent', 'service': service, 'expected_before': fingerprint(current),
                  'guest_ip': data['network']['guest_ip'], 'bridge_ip': data['network']['bridge_ip']}
        if action == 'probe':
            if service['access'] != 'host-loopback': raise ValueError('expected loopback service')
            with socket.create_connection(('127.0.0.1', service['publish_port']), timeout=5):
                result['host_to_guest_tcp'] = True
        return result
    if action not in {'plan', 'apply', 'remove'}:
        raise ValueError('unsupported service operation')
    service_id = request.get('service_id')
    guest_port = request.get('guest_port')
    if not service_id:
        service_id = next((s['id'] for s in current if s['guest_port'] == guest_port), f'tcp-{guest_port}')
    if action == 'remove':
        desired, auto = publisher._resolve_cli_services(profile_path, None, None, None, service_id)
    else:
        desired, auto = publisher._resolve_cli_services(profile_path, service_id, guest_port,
                                                       request.get('publish_port'), None)
    if request.get('expected_before') is not None and request['expected_before'] != fingerprint(current):
        raise RuntimeError('service list changed since preflight; rerun the request')
    existing = next((s for s in current if s['id'] == service_id), None)
    if existing and action != 'remove' and (existing['guest_port'] != guest_port or existing['access'] != 'host-loopback'):
        raise ValueError('existing service differs; use a new service ID or explicitly remove it first')
    plan = publisher._prepare_services(profile_path, desired)
    plan['auto_publish_port'] = auto
    helper = root / 'scripts/lib' / (data['name'] + '_network.py')
    checked = publisher.upgrade.run(['python3', str(helper), 'verify', '--backend', 'iptables-nft'])
    if checked.returncode:
        raise RuntimeError('network isolation guard failed: ' + (checked.stderr or checked.stdout))
    service = existing if action == 'remove' else next(s for s in desired if s['id'] == service_id)
    result = {'status': 'ready', 'service': service, 'expected_before': fingerprint(current),
              'guest_ip': data['network']['guest_ip'], 'bridge_ip': data['network']['bridge_ip'],
              'vm_state': plan['evidence']['vm_state'], 'existing': existing is not None}
    if action != 'plan':
        if desired == current and not plan['evidence'].get('network_update_required'):
            result.update(status='unchanged', backup=None)
        else:
            result.update(publisher.apply_plan(plan))
    return result


if __name__ == '__main__':
    try:
        print(json.dumps(main(REQUEST), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'status': 'blocked', 'reason': str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
