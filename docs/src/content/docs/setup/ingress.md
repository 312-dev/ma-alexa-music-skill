---
title: Public HTTPS endpoint
description: How Amazon reaches your bridge, why a reverse proxy beats a tunnel, and what the audio actually costs.
sidebar:
  order: 4
---

Amazon calls your bridge over the public internet, and then Amazon's audio
fetchers call it again for every track. This page is about that path.

It is the step most likely to be got wrong, and the failure is quiet: if the
endpoint is not reachable, or the certificate type in the manifest is wrong,
Amazon never calls you and reports nothing anywhere.

## Recommended: your own reverse proxy

If you already terminate TLS for anything else, put the bridge behind that. Not
because tunnels do not work, but for four specific reasons:

- **The audio does not traverse a third party.** Every track your household
  plays is a sustained transfer through whatever is in the middle. Keeping it on
  infrastructure you control means no one else is carrying your library.
- **No terms-of-service grey area.** Sustained non-HTML content through a free
  tier of a CDN or tunnel provider is at best undefined and at worst against the
  terms. A reverse proxy on your own host has no such question.
- **No relay latency against a hard budget.** Amazon's `Initiate` directive has
  a 100ms p50 and 400ms p99 budget. A relay hop is spent before your code runs.
- **No dependency that can change its terms.** Certificates are already solved
  by Caddy or Traefik. Adding a provider adds something that can change under
  you.

### Caddy

Caddy obtains and renews the certificate itself. This is the whole
configuration:

```caddyfile
ampere.example.com {
	reverse_proxy 127.0.0.1:5056
}
```

Caddy streams responses and passes `Range` requests and `206 Partial Content`
straight through, which is what the `/stream` proxy needs.

### Traefik

As Docker labels on the bridge's container:

```yaml
labels:
  - "traefik.enable=true"
  - "traefik.http.routers.ampere.rule=Host(`ampere.example.com`)"
  - "traefik.http.routers.ampere.entrypoints=websecure"
  - "traefik.http.routers.ampere.tls.certresolver=letsencrypt"
  - "traefik.http.services.ampere.loadbalancer.server.port=5056"
```

Or as a file-provider router, if you prefer to keep routing out of the container:

```yaml
http:
  routers:
    ampere:
      rule: "Host(`ampere.example.com`)"
      entryPoints: [websecure]
      service: ampere
      tls:
        certResolver: letsencrypt
  services:
    ampere:
      loadBalancer:
        servers:
          - url: "http://127.0.0.1:5056"
```

### What the proxy must not do

- **Do not buffer response bodies.** Audio is streamed with no `Content-Length`
  when the Subsonic server is transcoding. A proxy that buffers the whole body
  before forwarding turns every track start into a long pause.
- **Do not strip `Range`, `Accept-Ranges` or `Content-Range`.** The bridge
  forwards all three, and Alexa issues ranged GETs.
- **Do not add authentication in front of `/stream`, `/art` or `/icons`.**
  Amazon fetches those, and it has no credentials. They carry their own
  expiring HMAC signature instead.
- **Do not add caching headers to `/icons`.** The bridge deliberately serves
  those with no cache validators, for
  [a reason worth reading](../../how-it-works/findings/#icons-must-carry-no-cache-validators).

## Certificate type

The skill manifest carries an `sslCertificateType` field that has to match
reality:

| Certificate | Manifest value |
|---|---|
| Certificate whose SAN covers the exact hostname | `Trusted` |
| Wildcard certificate, for example `*.example.com` | `Wildcard` |

Getting this wrong means Amazon never calls the endpoint, with no error surfaced
anywhere. The [setup wizard](../validate/) reads the SAN off the live TLS
handshake and sets the field for you rather than asking.

## Bandwidth

The `/stream` proxy transcodes to MP3 at 256 kbit/s.

| Listening | Transfer |
|---|---|
| One hour | about 115 MB |
| Three hours a day, one month | about 10 GB |
| Four Echoes, one hour, same group | about 115 MB |

That last row is the interesting one. **Multi-room does not multiply the
transfer.** Alexa fetches the stream once and distributes it locally within the
group. This was confirmed from bridge logs while four Echoes played the same
queue.

## Behind CGNAT, or cannot open ports

If you genuinely cannot expose 443, a tunnel will work. Cloudflare Tunnel and
Tailscale Funnel both terminate TLS for you and both have been used to reach a
service like this one. Use one if you must, with the caveats above understood
rather than discovered:

- Your audio traverses the provider. All of it, every hour you listen.
- Sustained non-HTML transfer through a free tier is a terms question you should
  answer for yourself before your library depends on it.
- The relay hop is added to every request, including the ones against Amazon's
  400ms p99 budget for `Initiate`.
- The certificate, and therefore the correct `sslCertificateType`, is the
  provider's rather than yours. A tunnel hostname under a shared domain is
  usually `Wildcard`. Verify rather than assume.

Ampere does not automate tunnel configuration and will not ask for a Cloudflare
API token or a Tailscale auth key. Set the tunnel up with the provider's own
tooling, point it at the bridge's port, and give the resulting hostname to the
wizard as `PUBLIC_BASE`.

## Next

[Deploy the bridge](../deploy/).
