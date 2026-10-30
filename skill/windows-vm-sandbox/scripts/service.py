#!/usr/bin/env python3
"""Windows entrypoint: guest firewall, host publication and owned SSH forward."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
import contextlib

from profile import load

HERE = Path(__file__).resolve().parent


def json_command(arguments, *, source=None, timeout=180):
    result = subprocess.run(arguments, input=source, text=True, encoding='utf-8',
                            capture_output=True, timeout=timeout)
    try:
        output = json.loads(result.stdout.lstrip('\ufeff').strip())
    except ValueError as exc:
        raise RuntimeError('command did not return JSON: ' + (result.stderr or result.stdout)[-1500:]) from exc
    if result.returncode or output.get('status') in {'blocked', 'failed'}:
        raise RuntimeError(output.get('reason') or result.stderr or str(output))
    return output


def ssh_arguments(data):
    ssh = data['ssh']
    for field in ('user', 'host'):
        if not re.fullmatch(r'[A-Za-z0-9_.:@-]+', ssh[field]) or ssh[field].startswith('-'):
            raise ValueError('invalid SSH ' + field)
    args = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
            '-p', str(ssh.get('port', 22))]
    for key, option in [('bind_address', 'BindAddress'), ('host_key_alias', 'HostKeyAlias')]:
        if ssh.get(key):
            args += ['-o', option + '=' + ssh[key]]
    if ssh.get('identity_file'):
        args += ['-i', ssh['identity_file']]
    args += [ssh['user'] + '@' + ssh['host']]
    args += ['sudo', '-n', 'python3', '-'] if ssh.get('sudo', True) else ['python3', '-']
    return args


class Operations:
    def __init__(self, data, profile_path, state_dir):
        self.data, self.profile_path, self.state_dir = data, profile_path, state_dir
        msi = Path(r'C:\Program Files\PowerShell\7\pwsh.exe')
        self.pwsh = str(msi) if msi.is_file() else shutil.which('pwsh') or shutil.which('powershell')
        if not self.pwsh:
            raise RuntimeError('PowerShell is required on the Windows client')

    def host(self, action, **options):
        host_root = self.data.get('host_root') or self.data['root']
        request = {key: self.data[key] for key in ('name', 'uuid', 'root')}
        request.update(host_scripts=self.data.get('service_client', {}).get('host_scripts',
                       host_root + '/skill/VirtOps/skill/windows-vm-sandbox/scripts'), action=action, **options)
        encoded = base64.b64encode(json.dumps(request).encode()).decode()
        source = "import base64,json\nREQUEST=json.loads(base64.b64decode('" + encoded + "'))\n"
        source += (HERE / 'service_remote.py').read_text(encoding='utf-8')
        return json_command(ssh_arguments(self.data), source=source)

    def guest(self, action, service, plan):
        return json_command([self.pwsh, '-NoProfile', '-NonInteractive', '-File', str(HERE / 'service_guest.ps1'),
                             '-Action', action, '-ProfilePath', str(self.profile_path),
                             '-ServiceId', service['id'], '-GuestPort', str(service['guest_port']),
                             '-GuestIp', plan['guest_ip'], '-BridgeIp', plan['bridge_ip']])

    def tunnel(self, action, service, local_port=0):
        args = [self.pwsh, '-NoProfile', '-NonInteractive', '-File', str(HERE / 'service_tunnel.ps1'),
                '-Action', action, '-ProfilePath', str(self.profile_path), '-ServiceId', service['id'],
                '-PublishPort', str(service['publish_port']), '-StateDir', str(self.state_dir / 'tunnels')]
        if local_port:
            args += ['-LocalPort', str(local_port)]
        return json_command(args)

    def recovery_plan(self, service_id):
        folder = self.state_dir / 'operations'
        for path in sorted(folder.glob('*.json'), key=lambda p: p.stat().st_mtime_ns, reverse=True):
            item = json.loads(path.read_text(encoding='utf-8'))
            plan = item.get('plan', {})
            if (item.get('uuid') == self.data['uuid'] and item.get('action') == 'remove'
                    and plan.get('service', {}).get('id') == service_id):
                return plan if item.get('phase') != 'complete' else None
        return None


def publish(ops, *, guest_port, service_id=None, apply=False, local_port=0, record=lambda **fields: None):
    plan = ops.host('plan', guest_port=guest_port, service_id=service_id)
    service = plan['service']
    guest = ops.guest('inspect', service, plan)
    if not apply:
        return {'status': 'ready', 'mode': 'check', 'host': plan, 'guest': guest}
    record(phase='prepared', plan=plan)
    created_guest = False
    host_result = None
    tunnel = None
    phase = 'guest'
    try:
        guest = ops.guest('ensure', service, plan)
        created_guest = bool(guest.get('created'))
        record(phase='guest_ready', guest=guest)
        phase = 'host'
        host_result = ops.host('apply', guest_port=guest_port, service_id=service['id'],
                               publish_port=service['publish_port'], expected_before=plan['expected_before'])
        record(phase='host_ready', host=host_result)
        phase = 'tunnel'
        if guest.get('listener'):
            ops.host('probe', service_id=service['id'])
        tunnel = ops.tunnel('start', service, local_port)
        record(phase='complete', tunnel=tunnel)
        return {'status': 'ready' if guest.get('listener') else 'configured', 'mode': 'apply', 'service': service, 'tunnel': tunnel,
                'guest': guest, 'host': host_result,
                'endpoint': '127.0.0.1:' + str(tunnel['local_port']),
                'note': 'TCP transport configured; application authentication/protocol is application-owned' if guest.get('listener') else 'Port configured; guest application is not listening yet'}
    except Exception as exc:
        errors = []
        # An interrupted SSH call may already have committed remotely.  Keep
        # the exact inbound guest rule until the host state can be reconciled.
        if phase == 'host' and host_result is None:
            record(phase='reconcile_required', error=str(exc))
            raise RuntimeError('host outcome requires reconciliation; rerun the same service request: ' + str(exc)) from exc
        if tunnel and tunnel.get('created'):
            try: ops.tunnel('stop', service)
            except Exception as error: errors.append(str(error))
        if host_result and host_result.get('backup'):
            try: ops.host('rollback', backup=host_result['backup'])
            except Exception as error: errors.append(str(error))
        if created_guest and not errors:
            try: ops.guest('remove', service, plan)
            except Exception as error: errors.append(str(error))
        record(phase='rollback_failed' if errors else 'restored', error=str(exc), recovery_errors=errors)
        raise RuntimeError(str(exc) + ('; recovery: ' + '; '.join(errors) if errors else '; changes restored')) from exc


def remove(ops, *, service_id, apply=False, record=lambda **fields: None):
    plan = ops.host('state', service_id=service_id)
    host_absent = plan.get('service') is None
    if host_absent:
        recovery = ops.recovery_plan(service_id)
        if not recovery:
            return {'status': 'absent', 'mode': 'apply' if apply else 'check',
                    'note': 'Service is not published; no unfinished local removal was found'}
        if any(recovery[key] != plan[key] for key in ('guest_ip', 'bridge_ip')):
            raise RuntimeError('VM addresses changed since removal; inspect the operation record before cleanup')
        plan = recovery
    service = plan['service']
    if not apply:
        return {'status': 'ready', 'mode': 'check', 'service': service,
                'tunnel': ops.tunnel('status', service), 'guest': ops.guest('inspect', service, plan)}
    record(phase='removing', plan=plan)
    host = ({'status': 'absent'} if host_absent else
            ops.host('remove', service_id=service_id, expected_before=plan['expected_before']))
    # Close the host gate first.  A cleanup failure must not republish a revoked service.
    record(phase='host_closed', host=host)
    try:
        tunnel = ops.tunnel('stop', service)
        guest = ops.guest('remove', service, plan)
    except Exception as exc:
        record(phase='cleanup_failed', error=str(exc))
        raise RuntimeError('host publication is closed; rerun the same removal to finish cleanup: ' + str(exc)) from exc
    record(phase='complete', tunnel=tunnel, guest=guest)
    return {'status': 'removed', 'host': host, 'tunnel': tunnel, 'guest': guest}


@contextlib.contextmanager
def client_lock(state, identity, apply):
    if not apply:
        yield
        return
    import msvcrt
    directory = state / 'locks'
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / (str(uuid.UUID(identity)) + '.lock'), 'a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['publish', 'remove'])
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--guest-port', type=int)
    parser.add_argument('--service-id')
    parser.add_argument('--local-port', type=int, default=0)
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    journal = None
    try:
        if os.name != 'nt': raise ValueError('service.py runs on the Windows client')
        if args.action == 'publish' and not (args.guest_port and 1 <= args.guest_port <= 65535):
            raise ValueError('publish requires --guest-port 1..65535')
        if args.action == 'remove' and not args.service_id: raise ValueError('remove requires --service-id')
        if not 0 <= args.local_port <= 65535: raise ValueError('invalid local port')
        data = load(args.profile)
        state = args.state_dir or Path(os.environ['LOCALAPPDATA']) / 'VirtOps'
        ops = Operations(data, args.profile.resolve(), state)
        record = lambda **fields: None
        if args.apply:
            folder = state / 'operations'
            folder.mkdir(parents=True, exist_ok=True)
            journal = folder / (str(uuid.uuid4()) + '.json')
            evidence = {'vm': data['name'], 'uuid': data['uuid'], 'action': args.action}
            def record(**fields):
                evidence.update(fields)
                temporary = journal.with_suffix('.tmp')
                temporary.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
                os.replace(temporary, journal)
        with client_lock(state, data['uuid'], args.apply):
            if args.action == 'publish':
                result = publish(ops, guest_port=args.guest_port, service_id=args.service_id,
                                 local_port=args.local_port, apply=args.apply, record=record)
            else:
                result = remove(ops, service_id=args.service_id, apply=args.apply, record=record)
        print(json.dumps({**result, 'operation_record': str(journal) if journal else None}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'blocked', 'reason': str(exc), 'operation_record': str(journal) if journal else None}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
