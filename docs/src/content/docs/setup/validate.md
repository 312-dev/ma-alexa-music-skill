---
title: Validate the endpoint
description: Prove Amazon can reach the bridge before you create the skill, including the QR-code check.
sidebar:
  order: 6
---

Do this before creating the skill, not after. A skill pointed at an endpoint
Amazon cannot reach behaves exactly like a skill that is broken in a dozen other
ways, and Amazon tells you nothing.

## The setup wizard

The bridge serves a web wizard at `/setup` on its own hostname. It exists
because most of this work needs a browser anyway: `ask configure` opens one for
its OAuth flow, and the alias checker has to query your library live.

The wizard covers the whole path:

| Section | What it is for |
|---|---|
| `/setup/status` | The states that look identical from outside, including when Amazon last called the bridge |
| `/setup/endpoint` | The checks on this page, including the QR proof |
| `/setup/alias` | Alias checker against your own library, described on [Choosing an alias](../alias/) |
| `/setup/wizard` | Skill creation, catalogs, ingestion polling and enablement |
| `/setup/stations` | Station tuning, with a live preview of the artist pool for a seed |

:::caution[`/setup` refuses to serve unless `ADMIN_TOKEN` is set]
The wizard can create skills against your Amazon account and read your library,
so serving it open is not an option. With no token set it answers 503 and says
why. You log in with the token once, and the wizard holds a signed session
cookie for twelve hours. Rotating the token invalidates every session.
:::

:::note[The wizard is under active development]
The exact screens and wording will move. The checks below are what it performs,
and each one can be done by hand if you would rather not wait for the button.
:::

## The four checks, cheapest first

### 1. `PUBLIC_BASE` is not a private address

If the hostname in `PUBLIC_BASE` resolves into RFC1918 space, Amazon cannot
reach it from the internet, whatever else is true. This catches the most common
mistake in a fraction of a second.

```sh
getent hosts ampere.example.com
```

### 2. The TLS handshake succeeds, and the SAN is readable

The certificate's subject alternative names determine the manifest's
`sslCertificateType`:

```sh
openssl s_client -connect ampere.example.com:443 \
  -servername ampere.example.com </dev/null 2>/dev/null \
  | openssl x509 -noout -text \
  | grep -A1 'Subject Alternative Name'
```

A SAN of `ampere.example.com` means `Trusted`. A SAN of `*.example.com` means
`Wildcard`. The wizard reads this and sets the field rather than asking you to
choose, because the consequence of choosing wrong is invisible.

### 3. `/healthz` returns 200

```sh
curl -s https://ampere.example.com/healthz
# {"ok":true}
```

If this fails but the container answers on localhost, the problem is between
your reverse proxy and the bridge.

### 4. External proof: the QR check

The first three checks all run from inside your network, or from the bridge
itself. None of them prove that **the public internet** can reach the endpoint.
Split-horizon DNS, a hairpin NAT rule, a firewall that only allows local
sources: each of those passes the checks above and still leaves Amazon unable to
call you.

So the wizard mints a token, renders `PUBLIC_BASE/setup/verify/<token>` as a QR
code, and waits.

**Scan it with a phone that has WiFi turned off.** The request then arrives over
a mobile network, from a genuinely external address, on the same path Amazon
will use. The bridge sees the hit and the page goes green.

:::tip[Why this and not a third-party checker]
A hosted "is my port open" service tells you a TCP handshake completed. This
tells you an HTTPS request to your exact hostname, with your exact certificate,
reached your exact process. It needs no third party and no account.
:::

## Why this page exists

:::danger[A wrong endpoint produces silence, not an error]
If the host is not publicly reachable, or `sslCertificateType` does not match
the certificate, Amazon does not call the endpoint at all. There is no error in
the developer console, nothing in the skill's status, and nothing in your logs,
because nothing arrives.

The only observable symptom is that Alexa says something unhelpful and your
capture directory stays empty. That symptom is shared with several unrelated
faults, which is what makes it expensive.
:::

Validating first turns a whole class of later confusion into a check you already
did.

## Next

[Create the skill](../skill/).
