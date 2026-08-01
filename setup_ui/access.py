"""Who is allowed to reach the setup UI, and from where.

The bridge has two planes on one port and they have opposite exposure needs.
The data plane has to be reachable from the public internet, because Amazon
calls it directly and there is no way to ask them to come from somewhere else.
The admin plane can create skills against the operator's Amazon account, read
their whole library and rewrite settings, and Amazon never needs it at all.

Serving both on the same public surface behind one shared secret means the
admin plane is exposed by construction and the only thing between an attacker
and it is a token they can guess at line rate. So this narrows it two ways:

- By source address. Private, loopback, link-local and carrier-grade NAT ranges
  are allowed by default, everything else is refused. Tailscale lives in
  100.64.0.0/10, which is why CGNAT is on the list rather than off it.
- By failure count. Repeated bad tokens from one address lock that address out
  for a while, so a token cannot be brute forced even from an allowed network.

Behind a reverse proxy every request appears to come from the proxy, so the
real client has to be read out of X-Forwarded-For. Trusting that header blindly
would be worse than not checking at all, because anyone could set it. It is
therefore only consulted when the immediate peer is itself a trusted proxy,
which is the same rule Home Assistant applies with trusted_proxies.
"""

from __future__ import annotations

import ipaddress
import os
import threading
import time

# Loopback, RFC1918, link-local, unique-local v6, and CGNAT. CGNAT is included
# deliberately: 100.64.0.0/10 is where Tailscale addresses live, and reaching
# the setup UI over a tailnet is a legitimate and common way to do this.
_PRIVATE = [
    ipaddress.ip_network(n) for n in (
        "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
        "169.254.0.0/16", "100.64.0.0/10",
        "::1/128", "fc00::/7", "fe80::/10",
    )
]

LOCKOUT_THRESHOLD = int(os.environ.get("SETUP_LOCKOUT_THRESHOLD", "5"))
LOCKOUT_SECONDS = int(os.environ.get("SETUP_LOCKOUT_SECONDS", "900"))

_FAILURES: dict[str, tuple[int, float]] = {}
_LOCK = threading.Lock()


def _networks(raw: str) -> list:
    out = []
    for part in raw.replace(",", " ").split():
        try:
            out.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    return out


def allowed_networks() -> list | None:
    """Networks the setup UI answers on. None means anywhere.

    SETUP_ALLOW_NETWORKS takes "private" (the default), "any", or an explicit
    list of CIDRs. "any" is offered because someone will genuinely need it, and
    a documented switch is better than the workaround people reach for
    otherwise, which is deleting the check.
    """
    raw = (os.environ.get("SETUP_ALLOW_NETWORKS") or "private").strip().lower()
    if raw == "any":
        return None
    if raw == "private":
        return list(_PRIVATE)
    return _networks(raw) or list(_PRIVATE)


def trusted_proxies() -> list:
    return _networks(os.environ.get("TRUSTED_PROXIES") or "")


def _ip(value: str):
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def client_ip(remote_addr: str | None, forwarded_for: str | None) -> str:
    """The address to judge this request by.

    X-Forwarded-For is only read when the immediate peer is a configured
    trusted proxy. Otherwise the header is attacker-controlled and consulting
    it would let anyone claim to be on the LAN.

    Walks right to left and returns the first address that is not itself a
    trusted proxy, so a chain of proxies does not hide the real client.
    """
    peer = (remote_addr or "").strip()
    trusted = trusted_proxies()
    if not trusted or not forwarded_for:
        return peer

    address = _ip(peer)
    if address is None or not any(address in net for net in trusted):
        return peer

    for hop in reversed([h.strip() for h in forwarded_for.split(",") if h.strip()]):
        candidate = _ip(hop)
        if candidate is None:
            continue
        if any(candidate in net for net in trusted):
            continue
        return str(candidate)
    return peer


def forwarded_untrusted(remote_addr: str | None, forwarded_for: str | None) -> bool:
    """A proxied request whose real client cannot be determined.

    This is the case that made the source-address check useless on the most
    common deployment there is. A reverse proxy on the same host, which is what
    Caddy, Traefik and cloudflared all are by default, dials the bridge on
    loopback. So a request that came from the public internet arrives looking
    like 127.0.0.1, sails through the private-network rule, and the admin plane
    is exposed exactly as if there were no check at all.

    Falling back to judging the peer is the wrong direction here. If a request
    carries X-Forwarded-For and the peer is not a proxy we were told to trust,
    the honest answer is that we do not know who is asking, and the admin plane
    is not something to hand out on a guess. Naming the proxy in
    TRUSTED_PROXIES turns the header back on and the real client gets judged.

    Direct access over the LAN or a tailnet carries no such header and is
    unaffected.
    """
    if not forwarded_for:
        return False
    trusted = trusted_proxies()
    if not trusted:
        return True
    address = _ip((remote_addr or "").strip())
    return address is None or not any(address in net for net in trusted)


def peer_is_trusted(remote_addr: str | None) -> bool:
    """Is the immediate peer a proxy we were told to trust."""
    trusted = trusted_proxies()
    if not trusted:
        return False
    address = _ip((remote_addr or "").strip())
    return address is not None and any(address in net for net in trusted)


def address_allowed(ip: str) -> bool:
    networks = allowed_networks()
    if networks is None:
        return True
    address = _ip(ip)
    if address is None:
        # An unparseable peer is not a reason to open the door. This happens
        # with unix sockets, where remote_addr can be empty.
        return False
    return any(address in net for net in networks)


def locked_out(ip: str) -> int:
    """Seconds remaining on a lockout, or 0."""
    with _LOCK:
        count, until = _FAILURES.get(ip, (0, 0.0))
    if count < LOCKOUT_THRESHOLD:
        return 0
    return max(0, int(until - time.time()))


def record_failure(ip: str) -> None:
    with _LOCK:
        count, _ = _FAILURES.get(ip, (0, 0.0))
        _FAILURES[ip] = (count + 1, time.time() + LOCKOUT_SECONDS)
        # Bounded, so a spray from many addresses cannot grow this forever.
        if len(_FAILURES) > 4096:
            for key, (_, until) in list(_FAILURES.items()):
                if until < time.time():
                    _FAILURES.pop(key, None)


def record_success(ip: str) -> None:
    with _LOCK:
        _FAILURES.pop(ip, None)


def reset() -> None:
    with _LOCK:
        _FAILURES.clear()
