# Web UI / operator playground (`/ui`)

An optional, hardened, **fully-offline** single-page operator playground, served
by the broker at `/ui`. It is **off by default** — set `BROKER_UI_ENABLED=true` to
enable it. When disabled, `/ui` returns `404` and no route is registered (behavior
is byte-for-byte v0.5.0).

Decision record: [ADR 0015](adr/0015-web-ui-playground.md) (extends
[ADR 0012](adr/0012-caching-runtime-control-observability.md)). Research:
[research/adr-0015-web-ui.md](research/adr-0015-web-ui.md).

## What it is

- A static page (vanilla HTML/CSS/JS) shipped as **package data** under
  `broker/ui/static/`. **No CDN, no external fonts/URLs, no build toolchain, and
  no new runtime dependency** — it works on an air-gapped host.
- A thin client over the broker's **existing authenticated API**. Enabling the UI
  adds **no new unauthenticated endpoints** — only the static routes `GET /ui`,
  `GET /ui/`, and `GET /ui/{path:path}`.

## Enable it

```bash
# env var (or BROKER_UI_ENABLED=true in your .env)
export BROKER_UI_ENABLED=true
broker                       # or: python -m broker
# open http://<host>:<port>/ui in a browser
```

With Docker Compose, set `BROKER_UI_ENABLED=true` in the service environment.

On the page:

1. Leave **Broker base URL** blank to use the same origin (recommended), or point
   it at another broker.
2. Paste your **API key** (and, to unlock the admin panel, an **admin key**).
3. Click **Connect**. The model list loads; the Admin and Rerank tabs appear only
   when applicable.

## Features

- **Models** — `GET /v1/models`: id, backend, capabilities, pool/router
  candidates, context limit.
- **Chat playground** — model or `auto`, temperature, optional system prompt,
  streaming via Server-Sent Events consumed over `fetch()` + `ReadableStream`.
  Shows the `x-broker-route`, `x-broker-cache`, rate-limit, and request-id
  response headers.
- **Completions / Responses / Embeddings** — the other `/v1` endpoints.
- **Rerank** — hidden unless `/v1/rerank` exists (the UI probes it and treats a
  `404` as "not wired in"), so the tab lights up automatically if a future rerank
  endpoint is added.
- **Status** — `GET /status` (lifecycle, pools, router recent decisions,
  cache-aware stats, response-cache stats).
- **Usage** — `GET /usage` (scoped: a user key sees its own usage; an admin key
  sees all).
- **Admin** — appears only when an admin key is supplied: list models, load,
  unload, and reload the registry, each behind a confirmation prompt. These call
  the same `require_admin`-gated `/admin/*` endpoints as any other client.

Accessibility: labelled controls, visible keyboard focus, `Ctrl/Cmd+Enter` to send
a chat message, `aria-live` status regions, `prefers-color-scheme` dark/light, and
a responsive layout.

## Security model

Every `/ui` response (including 404s) carries:

```
Content-Security-Policy: default-src 'self'; base-uri 'none'; frame-ancestors 'none'; object-src 'none'; form-action 'self'
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
X-Frame-Options: DENY
Cache-Control: no-store
```

- **Strict CSP, no inline code.** `default-src 'self'` with **no `unsafe-inline`,
  no `unsafe-eval`, and no external origin**. All script is an external
  same-origin ES module; all style is an external stylesheet — there is no inline
  `<script>`/`<style>` and no `eval`/`Function` constructor. `connect-src`
  inherits `'self'`, so an injected script cannot exfiltrate a key off-origin.
- **Keys in memory only.** The API key and admin key are held in a JavaScript
  variable for the tab's lifetime and are **never written to `localStorage`,
  `sessionStorage`, or cookies**. Reloading the page clears them; the "Forget
  keys" button wipes them on demand.
- **Clickjacking.** `frame-ancestors 'none'` plus `X-Frame-Options: DENY`.
- **Path traversal.** Asset serving resolves the requested path and requires it to
  stay under the packaged UI directory, serving only allow-listed extensions;
  `..` traversal and non-asset files return `404` (no package file is disclosed).
- **No surface widening.** The UI uses the authenticated API; `/v1/*`, `/status`,
  `/usage`, and `/admin/*` still require their credentials.

See [security-model.md](security-model.md#web-ui-surface-adr-0015) and
[threat-model.md](threat-model.md).

## Limitations

- **Operator tool, not a product.** It is a lightweight playground for driving and
  inspecting a local broker, not a multi-user chat app.
- **No server-side session.** Keys live only in the browser tab; there is no
  login, no cookie, and nothing persisted. A reload means re-entering keys.
- **Streaming assembles into one output box.** Multi-turn chat history is not
  retained between sends (each send is a fresh request); the Clear button resets
  the view.
- **Rerank** is shown only if `/v1/rerank` is present; it is not part of the
  current endpoint surface.
- The UI inherits the broker's auth: with `BROKER_AUTH_ENABLED=false` the API
  needs no key, but running an open broker is for local development only.

## Verification / smoke evidence

Automated: `tests/test_ui.py` (26 tests) covers default-off 404s, the exact
security headers (on the index, assets, and 404s), the CSP having no `unsafe-*` or
external origin, shipped-asset hygiene (no external URLs, no inline script/style,
no `eval`), "enabling adds only `/ui*` routes", data endpoints still `401`, path
traversal blocked, content types, and a streaming-chat functional smoke.

Manual browser smoke (executed against a live broker with the fake backend,
`BROKER_UI_ENABLED=true`):

```bash
# headers + body served
curl -s -D - -o /dev/null http://127.0.0.1:8080/ui        # 200 + strict headers
curl -s http://127.0.0.1:8080/ui | grep 'broker playground'
curl -s -D - -o /dev/null http://127.0.0.1:8080/ui/app.js | grep -i content-type
# traversal blocked
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/ui/%2e%2e%2fconfig.py   # 404
```

Loaded in a real browser under the strict CSP with **no console violations**:
entering a key connected and listed models with capabilities; a streaming chat
rendered with `route: echo-fake` metadata; the rerank probe returned `404` so that
tab stayed hidden; `/v1/models` without a key returned `401`.

See [`examples/web-ui/`](../examples/web-ui/) for a ready-to-run fake-backend
setup.
