"""Create only the dedicated VM bridge; do not touch physical interfaces."""
import ipaddress
import json
import pathlib
import subprocess

# The published address is normally reached over a tunnel whose MTU is
# smaller than 1500.  The bridge must not claim a larger MTU than the
# transport that carries the publish address, otherwise the guest negotiates
# a TCP segment size that the transport silently drops.  The renderer
# replaces this value with network.mtu from the profile.
MTU = 1500


def run(args, check=True):
    return subprocess.run(args, capture_output=True, text=True, check=check)


def main():
    routes = json.loads(run(['ip', '-j', '-4', 'route', 'show', 'table', 'all']).stdout)
    wanted = ipaddress.ip_network('172.30.61.0/30')
    for route in routes:
        dst = route.get('dst', 'default')
        if dst == 'default' or route.get('dev') == 'winbr01':
            continue
        if ipaddress.ip_network(dst, strict=False).overlaps(wanted):
            raise RuntimeError('Dedicated VM subnet conflicts with existing route')
    link = run(['ip', '-j', '-d', 'link', 'show', 'dev', 'winbr01'], check=False)
    if link.returncode:
        run(['ip', 'link', 'add', 'name', 'winbr01', 'type', 'bridge'])
    elif json.loads(link.stdout)[0].get('linkinfo', {}).get('info_kind') != 'bridge':
        raise RuntimeError('Existing winbr01 is not a bridge')
    ports = json.loads(run(['ip', '-j', '-d', 'link', 'show', 'master', 'winbr01']).stdout)
    if any(p.get('linkinfo', {}).get('info_kind') != 'tun' for p in ports):
        raise RuntimeError('VM bridge has a non-tap port; refusing configuration')
    addresses = json.loads(run(['ip', '-j', '-4', 'addr', 'show', 'dev', 'winbr01']).stdout)
    for addr in (addresses[0].get('addr_info', []) if addresses else []):
        if addr['local'] != '172.30.61.1' or addr['prefixlen'] != 30:
            raise RuntimeError('Existing bridge has unexpected IPv4 configuration')
    run(['ip', 'addr', 'replace', '172.30.61.1/30', 'dev', 'winbr01'])
    pathlib.Path('/proc/sys/net/ipv6/conf/winbr01/disable_ipv6').write_text('1')
    # Only this bridge permits localhost DNAT replies; global routing stays unchanged.
    pathlib.Path('/proc/sys/net/ipv4/conf/winbr01/route_localnet').write_text('1')
    # Keep the bridge MTU in step with the transport that carries the publish
    # address, so no guest segment is larger than the path can carry.
    run(['ip', 'link', 'set', 'dev', 'winbr01', 'mtu', str(MTU)])
    run(['ip', 'link', 'set', 'dev', 'winbr01', 'up'])
    print(f'Dedicated bridge ready: winbr01, 172.30.61.1/30, mtu {MTU}, no physical ports')


if __name__ == '__main__':
    main()
