"""Advertise the bridge on the local network over mDNS.

Optional, and off unless asked for. The point is to make the setup UI reachable
as a name rather than an address someone has to go and look up, which is the
same job Home Assistant's `homeassistant.local` does.

The reason it is not on by default is that it cannot work by default. mDNS is
multicast on the local link, so the address being advertised has to be one that
other machines on that link can actually route to. On Docker's default bridge
network the container holds a 172.x address inside a private namespace, and a
name resolving to that is worse than no name at all: it resolves, then times
out, and the failure looks like the service is down rather than like a
networking choice. So this refuses to advertise an address it can tell is
unreachable, and says why.

Requires `network_mode: host` (or a macvlan). Requires the `zeroconf` package,
which is optional: absent, this is a no-op with one log line.
"""

from __future__ import annotations

import atexit
import ipaddress
import logging
import os
import socket

logger = logging.getLogger("ma-music-skill.mdns")

try:
    from zeroconf import ServiceInfo, Zeroconf

    HAVE_ZEROCONF = True
except ImportError:  # pragma: no cover, exercised by the absence test
    ServiceInfo = Zeroconf = None
    HAVE_ZEROCONF = False


# Docker's own default pools. An address in one of these means the container is
# almost certainly on a bridge network, where advertising is pointless.
_DOCKER_POOLS = [
    ipaddress.ip_network("172.17.0.0/16"),
    ipaddress.ip_network("172.18.0.0/16"),
    ipaddress.ip_network("172.19.0.0/16"),
    ipaddress.ip_network("172.20.0.0/14"),
]

_zc = None


def enabled() -> bool:
    return (os.environ.get("MDNS", "") or "").strip().lower() in ("1", "true", "yes", "on")


def service_name() -> str:
    raw = (os.environ.get("MDNS_NAME") or "ampere").strip().lower()
    # A label, not a hostname: no dots, since the .local suffix is added here.
    return "".join(c for c in raw if c.isalnum() or c == "-").strip("-") or "ampere"


def primary_address() -> str | None:
    """The address other machines on this link would reach us on.

    Opening a UDP socket towards a public address and reading back the local
    end is the portable way to ask the routing table which interface would be
    used, without sending anything or needing the destination to exist.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed anywhere
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def reachable(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if parsed.is_loopback:
        return False
    return not any(parsed in pool for pool in _DOCKER_POOLS)


def advertise(port: int) -> bool:
    """Register the service. Returns whether anything was published."""
    global _zc

    if not enabled():
        return False
    if not HAVE_ZEROCONF:
        logger.warning(
            "MDNS is set but the zeroconf package is not installed, so no name "
            "is being advertised. Install it, or reach setup by address."
        )
        return False

    address = primary_address()
    if not address:
        logger.warning("MDNS is set but no outbound address could be determined")
        return False

    if not reachable(address):
        logger.warning(
            "MDNS is set but this container's address (%s) is not reachable "
            "from the local network, so no name is being advertised. mDNS "
            "needs host networking: a name pointing at a bridge address "
            "resolves and then times out, which looks like an outage rather "
            "than a networking choice. Use network_mode: host, or reach setup "
            "by address.",
            address,
        )
        return False

    name = service_name()
    try:
        info = ServiceInfo(
            "_http._tcp.local.",
            f"{name}._http._tcp.local.",
            addresses=[socket.inet_aton(address)],
            port=port,
            properties={"path": "/setup"},
            server=f"{name}.local.",
        )
        _zc = Zeroconf()
        _zc.register_service(info)
    except Exception:
        # Port 5353 is often already held by an Avahi or Bonjour responder on
        # the host. That is a reason to skip advertising, never a reason to
        # stop the bridge from serving music.
        logger.exception("mDNS registration failed, continuing without it")
        _zc = None
        return False

    atexit.register(stop)
    logger.info("advertising http://%s.local:%d/setup at %s", name, port, address)
    return True


def stop() -> None:
    global _zc
    if _zc is not None:
        try:
            _zc.close()
        finally:
            _zc = None
