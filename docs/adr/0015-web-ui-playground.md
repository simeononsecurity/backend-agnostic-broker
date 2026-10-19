# ADR 0015: Secure offline web UI / operator playground at /ui (default-off)

- Status: Accepted
- Date: 2026-10-19
- Supersedes: none
- Extends: ADR 0012 (reserved the inert `BROKER_UI_ENABLED` flag and shipped the
  admin + cache APIs this UI consumes)
- Related: ADR 0006 (OpenAI endpoint surface the playground drives); ADR 0008
  (virtual keys / admin scope); ADR 0012 (runtime control + caching + tracing);
  `docs/roadmap.md` item 12; research:
  `docs/research/adr-0015-web-ui.md` (19 primary sources).

## Context

The broker exposes an OpenAI-compatible API, `/status`, `/usage`, and an admin
control plane (ADR 0012), but no UI. Peer local-LLM gateways ship an operator
playground — llama-swap's `/ui`, LiteLLM's admin UI, Open WebUI — because a
browser surface makes a local broker dramatically easier to drive, inspect, and
demo: list models, run a chat with streaming, watch routing/cache decisions, read
status and usage, and (for operators) load/unload models and reload the registry.

ADR 0012 deferred the UI and reserved `BROKER_UI_ENABLED` as an inert flag,
explicitly because a hardened static page is work orthogonal to the three backend
features, and because the admin + cache APIs are the substrate a UI consumes.
That substrate now exists, so this ADR makes the flag real.

Hard constraints from our invariants and the worker brief:

- **Security-first.** The UI is an attack surface (XSS, clickjacking, key theft,
  SSRF-by-proxy). It must not weaken the broker's posture.
- **Default-off.** Defaults must reproduce v0.5.0 — with the flag off, `/ui` does
  not exist (404) and no route is registered.
- **Zero new runtime dependencies, no build toolchain.** The UI ships as plain,
  pre-built static files (vanilla HTML/CSS/JS) served as package data. No npm, no
  bundler, no transpile step at install or runtime.
- **Fully offline.** No CDN, no external fonts, no third-party URLs — it must work
  on an air-gapped host.
- **No new unauthenticated data endpoints.** The UI calls the *existing*
  authenticated API; it adds only static-asset routes under `/ui`.

The research note surveys OWASP (Secure Headers, CSP, Clickjacking, HTML5
storage), MDN (CSP, Web Storage, fetch/streams), and the peer UIs, and records
the adopt/reject decisions distilled below.

## Decision

Ship an **optional, hardened, fully-offline single-page operator playground**,
served by the broker at `/ui`, gated by `BROKER_UI_ENABLED` (default **off**).

### Serving model (`broker/ui.py`)

- Static assets live under `broker/ui/static/` (`index.html`, `app.js`,
  `styles.css`, `favicon.svg`) and are declared as setuptools package data.
- `register_ui(app, settings)` registers the `/ui` routes **only when enabled**;
  with the flag off no route exists, so `/ui` returns the normal 404 — the surface
  is absent, not merely hidden.
- Three GET routes: `/ui`, `/ui/`, and `/ui/{path:path}`. Every asset is served
  through one confinement function that resolves the requested path and requires
  it to stay **under the static dir** (defeats `..` traversal and symlink escape),
  serving only an **allow-listed set of extensions** with their correct
  `Content-Type`; anything else (including a real package file reached via `..`)
  is a 404.

### Security headers on every UI response (including 404s)

- `Content-Security-Policy: default-src 'self'; base-uri 'none';
  frame-ancestors 'none'; object-src 'none'; form-action 'self'` — **no
  `unsafe-inline`, no `unsafe-eval`, no external origins, no `*`.** Every script,
  style, image, font, and `connect-src` (fetch) is same-origin. This forces the
  no-inline discipline: all script is an external same-origin `<script
  type="module" src>` and all CSS is an external stylesheet (no `<style>`, no
  `style=` attributes).
