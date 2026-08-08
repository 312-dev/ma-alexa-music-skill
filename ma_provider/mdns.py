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

Requires `network_mode: host` (or a macvlan).

The responder is Music Assistant's own. MA already runs one shared
`AsyncZeroconf` for every provider that announces or discovers a service, so
this registers on `mass.discovery.aiozc` rather than standing up a second
responder that would then fight MA's for port 5353. Registration and teardown
are async, and the provider drives both from its own lifecycle: it holds `mass`
and it is the thing whose load and unload the advertisement should follow.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from typing import Any

from zeroconf.asyncio import AsyncServiceInfo

logger = logging.getLogger("ma-music-skill.mdns")

# A never-routed address (TEST-NET-1). Connecting a UDP socket toward it and
# reading back the local end is the portable way to ask the routing table which
# interface would be used, without sending anything or needing the destination
# to exist. MA's `get_source_ip_for_target` does exactly this.
_PROBE_TARGET = "192.0.2.1"

# Docker's own default pools. An address in one of these means the container is
# almost certainly on a bridge network, where advertising is pointless.
_DOCKER_POOLS = [
    ipaddress.ip_network("172.17.0.0/16"),
    ipaddress.ip_network("172.18.0.0/16"),
    ipaddress.ip_network("172.19.0.0/16"),
    ipaddress.ip_network("172.20.0.0/14"),
]

# The registered service and the responder it went on, kept so stop() can
# withdraw it. At module scope because advertise and stop are wired to two
# different points of the provider's lifecycle.
_info: AsyncServiceInfo | None = None
_aiozc: Any = None


def enabled() -> bool:
    return (os.environ.get("MDNS", "") or "").strip().lower() in ("1", "true", "yes", "on")


def service_name() -> str:
    raw = (os.environ.get("MDNS_NAME") or "ma-alexa").strip().lower()
    # A label, not a hostname: no dots, since the .local suffix is added here.
    return "".join(c for c in raw if c.isalnum() or c == "-").strip("-") or "ma-alexa"


async def primary_address() -> str | None:
    """The address other machines on this link would reach us on.

    Delegates to Music Assistant's own routing-table probe rather than opening a
    socket by hand, so the bridge asks the interface question the same way MA
    asks it everywhere else. MA answers "" when no route can be determined;
    normalise that to None.
    """
    from music_assistant.helpers.util import get_source_ip_for_target

    return (await get_source_ip_for_target(_PROBE_TARGET)) or None


def reachable(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if parsed.is_loopback:
        return False
    return not any(parsed in pool for pool in _DOCKER_POOLS)


async def _addresses(address: str) -> list[bytes]:
    """The advertised address, packed the way zeroconf wants it.

    Via MA's helper, which handles both address families where the previous
    `socket.inet_aton` was IPv4 only.
    """
    from music_assistant.helpers.util import get_ip_pton

    return [await get_ip_pton(address)]


async def advertise(mass: Any, port: int) -> bool:
    """Register the service on MA's shared responder. Returns whether it published.

    Takes the port rather than reading it, because the two adapters do not
    listen on the same one: standalone owns its port outright, while inside
    Music Assistant this is a second listener alongside MA's own.
    """
    global _info, _aiozc

    if not enabled():
        return False

    address = await primary_address()
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
        info = AsyncServiceInfo(
            "_http._tcp.local.",
            f"{name}._http._tcp.local.",
            addresses=await _addresses(address),
            port=port,
            properties={"path": "/setup"},
            server=f"{name}.local.",
        )
        await mass.discovery.aiozc.async_register_service(info)
    except Exception:
        # Port 5353 is often already held by an Avahi or Bonjour responder on
        # the host, and a name still registered from a prior load raises too.
        # Either is a reason to skip advertising, never a reason to stop the
        # bridge from serving music.
        logger.exception("mDNS registration failed, continuing without it")
        _info = _aiozc = None
        return False

    _info = info
    _aiozc = mass.discovery.aiozc
    logger.info("advertising http://%s.local:%d/setup at %s", name, port, address)
    return True


async def stop() -> None:
    global _info, _aiozc
    if _info is not None and _aiozc is not None:
        try:
            await _aiozc.async_unregister_service(_info)
        finally:
            _info = _aiozc = None
