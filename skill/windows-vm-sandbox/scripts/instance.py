#!/usr/bin/env python3
"""Create or replace independent VMs from a published Windows base image."""
from __future__ import annotations
import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import uuid

from profile import load, validate, cpus
import prepare_host as prepare
from base_image import inspect_manifest, trusted


def save(path, value):
    path=Path(path);temporary=path.with_name(path.name+'.tmp')
    fd=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


def module(path, name):
    path=trusted(path);spec=importlib.util.spec_from_file_location(name,path)
    value=importlib.util.module_from_spec(spec);sys.modules[name]=value;spec.loader.exec_module(value);return value


def ready(data, timeout):
    from guest_status import inspect
    deadline=time.monotonic()+timeout
    while True:
        try:
            value=inspect(data)
            if (value.get('provision_success') is True and value.get('computer_name')==data['windows']['computer_name'] and
                    value.get('winrm')=='Running' and value.get('rdp')=='Running' and
                    value.get('enabled_users')==[data['windows']['admin_user']] and
                    value.get('logical_cpus')==data['resources']['vcpus'] and
                    (data['resources']['memory_gib']-1)*1024**3 < value.get('memory_bytes',0) <= data['resources']['memory_gib']*1024**3):
                return value
        except (RuntimeError,OSError,ValueError):
            pass
        if time.monotonic()>=deadline:raise RuntimeError('guest readiness timed out; preserve this instance and resume, never reinstall automatically')
        time.sleep(3)


def authenticate(data):
    root=Path(data['root']);lib=trusted(root/'scripts/lib');sys.path.insert(0,str(lib))
    ctl=module(lib/'vmctl.py','virtops_instance_ctl')
    qga=module(lib/(data['name']+'_qga.py'),'virtops_instance_qga')
    owner=module(lib/'owner_environment.py','virtops_instance_owner')
    values=owner.private_json(root/'vol/state/private/credentials.json')
    request={'schema_version':1,'request_id':str(uuid.uuid4()),'computer_name':data['windows']['computer_name'],
             'admin_user':data['windows']['admin_user'],'password':values[data['windows']['admin_user']]}
    result=owner.invoke_guest(ctl,qga,request,'owner_verify.ps1')
    if not result.get('execution',{}).get('is_admin') or result.get('execution',{}).get('exit_code')!=0:
        raise RuntimeError('new instance WinRM administrator/upload verification failed')
    owner.host_tls_pin(data['network']['winrm_port'],result['thumbprint'])
    return {key:result[key] for key in ('thumbprint','upload_sha256','execution','rdp_service_running')}


RESUME_KEYS = ('name','uuid','root','windows','network','image','credentials','limits')
DERIVED_RESOURCES = ('cpu_order','threads_per_core','amd')


def cpu_ids(value):
    if not value:
        return set()
    if isinstance(value, (list, tuple)):
        ids = set()
        for item in value:
            ids.update(cpus(str(item)))
        return ids
    return set(cpus(value))


def same_resources(recorded, current):
    """Compare a preflight-normalized resources block with the requested one.

    The preflight rewrites an explicit CPU selection into a sorted comma list
    and adds the derived cpu_order/threads_per_core/amd fields, so the CPU set
    is compared as a set of ids and the derived fields are ignored, exactly
    like the storage stage compares a saved profile.
    """
    for key, value in recorded.items():
        if key in DERIVED_RESOURCES:
            continue
        if key not in current:
            return False
        if key == 'cpu_set':
            if cpu_ids(value) != cpu_ids(current.get('cpu_set')):
                return False
        elif current[key] != value:
            return False
    return not [key for key in current if key not in recorded]


def same_request(recorded, current):
    """Decide whether a create journal belongs to the requested instance.

    The journal stores the preflight-normalized profile, so the resources
    block is compared semantically while every other identity field is
    compared unchanged.
    """
    if any(recorded.get(key) != current.get(key) for key in RESUME_KEYS):
        return False
    return same_resources(recorded.get('resources') or {}, current.get('resources') or {})


def create(data, *, apply=False, resume=None, timeout=900):
    validate(data,new=True)
    if data.get('image',{}).get('kind')!='base':raise ValueError('instance create requires image.kind=base; use prepare_host for the disposable installer builder')
    if not apply:
        checked=prepare.preflight(copy.deepcopy(data))
        return {'status':'ready','target':data['name'],'source_image':checked['image_verified']['image_id'],
                'resources':checked['resources'],'account':data['windows']['admin_user'],
                'credentials_mode':data['credentials']['mode'],'next':'create --apply'}
    if resume:
        journal=trusted(resume);state=json.loads(journal.read_text())
        if state.get('action')!='create' or not same_request(state.get('profile') or {}, data):
            raise ValueError('resume journal belongs to another instance request')
        if state['stage']=='allocating':raise ValueError('storage allocation was interrupted; inspect bounded mounts and journal before cleanup, never repeat formatting')
        current=state['profile']
    else:
        current=prepare.preflight(copy.deepcopy(data))
        host=trusted(data['host_root']);operations=host/'operations';operations.mkdir(mode=0o700,exist_ok=True);trusted(operations)
        journal=operations/('create-'+str(uuid.uuid4())+'.json')
        state={'action':'create','stage':'allocating','target':data['name'],'profile':current,'evidence':{},'needs_user_input':False}
        save(journal,state)
        prepare.reserve_registry(current);prepare.install_host_catalog(current);prepare.storage(current)
        state['stage']='storage_ready';state['profile']=current;save(journal,state)
    def advance(stage):
        state['stage']=stage;save(journal,state)
    stage=state['stage']
    if stage=='storage_ready':prepare.seed(current);advance('seed_ready');stage='seed_ready'
    if stage=='seed_ready':prepare.activate(current);advance('booting');stage='booting'
    if stage=='booting':
        state['evidence']['guest']=ready(current,timeout)
        state['evidence']['winrm']=authenticate(current)
        advance('authenticated');stage='authenticated'
    if stage=='authenticated':prepare.finish(current);advance('finishing');stage='finishing'
    if stage=='finishing':
        # finish deliberately removes the original provisioning marker. Validate
        # real services/resources/identity plus authenticated execution again.
        state['evidence']['final_winrm']=authenticate_after_restart(current,timeout)
        pin=state['evidence']['final_winrm']['thumbprint']
        config=Path(current['root'])/'instance.json';saved=json.loads(config.read_text());saved['winrm_thumbprint']=pin
        save(config,saved);current['winrm_thumbprint']=pin
        state['profile']=current;advance('running')
    return {'status':'running','target':current['name'],'uuid':current['uuid'],'journal':str(journal),
            'winrm':'guest-loopback authenticated; host published TLS pinned','rdp':'service ready; interactive session requires separate validation',
            'rdp_endpoint':f"{current['network']['publish_ip']}:{current['network']['rdp_port']}",
            'winrm_thumbprint':current.get('winrm_thumbprint'),'c_recovery_baseline_created':False}