- `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `X-Frame-Options: DENY` (defense-in-depth with `frame-ancestors 'none'`),
  `Cache-Control: no-store`.

### Credential handling

- The operator enters an **API key** (and optionally an **admin key**) at runtime.
  Keys are held in a single in-memory JavaScript object for the tab's lifetime and
  are **never written to `localStorage`, `sessionStorage`, or cookies**; a reload
  clears them, and a "Forget keys" button wipes them on demand. This matches the
  OWASP/MDN guidance that browser-persisted secrets are readable by any XSS and
  survive the session — in-memory is the smallest blast radius for a key a human
  pastes into a local tool.
- Keys are sent only as `Authorization: Bearer`/`X-Admin-Key` to the **same
  origin**; `connect-src 'self'` makes exfiltration to a third party a CSP
  violation even under an injected script.

### Features

- **Models**: `GET /v1/models` with capabilities, backend, pool/router candidates,
  context limit.
- **Chat playground**: model or `auto`, temperature, optional system prompt,
  streaming via **SSE consumed over `fetch()` + `ReadableStream`** (not
  `EventSource`, which cannot send an `Authorization` header), showing
  `x-broker-route`, `x-broker-cache`, rate-limit, and request-id headers.
- **Completions / Responses / Embeddings** tabs (same streaming/metadata model).
- **Rerank** tab that **hides itself unless `/v1/rerank` exists** (a probe that
  treats 404 as "not wired in"), so the UI is forward-compatible with a future
  rerank endpoint without assuming one.
- **Status** (`/status`) and scoped **Usage** (`/usage`) views.
- **Admin panel** (list / load / unload / registry-reload) that appears **only
  when an admin key is supplied**, with a confirmation prompt before every
  destructive action.
- Accessibility basics (labelled controls, visible focus, keyboard send,
  `aria-live` regions, dark/light `prefers-color-scheme`, responsive layout).

### Rejected within this ADR (not deferred)

- **A build toolchain / framework (React/Vite bundle).** Rejected: violates the
  zero-build, offline, zero-dep constraint; vanilla JS is sufficient for an
  operator tool and keeps the audit surface tiny.
- **A CSP nonce/hash for inline script.** Rejected: external same-origin files
  under `default-src 'self'` are simpler and need no per-response nonce plumbing;
  there is no inline script or style at all.
- **Persisting the key in `sessionStorage`/`localStorage` "for convenience".**
  Rejected on the XSS/persistence risk above; in-memory only.
- **A new unauthenticated `/ui/*` data/config endpoint** (e.g. to hand the UI a
  model list without a key). Rejected: it would be an unauthenticated data leak;
  the UI uses the authenticated API like any other client.
- **Serving the UI by default.** Rejected: an operator surface is opt-in;
  default-off preserves v0.5.0 behavior and keeps the attack surface closed unless
  wanted.

## Alternatives considered

- **No UI, docs only** — simplest, but leaves the single biggest ease-of-use gap
  vs peer gateways unaddressed; the backend substrate was built for this.
- **Reverse-proxy an existing UI (Open WebUI)** — heavy external dependency, not
  offline, and a different security model; rejected for an in-tree, auditable,
  dependency-free page.
- **`EventSource` for streaming** — cannot attach an `Authorization` header and is
  GET-only; rejected in favor of `fetch()` + `ReadableStream`.

## Consequences

- A new, **opt-in** attack surface exists only when an operator enables it; the
  strict CSP, header set, traversal confinement, extension allow-list, in-memory
  key, and "no new unauthenticated route" rule are the mitigations — each tested.
- Packaging now ships static assets as package data; the wheel is a few KB larger.
  No runtime dependency is added.
- The UI is a thin client over the public API, so it stays correct as the API
  evolves; the rerank tab's capability probe means a future `/v1/rerank` lights up
  with no UI change.
- Defaults are byte-for-byte v0.5.0: with `BROKER_UI_ENABLED` unset, `/ui` 404s
  and nothing about the running broker changes.

## Confirmation

Verified by `tests/test_ui.py` (241 tests, green in the shared venv and the private
`[dev,redis,otel]` venv, and under the Python 3.11 container gate incl. mypy):

- **Default-off**: `/ui` and `/ui/app.js` 404 when disabled.
- **Enabled serving**: `/ui` (and `/ui/`) serve `index.html`; `app.js`,
  `styles.css`, `favicon.svg` served with correct content types.
- **Exact security headers** on the index, on asset responses, and on 404s;
  **CSP has no `unsafe-inline`/`unsafe-eval`, no external origin, no `*`**, and
  contains `default-src 'self'` + `frame-ancestors 'none'`.
- **Shipped-asset hygiene** (scanning files on disk): no external `http(s)://`
  references (the SVG XML namespace, a non-fetchable identifier, is the sole
  allow-listed URI); no inline `<script>` without `src`; no `<style>` block or
  `style=` attribute; no `eval`/`Function` constructor in `app.js`.
- **No new unauthenticated data routes**: enabling the UI adds only
  `{"/ui", "/ui/", "/ui/{path:path}"}`, and `/v1/models`, `/status`, `/usage`
  still 401 without a key.
- **Path traversal** via `%2e%2e%2f` to `config.py`/`app.py` is blocked (404, no
  file contents leaked); unknown assets and non-allow-listed extensions 404.
- **Functional smoke**: a streaming chat against the fake backend yields deltas,
  `[DONE]`, and the `x-broker-route` header the UI displays.
- **Hardened path handling (follow-up)**: `_serve_asset` never raises for any
  client-supplied path — embedded NUL, C0/DEL control chars, backslashes,
  drive-like/colon prefixes, over-long segments, and over-long total paths are
  rejected *before* the filesystem is touched, and `resolve`/`is_file`/`read_bytes`
  are wrapped in `except (ValueError, OSError)`, so every such request returns the
  hardened 404 **with the full security headers** instead of a header-less 500.
  Covered by a 13-case regression matrix (incl. `%252e%252e` double-encoding) plus
  a 200-path deterministic fuzz asserting status ∈ {200, 404} and CSP present. We
  kept the resolve-under-`_STATIC_DIR` guard rather than a startup allow-list: the
  pre-validation + exception wrapping is a smaller, equally safe change that keeps
  the traversal/symlink defense already shipped, with no new startup scan.

Additionally exercised manually (evidence in `docs/web-ui.md`): the page was
served by a live broker and loaded in a real browser under the strict CSP with no
console violations; entering a key connected, listed models with capabilities,
and a streaming chat rendered with `route: echo-fake` metadata; the rerank probe
returned 404 so that tab stayed hidden; `curl` confirmed the exact response
headers and that `/ui/%2e%2e%2fconfig.py` returns 404.
