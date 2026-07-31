---
title: Reaching setup safely
description: The bridge has to be on the public internet. The setup UI must not be, and this is how that split is enforced.
sidebar:
  order: 6
---

The bridge has two planes on one port, and they have opposite exposure needs.

The **data plane** has to be reachable from the public internet. Amazon calls
`POST /music` directly, fetches `/stream` and `/art` for every track, and
refetches `/icons` on every manifest update. There is no way to ask them to
come from somewhere else.

The **admin plane** is `/setup`, `/diag` and `/captures`. It can create skills
against your Amazon developer account, read your music library, replay captured
Amazon traffic and rewrite settings. Amazon never asks for any of it.

Serving both on the same public surface behind one shared secret would put the
admin plane on the internet by construction, with nothing between it and an
attacker but a token they can guess at line rate. So it is narrowed two ways.

## By source address

`SETUP_ALLOW_NETWORKS` decides where the admin plane answers. The default is
`private`, meaning loopback, RFC1918, link-local, and carrier-grade NAT.

:::note[CGNAT is on the list on purpose]
Tailscale addresses live in `100.64.0.0/10`. Reaching setup over a tailnet is a
normal and good way to do this, so that range is allowed rather than excluded.
:::

```sh
SETUP_ALLOW_NETWORKS=private          # default
SETUP_ALLOW_NETWORKS=192.168.1.0/24   # an explicit list replaces the default
SETUP_ALLOW_NETWORKS=any              # off, see the warning below
```

The check runs before authentication, so a **valid token presented from the
internet is still refused**. That is deliberate: it means a leaked token is not
by itself enough.

:::danger[What `any` actually means]
`any` puts an interface that can create Amazon skills on the public internet
behind a single shared secret. It exists because some deployments genuinely
need it, and a documented switch is better than the alternative people reach
for, which is deleting the check. Prefer a VPN or a tailnet.
:::

## Behind a reverse proxy

Every request will appear to come from the proxy, so the real client has to be
read out of `X-Forwarded-For`. That header is only consulted when the immediate
peer is a proxy you have named:

```sh
TRUSTED_PROXIES=172.18.0.0/16
```

Without it the header is ignored entirely, because it is attacker-controlled
and trusting it blindly would be worse than not checking at all. Anyone could
set `X-Forwarded-For: 192.168.1.10` and claim to be on your LAN.

The chain is walked right to left and the first address that is not itself a
trusted proxy wins, so prepending a fake hop does not hide the real client.

This is the same rule Home Assistant applies with its `trusted_proxies` setting,
and for the same reason.

## Deny it at the proxy too

Belt and braces. Amazon needs `POST /music`, `/stream`, `/art`, `/icons`, the
OAuth routes and the legal pages. It does not need `/setup`, and blocking it at
the edge means a misconfiguration inside the container cannot expose it.

```text
music.example.com {
	@admin path /setup* /diag /captures
	respond @admin 404

	reverse_proxy 127.0.0.1:5056
}
```

```nginx
location ~ ^/(setup|diag|captures) { return 404; }
location / { proxy_pass http://127.0.0.1:5056; }
```

Returning 404 rather than 403 keeps the edge from confirming the paths exist.

With this in place you reach setup by going to the container directly on your
LAN, not through the public hostname.

## Guessing the token

Failed logins are counted per address and lock that address out:

```sh
SETUP_LOCKOUT_THRESHOLD=5
SETUP_LOCKOUT_SECONDS=900
```

The counter is in memory, so restarting the bridge clears it.

## How do I actually open it, then

By address and port, from a machine on an allowed network:

```
http://192.168.1.50:5056/setup
```

There is no `.local` name. Home Assistant gets `homeassistant.local` from mDNS
advertised by Avahi on the host, which needs host networking and a daemon this
image deliberately does not carry. If you want a name, the options in
increasing order of effort are a `hosts` entry on the machine you set up from,
a DNS record on your router or Pi-hole, or a second reverse-proxy virtual host
bound only to your LAN interface.

The one thing to avoid is giving setup a public hostname and relying on the
token alone.

## The exception

`/setup/verify/<token>` is reachable from anywhere, deliberately. Its whole
purpose is to be opened on a phone **with WiFi off**, to prove your endpoint
answers from the public internet the way Amazon's requests will. It carries a
random token that expires in fifteen minutes and reveals nothing.
