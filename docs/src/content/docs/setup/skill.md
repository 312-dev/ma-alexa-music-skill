---
title: Create the skill
description: The manifest fields that matter, account linking, and enabling the skill in the development stage.
sidebar:
  order: 7
---

With the endpoint validated, the skill can be created. The wizard does this from
a manifest template; this page explains what goes into it and why, so that the
result is inspectable rather than magic.

## An HTTPS endpoint, not a Lambda

Amazon's music-skill documentation only shows a Lambda ARN. An HTTPS endpoint
works, and no AWS account is required.

The evidence is in the rejection: SMAPI refuses a music-skill manifest with
`MISSING_REQUIRED_PROPERTY: sslCertificateType`, and `sslCertificateType` is a
field that only applies to HTTPS endpoints. The API expects one; the
documentation happens not to show it.

The endpoint is your `PUBLIC_BASE`. The bridge accepts directives on `POST /`
and `POST /music`, so either form works.

## Manifest fields that matter

| Field | Value | Why |
|---|---|---|
| Invocation name | Your [alias](../alias/) | The word you say. Expensive to change later. |
| Endpoint URI | `https://ampere.example.com/music` | Where directives are POSTed. |
| `sslCertificateType` | `Trusted` or `Wildcard` | Must match the certificate's SAN. Wrong value means Amazon never calls. See [Validate the endpoint](../validate/). |
| Large icon URI | `https://ampere.example.com/icons/ampere-512.png` | Fetched by Amazon's validator on every manifest update. |
| Small icon URI | `https://ampere.example.com/icons/ampere-108.png` | Same. |
| Privacy policy URL | `https://ampere.example.com/privacy` | The bridge serves a minimal one. |
| Terms of use URL | `https://ampere.example.com/terms` | The bridge serves a minimal one. |
| Distribution | Private, development stage | It is not going to the store. |

:::caution[The icons have to be fetchable, every time]
Amazon's manifest validator re-fetches both icons on every update and reports a
failure to fetch as `RESOURCE_NOT_FOUND` against `largeIconUri`, failing the
whole update. The bridge serves `/icons` with no cache validators specifically to
prevent that. If you put a caching proxy in front of it, you can reintroduce the
failure. [The full account is in Findings](../../how-it-works/findings/#icons-must-carry-no-cache-validators).
:::

## Account linking

Alexa requires account linking before a music skill becomes usable, even for a
private single-user skill. The bridge implements the authorization-code grant
itself, so there is no identity provider to run.

| Setting | Value |
|---|---|
| Grant type | Authorization code |
| Authorization URI | `https://ampere.example.com/oauth/authorize` |
| Access token URI | `https://ampere.example.com/oauth/token` |
| Client ID | Your `OAUTH_CLIENT_ID` |
| Client secret | Your `OAUTH_CLIENT_SECRET` |

Client authentication is accepted either as form parameters or as HTTP Basic,
because Amazon has used both.

When you link the skill in the Alexa app, the bridge serves a single form asking
for a linking passphrase. That is your `OAUTH_LINK_SECRET`. Type it once. The
bridge then issues an access token valid for 30 days by default
(`OAUTH_ACCESS_TTL`) and a refresh token valid for a year.

Every token is a self-contained HMAC-signed blob rather than a database row,
which is what keeps it correct across gunicorn workers. It also means rotating
`SIGNING_KEY` invalidates every issued token and forces a relink.

:::note[Redirect URIs are pinned]
The bridge only accepts redirects to Amazon's own per-vendor link endpoints:
`alexa.amazon.com`, `pitangui.amazon.com`, `layla.amazon.com` and
`alexa.amazon.co.jp`, all under `/api/skill/link/`. Anything else is rejected as
`invalid_redirect_uri`. If linking fails with that error, the response body
includes the URI Amazon actually sent.
:::

## The landing page

The Alexa app opens the skill's endpoint root in a webview. Serving only POST
there returns a 405 with no body, which the webview renders as a blank black
screen. The bridge answers `GET /` with a small landing page for that reason.
This is cosmetic, and worth knowing before you assume the blank screen means
something broke.

## Enable it

A created skill is not an enabled skill, and an unenabled skill fails silently
by falling back to your default music provider.

```sh
ask smapi set-skill-enablement --skill-id <skill-id> --stage development
```

Stage is `development` throughout. The skill is not certified and is not going
to the store.

## Next

[Catalog and enablement](../catalog/). The catalog is what turns spoken names
into entity ids, and uploading it has a trap in it.
