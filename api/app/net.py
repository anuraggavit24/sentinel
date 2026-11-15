"""Client IP behind proxies. X-Forwarded-For is only trusted when it arrives from a trusted proxy;
otherwise anyone could set the header and choose their own IP."""
from __future__ import annotations

import ipaddress
from typing import Iterable, Optional


def _parse(ip: str) -> Optional[ipaddress._BaseAddress]:
    try:
        addr = ipaddress.ip_address(ip.strip().strip("[]"))
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        return addr.ipv4_mapped
    return addr


def parse_networks(spec: str) -> list[ipaddress._BaseNetwork]:
    return [ipaddress.ip_network(p.strip(), strict=False) for p in spec.split(",") if p.strip()]


def client_ip(peer: Optional[str], forwarded_for: Optional[str], trusted: Iterable[ipaddress._BaseNetwork]) -> Optional[str]:
    """Walk X-Forwarded-For from the right, skipping trusted proxies; the first untrusted hop is the client.
    If every hop is trusted (e.g. a local demo where the browser is on the same machine), use the leftmost."""
    trusted = list(trusted)

    def is_trusted(a: ipaddress._BaseAddress) -> bool:
        return any(a in n for n in trusted)

    peer_addr = _parse(peer) if peer else None
    if peer_addr is None:
        return None
    if not is_trusted(peer_addr) or not forwarded_for:
        return str(peer_addr)
    hops = [h for h in (_parse(x) for x in forwarded_for.split(",")) if h is not None]
    for hop in reversed(hops):
        if not is_trusted(hop):
            return str(hop)
    return str(hops[0]) if hops else str(peer_addr)
