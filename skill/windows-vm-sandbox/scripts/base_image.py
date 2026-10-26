"""Immutable generalized-image validation and standalone instance materialization."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

ASSETS = Path(__file__).resolve().parents[1] / 'assets'
GIB = 1024**3


def run(args, timeout=600):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout)


def trusted(path, *, immutable=False):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(path)):
        raise ValueError('image path must be a clean absolute Linux path')
    for item in [path, *path.parents]:
        info = item.lstat()
        if item.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('image path must be root-owned, without symlink or writable ancestors')
    if immutable and path.stat().st_mode & 0o222:
        raise ValueError('published template files must be read-only')
    return path


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8*1024**2), b''):
            result.update(block)
    return result.hexdigest()


def disk_info(path):
    value = json.loads(run(['qemu-img', 'info', '--output=json', '--backing-chain', str(path)]).stdout)
    if not isinstance(value, list) or len(value) != 1 or value[0].get('format') != 'qcow2' or value[0].get('backing-filename'):
        raise ValueError('base and instance disks must be standalone qcow2, without a backing chain')
    return value[0]


def inspect_manifest(path, *, verify_hash=True):
    path = trusted(path, immutable=True)
    value = json.loads(path.read_text(encoding='utf-8'))
    expected = {'schema_version','kind','image_id','disk','sha256','virtual_size','windows','c_partition','generalization'}
    if set(value) != expected or value['schema_version'] != 1 or value['kind'] != 'generalized-windows-qcow2':
        raise ValueError('invalid base-image manifest schema')
    if not re.fullmatch(r'[a-z][a-z0-9-]{1,39}', value['image_id']) or value['disk'] != 'disk.qcow2':
        raise ValueError('invalid base-image identity or disk name')
    if not re.fullmatch('[0-9a-f]{64}', value['sha256']):
        raise ValueError('invalid base-image hash')
    if type(value['virtual_size']) is not int or value['virtual_size'] < 64*GIB:
        raise ValueError('invalid base-image virtual size')
    part = value['c_partition']
    if set(part) != {'index','start_bytes','size_bytes','unique_guid'}:
        raise ValueError('invalid template C partition evidence')
    for key in ('index','start_bytes','size_bytes'):
        if type(part[key]) is not int or part[key] <= 0:
            raise ValueError('invalid C partition range')
    if (part['start_bytes'] % 512 or part['size_bytes'] % 512 or
            part['start_bytes'] + part['size_bytes'] > value['virtual_size']):
        raise ValueError('template C range is outside its disk')
    if value['generalization'] != {'image_state':'IMAGE_STATE_GENERALIZE_RESEAL_TO_OOBE', 'builder_account_removed':True}:
        raise ValueError('template lacks a completed Sysprep/OOBE and builder-removal record')
    if set(value['windows']) != {'language','edition','architecture'} or value['windows']['architecture'] != 'amd64':
        raise ValueError('invalid base Windows metadata')
    if not str(value['windows']['edition']).lower().startswith(('professional','enterprise')):
        raise ValueError('base Windows must support incoming RDP')
    image = trusted(path.parent / value['disk'], immutable=True)
    info = disk_info(image)
    if info['virtual-size'] != value['virtual_size']:
        raise ValueError('base virtual size differs from manifest')
    if verify_hash and digest(image) != value['sha256']:
        raise ValueError('base image hash differs from manifest')
    return value


@contextmanager
def offline_disk(path, virtual_size):
    """Reuse the existing CRC-checked GPT reader and partition-clipped NBD IO.

    The imported helper has no command-line entry side effects. Each operation
    gets its own module globals and a scratch area inside the store that owns
    the disk being served.
    """
    spec = importlib.util.spec_from_file_location('virtops_image_disk_io', ASSETS/'runtime/winvm01_c_reset.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    helper.CURRENT = Path(path)
    helper.DISK_BYTES = virtual_size
    # The imported helper keeps every scratch clip inside its own bounded store
    # and refuses files outside it. Its module default points at the first
    # instance's store, so rebind the store to the directory that owns the disk
    # being served; otherwise the bounded-file check rejects the temporary clip
    # and every read of a template disk fails.
    helper.STORE = Path(path).parent
    helper.RECOVERY = helper.STORE / 'recovery'
    yield helper


def verify_partition(helper, expected):
    _, partitions = helper.read_gpt_pair()
    matches = [p for p in partitions if p['index'] == expected['index']]
    if len(matches) != 1 or any(str(matches[0][key]).lower() != str(expected[key]).lower()
                                for key in ('unique_guid','start_bytes','size_bytes')):
        raise ValueError('template C evidence differs from the actual GPT')


def generalized_state(path, manifest):
    with offline_disk(path, manifest['virtual_size']) as helper:
        verify_partition(helper, manifest['c_partition'])
        p = manifest['c_partition']
        with helper.clipped_nbd(p['start_bytes'], p['size_bytes'], readonly=True) as device:
            state = run(['ntfscat', str(device), '/Windows/Setup/State/State.ini']).stdout
    match = re.search(r'(?mi)^ImageState\s*=\s*(\S+)\s*$', state)
    if not match or match.group(1) != 'IMAGE_STATE_GENERALIZE_RESEAL_TO_OOBE':
        raise ValueError('offline Windows state is not generalized and resealed for OOBE')
    return match.group(1)


def materialize(data, destination):
    manifest_path = Path(data['image']['manifest'])
    manifest = inspect_manifest(manifest_path)
    target = Path(destination)
    if target.exists() or target.is_symlink():
        raise ValueError('instance disk already exists; refusing overwrite')
    if manifest['virtual_size'] != data['resources']['storage_gib']*GIB:
        raise ValueError('base virtual disk size must match the instance storage budget')
    source = manifest_path.parent / manifest['disk']
    # Fully materialize: no mutable shared layer, linked clone, copied firmware,
    # TPM state, owner metadata or source credentials.
    run(['qemu-img','convert','-f','qcow2','-O','qcow2',str(source),str(target)], timeout=3600)
    info = disk_info(target)
    a,b=source.stat(),target.stat()
    if info['virtual-size'] != manifest['virtual_size'] or (a.st_dev,a.st_ino)==(b.st_dev,b.st_ino):
        raise ValueError('materialization did not create an independent disk')
    return manifest


def inject_unattend(data, answer_path):
    """Only on a new, stopped, verified instance; expose exactly its C range."""
    manifest = inspect_manifest(data['image']['manifest'])
    disk = Path(data['root'])/'vol/current.qcow2'
    info = disk_info(disk)
    if info['virtual-size'] != manifest['virtual_size']:
        raise ValueError('instance disk virtual size differs from its template')
    # Storage/seed have not defined the domain. Refuse even an inactive existing
    # definition to keep offline writes out of normal running VM management.
    domain = subprocess.run(['virsh','--readonly','-c','qemu:///system','dominfo',data['name']],
                            text=True,capture_output=True,timeout=30)
    if domain.returncode == 0:
        raise ValueError('template seed injection requires an undefined new instance')
    with offline_disk(disk, manifest['virtual_size']) as helper:
        verify_partition(helper, manifest['c_partition'])
        part = manifest['c_partition']
        with helper.clipped_nbd(part['start_bytes'], part['size_bytes'], readonly=False) as device:
            run(['ntfscp',str(device),str(answer_path),'/Windows/Panther/Unattend.xml'])
            actual = run(['ntfscat',str(device),'/Windows/Panther/Unattend.xml']).stdout
            if actual.lstrip('\ufeff') != Path(answer_path).read_text(encoding='utf-8-sig'):
                raise ValueError('offline unattend write did not verify')