def authenticate_after_restart(data, timeout):
    deadline=time.monotonic()+timeout
    while True:
        try:return authenticate(data)
        except (RuntimeError,OSError,ValueError):
            if time.monotonic()>=deadline:raise RuntimeError('final instance authentication timed out; inspect/resume without recreating disks')
            time.sleep(3)


def replacement_profile(old, *, replacement=None):
    if replacement is None:
        result=copy.deepcopy(old);result['uuid']=str(uuid.uuid4())
        result['credentials']={'mode':'generate','secret_file':None,'local_clixml':None};result['winrm_thumbprint']=None
        for key in ('store_identity','registry','media_verified','image_verified','host_facts','gpu_verified'):
            result.pop(key,None)
    else:result=copy.deepcopy(replacement)
    validate(result,new=True)
    if result['uuid']==old['uuid']:raise ValueError('replacement requires a new UUID and fresh private firmware/TPM state')
    if any(result[k]!=old[k] for k in ('name','root','host_root')):raise ValueError('rebuild keeps the named instance slot; use create for a different slot')
    if result.get('image',{}).get('kind')!='base':raise ValueError('rebuild requires a published base image')
    return result


def rebuild(old, *, replacement=None, apply=False, discard=False, timeout=900):
    from destroy import destroy, validate_identity, inventory
    candidate=replacement_profile(old,replacement=replacement)
    # Check image integrity and desired identity before discarding anything.
    image=inspect_manifest(candidate['image']['manifest'])
    if image['virtual_size']!=candidate['resources']['storage_gib']*1024**3:raise ValueError('replacement disk budget differs from base image')
    if image['windows']['language'].lower()!=candidate['windows']['language'].lower():raise ValueError('replacement Windows language differs from base')
    for path in candidate['media'].values():
        if path and not Path(path).is_file():raise ValueError('replacement media is missing')
    validate_identity(old,inventory(old))
    if not apply:return {'status':'ready','action':'replace_instance','old_uuid':old['uuid'],'replacement_profile':candidate,
                         'warning':'old system disk, user data, TPM and firmware will be deleted; base image and other instances are retained'}
    if not discard:raise ValueError('rebuild discards all instance data; --discard is required')
    operations=trusted(old['host_root'])/'operations';operations.mkdir(mode=0o700,exist_ok=True)
    request=operations/('rebuild-'+str(uuid.uuid4())+'.json')
    save(request,{'action':'rebuild','stage':'before_delete','profile':candidate,'old_uuid':old['uuid']})
    destroy(old,apply=True,discard=True)
    save(request,{'action':'rebuild','stage':'old_deleted','profile':candidate,'next_action':'create using this exact new profile; never reuse the deleted UUID'})
    result=create(candidate,apply=True,timeout=timeout);result['replacement_request']=str(request)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('create','rebuild'))
    parser.add_argument('--profile',required=True,type=Path);parser.add_argument('--replacement-profile',type=Path)
    parser.add_argument('--apply',action='store_true');parser.add_argument('--discard',action='store_true')
    parser.add_argument('--resume',type=Path);parser.add_argument('--timeout',type=int,default=900)
    args=parser.parse_args()
    try:
        if sys.platform!='linux' or os.geteuid()!=0:raise ValueError('run instance.py on the selected Linux host as root')
        if not 30<=args.timeout<=3600:raise ValueError('timeout must be 30-3600 seconds')
        if args.action=='create':
            if args.replacement_profile or args.discard:raise ValueError('replacement/discard options apply only to rebuild')
            result=create(load(args.profile,new=True),apply=args.apply,resume=args.resume,timeout=args.timeout)
        else:
            if args.resume:raise ValueError('rebuild recovery uses its saved replacement profile and create --resume journal; never repeat deletion blindly')
            result=rebuild(load(args.profile),replacement=load(args.replacement_profile,new=True) if args.replacement_profile else None,apply=args.apply,discard=args.discard,timeout=args.timeout)
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except Exception as exc:
        print(json.dumps({'status':'blocked','reason':str(exc)},ensure_ascii=False));return 2


if __name__=='__main__':raise SystemExit(main())
