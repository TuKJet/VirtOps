"""Render the verified runtime for a fresh instance; no host mutations."""
from __future__ import annotations
import ast
import ipaddress
from pathlib import Path
import re
import uuid
import xml.etree.ElementTree as ET

ASSETS = Path(__file__).resolve().parents[1] / 'assets'
GIB = 1024**3
BASIC = {'start.sh', 'stop.sh', 'status.sh', 'logs.sh', 'counters.sh', 'reset.sh', 'delete.sh', 'recreate.sh'}
# Keep the rendered runtime a reviewed allowlist.  A stray file dropped into
# assets/runtime must never become executable on a new host by accident.
RUNTIME_SOURCES = frozenset({
    'vm.sh', 'vmctl.py', 'domain.xml',
    'start.sh', 'stop.sh', 'status.sh', 'logs.sh', 'counters.sh', 'reset.sh',
    'delete.sh', 'recreate.sh',
    'winvm01_bridge.py', 'winvm01_network.py', 'winvm01_log_collector.py',
    'winvm01_store_guard.py', 'winvm01_qga.py', 'winvm01_recovery.py',
    'winvm01_c_reset.py', 'winvm01_reset_context.ps1',
    'winvm01_reset_postboot.ps1',
    # This guest-side helper is deliberately instance-neutral.  It is copied
    # byte-for-byte; its Windows state root must never inherit host/profile
    # substitutions from the current reference VM.
    'owner_apply.ps1', 'owner_verify.ps1',
    'owner_environment.py', 'maintenance.py', 'instance_registry.py',
    'admission.py',
})
NEUTRAL_RUNTIME_SOURCES = frozenset({
    'owner_apply.ps1', 'owner_verify.ps1', 'owner_environment.py', 'maintenance.py',
    'instance_registry.py',
    'admission.py',
})


def substitutions(data, store_uuid, inode):
    net = data['network']
    return {
        '/home/vm_run': data['root'],
        '8cb9f0f6-3d77-4c1c-8d0b-5ab70ac5f201': data['uuid'],
        'e68ccc11-f548-4a28-a341-3707945468b1': store_uuid,
        '73924612': str(inode),
        '172.30.61.0/30': net['subnet'],
        '172.30.61.1': net['bridge_ip'], '172.30.61.2': net['guest_ip'],
        '192.168.20.5': net['publish_ip'], '192.168.20.0/24': net['client_cidr'],
        'winbr01': net['bridge'],
        '13389': str(net['rdp_port']), '15986': str(net['winrm_port']),
        'WINVM01': data['name'].upper(), 'WinVM01': data['name'].upper(), 'winvm01': data['name'],
    }


def replace_text(text, mapping):
    # One pass prevents replacements from rewriting values introduced by earlier replacements.
    pattern = re.compile('|'.join(re.escape(key) for key in sorted(mapping, key=len, reverse=True)))
    return pattern.sub(lambda match: mapping[match.group()], text)


def assignments(source, values):
    lines = source.splitlines(keepends=True)
    replacements = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in values:
                replacements.append((node.lineno - 1, node.end_lineno, f'{name} = {values[name]!r}\n'))
    for start, end, replacement in reversed(replacements):
        lines[start:end] = [replacement]
    return ''.join(lines)


