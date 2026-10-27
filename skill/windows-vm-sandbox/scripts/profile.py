#!/usr/bin/env python3
"""Validate an explicit VM profile. No SSH, passwords, or deployment side effects."""
from __future__ import annotations
import argparse
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re
import uuid


def required(data, dotted):
    value = data
    for field in dotted.split('.'):
        if not isinstance(value, dict) or field not in value or value[field] is None or value[field] == "":
            raise ValueError(f"NEEDS_USER_INPUT: {dotted}")
        value = value[field]
    return value


def cpus(text: str) -> list[int]:
    result = []
    for part in text.split(','):
        if not re.fullmatch(r'\d+(?:-\d+)?', part):
            raise ValueError('invalid CPU set')
        bounds = [int(n) for n in part.split('-')]
        if len(bounds) == 2 and bounds[1] < bounds[0]:
            raise ValueError('reversed CPU range')
        result.extend(range(bounds[0], bounds[-1] + 1))
    if len(result) != len(set(result)):
        raise ValueError('duplicate CPU IDs')
    return result


def validate(data: dict, *, new: bool = False) -> dict:
    def no_secrets(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in {'password', 'admin_password', 'token', 'private_key', 'secret'}:
                    raise ValueError('profile must contain credential paths, never secret values')
                no_secrets(item)
        elif isinstance(value, list):
            for item in value:
                no_secrets(item)
    no_secrets(data)
    if 'template_builder' in data and type(data['template_builder']) is not bool:
        raise ValueError('template_builder must be an explicit boolean')
    if required(data, 'schema_version') != 1:
        raise ValueError('unsupported profile schema')
    name = required(data, 'name')
    if not re.fullmatch('[a-z][a-z0-9-]{1,11}', name):
        raise ValueError('VM name must be 2-12 lowercase ASCII letters/digits/hyphens')
    if uuid.UUID(required(data, 'uuid')).int == 0:
        raise ValueError('generate a unique nonzero UUID for this VM')
    root = required(data, 'root')
    if not re.fullmatch(r'/(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+', root) or '..' in PurePosixPath(root).parts:
        raise ValueError('root must be a specific absolute Linux instance directory without spaces or ..')
    if root in {'/var/lib', '/var/lib/libvirt', '/usr/local', '/home/root'}:
        raise ValueError('root is a shared system directory')
    host_root = required(data, 'host_root') if new or 'host_root' in data else str(PurePosixPath(root).parent)
    if not re.fullmatch(r'/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+', host_root) or '..' in PurePosixPath(host_root).parts:
        raise ValueError('host_root must be a specific absolute Linux directory without spaces or ..')
    if host_root in {'/var/lib', '/var/lib/libvirt', '/usr/local', '/home/root'}:
        raise ValueError('host_root is a shared system directory')
    try:
        PurePosixPath(root).relative_to(PurePosixPath(host_root))
    except ValueError as exc:
        raise ValueError('instance root must be inside host_root') from exc
    if new and root == host_root:
        raise ValueError('new VM root must be a child directory of host_root')
    labels = data.get('labels', {})
    if not isinstance(labels, dict):
        raise ValueError('labels must be an object')
    for key in ('owner', 'purpose'):
        if new and not labels.get(key):
            raise ValueError(f'NEEDS_USER_INPUT: labels.{key}')
        if labels.get(key) is not None and (not isinstance(labels[key], str) or len(labels[key]) > 128 or '\n' in labels[key] or '\r' in labels[key]):
            raise ValueError(f'invalid labels.{key}')
    for field in ('host', 'user'):
        if not re.fullmatch(r'[A-Za-z0-9_.:@-]+', required(data, 'ssh.' + field)):
            raise ValueError('invalid SSH identity/host')
    if not 1 <= data['ssh'].get('port', 22) <= 65535:
        raise ValueError('invalid SSH port')
    if data['ssh'].get('bind_address'):
        ipaddress.ip_address(data['ssh']['bind_address'])
    for field, minimum in [('memory_gib', 4), ('host_limit_gib', 6), ('vcpus', 2), ('storage_gib', 64)]:
        value = required(data, 'resources.' + field)
        if type(value) is not int or value < minimum:
            raise ValueError(f'resources.{field} must be an integer >= {minimum}')
    resources = data['resources']
    if resources['host_limit_gib'] < resources['memory_gib'] + 2:
        raise ValueError('host_limit_gib must include QEMU overhead, at least guest RAM + 2 GiB')
    if resources.get('cpu_set') and len(cpus(resources['cpu_set'])) != resources['vcpus']:
        raise ValueError('CPU set length differs from requested vCPU count')
    net = data['network']
    if not re.fullmatch('[A-Za-z][A-Za-z0-9_-]{0,14}', required(data, 'network.bridge')):
        raise ValueError('invalid Linux bridge name')
    subnet = ipaddress.IPv4Network(required(data, 'network.subnet'))
    bridge_ip, guest_ip, publish_ip = [ipaddress.IPv4Address(required(data, 'network.' + k)) for k in ('bridge_ip', 'guest_ip', 'publish_ip')]
    if subnet.prefixlen != 30 or bridge_ip not in subnet.hosts() or guest_ip not in subnet.hosts() or bridge_ip == guest_ip:
        raise ValueError('dedicated subnet must be /30 with distinct host and guest addresses')
    clients = ipaddress.IPv4Network(required(data, 'network.client_cidr'))
    if clients.overlaps(subnet) or publish_ip in subnet:
        raise ValueError('dedicated guest subnet must not overlap client or publish addresses')
    if clients.prefixlen == 0 or publish_ip.is_unspecified:
        raise ValueError('explicit incoming client network and publish IP are required')
    for key in ('rdp_port', 'winrm_port'):
        port = required(data, 'network.' + key)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('published ports must be integers 1..65535')
    if net['rdp_port'] == net['winrm_port']:
        raise ValueError('RDP and WinRM published ports must differ')
    # The published address is normally reached over a tunnel whose MTU is
    # smaller than 1500.  The dedicated bridge and the guest NIC are aligned
    # to this value, so a packet that fits the transport also fits the bridge
    # and is never dropped without a reply.  1500 keeps the current behaviour
    # for a plain LAN publish address.
    mtu = net.get('mtu')
    if mtu is None:
        mtu = 1500
    if type(mtu) is not int or not 1280 <= mtu <= 9000:
        raise ValueError('network.mtu must be an integer between 1280 and 9000')
    account = required(data, 'windows.admin_user')
    if not re.fullmatch('[A-Za-z][A-Za-z0-9_.-]{0,19}', account) or account.lower() in {'administrator', 'guest', 'defaultaccount', 'system', 'administrators', 'wdagutilityaccount'}:
        raise ValueError('choose a new 1-20 character ASCII Windows account, excluding built-in names')
    if required(data, 'windows.computer_name').upper() != name.upper():
        raise ValueError('Windows computer_name must match the VM name')
    mode = required(data, 'credentials.mode')
    if mode not in {'existing', 'generate', 'prompt', 'file'} or (new and mode == 'existing'):
        raise ValueError('choose credentials.mode: generate, prompt, or file for a new VM')
    if mode == 'file':
        required(data, 'credentials.secret_file')
    for key in ('root_free_gib', 'start_free_gib', 'resume_free_gib', 'watch_free_gib'):
        value = required(data, 'limits.' + key)
        if type(value) is not int or value < 1:
            raise ValueError('limits must be positive GiB integers')
    if data['limits']['start_free_gib'] < resources['host_limit_gib']:
        raise ValueError('cold-start free-memory gate must cover the full VM host limit')
    if new:
        mode=required(data,'gpu.mode')
        if mode not in {'none','passthrough','vgpu'}:raise ValueError('choose GPU mode: none, passthrough, or vgpu')
        if mode=='passthrough':
            devices=required(data,'gpu.pci_devices')
            if not isinstance(devices,list) or not devices or len(devices)!=len(set(devices)) or any(not isinstance(n,str) or not re.fullmatch(r'[0-9a-f]{4}:[0-9a-f]{2}:[0-1][0-9a-f]\.[0-7]',n) for n in devices):
                raise ValueError('GPU PCI devices must be distinct lowercase domain:bus:slot.function addresses')
            if data['gpu'].get('dedicated') is not True:raise ValueError('PCI passthrough needs explicitly dedicated GPU resources')
        if mode=='none' and data['gpu'].get('pci_devices'):raise ValueError('GPU mode none cannot have PCI devices')
        image = data.get('image', {'kind': 'installer'})
        if not isinstance(image, dict) or image.get('kind') not in {'installer', 'base'}:
            raise ValueError('image.kind must be installer or base')
        if image['kind'] == 'base':
            if PurePosixPath(root)!=PurePosixPath(host_root)/'instances'/name:
                raise ValueError('base instance root must be host_root/instances/name')
            manifest = required(data, 'image.manifest')
            if not re.fullmatch(r'/(?:[A-Za-z0-9_.-]+/)+manifest\.json', manifest) or '..' in PurePosixPath(manifest).parts:
                raise ValueError('base manifest must be a specific absolute Linux manifest.json path')
        media_keys = ('virtio_iso',) if image['kind'] == 'base' else ('windows_iso', 'virtio_iso')
        for key in media_keys:
            path = required(data, 'media.'+key)
            if not path.startswith('/') or '\n' in path or '\r' in path:
                raise ValueError('media paths must be absolute paths on the Linux host')
        if not re.fullmatch(r'[A-Za-z]{2,3}-[A-Za-z0-9-]{2,12}', required(data, 'windows.language')):
            raise ValueError('invalid Windows language identifier')
        if not re.fullmatch(r'[A-Za-z0-9 +_-]{1,64}', data['windows'].get('time_zone','UTC')):
            raise ValueError('invalid Windows time zone identifier')
        if image['kind'] == 'installer' and (type(required(data, 'windows.image_index')) is not int or data['windows']['image_index'] < 1):
            raise ValueError('select the actual Pro/Enterprise image index from the ISO')
    return data


def load(path, *, new=False):
    return validate(json.loads(Path(path).read_text(encoding='utf-8-sig')), new=new)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path)
    parser.add_argument('--new', action='store_true')
    args = parser.parse_args()
    try:
        data = load(args.path, new=args.new)
        print(json.dumps({'valid': True, 'name': data['name'], 'root': data['root'], 'mode': 'new' if args.new else 'existing'}, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'needs_input_or_invalid', 'reason': str(exc)}, ensure_ascii=False))
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
