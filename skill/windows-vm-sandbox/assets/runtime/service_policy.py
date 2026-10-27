"""Pure validation and canonicalization for optional published VM services."""
from __future__ import annotations

import ipaddress
import re
from typing import Any


_ID = re.compile(r"[a-z][a-z0-9-]{1,31}\Z")
_FIELDS = {
    "id", "protocol", "publish_ip", "publish_port", "guest_port",
    "allowed_sources", "access", "snat_to_bridge", "allow_during_maintenance",
}


def normalize(network: dict[str, Any]) -> list[dict[str, Any]]:
    """Return validated extra services; legacy RDP/WinRM remain implicit."""
    raw = network.get("published_services", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ValueError("network.published_services must be a list")
    # Profiles created before extra-service support (and minimal registry test
    # fixtures) may only carry the legacy management network fields.  An empty
    # declaration has no policy to validate, so it must remain a true no-op;
    # the stricter network/subnet checks below apply only to actual extras.
    if not raw:
        return []
    subnet = ipaddress.ip_network(network["subnet"], strict=True)
    client = ipaddress.ip_network(network["client_cidr"], strict=True)
    publish_ip = ipaddress.ip_address(network["publish_ip"])
    legacy_publish = {network["rdp_port"], network["winrm_port"]}
    ids: set[str] = set()
    publish_ports: set[int] = set()
    guest_ports: set[int] = set()
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each published service must be an object")
        unknown = set(item) - _FIELDS
        if unknown:
            raise ValueError("unknown published service field: " + sorted(unknown)[0])
        ident = item.get("id")
        if not isinstance(ident, str) or not _ID.fullmatch(ident) or ident in {"rdp", "winrm"} or ident in ids:
            raise ValueError("published service id is invalid or duplicated")
        protocol = item.get("protocol", "tcp")
        if protocol != "tcp":
            raise ValueError("published service protocol must be tcp")
        try:
            external = int(item["publish_port"])
            guest = int(item["guest_port"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("published service ports must be integers") from exc
        if type(item.get("publish_port")) is not int or type(item.get("guest_port")) is not int:
            raise ValueError("published service ports must be integers")
        if not 1 <= external <= 65535 or not 1 <= guest <= 65535:
            raise ValueError("published service ports must be 1..65535")
        if external in legacy_publish or external in publish_ports:
            raise ValueError("published service port conflicts with another service")
        if guest in {3389, 5986} or guest in guest_ports:
            raise ValueError("published guest port conflicts with management service")
        access = item.get("access", "host-loopback")
        if access not in {"host-loopback", "network"}:
            raise ValueError("published service access must be host-loopback or network")
        try:
            address = ipaddress.ip_address(item.get("publish_ip", "127.0.0.1"))
        except ValueError as exc:
            raise ValueError("published service publish_ip must be an IP address") from exc
        if access == "host-loopback":
            if address != ipaddress.ip_address("127.0.0.1"):
                raise ValueError("host-loopback service must publish on 127.0.0.1")
            sources = item.get("allowed_sources", ["127.0.0.1/32"])
            if sources != ["127.0.0.1/32"]:
                raise ValueError("host-loopback service must allow only 127.0.0.1/32")
        else:
            if address != publish_ip:
                raise ValueError("network service publish_ip must match network.publish_ip")
            sources = item.get("allowed_sources")
            if not isinstance(sources, list) or not sources:
                raise ValueError("network service needs explicit allowed_sources")
        if not isinstance(sources, list) or any(not isinstance(value, str) for value in sources):
            raise ValueError("allowed_sources must be a list of CIDR strings")
        canonical_sources: list[str] = []
        for value in sources:
            try:
                source = ipaddress.ip_network(value, strict=True)
            except ValueError as exc:
                raise ValueError("allowed_sources must contain valid CIDRs") from exc
            if source.version != 4 or source.prefixlen == 0 or source.overlaps(subnet):
                raise ValueError("allowed_sources must be bounded IPv4 networks outside guest subnet")
            if access == "host-loopback" and source == ipaddress.ip_network("127.0.0.1/32"):
                canonical_sources.append(str(source))
                continue
            if source.is_loopback or source.is_multicast or not source.subnet_of(client):
                raise ValueError("allowed_sources must be inside network.client_cidr")
            canonical_sources.append(str(source))
        allow_maintenance = item.get("allow_during_maintenance", False)
        if type(allow_maintenance) is not bool:
            raise ValueError("allow_during_maintenance must be boolean")
        if allow_maintenance and access != "host-loopback":
            raise ValueError("only host-loopback services may allow maintenance access")
        if item.get("snat_to_bridge", True) is not True:
            raise ValueError("published services require snat_to_bridge=true")
        ids.add(ident)
        publish_ports.add(external)
        guest_ports.add(guest)
        result.append({
            "id": ident, "protocol": "tcp", "publish_ip": str(address),
            "publish_port": external, "guest_port": guest,
            "allowed_sources": canonical_sources, "access": access,
            "snat_to_bridge": True, "allow_during_maintenance": allow_maintenance,
        })
    return result
