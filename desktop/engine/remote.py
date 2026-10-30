"""Fixed desktop RPC adapter, sent over authenticated SSH; no model or shell API."""
import base64
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path, PureWindowsPath
import re
import socket
import sys
import xml.etree.ElementTree as ET
import subprocess
import uuid

ALLOWED = {'inspect', 'status', 'start', 'stop', 'logs', 'configure', 'services',
           'publish', 'remove_service', 'list_files'}


def safe_path(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Expected an absolute trusted host path')
    for item in (path, *path.parents):
        st = item.lstat()
        if item.is_symlink() or st.st_uid != 0 or st.st_mode & 0o022:
            raise ValueError('Host path must be root-owned and not group/world writable')
    return path


def module(path, name):
    safe_path(path)
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def trusted_context(client):
    root = safe_path(client['root'])
    package = safe_path(client.get('service_client', {}).get('host_scripts') or
                        (client.get('host_root') or str(root)) + '/skill/VirtOps/skill/windows-vm-sandbox/scripts')
    sys.path.insert(0, str(package))
    profile = module(package / 'profile.py', 'desktop_profile')
    data = profile.load(safe_path(root / 'instance.json'), new=False)
    if any(client.get(key) != data.get(key) for key in ('name', 'uuid', 'root')):
        raise ValueError('VM identity differs from the authoritative host profile')
    return data, root, package, profile


def call_printing(function, *args):
    capture = io.StringIO()
    with contextlib.redirect_stdout(capture):
        code = function(*args)
    output = capture.getvalue().strip()
    try:
        result = json.loads(output)
    except ValueError:
        result = {'status': 'completed' if not code else 'blocked', 'output': output[-32768:]}
    if code:
        raise RuntimeError(result.get('reason') or result.get('output') or str(result))
    return result


def qga_for(data, root):
    result = subprocess.run(['virsh', '--readonly', '-c', 'qemu:///system', 'dumpxml', data['name']],
                            text=True, capture_output=True, check=True, timeout=15)
    if ET.fromstring(result.stdout).findtext('uuid') != data['uuid']:
        raise ValueError('Live VM UUID differs from the selected instance')
    qga = module(root / 'scripts/lib' / (data['name'] + '_qga.py'), 'desktop_qga')
    if qga.VM != data['name']:
        raise ValueError('Guest agent VM identity differs')
    qga.call('guest-ping')
    return qga


def windows_path(value):
    if not isinstance(value, str) or len(value) > 240 or any(ord(c) < 32 for c in value):
        raise ValueError('Invalid Windows path')
    path = PureWindowsPath(value)
    if not re.fullmatch(r'[A-Za-z]:', path.drive) or not path.is_absolute() or '..' in path.parts:
        raise ValueError('Use an absolute local guest drive path without .. or UNC shares')
    return str(path)


def guest_firewall(qga, data, service, action):
    # Same bounded policy as the Windows client. QGA is a local host/guest
    # channel, so neither client platform needs guest network egress or WinRM.
    payload = {'mode': action, 'name': 'VirtOps-' + data['uuid'] + '-' + service['id'],
               'guest': data['network']['guest_ip'], 'bridge': data['network']['bridge_ip'],
               'port': service['guest_port']}
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    source = r'''
$ErrorActionPreference='Stop'
$r=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__DATA__'))|ConvertFrom-Json
$profiles=@(Get-NetFirewallProfile -PolicyStore ActiveStore)
if($profiles.Count -ne 3 -or @($profiles|Where-Object { [string]$_.Enabled -ne 'True' -or [string]$_.DefaultOutboundAction -ne 'Block' }).Count){throw 'Guest firewall profiles must be enabled with default outbound Block'}
if(@(Get-NetRoute|Where-Object DestinationPrefix -in @('0.0.0.0/0','::/0')).Count){throw 'Guest has a default route'}
$rule=Get-NetFirewallRule -Name $r.name -ErrorAction SilentlyContinue
$created=$false
if($rule){
  $p=$rule|Get-NetFirewallPortFilter; $a=$rule|Get-NetFirewallAddressFilter
  if([string]$rule.Direction -ne 'Inbound' -or [string]$rule.Action -ne 'Allow' -or [string]$rule.Enabled -ne 'True' -or [string]$p.Protocol -ne 'TCP' -or [string]$p.LocalPort -ne [string]$r.port -or @($a.LocalAddress).Count -ne 1 -or @($a.RemoteAddress).Count -ne 1 -or [string]$a.LocalAddress -ne $r.guest -or [string]$a.RemoteAddress -ne $r.bridge){throw 'Owned firewall rule scope differs'}
}
if($r.mode -eq 'ensure' -and -not $rule){
  New-NetFirewallRule -Name $r.name -DisplayName $r.name -Enabled True -Direction Inbound -Action Allow -Profile Any -Protocol TCP -LocalAddress $r.guest -RemoteAddress $r.bridge -LocalPort $r.port|Out-Null
  $created=$true; $rule=Get-NetFirewallRule -Name $r.name
}
if($r.mode -eq 'remove' -and $rule){Remove-NetFirewallRule -Name $r.name; $rule=$null}
$listener=@(Get-NetTCPConnection -State Listen -LocalPort $r.port -ErrorAction SilentlyContinue|Where-Object LocalAddress -in @($r.guest,'0.0.0.0')).Count -gt 0
@{status='ok';created=$created;present=[bool]$rule;listener=$listener}|ConvertTo-Json -Compress
'''.replace('__DATA__', encoded)
    return json.loads(qga.powershell(source, timeout=60))


def removal_record(root, service_id):
    private = safe_path(root / 'vol/state/private')
    return private / ('desktop-remove-' + service_id + '.json')


def save_removal(path, data):
    if path.exists():
        safe_path(path)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def services(data, root, package, action, parameters):
    adapter = module(package / 'service_remote.py', 'desktop_services')
    base = {key: data[key] for key in ('name', 'uuid', 'root')}
    base['host_scripts'] = str(package)
    def host(operation, **extra):
        return adapter.main(dict(base, action=operation, **extra))
    if action == 'services':
        return {'status': 'ok', 'services': data['network'].get('published_services', [])}
    service_id = parameters.get('service_id')
    if service_id is not None and not re.fullmatch(r'[a-z][a-z0-9-]{1,31}', service_id):
        raise ValueError('Invalid service ID')
    qga = qga_for(data, root)
    if action == 'remove_service':
        if not service_id:
            raise ValueError('Select a service to remove')
        state = host('state', service_id=service_id)
        record = removal_record(root, service_id)
        if state.get('service'):
            service = state['service']
            save_removal(record, {'uuid': data['uuid'], 'service': service,
                                 'guest_ip': data['network']['guest_ip'], 'bridge_ip': data['network']['bridge_ip']})
            result = host('remove', service_id=service_id, expected_before=state['expected_before'])
        elif record.exists():
            previous = json.loads(safe_path(record).read_text())
            if previous['uuid'] != data['uuid'] or previous['service']['id'] != service_id or any(
                    previous[k] != data['network'][k] for k in ('guest_ip', 'bridge_ip')):
                raise ValueError('Removal recovery record differs from this VM')
            service = previous['service']
            result = {'status': 'absent'}
        else:
            return {'status': 'absent', 'service_id': service_id}
        try:
            guest = guest_firewall(qga, data, service, 'remove')
        except Exception as exc:
            return {'status': 'cleanup_required', 'service': service, 'host_closed': True, 'reason': str(exc)}
        record.unlink()
        return {'status': 'removed', 'service': service, 'guest': guest, 'host': result}
    port = parameters.get('guest_port')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('Guest port must be 1..65535')
    plan = host('plan', guest_port=port, service_id=service_id)
    service = plan['service']
    guest = guest_firewall(qga, data, service, 'ensure')
    try:
        result = host('apply', guest_port=port, service_id=service['id'],
                      publish_port=service['publish_port'], expected_before=plan['expected_before'])
    except Exception:
        # Reconcile authoritative state before removing the exact guest rule.
        # A failed rollback must never be mistaken for an absent publication.
        current = host('state', service_id=service['id'])
        if guest['created'] and not current.get('service'):
            guest_firewall(qga, data, service, 'remove')
        raise
    reachable = False
    try:
        with socket.create_connection((data['network']['guest_ip'], port), timeout=3):
            reachable = True
    except OSError:
        pass
    return {'status': 'ready' if reachable else 'configured', 'service': service,
            'guest': guest, 'host': result, 'tcp_reachable': reachable,
            'local_target': f"127.0.0.1:{service['publish_port']}",
            'note': 'TCP reachability does not verify HTTP/MCP authentication.'}


def dispatch(request):
    action = request['action']
    if action not in ALLOWED:
        raise ValueError('Unsupported desktop operation')
    parameters = request.get('parameters') or {}
    if not isinstance(parameters, dict):
        raise ValueError('Operation parameters must be an object')
    data, root, package, profile = trusted_context(request['profile'])
    if action in {'inspect', 'status', 'start', 'stop', 'logs'}:
        host = module(package / 'host_remote.py', 'desktop_host')
        return call_printing(host.main, {'profile': data, 'action': action, 'network_only': action == 'logs'})
    if action == 'configure':
        desired = copy.deepcopy(data)
        for key in ('vcpus', 'memory_gib', 'host_limit_gib'):
            if key in parameters:
                if type(parameters[key]) is not int or parameters[key] <= 0:
                    raise ValueError('Resource values must be positive integers')
                desired['resources'][key] = parameters[key]
        profile.validate(desired, new=False)
        configure = module(package / 'configure_remote.py', 'desktop_configure')
        return configure.configure(data, desired, apply=True, stop=parameters.get('stop') is True,
                                   restart=parameters.get('restart') is True)
    if action in {'services', 'publish', 'remove_service'}:
        return services(data, root, package, action, parameters)
    if action == 'list_files':
        path = windows_path(parameters.get('path', 'C:\\'))
        encoded = base64.b64encode(path.encode('utf-8')).decode()
        script = "$ErrorActionPreference='Stop';$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + encoded + "'));@(Get-ChildItem -LiteralPath $p -Force|Select-Object -First 500 Name,FullName,@{n='directory';e={$_.PSIsContainer}},@{n='bytes';e={if($_.PSIsContainer){0}else{$_.Length}}},LastWriteTimeUtc)|ConvertTo-Json -Depth 3 -Compress"
        raw = qga_for(data, root).powershell(script, timeout=60).strip()
        items = json.loads(raw) if raw else []
        return {'status': 'ok', 'path': path, 'items': items if isinstance(items, list) else [items], 'limit': 500}


if __name__ == '__main__':
    try:
        print(json.dumps(dispatch(REQUEST), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'status': 'blocked', 'reason': str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
