#!/usr/bin/env python3
"""Fresh-instance stages for the supported Ubuntu/systemd KVM host. Default/check never installs a VM."""
from __future__ import annotations
import argparse
import copy
import getpass
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

from profile import load, cpus
from render import GIB, runtime_files, domain_xml, seed_files
from base_image import inspect_manifest, materialize, inject_unattend

_registry_spec = importlib.util.spec_from_file_location(
    'virtops_instance_registry',
    Path(__file__).resolve().parents[1] / 'assets/runtime/instance_registry.py',
)
if _registry_spec is None or _registry_spec.loader is None:
    raise RuntimeError('instance registry helper is unavailable')
_registry = importlib.util.module_from_spec(_registry_spec)
_registry_spec.loader.exec_module(_registry)


def run(args, *, check=True, timeout=120):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout, env=dict(os.environ, LC_ALL='C'))
    if check and result.returncode: raise RuntimeError((result.stderr or result.stdout).strip())
    return result


def write_new(path, content, mode=0o644):
    path = Path(path)
    if path.exists() or path.is_symlink():
        if not path.is_symlink() and path.is_file() and path.read_text(encoding='utf-8-sig') == content: return
        raise RuntimeError('Existing destination differs; refusing overwrite: ' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8', newline='\n') as stream: stream.write(content)
    path.chmod(mode)


def unit_name(path, suffix='mount'):
    return run(['systemd-escape', '--path', '--suffix=' + suffix, str(path)]).stdout.strip()


def select_cpu(data, text, reserved=()):
    groups = {}
    for line in text.splitlines():
        if not line or line.startswith('#'): continue
        cpu, core, socket, online = line.split(',')[:4]
        if online.lower() in {'y', 'yes', '1'}: groups.setdefault((int(socket), int(core)), []).append(int(cpu))
    wanted = data['resources']['vcpus']
    reserved = set(reserved)
    requested = data['resources'].get('cpu_set')
    if requested:
        selected = set(cpus(requested))
        if selected & reserved:raise RuntimeError('Requested CPUs are reserved by another VM')
        chosen = [sorted(set(ids) & selected) for ids in groups.values() if set(ids) & selected]
        if set(n for pair in chosen for n in pair) != selected or len({len(ids) for ids in chosen})!=1:
            raise RuntimeError('CPU set must select a uniform number of online threads per physical core')
    else:
        lengths = {len(ids) for ids in groups.values()}
        if len(lengths) != 1: raise RuntimeError('Nonuniform CPU topology: choose an explicit compatible CPU set')
        threads = next(iter(lengths))
        if wanted % threads: raise RuntimeError('vCPU count must fit complete SMT sibling groups')
        chosen = sorted((sorted(ids) for ids in groups.values() if not (set(ids)&reserved)), key=min)[:wanted // threads]
    if sum(map(len, chosen)) != wanted or len({len(ids) for ids in chosen}) != 1: raise RuntimeError('Requested CPU count does not fit the host')
    result = copy.deepcopy(data)
    result['resources'].update(cpu_set=','.join(str(n) for n in sorted(n for ids in chosen for n in ids)), cpu_order=[n for ids in chosen for n in ids], threads_per_core=len(chosen[0]))
    result['resources']['amd'] = 'AuthenticAMD' in Path('/proc/cpuinfo').read_text() if sys.platform == 'linux' else False
    return result


def iso_image(data):
    """Read-only loop mount; WIM XML may be UTF-16 and is parsed as bytes."""
    with tempfile.TemporaryDirectory(prefix='virtops-iso-') as directory:
        run(['mount', '-o', 'loop,ro,nodev,nosuid,noexec', data['media']['windows_iso'], directory])
        try:
            source = next((Path(directory) / 'sources' / n for n in ['install.wim', 'install.esd'] if (Path(directory) / 'sources' / n).is_file()), None)
            if source is None: raise RuntimeError('ISO lacks sources/install.wim or install.esd')
            raw = subprocess.check_output(['wimlib-imagex', 'info', str(source), '--xml'], timeout=120)
            root = ET.fromstring(raw)
            image = next((n for n in root.findall('IMAGE') if int(n.get('INDEX', '0')) == data['windows']['image_index']), None)
            if image is None: raise RuntimeError('Selected WIM index is absent')
            edition = image.findtext('WINDOWS/EDITIONID', '').lower()
            if not edition.startswith(('professional', 'enterprise')): raise RuntimeError('Select a Pro/Enterprise image supporting incoming RDP')
            language = image.findtext('WINDOWS/LANGUAGES/DEFAULT', '')
            if language.lower() != data['windows']['language'].lower(): raise RuntimeError('Selected Windows language differs from the image')
            if image.findtext('WINDOWS/ARCH') != '9': raise RuntimeError('This implementation requires x64 Windows media')
            return {'name': image.findtext('NAME'), 'index': data['windows']['image_index'], 'edition': edition, 'language': language}
        finally: run(['umount', directory])


def media_info(path):
    source=Path(path)
    if not source.is_file(): raise RuntimeError('NEEDS_DOWNLOAD: '+str(path))
    digest=hashlib.sha256()
    with source.open('rb') as stream:
        for block in iter(lambda:stream.read(8*1024**2),b''):digest.update(block)
    return {'path':str(source),'bytes':source.stat().st_size,'sha256':digest.hexdigest()}


def host_facts(path):
    """Return the filesystem that will hold the instance root.

    The bounded VM store is created *inside* this filesystem.  Reporting both
    paths prevents an operator from confusing the 150 GiB instance store with
    free space on some other mounted volume.
    """
    result = run(['findmnt', '--noheadings', '--raw', '--target', str(path),
                  '--output', 'SOURCE,FSTYPE,TARGET'])
    fields = result.stdout.strip().split(None, 2)
    if len(fields) != 3:
        raise RuntimeError('Unable to identify the instance-root filesystem')
    stat = os.statvfs(path)
    return {
        'instance_root': str(path),
        'filesystem_source': fields[0],
        'filesystem_type': fields[1],
        'mountpoint': fields[2],
        'free_bytes': stat.f_bavail * stat.f_frsize,
    }


def verify_virtio(path):
    with tempfile.TemporaryDirectory(prefix='virtops-virtio-') as directory:
        run(['mount','-o','loop,ro,nodev,nosuid,noexec',path,directory])
        try:
            for name in ('NetKVM/w11/amd64/netkvm.inf','vioserial/w11/amd64/vioser.inf','guest-agent/qemu-ga-x86_64.msi'):
                if not (Path(directory)/name).is_file():raise RuntimeError('VirtIO ISO lacks '+name)
        finally:run(['umount',directory])


def preflight(data):
    tools = ['virsh', 'qemu-img', 'qemu-nbd', 'qemu-system-x86_64', 'swtpm', 'xorriso', 'wimlib-imagex', 'mkntfs', 'ntfscp', 'ntfslabel', 'iptables-nft', 'ip6tables-nft', 'iptables-nft-save', 'ip', 'ss', 'mount', 'findmnt', 'mkfs.ext4', 'systemd-escape', 'journalctl', 'modprobe', 'blockdev', 'fuser']
    missing = [name for name in tools if not shutil.which(name)]
    if missing: raise RuntimeError('NEEDS_PACKAGES: ' + ', '.join(missing))
    os_release = {}
    release_path = Path('/etc/os-release')
    if release_path.is_file():
        for line in release_path.read_text(encoding='utf-8', errors='replace').splitlines():
            if '=' in line:
                key, value = line.split('=', 1)
                os_release[key] = value.strip().strip('"')
    if os_release.get('ID') != 'ubuntu' or not os_release.get('VERSION_ID', '').startswith('24.04'):
        raise RuntimeError('SUPPORTED_HOST_ONLY: Ubuntu 24.04 with systemd and monolithic libvirtd is supported; inspect/adapt other distributions explicitly')
    if not Path('/run/systemd/system').is_dir():
        raise RuntimeError('SUPPORTED_HOST_ONLY: systemd is required for the generated units')
    if not Path('/dev/kvm').exists(): raise RuntimeError('/dev/kvm is absent; verify virtualization support before deployment')
    if not Path('/sys/fs/cgroup/cgroup.controllers').is_file():raise RuntimeError('This runtime requires cgroup v2')
    if run(['systemctl','is-active','libvirtd.service'],check=False).returncode:
        raise RuntimeError('This generator targets active monolithic libvirtd.service (Ubuntu 24.04); modular virtqemud needs explicit unit adaptation, do not switch a shared host automatically')
    data['gpu_verified']=verify_gpu(data)
    if run(['virsh', '--readonly', '-c', 'qemu:///system', 'dominfo', data['name']], check=False).returncode == 0: raise RuntimeError('VM name already exists; use operate/adopt, not fresh initialization')
    if data['uuid'] in run(['virsh', '--readonly', '-c', 'qemu:///system', 'list', '--all', '--uuid']).stdout: raise RuntimeError('VM UUID already exists')
    reserved=set()
    for _,item in _registry._records(Path(data['host_root'])/'registry'):
        if item['name']!=data['name'] and (item.get('resources') or {}).get('cpu_set'):
            reserved.update(cpus(item['resources']['cpu_set']))
    data = select_cpu(data, run(['lscpu', '-p=CPU,CORE,SOCKET,ONLINE']).stdout, reserved)
    memory = next(int(line.split()[1]) * 1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:'))
    if memory < data['limits']['start_free_gib'] * GIB: raise RuntimeError('Insufficient host MemAvailable for the approved cold-start budget; do not stop other users jobs')
    root = Path(data['root']); ancestor = root
    if root.exists() or root.is_symlink():raise RuntimeError('Fresh instance root already exists; inspect partial state or adopt an existing VM, never overwrite it')
    while not ancestor.exists(): ancestor = ancestor.parent
    stat = os.statvfs(ancestor); free = stat.f_bavail * stat.f_frsize
    data['host_facts'] = host_facts(ancestor)
    if free < (data['resources']['storage_gib'] + data['limits']['root_free_gib']) * GIB: raise RuntimeError('Insufficient space on the instance-root filesystem for a fully allocated store and approved host reserve')
    from_base = data.get('image', {}).get('kind') == 'base'
    media_keys = ('virtio_iso',) if from_base else ('windows_iso', 'virtio_iso')
    if from_base and not shutil.which('ntfscat'):
        raise RuntimeError('NEEDS_PACKAGES: ntfscat (normally supplied by ntfs-3g)')
    for key in media_keys:
        if not Path(data['media'][key]).is_file(): raise RuntimeError('NEEDS_DOWNLOAD: ' + key + ' at ' + data['media'][key])
    pairs=[('/usr/share/OVMF/OVMF_CODE_4M.ms.fd','/usr/share/OVMF/OVMF_VARS_4M.ms.fd'),
           ('/usr/share/OVMF/OVMF_CODE_4M.secboot.fd','/usr/share/OVMF/OVMF_VARS_4M.ms.fd'),
           ('/usr/share/OVMF/OVMF_CODE.ms.fd','/usr/share/OVMF/OVMF_VARS.ms.fd')]
    firmware=next((pair for pair in pairs if all(Path(p).is_file() for p in pair)),None)
    if not firmware:raise RuntimeError('NEEDS_FIRMWARE: no supported Secure Boot OVMF code + Microsoft-enrolled VARS pair; inspect distro package, do not substitute unsigned VARS')
    data['ovmf_code'],data['ovmf_vars']=firmware
    net = data['network']
    addresses=json.loads(run(['ip','-j','-4','addr','show']).stdout)
    if net['publish_ip'] not in {a['local'] for item in addresses for a in item.get('addr_info',[])}:
        raise RuntimeError('Publish IP is not assigned on this host')
    for path in [Path('/etc/libvirt/hooks/qemu.d')/('50-'+data['name']+'-guard'),
                 Path('/var/lib/libvirt/swtpm')/data['uuid'],
                 *[Path('/etc/systemd/system')/(data['name']+suffix) for suffix in ('-bridge.service','-firewall.service','-log-collector.service','-capguard.service')],
                 Path('/etc/systemd/system')/unit_name('/machine/'+data['name'],'slice'),
                 Path('/etc/systemd/system')/unit_name(root/'vol')]:
        if path.exists() or path.is_symlink():raise RuntimeError('Fresh instance destination already exists: '+str(path))
    if run(['ip', 'link', 'show', 'dev', net['bridge']], check=False).returncode == 0: raise RuntimeError('Bridge name already exists; choose a distinct new bridge')
    for route in json.loads(run(['ip','-j','-4','route','show','table','all']).stdout):
        if route.get('dst','default') != 'default' and ipaddress.ip_network(route['dst'],strict=False).overlaps(ipaddress.ip_network(net['subnet'])): raise RuntimeError('Dedicated subnet overlaps an existing host route')
    rules = run(['iptables-nft-save', '-t', 'nat']).stdout
    listeners = run(['ss','-ltn']).stdout
    for port in [net['rdp_port'],net['winrm_port']]:
        if re.search(r':' + str(port) + r'\s', listeners) or re.search(r'--dport ' + str(port) + r'\b', rules): raise RuntimeError('Published port already used by a listener or NAT rule: ' + str(port))
    if from_base:
        base = inspect_manifest(data['image']['manifest'])
        if base['windows']['language'].lower() != data['windows']['language'].lower():
            raise RuntimeError('Base Windows language differs from the requested instance')
        if base['virtual_size'] != data['resources']['storage_gib']*GIB:
            raise RuntimeError('Select the base virtual disk size as storage_gib; disk resizing is a separate operation')
        data['image_verified'] = base
    else:
        data['image_verified'] = iso_image(data)
    verify_virtio(data['media']['virtio_iso'])
    data['media_verified']={key:media_info(data['media'][key]) for key in media_keys}
    return data


def verify_gpu(data):
    gpu=data.get('gpu',{'mode':'none'});mode=gpu['mode']
    if mode=='none':return {'mode':'none'}
    if mode=='vgpu':raise RuntimeError('NEEDS_GPU_INTEGRATION: select a vendor-supported vGPU profile, matching host/guest drivers and licensing before deployment; no silent CPU fallback')
    selected=set(gpu['pci_devices']);groups={};classes={}
    for pci in selected:
        path=Path('/sys/bus/pci/devices')/pci
        if not path.exists():raise RuntimeError('Selected PCI device absent: '+pci)
        if not (path/'driver').is_symlink() or (path/'driver').resolve().name!='vfio-pci':
            raise RuntimeError('NEEDS_GPU_HOST_PLAN: '+pci+' is not already bound to vfio-pci; do not detach a host GPU or stop its workloads automatically')
        if not (path/'iommu_group').is_symlink():raise RuntimeError('PCI device lacks hardware IOMMU group: '+pci)
        group=(path/'iommu_group').resolve();members={p.name for p in (group/'devices').iterdir()}
        if not members<=selected:raise RuntimeError('Select the complete isolated IOMMU group; missing '+','.join(sorted(members-selected)))
        if run(['fuser',str(Path('/dev/vfio')/group.name)],check=False).returncode==0:raise RuntimeError('IOMMU group is open by an existing workload: '+group.name)
        groups[group.name]=sorted(members)
        classes[pci]=(path/'class').read_text().strip()
    gpu_slots={pci.rsplit('.',1)[0] for pci,kind in classes.items() if kind.startswith('0x03')}
    if not gpu_slots:raise RuntimeError('Selected PCI functions do not include a graphics/compute display controller')
    for pci,kind in classes.items():
        if pci.rsplit('.',1)[0] not in gpu_slots or not kind.startswith(('0x03','0x04','0x0c')):
            raise RuntimeError('IOMMU group includes a non-GPU function requiring a separate hardware plan: '+pci)
    names=run(['virsh','--readonly','-c','qemu:///system','list','--all','--name']).stdout.split()
    for name in names:
        xml=ET.fromstring(run(['virsh','--readonly','-c','qemu:///system','dumpxml',name]).stdout)
        for address in xml.findall("devices/hostdev[@type='pci']/source/address"):
            actual='%04x:%02x:%02x.%x'%tuple(int(address.get(k,'0'),16) for k in ('domain','bus','slot','function'))
            if actual in selected:raise RuntimeError('GPU function is assigned to an existing VM: '+name)
    return {'mode':mode,'iommu_groups':groups,'binding':'vfio-pci','host_driver_changes':False}


def units_and_hook(data, store_uuid, inode):
    name, root = data['name'], data['root']; lib = root + '/scripts/lib'
    volume = root + '/vol'; tpm = '/var/lib/libvirt/swtpm/' + data['uuid']
    mount_unit, tpm_unit = unit_name(volume), unit_name(tpm)
    slice_unit = unit_name('/machine/' + name, 'slice')
    units = {
        mount_unit: f'[Unit]\nDescription=Bounded store for {name}\nBefore={tpm_unit} {name}-capguard.service\n\n[Mount]\nWhat={root}/.storage/{name}.store\nWhere={volume}\nType=ext4\nOptions=loop,nodiscard,nodev,nosuid,noexec\n\n[Install]\nWantedBy=multi-user.target\n',
        tpm_unit: f'[Unit]\nDescription=Bounded TPM for {name}\nRequires={mount_unit}\nAfter={mount_unit}\nBefore=libvirtd.service virtqemud.service\n\n[Mount]\nWhat={volume}/state/tpm\nWhere={tpm}\nType=none\nOptions=bind,nodev,nosuid,noexec\n\n[Install]\nWantedBy=multi-user.target\n',
        slice_unit: f"[Slice]\nAllowedCPUs={data['resources']['cpu_set']}\nCPUQuota={data['resources']['vcpus']*100}%\nCPUQuotaPeriodSec=100ms\nMemoryMax={data['resources']['host_limit_gib']}G\nMemorySwapMax=0\n",
        name+'-bridge.service': f'[Unit]\nDescription=Dedicated bridge for {name}\nAfter=network.target\nBefore={name}-firewall.service\n\n[Service]\nType=oneshot\nExecStart=/usr/bin/python3 {lib}/{name}_bridge.py\nRemainAfterExit=yes\n\n[Install]\nWantedBy=multi-user.target\n',
        name+'-firewall.service': f'[Unit]\nDescription=Isolation for {name}\nRequires={name}-bridge.service\nAfter={name}-bridge.service libvirtd.service\nPartOf=libvirtd.service\n\n[Service]\nType=oneshot\nExecStart=/usr/bin/python3 {lib}/{name}_network.py apply --backend iptables-nft\nRemainAfterExit=yes\n\n[Install]\nWantedBy=multi-user.target\n',
        name+'-log-collector.service': f'[Unit]\nDescription=Firewall drop log for {name}\nAfter=systemd-journald.service {mount_unit}\nRequiresMountsFor={volume}\n\n[Service]\nExecStart=/usr/bin/python3 {lib}/{name}_log_collector.py run\nRestart=always\nRestartSec=2\nUMask=0077\nNoNewPrivileges=yes\nProtectHome=read-only\nProtectSystem=strict\nReadWritePaths={volume}/logs\n\n[Install]\nWantedBy=multi-user.target\n',
        name+'-capguard.service': f'[Unit]\nDescription=Capacity and prerequisite guard for {name}\nRequires={name}-firewall.service {name}-log-collector.service\nAfter={name}-firewall.service {name}-log-collector.service\n\n[Service]\nExecStart={lib}/vmctl.py watch\nRestart=always\nRestartSec=2\nUMask=0077\nNoNewPrivileges=yes\nProtectHome=read-only\nProtectSystem=strict\nReadWritePaths=/run/lock {volume}/logs\n\n[Install]\nWantedBy=multi-user.target\n',
    }
    hook = f'''#!/bin/sh
set -eu
if [ "$1" != "{name}" ] || [ "$2" != prepare ] || [ "$3" != begin ]; then exit 0; fi
mountpoint -q {volume}
/usr/bin/python3 {lib}/{name}_store_guard.py
[ "$(findmnt -rn --mountpoint {volume} -o UUID)" = "{store_uuid}" ]
[ "$(stat -c '%i' {root}/.storage/{name}.store)" = "{inode}" ]
[ "$(stat -c '%d:%i' {volume}/state/tpm)" = "$(stat -c '%d:%i' {tpm})" ]
/usr/bin/python3 {lib}/{name}_network.py verify --backend iptables-nft >/dev/null
systemctl is-active --quiet {shlex.quote(slice_unit)}
awk '/MemAvailable:/ {{ if ($2 < {data['limits']['start_free_gib']*1024**2}) exit 1; found=1 }} END {{ if (!found) exit 1 }}' /proc/meminfo
logger -t {name}-guard 'prepare checks passed'
'''
    return units, hook


def storage(data):
    import pwd
    root = Path(data['root'])
    if root.exists() or root.is_symlink(): raise RuntimeError('Instance root already exists; inspect partial state, never overwrite or format it')
    for parent in root.parents:
        if parent.is_symlink(): raise RuntimeError('Instance parent is a symlink; use its explicit resolved path')
        if parent.exists() and (parent.stat().st_uid != 0 or parent.stat().st_mode & 0o022):
            raise RuntimeError('Instance parent must be root-owned and non-writable by other users: '+str(parent))
    root.mkdir(parents=True, mode=0o755); (root/'.storage').mkdir(mode=0o700); (root/'vol').mkdir(mode=0o755)
    store = root/'.storage'/(data['name']+'.store')
    with store.open('xb') as f: os.posix_fallocate(f.fileno(),0,data['resources']['storage_gib']*GIB);os.fsync(f.fileno())
    store.chmod(0o600);run(['mkfs.ext4','-q','-m','0','-E','nodiscard',str(store)],timeout=600)
    run(['mount','-o','loop,nodiscard,nodev,nosuid,noexec',str(store),str(root/'vol')])
    if not os.path.ismount(root/'vol'): raise RuntimeError('Exact bounded mount did not appear')
    fs_uuid=run(['findmnt','-rn','--mountpoint',str(root/'vol'),'-o','UUID']).stdout.strip();inode=store.stat().st_ino
    qemu=pwd.getpwnam('libvirt-qemu');swtpm=pwd.getpwnam('swtpm')
    for folder,mode in [('state',0o711),('state/tpm',0o711),('state/tpm/tpm2',0o700),('state/console',0o700),('state/private',0o700),('recovery',0o700),('logs',0o750)]:
        p=root/'vol'/folder;p.mkdir(mode=mode);p.chmod(mode)
    os.chown(root/'vol/state/tpm/tpm2',swtpm.pw_uid,swtpm.pw_gid);os.chown(root/'vol/state/console',qemu.pw_uid,qemu.pw_gid)
    nvram=root/'vol/state'/(data['name']+'_VARS.fd');shutil.copyfile(data['ovmf_vars'],nvram);os.chown(nvram,qemu.pw_uid,qemu.pw_gid);nvram.chmod(0o600)
    disk=root/'vol/current.qcow2'
    if data.get('image', {}).get('kind') == 'base':
        materialize(data,disk)
    else:
        run(['qemu-img','create','-f','qcow2','-o','preallocation=metadata',str(disk),str(data['resources']['storage_gib']*GIB)])
    os.chown(disk,qemu.pw_uid,qemu.pw_gid);disk.chmod(0o660)
    tpm=Path('/var/lib/libvirt/swtpm')/data['uuid'];tpm.parent.mkdir(parents=True,exist_ok=True);tpm.mkdir(mode=0o711);run(['mount','--bind',str(root/'vol/state/tpm'),str(tpm)])
    (root/'logs').symlink_to(root/'vol/logs',target_is_directory=True)
    rendered=runtime_files(data,fs_uuid,inode)
    for relative,text in rendered.items(): write_new(root/relative,text,0o755 if relative.endswith(('.py','.sh')) else 0o644)
    write_new(root/'vol/state/private/runtime-files.json',json.dumps({'schema_version':1,'name':data['name'],'uuid':data['uuid'],
        'sha256':{relative:hashlib.sha256((root/relative).read_bytes()).hexdigest() for relative in rendered}},sort_keys=True)+'\n',0o600)
    write_new(root/'vol'/(data['name']+'.xml'),domain_xml(data,installing=False),0o600)
    write_new(root/'vol/state/install.xml',domain_xml(data,installing=True),0o600)
    units,hook=units_and_hook(data,fs_uuid,inode)
    for name,text in units.items(): write_new(Path('/etc/systemd/system')/name,text)
    write_new(Path('/etc/libvirt/hooks/qemu.d')/('50-'+data['name']+'-guard'),hook,0o755)
    data['store_identity']={'uuid':fs_uuid,'inode':inode}
    write_new(root/'instance.json',json.dumps(data,indent=2)+'\n',0o600)
    run(['systemctl','daemon-reload']);run(['systemctl','enable',unit_name(root/'vol'),unit_name(tpm)])
    run(['python3',str(root/'scripts/lib'/(data['name']+'_store_guard.py'))])
    return data


def reserve_registry(data):
    """Claim host-wide identity before creating any instance storage.

    A failed storage stage intentionally leaves the reservation in place so a
    later run cannot silently steal the same ports or subnet from a partial
    deployment. Cleanup remains an explicit, inspected operation.
    """
    root = Path(data['root'])
    host_root = data.get('host_root')
    if not host_root:
        host_root = str(root.parent) if root.name == data['name'] else str(root)
        data['host_root'] = host_root
    record = _registry.claim(data)
    data['registry'] = record
    return record


def install_host_catalog(data):
    """Install the shared catalog helper alongside the instance scripts.

    This is intentionally fail-closed: an existing foreign status/catalog
    file is never overwritten during a new VM deployment.
    """
    host_root = Path(data['host_root'])
    if not host_root.is_dir() or host_root.is_symlink():
        raise RuntimeError('host_root must already be an existing non-symlink directory')
    current = host_root
    while True:
        stat = current.stat()
        if current.is_symlink() or stat.st_uid != 0 or stat.st_mode & 0o022:
            raise RuntimeError('host catalog ancestor must be root-owned and non-writable: ' + str(current))
        if current == current.parent:
            break
        current = current.parent
    scripts = host_root / 'scripts'
    library = scripts / 'lib'
    for path in (scripts, library):
        if path.exists() and (path.is_symlink() or not path.is_dir()):
            raise RuntimeError('host catalog destination is unsafe: ' + str(path))
    scripts.mkdir(mode=0o755, exist_ok=True)
    library.mkdir(mode=0o755, exist_ok=True)
    for path in (scripts, library):
        stat = path.stat()
        if stat.st_uid != 0 or stat.st_mode & 0o022:
            raise RuntimeError('host catalog directory must be root-owned and non-writable: ' + str(path))
    source = Path(__file__).resolve().parents[1] / 'assets/runtime/instance_registry.py'
    write_new(library / 'instance_registry.py', source.read_text(encoding='utf-8'), 0o755)
    # A shared host catalog has no per-instance vm.sh/vmctl.py.  Its status
    # entrypoint therefore invokes the neutral registry helper directly.
    legacy = (library / 'vmctl.py').is_file()
    status_path = scripts / 'status.sh'
    if not legacy:
        write_new(status_path, shared_status_wrapper(), 0o755)


def shared_status_wrapper():
    return '''#!/bin/sh
set -eu
script_path=$(readlink -f -- "$0")
script_dir=$(CDPATH= cd -- "$(dirname -- "$script_path")" && pwd -P)
host_root=$(dirname -- "$script_dir")
case "$#" in
  0) set -- list ;;
  1) [ "$1" = "--all" ] || { echo "usage: status.sh [--all]" >&2; exit 2; }; set -- list ;;
  *) echo "usage: status.sh [--all]" >&2; exit 2 ;;
esac
exec python3 "$script_dir/lib/instance_registry.py" --host-root "$host_root" "$@"
'''


def seed(data):
    root=Path(data['root']);private=root/'vol/state/private';credential=private/'credentials.json'
    if credential.exists(): raise RuntimeError('Credentials already exist; never replace an existing Windows password by rerunning seed')
    mode=data['credentials']['mode']
    if mode=='generate': password='Vm9!'+secrets.token_urlsafe(24)
    elif mode=='prompt':
        if not sys.stdin.isatty(): raise RuntimeError('NEEDS_USER_INPUT: password requires a private terminal prompt')
        password=getpass.getpass('New Windows password: ')
        if password!=getpass.getpass('Repeat Windows password: '): raise RuntimeError('Passwords do not match')
    elif mode=='file':
        path=Path(data['credentials']['secret_file']);st=path.stat()
        if path.is_symlink() or st.st_uid!=0 or st.st_mode&0o077: raise RuntimeError('Secret file must be root-owned 0600')
        password=json.loads(path.read_text())['password']
    else: raise RuntimeError('Choose a password mode for fresh initialization')
    if not isinstance(password,str) or not password:
        raise RuntimeError('A nonempty Windows password is required; the selected Windows policy is authoritative')
    write_new(credential,json.dumps({data['windows']['admin_user']:password}),0o600)
    with tempfile.TemporaryDirectory(prefix='seed-',dir=private) as directory:
        for name,text in seed_files(data,password).items():
            Path(directory,name).write_text(text,encoding='utf-8-sig' if name.endswith(('.ps1','.xml')) else 'utf-8',newline='')
        if data.get('image', {}).get('kind') == 'base':
            inject_unattend(data,Path(directory)/'Autounattend.xml')
        output=root/'vol/state/autounattend.iso'
        if output.exists():raise RuntimeError('Seed ISO already exists')
        run(['xorriso','-as','mkisofs','-J','-r','-V','VMSEED','-o',str(output),directory])
        import pwd
        os.chown(output,0,pwd.getpwnam('libvirt-qemu').pw_gid);output.chmod(0o640)


def needs_boot_key(data):
    """Only the fresh-installer path boots from removable installation media.

    A template derived instance starts from the EFI system partition of its own
    disk, so it never needs the OVMF keypress.
    """
    return bool(data.get('media',{}).get('windows_iso'))


def press_boot_key(name, seconds=90, interval=1.5):
    """Answer the OVMF prompt that asks for a keypress on installation media.

    OVMF treats a CD-ROM as removable media and waits for a keypress before it
    boots it. Without the keypress the builder stops at "No bootable option or
    device was found" and the unattended installation never starts.
    """
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        run(['virsh','send-key',name,'KEY_ENTER'],check=False,timeout=15)
        time.sleep(interval)


def activate(data):
    root=Path(data['root']);name=data['name']
    if not (root/'vol/state/autounattend.iso').is_file(): raise RuntimeError('Run the seed stage first')
    verify_gpu(data)
    if data.get('image', {}).get('kind') == 'base':
        if inspect_manifest(data['image']['manifest']) != data['image_verified']:
            raise RuntimeError('Published base image changed since storage preparation')
    for key in data['media_verified']:
        if media_info(data['media'][key])!=data['media_verified'][key]:raise RuntimeError('Installation ISO changed since preflight: '+key)
    if run(['virsh','--readonly','-c','qemu:///system','dominfo',name],check=False).returncode==0: raise RuntimeError('VM already defined; inspect and use existing management, not activate again')
    guests=Path('/etc/default/libvirt-guests');text=guests.read_text() if guests.exists() else ''
    if not re.search(r'(?m)^ON_SHUTDOWN=["\x27]?shutdown["\x27]?\s*$',text):
        others=run(['virsh','--readonly','-c','qemu:///system','list','--all','--name']).stdout.strip()
        if others: raise RuntimeError('NEEDS_USER_INPUT: other VMs exist and managed-save shutdown policy needs review; no global policy changed')
        write_new(root/'vol/state/private/libvirt-guests.original',text,0o600)
        changed=re.sub(r'(?m)^ON_SHUTDOWN=.*$', 'ON_SHUTDOWN=shutdown',text)
        if not re.search(r'(?m)^ON_SHUTDOWN=',changed):changed+='\nON_SHUTDOWN=shutdown\n'
        guests.write_text(changed)
    run(['systemctl','start',unit_name('/machine/'+name,'slice')])
    run(['systemctl','enable','--now',name+'-bridge.service',name+'-firewall.service',name+'-log-collector.service',name+'-capguard.service'])
    if Path('/proc/sys/net/ipv4/ip_forward').read_text().strip()!='1':
        write_new(Path('/etc/sysctl.d')/('90-'+name+'-forward.conf'),'net.ipv4.ip_forward=1\n')
        run(['sysctl','-w','net.ipv4.ip_forward=1'])
    run(['virsh','define','--validate',str(root/'vol/state/install.xml')])
    run([str(root/'scripts/start.sh')],timeout=150)
    if needs_boot_key(data):
        # Only the fresh-installer path boots from removable media; a template
        # derived instance starts from its own EFI system partition.
        press_boot_key(name)


def finish(data):
    import importlib.util
    root=Path(data['root']);name=data['name'];path=root/'scripts/lib'/(name+'_qga.py')
    spec=importlib.util.spec_from_file_location('qga',path);qga=importlib.util.module_from_spec(spec);spec.loader.exec_module(qga)
    qga.call('guest-ping')
    status=json.loads(qga.powershell("@{RDP=(Get-Service TermService).Status.ToString();WinRM=(Get-Service WinRM).Status.ToString();Provisioned=(Test-Path 'C:\\Windows\\Temp\\"+name+"-provision.success')}|ConvertTo-Json -Compress"))
    if status!={'RDP':'Running','WinRM':'Running','Provisioned':True}: raise RuntimeError('Guest configuration not ready; inspect Windows provisioning log through QGA')
    credential = root/'vol/state/private/credentials.json'
    if credential.is_symlink() or not credential.is_file() or credential.stat().st_uid != 0 or credential.stat().st_mode & 0o077:
        raise RuntimeError('Initial credentials.json must be root-owned and mode 0600')
    values = json.loads(credential.read_text(encoding='utf-8'))
    if not isinstance(values, dict) or set(values) != {data['windows']['admin_user']} or not isinstance(values[data['windows']['admin_user']], str):
        raise RuntimeError('Initial credentials.json does not match the selected administrator')
    owner_path = root/'vol/state/private/owner.json'
    owner_payload = {'schema_version': 1, 'name': name, 'uuid': data['uuid'],
                     'admin_user': data['windows']['admin_user'],
                     'password': values[data['windows']['admin_user']], 'data_policy': 'preserve'}
    write_new(owner_path, json.dumps(owner_payload, sort_keys=True)+'\n', 0o600)
    if owner_path.stat().st_mode & 0o077 or owner_path.stat().st_uid != 0:
        raise RuntimeError('Initial owner.json was not created root-only')
    qga.powershell(r"$w='HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon';Remove-ItemProperty $w -Name DefaultPassword -ErrorAction SilentlyContinue;Set-ItemProperty $w -Name AutoAdminLogon -Value '0';Get-ChildItem C:\Windows\Panther,C:\Windows\Panther\Unattend -Filter '*unattend*.xml' -ErrorAction SilentlyContinue|Remove-Item -Force;Remove-Item C:\Windows\Setup\Scripts\remote-windows-provision.ps1 -ErrorAction SilentlyContinue")
    run([str(root/'scripts/stop.sh')],timeout=150)
    run(['virsh','define','--validate',str(root/'vol'/(name+'.xml'))])
    (root/'vol/state/autounattend.iso').unlink()
    run([str(root/'scripts/start.sh')],timeout=150)


def verify_saved(requested,saved):
    for key in ('name','uuid','root','network','windows','media','limits','gpu','image','template_builder'):
        if requested.get(key)!=saved.get(key):raise RuntimeError('Profile changed after storage stage: '+key+'; inspect the immutable instance, do not overwrite it')
    for key,value in requested['resources'].items():
        if key=='cpu_set':
            if value and set(cpus(value))!=set(cpus(saved['resources'][key])):raise RuntimeError('CPU selection changed after storage')
        elif saved['resources'].get(key)!=value:raise RuntimeError('Resource changed after storage: '+key)
    for key in ('mode','secret_file'):
        if requested['credentials'].get(key)!=saved['credentials'].get(key):raise RuntimeError('Credential setup mode changed after storage')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--profile',required=True,type=Path)
    parser.add_argument('--stage',choices=['check','storage','seed','activate','finish'],default='check');parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    data=load(args.profile,new=True)
    if os.name!='posix' or sys.platform!='linux': raise RuntimeError('Run prepare_host.py on the Linux target, not the control PC')
    if os.geteuid()!=0: raise RuntimeError('Host preparation needs root; check mounts are read-only and automatically unmounted')
    if args.stage=='check' or args.stage=='storage':
        data=preflight(data)
        if args.stage=='check' or not args.apply:
            print(json.dumps({'status':'plan_ready','name':data['name'],'resources':data['resources'],'gpu':data['gpu_verified'],'image':data['image_verified'],'media':data['media_verified'],'host_facts':data['host_facts'],'root':data['root'],'next':'storage --apply within the approved deployment plan'},indent=2));return 0
        reserve_registry(data)
        install_host_catalog(data)
        storage(data)
    else:
        saved=json.loads((Path(data['root'])/'instance.json').read_text())
        verify_saved(data,saved)
        data=saved
        if not args.apply: print(json.dumps({'status':'plan_ready','stage':args.stage,'name':data['name'],'next':'add --apply only within approved initialization'}));return 0
        if args.stage=='seed':seed(data)
        elif args.stage=='activate':activate(data)
        else:finish(data)
    print(json.dumps({'status':'completed','stage':args.stage,'name':data['name'],'next':'continue only after stage acceptance'}));return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (OSError,RuntimeError,ValueError,KeyError,subprocess.SubprocessError,ET.ParseError) as exc:
        print(json.dumps({'status':'blocked','reason':str(exc)},ensure_ascii=False));raise SystemExit(2)
