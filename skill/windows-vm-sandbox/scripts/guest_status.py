#!/usr/bin/env python3
"""Read installed guest readiness and certificate pin through trusted host QGA, without credentials."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

from profile import load


class IntegrityError(RuntimeError):
    """A readiness failure that is not transient: identity or path integrity."""


def domain_state(name):
    """Return the libvirt power state of a domain, or None when it is not defined."""
    if not name: return None
    result=subprocess.run(['virsh','--readonly','-c','qemu:///system','domstate',name],
                          text=True,capture_output=True)
    if result.returncode: return None
    return result.stdout.strip() or None


def inspect(data):
    xml=ET.fromstring(subprocess.check_output(['virsh','--readonly','-c','qemu:///system','dumpxml',data['name']],text=True))
    if xml.findtext('uuid')!=data['uuid']:raise IntegrityError('VM UUID differs from profile')
    root=Path(data['root']);path=root/'scripts/lib'/(data['name']+'_qga.py')
    for parent in (root,root/'scripts',path.parent,path):
        st=parent.stat()
        if parent.is_symlink() or st.st_uid!=0 or st.st_mode&0o022:raise IntegrityError('Unsafe QGA helper path')
    spec=importlib.util.spec_from_file_location('virtops_qga',path);qga=importlib.util.module_from_spec(spec);spec.loader.exec_module(qga)
    if qga.VM!=data['name']:raise IntegrityError('QGA helper VM identity differs')
    qga.call('guest-ping')
    name=data['name']
    script=r"""
$listeners=@(Get-ChildItem WSMan:\localhost\Listener | Where-Object {$_.Keys -contains 'Transport=HTTPS'})
$pins=@(foreach($l in $listeners){(Get-Item ($l.PSPath+'\CertificateThumbprint')).Value})
@{computer_name=$env:COMPUTERNAME;time_utc=[DateTime]::UtcNow.ToString('o');
  provision_success=(Test-Path 'C:\Windows\Temp\__NAME__-provision.success');
  winrm=(Get-Service WinRM).Status.ToString();rdp=(Get-Service TermService).Status.ToString();
  enabled_users=@(Get-LocalUser|Where-Object Enabled|Select-Object -ExpandProperty Name);
  memory_bytes=(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory;
  logical_cpus=(Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors;
  winrm_thumbprints=$pins;default_routes=@(Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue|Select-Object NextHop,InterfaceAlias)}|ConvertTo-Json -Depth 5 -Compress
""".replace('__NAME__',name)
    return json.loads(qga.powershell(script))


def classify(data, exc):
    """Map a readiness failure onto an actionable status.

    An unattended Windows install needs tens of minutes and the guest agent
    only answers once the install finished.  Reporting that as a failure
    invites a reinstall of a healthy instance, so waiting, a stopped domain and
    a real integrity failure are reported separately.
    """
    name=(data or {}).get('name','')
    if not name:
        return 'blocked','the profile could not be read; fix the profile path and schema before retrying'
    state=domain_state(name)
    if state is None:
        return 'unknown_instance','the domain is not defined; verify the profile name and UUID before retrying'
    if state in {'shut off','shutoff','crashed','pmsuspended','paused'}:
        return 'not_running','the domain is not running; start the instance and wait for the guest agent to answer'
    if isinstance(exc,IntegrityError):
        return 'blocked','an identity or path integrity check failed; fix that condition instead of retrying'
    return 'not_ready','Windows is still installing or the guest agent has not answered yet; keep this instance and retry later, never reinstall from this status'


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--profile',required=True,type=Path);args=parser.parse_args()
    data={}
    try:
        if sys.platform!='linux' or os.geteuid()!=0:raise RuntimeError('Run on the selected Linux host as root')
        data=load(args.profile)
        result=inspect(data);result['status']='ready'
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except Exception as exc:
        status,hint=classify(data,exc)
        print(json.dumps({'status':status,'reason':str(exc),'domstate':domain_state((data or {}).get('name','')),'hint':hint},ensure_ascii=False))
        raise SystemExit(2)