def runtime_files(data, store_uuid, inode):
    mapping = substitutions(data, store_uuid, inode)
    resources, limits = data['resources'], data['limits']
    values = {'STORE_BYTES': resources['storage_gib'] * GIB, 'DISK_BYTES': resources['storage_gib'] * GIB,
              'SIZE': resources['storage_gib'] * GIB, 'UUID': store_uuid, 'INODE': inode,
              'MEMORY_MAX': resources['host_limit_gib'] * GIB, 'CPU_MAX': f"{resources['vcpus'] * 100000} 100000",
              'ALLOWED_CPUS': resources['cpu_set'], 'START_MEM_AVAILABLE_MIN': limits['start_free_gib'] * GIB,
              'START_ROOT_FREE_MIN': limits['root_free_gib'] * GIB, 'WATCH_MEM_AVAILABLE_MIN': limits['watch_free_gib'] * GIB,
              'MTU': data['network'].get('mtu') or 1500,
              'PUBLISHED': ((data['network']['rdp_port'], 3389), (data['network']['winrm_port'], 5986)),
              'PUBLISHED_UDP': ((data['network']['rdp_port'], 3389),)}
    outputs = {}
    runtime_dir = ASSETS / 'runtime'
    missing = sorted(name for name in RUNTIME_SOURCES if not (runtime_dir / name).is_file())
    if missing:
        raise ValueError('runtime source missing: ' + ', '.join(missing))
    for path in sorted((runtime_dir / name for name in RUNTIME_SOURCES), key=lambda item: item.name):
        if data.get('image', {}).get('kind') == 'base' and path.name in {
            'reset.sh', 'delete.sh', 'recreate.sh', 'winvm01_c_reset.py', 'winvm01_recovery.py',
            'winvm01_reset_context.ps1', 'winvm01_reset_postboot.ps1',
        }:
            # Disposable instances replace their whole private disk from the
            # template. Only legacy installations retain C-only recovery.
            continue
        if path.name == 'domain.xml':
            # The domain is regenerated from the profile below; retaining the
            # reference XML in the source tree is useful for review only.
            continue
        source_text = path.read_text(encoding='utf-8-sig')
        text = source_text if path.name in NEUTRAL_RUNTIME_SOURCES else replace_text(source_text, mapping)
        if path.suffix == '.py':
            # systemd escapes a literal '-' within a slice component.
            text = text.replace('machine-'+data['name']+'.slice', 'machine-'+data['name'].replace('-', r'\\x2d')+'.slice')
            text = assignments(text, values)
            text = text.replace('80 * GIB', str(limits['resume_free_gib']) + ' * GIB')
            for old,new in [('250 GiB',str(limits['root_free_gib'])+' GiB'),('204 GiB',str(limits['start_free_gib'])+' GiB'),('80 GiB',str(limits['resume_free_gib'])+' GiB'),('96-thread',str(resources['vcpus'])+'-thread')]:
                text=text.replace(old,new)
            compile(text, path.name, 'exec')
        name = path.name if path.name in NEUTRAL_RUNTIME_SOURCES else replace_text(path.name, mapping)
        directory = 'scripts' if path.name in BASIC else 'scripts/lib'
        outputs[directory + '/' + name] = text
    return outputs


