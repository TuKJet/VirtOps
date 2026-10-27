#!/usr/bin/env python3
"""winvm01 firewall/NAT rules for an isolated libvirt bridge.

This file is intentionally limited to the dedicated winvm01 iptables-nft
chains.  It does not create the libvirt network, enable forwarding, change
existing policies, flush any shared table, or restart a service.

The only supported backend is the already discovered iptables-nft backend.
Run ``preflight`` first on the target host and pass ``--backend iptables-nft``
to every mutating operation.  Rules are installed in private chains and
private jumps; existing Docker/libvirt chains are never flushed or rewritten.

Traffic model:
  * winbr01: 172.30.61.1/30, guest: 172.30.61.2/30
  * no guest default gateway or DNS is configured by this script
  * only 192.168.20.0/24 may reach 192.168.20.5:13389 -> guest:3389
    and 192.168.20.5:15986 -> guest:5986
  * the published RDP port is forwarded over TCP and UDP; WinRM over TCP only
  * localhost 127.0.0.1 may use the same two published ports
  * inbound connections are SNATed to 172.30.61.1 so a no-gateway guest can
    reply; no general masquerade or egress rule is installed
  * guest-originated IPv4 NEW traffic and all guest-originated IPv6 traffic
    are dropped by interface (winbr01), not by trusting the guest source IP
  * ``sysctl`` separately sets only ``net.ipv4.conf.winbr01.route_localnet=1``
    for localhost DNAT and ``net.ipv6.conf.winbr01.disable_ipv6=1``

The libvirt isolated network/bridge is created by the deployment owner. This
script refuses to configure rules against a missing or wrongly-addressed
bridge, and it does not alter libvirt's default network.

No SSH/10022 mapping is installed.  Add it only as a reviewed future change.

When private chains already exist, ``apply`` refuses to append to a possibly
old ordering.  Use ``apply --refresh-private`` only while ``winvm01`` is
stopped; it flushes and rebuilds this script's private chains only.  Shared
filter/nat chains and their existing rules are never flushed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import re
from pathlib import Path
import maintenance
from typing import Iterable, Sequence


BRIDGE = "winbr01"
VM_NAME = "winvm01"
BRIDGE_IP = "172.30.61.1"
GUEST_IP = "172.30.61.2"
PUBLISH_IP = "192.168.20.5"
CLIENT_NET = "192.168.20.0/24"
LOCALHOST = "127.0.0.1"
# EasyTier's userspace TCP proxy reconnects locally from the host overlay IP.
# Admit only that host address and the loopback endpoint in OUTPUT DNAT.
LOCAL_ENDPOINTS = ((LOCALHOST, LOCALHOST), (PUBLISH_IP, PUBLISH_IP))

PUBLISHED = ((13389, 3389), (15986, 5986))
# The RDP client negotiates a UDP transport (multitransport) on the same
# published port.  Publishing RDP over TCP only makes the Windows client abort
# the session seconds after connecting, so the RDP port is published over UDP
# as well.  WinRM stays TCP only.
PUBLISHED_UDP = ((13389, 3389),)
LOG_LIMIT = "10/min"
LOG_BURST = "20"

FILTER4 = {
    "input": "WINVM01_INPUT",
    "forward_from": "WINVM01_FWD_FROM",
    "forward_to": "WINVM01_FWD_TO",
    "output": "WINVM01_OUTPUT",
}
FILTER6 = {
    "input": "WINVM01_INPUT6",
    "forward_from": "WINVM01_FWD_FROM6",
    "forward_to": "WINVM01_FWD_TO6",
    "output": "WINVM01_OUTPUT6",
}
NAT = {
    "prerouting": "WINVM01_DNAT",
    "output": "WINVM01_DNAT_OUT",
    "postrouting": "WINVM01_SNAT",
}


class RuleError(RuntimeError):
    pass


def maintenance_active():
    return maintenance.active(Path(__file__).resolve().parents[2] / 'vol/recovery/owner-transition.json')


def maintenance_gate():
    return maintenance.Gate(VM_NAME, PUBLISHED, FILTER4['forward_to'], FILTER4['output'])


def log_rule(route: str) -> list[str]:
    prefix = f"WINVM01 DROP {route} "
    if len(prefix) > 29:
        prefix = f"WINVM01 DROP {route.replace('fwd-', '')} "
    if len(prefix) > 29:
        raise RuleError(f"iptables LOG prefix is too long: {prefix!r}")
    return [
        "-m", "limit", "--limit", LOG_LIMIT, "--limit-burst", LOG_BURST,
        "-j", "LOG", "--log-prefix", prefix, "--log-level", "warning",
    ]


def command(argv: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a host command without a shell and keep stderr available to caller."""

    try:
        result = subprocess.run(
            list(argv),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuleError(f"missing command: {argv[0]}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuleError(f"command failed ({result.returncode}): {' '.join(argv)}: {detail}")
    return result


def binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuleError(f"missing required command: {name}")
    return path


def iptables(v6: bool = False, table: str | None = None) -> list[str]:
    name = "ip6tables" if v6 else "iptables"
    argv = [binary(name)]
    if table:
        argv += ["-t", table]
    return argv


def require_root() -> None:
    if os.geteuid() != 0:
        raise RuleError("apply/remove/verify require root on the target Linux host")


def check_backend(backend: str) -> None:
    if backend != "iptables-nft":
        raise RuleError(
            "backend is not selectable implicitly; preflight must confirm "
            "iptables-nft, then pass --backend iptables-nft"
        )
    version = command([binary("iptables"), "--version"]).stdout.strip()
    if "nf_tables" not in version:
        raise RuleError(f"refusing non-nft iptables backend: {version}")


def rule_exists(argv: Sequence[str]) -> bool:
    return command(argv, check=False).returncode == 0


def ensure_chain(*, v6: bool = False, table: str, chain: str) -> None:
    cmd = iptables(v6, table) + ["-N", chain]
    if not rule_exists(iptables(v6, table) + ["-S", chain]):
        command(cmd)


def ensure_rule(*, v6: bool = False, table: str, parent: str, args: Sequence[str]) -> None:
    base = iptables(v6, table)
    if not rule_exists(base + ["-C", parent, *args]):
        command(base + ["-A", parent, *args])


def ensure_top_jump(*, v6: bool = False, table: str, parent: str, chain: str, match: Sequence[str]) -> None:
    """Put exactly one dedicated jump at the front without touching other rules."""

    base = iptables(v6, table)
    jump = [*match, "-j", chain]
    # Remove duplicate copies of our exact jump, bounded to prevent a broken
    # ruleset from causing an unbounded loop.  Only this dedicated jump is
    # changed; Docker/libvirt rules remain byte-for-byte untouched.
    for _ in range(32):
        if not rule_exists(base + ["-C", parent, *jump]):
            break
        command(base + ["-D", parent, *jump])
    command(base + ["-I", parent, "1", *jump])


def remove_rule_copies(*, v6: bool = False, table: str, parent: str, args: Sequence[str]) -> None:
    base = iptables(v6, table)
    for _ in range(32):
        if not rule_exists(base + ["-C", parent, *args]):
            return
        command(base + ["-D", parent, *args])
    raise RuleError(f"too many duplicate rules while removing {parent}: {' '.join(args)}")


def chain_rules() -> Iterable[tuple[bool, str, str]]:
    for name in FILTER4.values():
        yield False, "filter", name
    for name in FILTER6.values():
        yield True, "filter", name
    for name in NAT.values():
        yield False, "nat", name


def bridge_present() -> bool:
    return command([binary("ip"), "link", "show", "dev", BRIDGE], check=False).returncode == 0


def bridge_has_expected_address() -> bool:
    result = command([binary("ip"), "-4", "addr", "show", "dev", BRIDGE], check=False)
    return result.returncode == 0 and f"inet {BRIDGE_IP}/30" in result.stdout


def require_bridge(*, allow_missing: bool) -> None:
    if not bridge_present():
        if allow_missing:
            return
        raise RuleError(f"{BRIDGE} does not exist; create and verify the isolated libvirt bridge first")
    if not bridge_has_expected_address():
        raise RuleError(f"{BRIDGE} exists but does not have {BRIDGE_IP}/30")


def interface_sysctl_path(key: str) -> str:
    """Return an interface-scoped procfs sysctl path.

    Keep this deliberately narrow: callers may only use the two keys required
    by this design, so a typo cannot accidentally target a global sysctl.
    """

    allowed = {
        "net.ipv4.conf.winbr01.route_localnet",
        "net.ipv6.conf.winbr01.disable_ipv6",
    }
    if key not in allowed:
        raise RuleError(f"refusing non-interface sysctl: {key}")
    return "/proc/sys/" + key.replace(".", "/")


def read_interface_sysctl(key: str) -> str:
    try:
        with open(interface_sysctl_path(key), encoding="ascii") as handle:
            return handle.read().strip()
    except OSError as exc:
        raise RuleError(f"cannot read {key}: {exc}") from exc


def configure_sysctls() -> None:
    """Enable only the per-interface settings needed by the isolated bridge."""

    require_root()
    require_bridge(allow_missing=False)
    values = {
        "net.ipv4.conf.winbr01.route_localnet": "1",
        "net.ipv6.conf.winbr01.disable_ipv6": "1",
    }
    for key, value in values.items():
        try:
            with open(interface_sysctl_path(key), "w", encoding="ascii") as handle:
                handle.write(value + "\n")
        except OSError as exc:
            raise RuleError(f"cannot set {key}={value}: {exc}") from exc
        if read_interface_sysctl(key) != value:
            raise RuleError(f"kernel did not retain {key}={value}")


def verify_interface_sysctls() -> None:
    expected = {
        "net.ipv4.conf.winbr01.route_localnet": "1",
        "net.ipv6.conf.winbr01.disable_ipv6": "1",
    }
    for key, value in expected.items():
        actual = read_interface_sysctl(key)
        if actual != value:
            raise RuleError(f"{key} is {actual!r}; expected {value!r}; run the separate sysctl action")


def forwarding_enabled() -> bool:
    try:
        return open("/proc/sys/net/ipv4/ip_forward", encoding="ascii").read().strip() == "1"
    except OSError as exc:
        raise RuleError(f"cannot read IPv4 forwarding state: {exc}") from exc


def require_vm_stopped() -> None:
    """Require libvirt to confirm the VM is shut off before private rebuild."""

    virsh = shutil.which("virsh")
    if not virsh:
        raise RuleError("--refresh-private requires virsh to verify winvm01 is stopped")
    result = command([virsh, "domstate", VM_NAME], check=False)
    if result.returncode != 0:
        # An undefined VM has no packets and is safe for private-chain setup.
        detail = (result.stderr or result.stdout).lower()
        if "not found" in detail or "no domain" in detail:
            return
        raise RuleError(f"cannot determine {VM_NAME} state: {(result.stderr or result.stdout).strip()}")
    state = result.stdout.strip().lower()
    if state not in {"shut off", "shutoff"}:
        raise RuleError(f"refusing private-chain rebuild while {VM_NAME} state is {state!r}")


def filter4_specs() -> dict[str, list[list[str]]]:
    specs: dict[str, list[list[str]]] = {
        FILTER4["input"]: [
            ["-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
            ["-j", "DROP"],
        ],
        FILTER4["forward_from"]: [
            ["-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
            ["-j", "DROP"],
        ],
        FILTER4["forward_to"]: [
            ["-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
        ],
        FILTER4["output"]: [
            ["-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
        ],
    }
    for source, original_ip, original_port, guest_port in (
        (CLIENT_NET, PUBLISH_IP, 13389, 3389),
        (CLIENT_NET, PUBLISH_IP, 15986, 5986),
        (LOCALHOST, LOCALHOST, 13389, 3389),
        (LOCALHOST, LOCALHOST, 15986, 5986),
    ):
        specs[FILTER4["forward_to"]].append(
            [
                "-p", "tcp", "-s", source, "-d", GUEST_IP,
                "--dport", str(guest_port), "-m", "conntrack", "--ctstate", "NEW",
                "--ctorigdst", original_ip, "--ctorigdstport", str(original_port),
                "-j", "ACCEPT",
            ]
        )
    for original_port, guest_port in PUBLISHED:
        for source, original_ip in LOCAL_ENDPOINTS:
            specs[FILTER4["output"]].append(
                [
                    "-p", "tcp", "-s", source, "-d", GUEST_IP, "-o", BRIDGE,
                    "--dport", str(guest_port), "-m", "conntrack", "--ctstate", "NEW",
                    "--ctorigdst", original_ip, "--ctorigdstport", str(original_port),
                    "-j", "ACCEPT",
                ]
            )
    for original_port, guest_port in PUBLISHED_UDP:
        for source, original_ip in ((CLIENT_NET, PUBLISH_IP), (LOCALHOST, LOCALHOST)):
            specs[FILTER4["forward_to"]].append(
                [
                    "-p", "udp", "-s", source, "-d", GUEST_IP,
                    "--dport", str(guest_port), "-m", "conntrack", "--ctstate", "NEW",
                    "--ctorigdst", original_ip, "--ctorigdstport", str(original_port),
                    "-j", "ACCEPT",
                ]
            )
        for source, original_ip in LOCAL_ENDPOINTS:
            specs[FILTER4["output"]].append(
                [
                    "-p", "udp", "-s", source, "-d", GUEST_IP, "-o", BRIDGE,
                    "--dport", str(guest_port), "-m", "conntrack", "--ctstate", "NEW",
                    "--ctorigdst", original_ip, "--ctorigdstport", str(original_port),
                    "-j", "ACCEPT",
                ]
            )
    specs[FILTER4["input"]].insert(-1, log_rule("input"))
    specs[FILTER4["forward_from"]].insert(-1, log_rule("fwd-from"))
    specs[FILTER4["forward_to"]].extend([log_rule("fwd-to"), ["-j", "DROP"]])
    specs[FILTER4["output"]].extend([log_rule("output"), ["-j", "DROP"]])
    if maintenance_active():
        for key in ('forward_to', 'output'):
            specs[FILTER4[key]].insert(0, ['-j', maintenance_gate().chain])
    return specs


def filter6_specs() -> dict[str, list[list[str]]]:
    routes = {
        FILTER6["input"]: "v6-input",
        FILTER6["forward_from"]: "v6-fwd-from",
        FILTER6["forward_to"]: "v6-fwd-to",
        FILTER6["output"]: "v6-output",
    }
    return {chain: [log_rule(route), ["-j", "DROP"]] for chain, route in routes.items()}


def nat_specs() -> dict[str, list[list[str]]]:
    specs: dict[str, list[list[str]]] = {
        NAT["prerouting"]: [
            ["-p", "tcp", "-s", CLIENT_NET, "-d", PUBLISH_IP, "--dport", "13389", "-j", "DNAT", "--to-destination", f"{GUEST_IP}:3389"],
            ["-p", "tcp", "-s", CLIENT_NET, "-d", PUBLISH_IP, "--dport", "15986", "-j", "DNAT", "--to-destination", f"{GUEST_IP}:5986"],
        ],
        NAT["output"]: [],
        NAT["postrouting"]: [],
    }
    for original_port, guest_port in PUBLISHED:
        for source, original_ip in LOCAL_ENDPOINTS:
            specs[NAT["output"]].append(
                ["-p", "tcp", "-s", source, "-d", original_ip, "--dport", str(original_port), "-j", "DNAT", "--to-destination", f"{GUEST_IP}:{guest_port}"]
            )
        for source, original_ip in ((CLIENT_NET, PUBLISH_IP), (LOCALHOST, LOCALHOST)):
            specs[NAT["postrouting"]].append(
                [
                    "-p", "tcp", "-s", source, "-d", GUEST_IP, "--dport", str(guest_port),
                    "-m", "conntrack", "--ctstate", "DNAT",
                    "--ctorigdst", original_ip, "--ctorigdstport", str(original_port),
                    "-j", "SNAT", "--to-source", BRIDGE_IP,
                ]
            )
    for original_port, guest_port in PUBLISHED_UDP:
        specs[NAT["prerouting"]].append(
            ["-p", "udp", "-s", CLIENT_NET, "-d", PUBLISH_IP, "--dport", str(original_port), "-j", "DNAT", "--to-destination", f"{GUEST_IP}:{guest_port}"]
        )
        for source, original_ip in LOCAL_ENDPOINTS:
            specs[NAT["output"]].append(
                ["-p", "udp", "-s", source, "-d", original_ip, "--dport", str(original_port), "-j", "DNAT", "--to-destination", f"{GUEST_IP}:{guest_port}"]
            )
        for source, original_ip in ((CLIENT_NET, PUBLISH_IP), (LOCALHOST, LOCALHOST)):
            specs[NAT["postrouting"]].append(
                [
                    "-p", "udp", "-s", source, "-d", GUEST_IP, "--dport", str(guest_port),
                    "-m", "conntrack", "--ctstate", "DNAT",
                    "--ctorigdst", original_ip, "--ctorigdstport", str(original_port),
                    "-j", "SNAT", "--to-source", BRIDGE_IP,
                ]
            )
    return specs


def private_chain_specs() -> Iterable[tuple[bool, str, str, list[list[str]]]]:
    for chain, rules in filter4_specs().items():
        yield False, "filter", chain, rules
    for chain, rules in filter6_specs().items():
        yield True, "filter", chain, rules
    for chain, rules in nat_specs().items():
        yield False, "nat", chain, rules


def verify_private_chains() -> None:
    """Check every private chain's complete rule set, including no extras."""

    for v6, table, chain, expected in private_chain_specs():
        base = iptables(v6, table)
        if not rule_exists(base + ["-S", chain]):
            raise RuleError(f"missing private chain: {table}/{chain}")
        save_binary = binary("ip6tables-save" if v6 else "iptables-save")
        listing = command([save_binary, "-t", table]).stdout.splitlines()
        actual = [line for line in listing if line.startswith(f"-A {chain} ")]
        if len(actual) != len(expected):
            raise RuleError(
                f"private chain rule count mismatch in {table}/{chain}: "
                f"expected {len(expected)}, found {len(actual)}; inspect iptables-save"
            )
        for rule in expected:
            if not rule_exists(base + ["-C", chain, *rule]):
                raise RuleError(f"private chain rule mismatch in {table}/{chain}: {' '.join(rule)}")


def drop_counters(backend: str) -> list[dict[str, object]]:
    """Return only dedicated DROP counters, separate from sampled LOG records."""

    require_root()
    check_backend(backend)
    counter_pattern = re.compile(r"^\[(?P<packets>\d+):(?P<bytes>\d+)\] (?P<rule>-A .+)$")
    result: list[dict[str, object]] = []
    for v6, table, chain, _expected in private_chain_specs():
        save_binary = binary("ip6tables-save" if v6 else "iptables-save")
        lines = command([save_binary, "-c", "-t", table]).stdout.splitlines()
        for line in lines:
            if not line.startswith(f"[") or f" -A {chain} " not in line or " -j DROP" not in line:
                continue
            match = counter_pattern.match(line)
            if not match:
                raise RuleError(f"cannot parse iptables-save counter line: {line}")
            result.append(
                {
                    "family": 6 if v6 else 4,
                    "table": table,
                    "chain": chain,
                    "packets": int(match.group("packets")),
                    "bytes": int(match.group("bytes")),
                    "rule": match.group("rule"),
                }
            )
    return result


def existing_private_chains() -> list[tuple[bool, str, str]]:
    return [
        (v6, table, chain)
        for v6, table, chain in chain_rules()
        if rule_exists(iptables(v6, table) + ["-S", chain])
    ]


def refresh_private_chains() -> None:
    """Flush and rebuild only this script's private chains.

    The caller must ensure the VM is stopped.  The shared INPUT/FORWARD/
    OUTPUT and NAT chains are never passed to ``-F`` here.
    """

    for v6, table, chain in chain_rules():
        ensure_chain(v6=v6, table=table, chain=chain)
        command(iptables(v6, table) + ["-F", chain])
    add_filter_rules()
    add_nat_rules()


def add_filter_rules() -> None:
    # Guest -> host: permit only replies to a connection already opened by a
    # permitted client; drop everything else by interface, including spoofed
    # 172.30.61.2 traffic.
    for chain, rules in filter4_specs().items():
        for rule in rules:
            ensure_rule(table="filter", parent=chain, args=rule)
    # No IPv6 address, route, NAT, or published service is part of this
    # design.  Drop every packet touching this bridge in the IPv6 paths.
    for chain, rules in filter6_specs().items():
        for rule in rules:
            ensure_rule(v6=True, table="filter", parent=chain, args=rule)


def add_nat_rules() -> None:
    for chain, rules in nat_specs().items():
        for rule in rules:
            ensure_rule(table="nat", parent=chain, args=rule)


def add_jumps() -> None:
    ensure_top_jump(table="filter", parent="INPUT", chain=FILTER4["input"], match=["-i", BRIDGE])
    ensure_top_jump(table="filter", parent="FORWARD", chain=FILTER4["forward_from"], match=["-i", BRIDGE])
    ensure_top_jump(table="filter", parent="FORWARD", chain=FILTER4["forward_to"], match=["-o", BRIDGE])
    ensure_top_jump(table="filter", parent="OUTPUT", chain=FILTER4["output"], match=["-o", BRIDGE])
    ensure_top_jump(v6=True, table="filter", parent="INPUT", chain=FILTER6["input"], match=["-i", BRIDGE])
    ensure_top_jump(v6=True, table="filter", parent="FORWARD", chain=FILTER6["forward_from"], match=["-i", BRIDGE])
    ensure_top_jump(v6=True, table="filter", parent="FORWARD", chain=FILTER6["forward_to"], match=["-o", BRIDGE])
    ensure_top_jump(v6=True, table="filter", parent="OUTPUT", chain=FILTER6["output"], match=["-o", BRIDGE])
    ensure_top_jump(table="nat", parent="PREROUTING", chain=NAT["prerouting"], match=[])
    ensure_top_jump(table="nat", parent="OUTPUT", chain=NAT["output"], match=[])
    ensure_top_jump(table="nat", parent="POSTROUTING", chain=NAT["postrouting"], match=[])


def apply(backend: str, *, allow_missing_bridge: bool = False, refresh_private: bool = False) -> None:
    require_root()
    check_backend(backend)
    if maintenance_active():
        maintenance_gate().install()
    require_bridge(allow_missing=allow_missing_bridge)
    if not forwarding_enabled():
        raise RuleError("IPv4 forwarding is disabled; refusing to change sysctl implicitly")
    existing = existing_private_chains()
    total = len(list(chain_rules()))
    if existing and not refresh_private:
        if len(existing) != total:
            raise RuleError(
                "some private chains already exist; refusing a partial append; "
                "rerun --refresh-private while winvm01 is stopped"
            )
        # This blocks the old append behavior from putting LOG after a DROP.
        verify_private_chains()
    if refresh_private:
        require_vm_stopped()
        refresh_private_chains()
    else:
        for v6, table, chain in chain_rules():
            ensure_chain(v6=v6, table=table, chain=chain)
        add_filter_rules()
        add_nat_rules()
    add_jumps()
    if maintenance_active():
        maintenance_gate().verify()


def remove(backend: str) -> None:
    require_root()
    check_backend(backend)
    require_vm_stopped()
    jumps = [
        (False, "filter", "INPUT", ["-i", BRIDGE, "-j", FILTER4["input"]]),
        (False, "filter", "FORWARD", ["-i", BRIDGE, "-j", FILTER4["forward_from"]]),
        (False, "filter", "FORWARD", ["-o", BRIDGE, "-j", FILTER4["forward_to"]]),
        (False, "filter", "OUTPUT", ["-o", BRIDGE, "-j", FILTER4["output"]]),
        (True, "filter", "INPUT", ["-i", BRIDGE, "-j", FILTER6["input"]]),
        (True, "filter", "FORWARD", ["-i", BRIDGE, "-j", FILTER6["forward_from"]]),
        (True, "filter", "FORWARD", ["-o", BRIDGE, "-j", FILTER6["forward_to"]]),
        (True, "filter", "OUTPUT", ["-o", BRIDGE, "-j", FILTER6["output"]]),
        (False, "nat", "PREROUTING", ["-j", NAT["prerouting"]]),
        (False, "nat", "OUTPUT", ["-j", NAT["output"]]),
        (False, "nat", "POSTROUTING", ["-j", NAT["postrouting"]]),
    ]
    for v6, table, parent, args in jumps:
        remove_rule_copies(v6=v6, table=table, parent=parent, args=args)
    for v6, table, chain in chain_rules():
        base = iptables(v6, table)
        if rule_exists(base + ["-S", chain]):
            command(base + ["-F", chain])
            command(base + ["-X", chain])


def preflight() -> dict[str, object]:
    result: dict[str, object] = {"read_only": True, "bridge": BRIDGE}
    for name in ("ip", "iptables", "ip6tables", "ss"):
        path = shutil.which(name)
        result[f"{name}_path"] = path
    if shutil.which("iptables"):
        result["iptables_version"] = command([binary("iptables"), "--version"], check=False).stdout.strip()
    if shutil.which("ip6tables"):
        result["ip6tables_version"] = command([binary("ip6tables"), "--version"], check=False).stdout.strip()
    result["bridge_present"] = bridge_present() if shutil.which("ip") else False
    result["bridge_expected_ipv4"] = bridge_has_expected_address() if result["bridge_present"] else False
    try:
        result["ipv4_forward"] = open("/proc/sys/net/ipv4/ip_forward", encoding="ascii").read().strip()
        result["ipv6_forward"] = open("/proc/sys/net/ipv6/conf/all/forwarding", encoding="ascii").read().strip()
    except OSError as exc:
        result["forwarding_read_error"] = str(exc)
    if shutil.which("iptables"):
        result["filter4"] = command([binary("iptables"), "-S"], check=False).stdout
        result["nat4"] = command([binary("iptables"), "-t", "nat", "-S"], check=False).stdout
    if shutil.which("ip6tables"):
        result["filter6"] = command([binary("ip6tables"), "-S"], check=False).stdout
    return result


def verify(backend: str) -> None:
    require_root()
    check_backend(backend)
    if maintenance_active():
        maintenance_gate().verify()
    checks = [
        (False, "filter", "INPUT", ["-i", BRIDGE, "-j", FILTER4["input"]]),
        (False, "filter", "FORWARD", ["-i", BRIDGE, "-j", FILTER4["forward_from"]]),
        (False, "filter", "FORWARD", ["-o", BRIDGE, "-j", FILTER4["forward_to"]]),
        (False, "filter", "OUTPUT", ["-o", BRIDGE, "-j", FILTER4["output"]]),
        (True, "filter", "INPUT", ["-i", BRIDGE, "-j", FILTER6["input"]]),
        (True, "filter", "FORWARD", ["-i", BRIDGE, "-j", FILTER6["forward_from"]]),
        (True, "filter", "FORWARD", ["-o", BRIDGE, "-j", FILTER6["forward_to"]]),
        (True, "filter", "OUTPUT", ["-o", BRIDGE, "-j", FILTER6["output"]]),
        (False, "nat", "PREROUTING", ["-j", NAT["prerouting"]]),
        (False, "nat", "OUTPUT", ["-j", NAT["output"]]),
        (False, "nat", "POSTROUTING", ["-j", NAT["postrouting"]]),
    ]
    for v6, table, parent, args in checks:
        if not rule_exists(iptables(v6, table) + ["-C", parent, *args]):
            raise RuleError(f"missing dedicated jump: {' '.join(args)} in {table}/{parent}")
    verify_private_chains()
    require_bridge(allow_missing=False)
    verify_interface_sysctls()
    print("winvm01 dedicated firewall/NAT chains, bridge address, and interface sysctls are present")


def print_systemd_unit() -> None:
    print("""[Unit]
Description=Reconcile isolated winvm01 firewall rules
Wants=network-pre.target
After=network-pre.target libvirtd.service virtnetworkd.service
Before=libvirt-guests.service
PartOf=libvirtd.service virtnetworkd.service

[Service]
Type=oneshot
ExecStart=/home/vm_run/scripts/lib/winvm01_network.py apply --backend iptables-nft --allow-missing-bridge
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
""")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("preflight", "sysctl", "apply", "verify", "counters", "remove", "systemd-unit"),
    )
    parser.add_argument("--backend", default=None, help="must be the preflight-confirmed iptables-nft")
    parser.add_argument("--allow-missing-bridge", action="store_true", help="only for boot-time rule reapply")
    parser.add_argument(
        "--refresh-private",
        action="store_true",
        help="flush/rebuild only private chains; use only while winvm01 is stopped",
    )
    parser.add_argument("--yes", action="store_true", help="required for remove")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.action == "preflight":
            print(json.dumps(preflight(), ensure_ascii=False, indent=2))
            return 0
        if args.action == "systemd-unit":
            print_systemd_unit()
            return 0
        if args.action == "sysctl":
            configure_sysctls()
            print("winvm01 interface sysctls configured")
            return 0
        if not args.backend:
            raise RuleError("--backend is required after read-only preflight")
        if args.action == "apply":
            apply(
                args.backend,
                allow_missing_bridge=args.allow_missing_bridge,
                refresh_private=args.refresh_private,
            )
            print("winvm01 rules applied")
        elif args.action == "verify":
            verify(args.backend)
        elif args.action == "counters":
            print(json.dumps(drop_counters(args.backend), ensure_ascii=False, indent=2))
        elif args.action == "remove":
            if not args.yes:
                raise RuleError("remove requires --yes; it only removes this script's private chains")
            remove(args.backend)
            print("winvm01 private rules removed")
        return 0
    except RuleError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
