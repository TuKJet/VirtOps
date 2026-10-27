#!/usr/bin/env python3
"""Delete only one verified instance slot; never a shared/legacy host root."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
import importlib.util
try:
    import fcntl
except ImportError:
    fcntl=None

class DestroyError(RuntimeError):pass


def _published_services(network):
    path = Path(__file__).resolve().parents[1] / 'assets/runtime/service_policy.py'
    spec = importlib.util.spec_from_file_location('virtops_destroy_service_policy', path)
    if spec is None or spec.loader is None:
        raise DestroyError('published service policy is unavailable')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    try:
        return module.normalize(network)
    except (KeyError, TypeError, ValueError) as exc:
        raise DestroyError(f'invalid published service policy: {exc}') from exc

def run(argv, *, timeout=120, check=True):
    result=subprocess.run(argv,text=True,capture_output=True,timeout=timeout,env=dict(os.environ,LC_ALL='C'))
    if check and result.returncode:raise DestroyError((result.stderr or result.stdout).strip() or 'command failed')
    return result

def _instance_root(profile):
    root=Path(profile['root']);host=Path(profile['host_root'])
    if root==host:raise DestroyError('legacy root == host_root is blocked; shared catalog is protected')
    if not root.is_absolute() or '..' in root.parts or root!=host/'instances'/profile['name']:
        raise DestroyError('instance root must be exactly host_root/instances/<name>')
    for p in (root,*root.parents):
        if p.is_symlink():raise DestroyError('symlinked instance ancestor')
        if os.name=='posix' and p.exists() and (p.stat().st_uid!=0 or p.stat().st_mode&0o022):raise DestroyError('untrusted instance ancestor')
    return root,host

def read_json(path):
    p=Path(path)
    if p.is_symlink() or not p.is_file():raise DestroyError('missing or unsafe metadata: '+str(p))
    return json.loads(p.read_text(encoding='utf-8-sig'))

def save(path,value):
    p=Path(path);tmp=p.with_name(p.name+'.tmp')
    fd=os.open(tmp,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as stream:json.dump(value,stream,indent=2);stream.flush();os.fsync(stream.fileno())
    os.replace(tmp,p)

def inventory(profile, *, command=run):
    root,host=_instance_root(profile)
    domain=command(['virsh','--readonly','-c','qemu:///system','dumpxml',profile['name']],check=False)
    state=command(['virsh','--readonly','-c','qemu:///system','domstate',profile['name']],check=False)
    return {'target':profile['name'],'domain_xml':domain.stdout if not domain.returncode else None,
            'state':state.stdout.strip().lower() if not state.returncode else 'undefined',
            'root':str(root),'host_root':str(host)}

def validate_identity(profile, inv):
    root,host=_instance_root(profile);actual=read_json(root/'instance.json')
    for key in ('name','uuid','root','host_root'):
        if actual.get(key)!=profile.get(key):raise DestroyError('instance configuration identity mismatch: '+key)
    row=read_json(host/'registry'/(profile['name']+'.json'))
    for key in ('name','uuid','root'):
        if row.get(key)!=profile[key]:raise DestroyError('registry identity mismatch: '+key)
    for key in ('bridge','subnet','rdp_port','winrm_port'):
        if key in profile['network'] and row.get(key)!=profile['network'][key]:raise DestroyError('registry network identity mismatch')
    if row.get('published_services', []) != _published_services(profile['network']):
        raise DestroyError('registry published service identity mismatch')
    if inv['domain_xml']:
        xml=ET.fromstring(inv['domain_xml'])
        if xml.findtext('name')!=profile['name'] or xml.findtext('uuid')!=profile['uuid']:raise DestroyError('domain identity mismatch')
        sources=xml.findall("devices/disk[@device='disk']/source")
        if len(sources)!=1 or sources[0].get('file')!=str(root/'vol/current.qcow2'):raise DestroyError('domain system disk differs')
        nvram=xml.findtext('os/nvram')
        if nvram and nvram!=str(root/'vol/state'/(profile['name']+'_VARS.fd')):raise DestroyError('domain NVRAM differs')
        iface=xml.find('devices/interface/source')
        if iface is not None and iface.get('bridge')!=profile['network']['bridge']:raise DestroyError('domain bridge differs')
    for marker in ('owner-transition.json','c-reset-in-progress.json'):
        if (root/'vol/recovery'/marker).exists():raise DestroyError('instance recovery is pending')
    if (root/'vol/state/private/account-transition.json').exists():raise DestroyError('account transition is pending')
    return {'name':profile['name'],'uuid':profile['uuid'],'root':str(root),'host_root':str(host),'store_identity':actual.get('store_identity')}

def verify_references(profile, command):
    root,host=_instance_root(profile)
    for path in (host/'registry').glob('*.json'):
        row=read_json(path)
        if row['name']==profile['name']:continue
        other=Path(row['root'])
        # A legacy VM can share host_root with the catalog. Its private vol is
        # a sibling of instances/, so that shared ancestor is not ownership of
        # every newly created child slot.
        if other==root or (other!=host and root.is_relative_to(other)) or other.is_relative_to(root) or row['uuid']==profile['uuid']:
            raise DestroyError('another registry entry references this instance')
    names=command(['virsh','--readonly','-c','qemu:///system','list','--all','--name']).stdout.split()
    for name in names:
        if name==profile['name']:continue
        text=command(['virsh','--readonly','-c','qemu:///system','dumpxml',name]).stdout
        if str(root)+'/' in text or profile['uuid'] in text:raise DestroyError('another domain references this instance storage/state')
    for path in root.rglob('*'):
        if path.is_symlink():
            if path!=root/'logs' or os.readlink(path)!=str(root/'vol/logs'):
                raise DestroyError('unrecognized symlink in instance root')

def owned_units(profile, command):
    root=Path(profile['root']);name=profile['name']
    def escape(path,suffix):return command(['systemd-escape','--path','--suffix='+suffix,str(path)]).stdout.strip()
    units=[name+s for s in ('-firewall.service','-bridge.service','-log-collector.service','-capguard.service')]
    units += [escape(root/'vol','mount'),escape(Path('/var/lib/libvirt/swtpm')/profile['uuid'],'mount'),escape('/machine/'+name,'slice')]
    if any(not u or '/' in u or '..' in u for u in units):raise DestroyError('invalid owned unit identity')
    return units

def verify_store(profile, command):
    root=Path(profile['root']);vol=root/'vol';store=root/'.storage'/(profile['name']+'.store')
    expected=read_json(root/'instance.json').get('store_identity') or {}
    if not expected.get('uuid') or not expected.get('inode') or store.stat().st_ino!=expected['inode']:
        raise DestroyError('bounded backing store identity mismatch')
    mount=command(['findmnt','-rn','--mountpoint',str(vol),'-o','SOURCE,UUID,FSTYPE']).stdout.split()
    if len(mount)!=3 or mount[1]!=expected['uuid'] or mount[2]!='ext4' or not mount[0].startswith('/dev/loop'):
        raise DestroyError('bounded filesystem mount identity mismatch')
    backing=command(['losetup','--noheadings','--output','BACK-FILE',mount[0]]).stdout.strip()
    if Path(backing).resolve()!=store.resolve():raise DestroyError('loop backing file differs')
    tpm=Path('/var/lib/libvirt/swtpm')/profile['uuid']
    command(['findmnt','-rn','--mountpoint',str(tpm)])
    a=(vol/'state/tpm').stat();b=tpm.stat()
    if (a.st_dev,a.st_ino)!=(b.st_dev,b.st_ino):raise DestroyError('TPM bind identity differs')
    manifest=read_json(vol/'state/private/runtime-files.json')
    if manifest.get('uuid')!=profile['uuid'] or not manifest.get('sha256'):raise DestroyError('missing managed runtime manifest')
    for relative,digest in manifest['sha256'].items():
        path=root/relative
        if Path(relative).is_absolute() or '..' in Path(relative).parts or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            raise DestroyError('managed runtime manifest changed')
    nested=command(['findmnt','-rn','--submounts','--target',str(vol),'-o','TARGET']).stdout.splitlines()
    if any(x.strip()!=str(vol) for x in nested):raise DestroyError('foreign nested mount inside instance volume')
    return mount[0]

@contextmanager
def locked(profile):
    if fcntl is None:yield;return
    root,host=_instance_root(profile)
    for path in (host/'registry/.admission.lock',Path('/run/lock')/(profile['name']+'.lock')):
        if path.is_symlink():raise DestroyError('unsafe lock')
    with (Path('/run/lock')/(profile['name']+'.lock')).open('a+') as lifecycle, (host/'registry/.admission.lock').open('a+') as admission, (host/'registry/.registry.lock').open('a+') as registry:
        fcntl.flock(lifecycle,fcntl.LOCK_EX);fcntl.flock(admission,fcntl.LOCK_EX)
        fcntl.flock(registry,fcntl.LOCK_EX)
        yield

def _destroy(profile, *, apply=False, discard=False, command=run):
    inv=inventory(profile,command=command);identity=validate_identity(profile,inv)
    root,host=_instance_root(profile);verify_references(profile,command)
    # Real host operations require all mount/hash ownership proof. Tests replace
    # this host-specific seam with a virtual mounted tree, never global paths.
    loop=verify_store(profile,command)
    units=owned_units(profile,command)
    if not apply:return {'status':'dry-run','identity':'validated','inventory':inv,'would_delete':True}
    if not discard:raise DestroyError('full deletion requires --apply --discard')
    if inv['state'] not in {'running','shut off','shutoff'}:raise DestroyError('VM is not in a safe state; no forced power-off')
    operations=host/'operations';operations.mkdir(mode=0o700,exist_ok=True)
    journal=operations/('destroy-'+str(uuid.uuid4())+'.json');record={'stage':'validated','target':identity,'evidence':{'units':units,'loop':loop},'needs_user_input':False}
    def phase(stage):record['stage']=stage;save(journal,record)
    phase('validated')
    if inv['state']=='running':
        command(['virsh','-c','qemu:///system','shutdown',profile['name'],'--mode','agent,acpi'])
        deadline=time.monotonic()+180
        while command(['virsh','--readonly','-c','qemu:///system','domstate',profile['name']]).stdout.strip() not in {'shut off','shutoff'}:
            if time.monotonic()>=deadline:raise DestroyError('guest did not stop; journal retained, no forced shutdown')
            time.sleep(2)
    phase('stopped')
    # Stop users of the private filesystem before testing fuser or removing the
    # instance network. No shared system service is stopped or restarted.
    for unit in units:
        if unit.endswith('.service'):command(['systemctl','stop',unit])
        command(['systemctl','disable',unit])
    for path in (root/'vol',root/'.storage'/(profile['name']+'.store')):
        args=['fuser']+(['-m'] if path==root/'vol' else [])+['--',str(path)]
        opened=command(args,check=False)
        if opened.returncode!=1 or opened.stdout.strip():raise DestroyError('instance is busy or reference check failed')
    verify_references(profile,command)
    command(['/usr/bin/python3',str(root/'scripts/lib'/(profile['name']+'_network.py')),'remove','--backend','iptables-nft','--yes'])
    phase('network_removed')
    bridge=profile['network']['bridge']
    ports=command(['ip','-j','link','show','master',bridge]).stdout
    if json.loads(ports):raise DestroyError('instance bridge still has ports')
    command(['ip','link','delete','dev',bridge,'type','bridge'])
    tpm=Path('/var/lib/libvirt/swtpm')/profile['uuid']
    command(['umount',str(tpm)]);command(['umount',str(root/'vol')])
    for path in (tpm,root/'vol'):
        check=command(['findmnt','-rn','--mountpoint',str(path)],check=False)
        if check.returncode!=1:raise DestroyError('mount remains or mount check failed')
    associated=command(['losetup','--associated',str(root/'.storage'/(profile['name']+'.store')),'--noheadings','--output','NAME']).stdout.split()
    if any(device!=loop for device in associated):raise DestroyError('unexpected loop alias')
    if associated:command(['losetup','--detach',loop])
    phase('mounts_released')
    command(['virsh','-c','qemu:///system','undefine',profile['name'],'--keep-nvram'])
    phase('domain_undefined')
    for unit in units:
        command(['systemctl','stop',unit],check=False)
        path=Path('/etc/systemd/system')/unit
        if path.is_symlink():raise DestroyError('unit file changed to symlink')
        path.unlink(missing_ok=True)
    hook=Path('/etc/libvirt/hooks/qemu.d')/('50-'+profile['name']+'-guard');hook.unlink(missing_ok=True)
    forwarding=Path('/etc/sysctl.d')/('90-'+profile['name']+'-forward.conf');forwarding.unlink(missing_ok=True)
    # Generated runtime resource overrides contain only these owned fields.
    for unit in units:
        for folder in (Path('/run/systemd/system.control')/(unit+'.d'),Path('/run/systemd/system')/(unit+'.d')):
            if folder.exists():
                for file in folder.iterdir():
                    if file.is_symlink() or not file.is_file():raise DestroyError('foreign runtime unit override')
                    keys={line.split('=',1)[0] for line in file.read_text().splitlines() if '=' in line and not line.startswith('#')}
                    if not keys <= {'AllowedCPUs','CPUQuota','CPUQuotaPeriodSec','MemoryMax','MemorySwapMax'}:raise DestroyError('foreign runtime unit override')
                    file.unlink()
                folder.rmdir()
    command(['systemctl','daemon-reload'])
    # All VM content is now inside the unmounted private store file. Check root
    # again immediately before deletion, never traverse a mounted filesystem.
    _instance_root(profile);verify_references(profile,command)
    for path in root.rglob('*'):
        if path.is_symlink():path.unlink()  # only the exact logs link passed above
    shutil.rmtree(root)
    registry=host/'registry'/(profile['name']+'.json')
    if read_json(registry)['uuid']!=profile['uuid']:raise DestroyError('registry changed during deletion')
    registry.unlink()
    if tpm.exists():tpm.rmdir()
    phase('deleted')
    return {'status':'deleted','name':profile['name'],'journal':str(journal)}

def destroy(profile, *, apply=False, discard=False, command=run):
    if not apply:return _destroy(profile,apply=False,command=command)
    with locked(profile):return _destroy(profile,apply=True,discard=discard,command=command)

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--profile',required=True,type=Path)
    parser.add_argument('--apply',action='store_true');parser.add_argument('--discard',action='store_true');args=parser.parse_args()
    try:
        if sys.platform!='linux' or os.geteuid()!=0:raise DestroyError('run destroy.py on the selected Linux host as root')
        from profile import load
        print(json.dumps(destroy(load(args.profile),apply=args.apply,discard=args.discard),indent=2));return 0
    except Exception as exc:print(json.dumps({'status':'blocked','reason':str(exc)}));return 2

if __name__=='__main__':raise SystemExit(main())