def domain_xml(data, *, installing):
    if data.get('gpu',{}).get('mode')=='vgpu':raise ValueError('NEEDS_GPU_INTEGRATION: vendor-specific vGPU compatibility, licensing and XML must be resolved first')
    text = replace_text((ASSETS / 'runtime/domain.xml').read_text(), substitutions(data, 'unused', 0))
    root = ET.fromstring(text)
    root.attrib.pop('id', None)
    for label in list(root.findall('seclabel')):
        root.remove(label)
    root.find('name').text = data['name']; root.find('uuid').text = data['uuid']
    r = data['resources']
    for tag in ('memory', 'currentMemory'):
        root.find(tag).text = str(r['memory_gib'] * 1024**2)
    for tag in ('hard_limit', 'swap_hard_limit'):
        root.find('memtune/' + tag).text = str(r['host_limit_gib'] * 1024**2)
    root.find('vcpu').text = str(r['vcpus'])
    tune = root.find('cputune'); tune.clear()
    ET.SubElement(tune, 'global_period').text = '100000'
    ET.SubElement(tune, 'global_quota').text = str(r['vcpus'] * 100000)
    for index, cpu in enumerate(r['cpu_order']):
        ET.SubElement(tune, 'vcpupin', {'vcpu': str(index), 'cpuset': str(cpu)})
    ET.SubElement(tune, 'emulatorpin', {'cpuset': r['cpu_set']})
    threads = r['threads_per_core']
    root.find('cpu/topology').set('cores', str(r['vcpus'] // threads))
    root.find('cpu/topology').set('threads', str(threads))
    if not r.get('amd', False):
        for feature in list(root.findall('cpu/feature')):
            if feature.get('name') == 'topoext':
                root.find('cpu').remove(feature)
    root.find('os/type').set('machine', 'q35')
    root.find('os/loader').text = data.get('ovmf_code') or '/usr/share/OVMF/OVMF_CODE_4M.ms.fd'
    root.find('os/nvram').set('template', data.get('ovmf_vars') or '/usr/share/OVMF/OVMF_VARS_4M.ms.fd')
    root.find('os/nvram').text = data['root'] + '/vol/state/' + data['name'] + '_VARS.fd'
    devices = root.find('devices')
    # A GPU can expose display/audio/USB functions. Give each a PCIe root port;
    # the reference VM only had room for its original virtio devices.
    next_index=max(int(n.get('index','0')) for n in devices.findall("controller[@type='pci']"))+1
    for index in range(next_index,next_index+len(data.get('gpu',{}).get('pci_devices',[]))):
        controller=ET.SubElement(devices,'controller',{'type':'pci','index':str(index),'model':'pcie-root-port'})
        ET.SubElement(controller,'model',{'name':'pcie-root-port'})
        ET.SubElement(controller,'target',{'chassis':str(index),'port':hex(0x10+index-1)})
    for pci in data.get('gpu',{}).get('pci_devices',[]):
        domain,bus,slot_function=pci.split(':');slot,function=slot_function.split('.')
        dev=ET.SubElement(devices,'hostdev',{'mode':'subsystem','type':'pci','managed':'no'})
        src=ET.SubElement(dev,'source')
        ET.SubElement(src,'address',{'domain':'0x'+domain,'bus':'0x'+bus,'slot':'0x'+slot,'function':'0x'+function})
    for interface in devices.findall('interface'):
        for target in list(interface.findall('target')):
            interface.remove(target)
        interface.find('mac').set('address', '52:54:00:' + ':'.join(f'{n:02x}' for n in uuid.UUID(data['uuid']).bytes[-3:]))
    for alias in root.findall('.//alias'):
        for parent in root.iter():
            if alias in list(parent): parent.remove(alias); break
    disk = devices.find("disk[@device='disk']")
    from_base = data.get('image', {}).get('kind') == 'base'
    disk.find('boot').set('order', '2' if installing and not from_base else '1')
    if installing:
        media_keys = ('virtio_iso', 'seed_iso') if from_base else ('windows_iso', 'virtio_iso', 'seed_iso')
        for index, key in enumerate(media_keys, 1):
            path = data['media'][key] if key != 'seed_iso' else data['root'] + '/vol/state/autounattend.iso'
            cd = ET.SubElement(devices, 'disk', {'type': 'file', 'device': 'cdrom'})
            ET.SubElement(cd, 'driver', {'name': 'qemu', 'type': 'raw'})
            ET.SubElement(cd, 'source', {'file': path})
            ET.SubElement(cd, 'target', {'dev': 'sd' + chr(ord('a') + index), 'bus': 'sata'})
            ET.SubElement(cd, 'readonly')
            if index == 1 and not from_base: ET.SubElement(cd, 'boot', {'order': '1'})
    ET.indent(root)
    return ET.tostring(root, encoding='unicode') + '\n'


def seed_files(data, password):
    ET.register_namespace('', 'urn:schemas-microsoft-com:unattend')
    ET.register_namespace('wcm', 'http://schemas.microsoft.com/WMIConfig/2002/State')
    mapping = substitutions(data, 'unused', 0)
    mapping.update({'__VM_NAME__': data['windows']['computer_name'], '__BOOTSTRAP_ADMIN_USER__': data['windows']['admin_user'],
                    '__VM_MTU__': str(data['network'].get('mtu') or 1500)})
    root = ET.fromstring(replace_text((ASSETS / 'templates/remote-windows-unattend-template.xml').read_text(encoding='utf-8-sig'), mapping))
    ns = {'u': 'urn:schemas-microsoft-com:unattend'}
    if data.get('image', {}).get('kind') == 'base':
        # A generalized disk already contains Windows. Never carry the installer
        # disk wipe / ImageInstall pass into a template-derived instance.
        for settings in list(root.findall('u:settings', ns)):
            if settings.get('pass') == 'windowsPE':
                root.remove(settings)
    for node in root.iter():
        if node.text == '__BOOTSTRAP_ADMIN_PASSWORD__': node.text = password
        if node.tag.split('}')[-1] in {'InputLocale', 'SystemLocale', 'UILanguage', 'UserLocale'}: node.text = data['windows']['language']
        if node.tag.split('}')[-1] == 'TimeZone': node.text = data['windows'].get('time_zone', 'UTC')
    for item in root.findall('.//u:MetaData', ns):
        if item.findtext('u:Key', namespaces=ns) == '/IMAGE/INDEX': item.find('u:Value', ns).text = str(data['windows']['image_index'])
    for node in root.findall('.//u:RunSynchronousCommand/u:Path', ns):
        if not node.text or len(node.text) > 259: raise ValueError('RunSynchronous command is too long')
    provision = replace_text((ASSETS / 'templates/remote-windows-provision-template.ps1').read_text(encoding='utf-8-sig'), mapping)
    provision = provision.replace('China Standard Time', data['windows'].get('time_zone', 'UTC'))
    bootstrap = '@echo off\r\nif not exist C:\\Windows\\Setup\\Scripts mkdir C:\\Windows\\Setup\\Scripts\r\ncopy /y "%~dp0remote-windows-provision.ps1" C:\\Windows\\Setup\\Scripts\\remote-windows-provision.ps1\r\nexit /b %errorlevel%\r\n'
    return {'Autounattend.xml': ET.tostring(root, encoding='unicode'), 'remote-windows-provision.ps1': provision, 'bootstrap.cmd': bootstrap, 'VMSEED': data['name']}
