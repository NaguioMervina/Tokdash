# Security policy

## Reporting a vulnerability

If you find a security issue, please **do not** open a public GitHub issue.

Preferred:
- Use GitHub “Report a vulnerability” / Security Advisories (private report)

If that’s not available for your fork:
- Open a minimal issue without sensitive details and ask for a private contact channel

## Scope notes

- Tokdash is a **local** dashboard by default (`127.0.0.1` bind).
- Tokdash does **not** provide authentication/authorization for reads.
- If you run with `--bind 0.0.0.0`, you are exposing the dashboard to your LAN. Do not expose it to the public internet.

## Write-protection model

The API is unauthenticated, so **every state-changing request** — today `PUT /api/pricing-db`,
`POST /api/update-check`, and `POST /api/update-check/consent` (any `POST`/`PUT`/`PATCH`/`DELETE`) —
must clear a gate before it reaches a handler — it fails closed (an unknown bind is treated
as non-loopback):

- **Loopback bind required.** Mutating endpoints are served only when the effective bind is
  loopback. Bound to `0.0.0.0` (or any non-loopback address), writes return `403` — there is
  no safe way to expose a writable unauthenticated API.
- **Host/Origin allowlist.** `Host` (and any `Origin`/`Referer`) must be a loopback address
  derived from the configured bind/port. `Origin`/`Referer` are matched scheme-aware and
  HTTP-only. This blocks DNS-rebinding and writes arriving through **Tailscale Serve**: it
  forwards from `127.0.0.1` but carries the tailnet hostname as `Host` and an `https://`
  `Origin`, both of which are rejected. A malformed/unparseable `Referer` also fails closed
  (treated as cross-origin → `403`, never a `500`).
- **Per-session token.** A random token is minted each server start and required as
  `X-Tokdash-Token`. The dashboard fetches it from `GET /api/csrf-token` (itself loopback/
  same-origin gated, so another localhost port can't read it).

For remote access, prefer `tailscale serve` or `ssh -L` forwarding over a non-loopback bind.
The two differ for **writes**: **Tailscale Serve** requests are effectively read-only (their
foreign `Host` / `https` `Origin` fail the allowlist), but an **`ssh -L` forward to
`localhost`/`127.0.0.1` preserves a loopback `Host`, so writes from the SSH-authenticated user
are allowed by design** — SSH itself is the authentication layer there, and reliably
distinguishing a forwarded-localhost connection from a genuine local one is not possible from
HTTP headers. If you do not want SSH-forwarded writes, bind to a non-loopback address (which
disables all writes) or stop the service when you are done.

