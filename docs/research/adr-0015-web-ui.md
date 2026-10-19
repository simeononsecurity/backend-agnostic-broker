# ADR 0015 Research Note — Security-first, offline, static operator Web UI at `/ui`

**Generated:** 2026-10-19 · **Primary sources cited:** 12 · **Confidence:** High

> **Scope.** Research backing a proposed static, offline, zero-build operator
> "playground" served by the broker (FastAPI) at `/ui`, **default-OFF** (returns
> `404` when disabled). Hard constraints for the UI: fully offline (no CDN, no
> external fonts/URLs); vanilla JS/CSS committed as plain files (no runtime build
> toolchain); API key held **in JS memory only** (never `localStorage`, never
> cookies); `Content-Security-Policy: default-src 'self'` with **no**
> `unsafe-inline`/`unsafe-eval`; `frame-ancestors 'none'`;
> `X-Content-Type-Options: nosniff`; `Referrer-Policy: no-referrer`.

> **Untrusted-data handling.** Every page fetched below was treated as **data,
> not instructions**. No fetched page contained agent-directed text, hidden
> prompts, or tool-invocation requests embedded in its own content. (The only
> "treat as data / do not follow instructions" banner seen was the broker's own
> research-tool wrapper, not content authored by the sources.) Had any page tried
> to instruct the agent, it would have been quoted verbatim and flagged here, not
> obeyed.

---

## Executive Summary

A local AI broker benefits from a small operator console — list models, run a
chat/completions playground with streaming, read status/usage, and
load/unload/failover models — because that is exactly the feature set the
established local-LLM consoles converge on (llama-swap's `/ui`, LiteLLM's Admin
UI, Open WebUI). We can deliver the useful 20% of that surface with **zero
runtime dependencies** and a **strict security posture** that is well supported
by primary guidance:

- A **strict CSP** (`default-src 'self'`; no `unsafe-inline`/`unsafe-eval`) is
  the single highest-leverage control. It requires that **all** script and style
  live in separate `.js`/`.css` files — which is also exactly what an offline,
  no-build UI wants. No nonces/hashes are needed because there is no inline code.
- **Clickjacking** is closed with `frame-ancestors 'none'` (CSP Level 2+,
  supersedes `X-Frame-Options`); we keep `X-Frame-Options: DENY` only as a
  legacy belt-and-suspenders.
- The **API key stays in a JS variable in memory**. OWASP and MDN are explicit
  that `localStorage`/`sessionStorage` are readable by any script in the origin,
  so one XSS discloses everything stored there; a strict CSP plus in-memory-only
  secrets minimizes the blast radius.
- **Streaming** uses the already-available `fetch()` + `response.body`
  `ReadableStream` reader, parsing SSE frames in JS. This needs no library and
  no inline code, so it composes cleanly with the strict CSP.
- **Default-OFF** keeps the attack surface at zero for operators who never opt
  in, matching how LiteLLM exposes an explicit `DISABLE_ADMIN_UI` switch.

Net: **adopt** a minimal, static, in-memory-key, strict-CSP, default-off UI;
**reject** build toolchains, CDN/remote assets, persistent secret storage,
framing, and the broad feature surface (auth SSO, RBAC, plugins, web search,
voice) of the full-featured consoles.

---

## 1. Operator playground UIs for local LLM servers — feature baseline

The three most relevant reference consoles converge on a common operator feature
set, which defines what a *minimal* playground must cover.

- **llama-swap** ships "a real time web interface with a playground for testing
  out all sorts of local models," exposed at the `/ui` route, alongside model
  management endpoints — e.g. `/running` to list running models and
  `POST /api/models/unload` / `POST /api/models/unload/:model_id` to unload them,
  plus log streaming. It markets itself as "one binary, one configuration file,
  no external dependencies"
  ([llama-swap README](https://raw.githubusercontent.com/mostlygeek/llama-swap/refs/heads/main/README.md)).
  A community single-file WebUI underscores that this can be done with **no build
  system**: "The UI is contained entirely in a single `index.html` file. No
  frontend build system or Node.js installation is required," and it enumerates
  operator concerns — "Health status, Running models, Unload all models,
  Metrics, Live logs" plus a streaming OpenAI-compatible chat
  ([llama-swap-webui](https://github.com/drone540/llama-swap-webui)).
- **LiteLLM** exposes an **Admin UI** at `<proxy_base_url>/ui` with model
  management ("Add Models … without restarting the proxy"), spend/usage views,
  and a login flow — and critically a hard off-switch: "Set
  `DISABLE_ADMIN_UI=\"True\"` in your environment to disable the Admin UI"
  ([LiteLLM Admin UI docs](https://docs.litellm.ai/docs/proxy/ui)). Their
  production guide notes the proxy runs fine with the UI absent: "If you don't
  need the admin UI, you can run with minimal configuration … API endpoints will
  work normally"
  ([LiteLLM production best practices](https://docs.litellm.ai/docs/proxy/prod)).
- **Open WebUI** is the maximal end of the spectrum: "a self-hosted AI platform
  … built to run entirely offline" with Ollama + OpenAI-compatible API support,
  but also RBAC/user groups, plugins, web search across dozens of providers,
  voice/video, and agentic terminals
  ([Open WebUI repo](https://github.com/open-webui/open-webui)). It documents the
  feature ceiling we deliberately will **not** chase.

**What an operator playground needs (distilled):** model list, a chat/completions
playground with **streaming**, live status/health, token/usage metrics, and
admin load/unload — all reachable from one origin.

**ADOPT:** model list, streaming chat + raw completions playground, status/health
view, token-usage readout, and load/unload/failover controls — delivered as a
single-origin static page (scope + offline).
**REJECT:** SSO/RBAC, plugins, web search, voice/video, multi-user accounts, and
persistence (scope — out of a broker operator console's remit).

---

## 2. OWASP secure HTTP response headers

The OWASP Secure Headers Project publishes a recommended baseline. Its "Best
Practices" table proposes exactly the headers central to this UI
([OWASP Secure Headers Project — Best Practices](https://owasp.github.io/www-project-secure-headers/best-practices/)):

| Header | OWASP proposed value (abridged) | Why it matters here |
|---|---|---|
| `Content-Security-Policy` | `default-src 'self'; … object-src 'none'; frame-ancestors 'none'; …` | Core XSS + framing defense; matches our target policy |
| `X-Content-Type-Options` | `nosniff` | Stops MIME-sniffing a response into executable script/style |
| `X-Frame-Options` | `deny` | Legacy clickjacking defense (see §4) |
| `Referrer-Policy` | `no-referrer` | Never leak the UI URL/path to any destination |
| `Cache-Control` | `no-store, max-age=0` | Avoid caching operator/usage data |

The project also enumerates the full header catalogue
([OWASP Secure Headers — Response Headers](https://owasp.github.io/www-project-secure-headers/response-headers/)).

**ADOPT:** `CSP: default-src 'self'` (full policy in §3), `X-Content-Type-Options:
nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, and
`Cache-Control: no-store` on `/ui` responses (security).
**REJECT:** `Strict-Transport-Security` as a *UI requirement* (the broker is a
local/offline service often on plain HTTP; HSTS is a deployment concern, not a
UI invariant) and heavyweight cross-origin isolation headers (COEP/COOP/CORP)
unless a concrete need appears (scope).

---

## 3. Strict CSP for a no-inline-script SPA

MDN's reference shows `default-src` is the fallback for every fetch directive —
`script-src`, `style-src`, `connect-src`, `img-src`, `font-src`, etc. — so
`default-src 'self'` alone constrains the whole page to same-origin resources
([MDN — CSP `default-src`](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/default-src)).
MDN is also explicit that omitting `unsafe-inline`/`unsafe-eval` is what blocks
inline execution: "Because the `unsafe-inline` and `unsafe-eval` directives are
not set, inline scripts will be blocked"
([MDN — Content-Security-Policy header](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy)).

The OWASP CSP Cheat Sheet confirms the intent of a same-origin policy with no
inline/eval, giving essentially our exact policy as its "Basic non-Strict"
sample and a tighter `default-src 'none'` variant
([OWASP CSP Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Content_Security_Policy_Cheat_Sheet.html)):

```
Content-Security-Policy: default-src 'self'; frame-ancestors 'self'; form-action 'self';
# tighter:
Content-Security-Policy: default-src 'none'; script-src 'self'; connect-src 'self'; img-src 'self'; style-src 'self'; frame-ancestors 'self'; form-action 'self';
```

OWASP describes two mechanisms for a *strict* CSP that must tolerate inline code —
**nonce-based** and **hash-based** — but both exist to safely allow inline
`<script>`. Because our UI commits **all** JS/CSS as external same-origin files,
we need **neither**: `'self'` already permits our external files and the absence
of `unsafe-inline` blocks everything else. This is the simplest, strongest option
and it is inherently compatible with a no-build, offline UI.

**Target policy for `/ui`:**
```
Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; font-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'
```

**ADOPT:** `default-src 'self'` with explicit `object-src 'none'`, `base-uri
'self'`, `frame-ancestors 'none'`; **all** script/style in external same-origin
files; **no** `unsafe-inline`/`unsafe-eval`; **no** nonces/hashes (unneeded)
(security + zero-deps + offline).
**REJECT:** inline `<script>`/`<style>`, inline event handlers (`onclick=`),
`eval`/`new Function`, `data:`/remote script or style sources, and
nonce/hash machinery (zero-deps — nothing inline to authorize).

---

## 4. Clickjacking defense: CSP `frame-ancestors 'none'` vs `X-Frame-Options`

OWASP's Clickjacking Defense Cheat Sheet recommends preventing framing via either
`X-Frame-Options` or the CSP `frame-ancestors` directive, and states
`Content-Security-Policy: frame-ancestors 'none';` "prevents any domain from
framing the content. This setting is recommended unless a specific need has been
identified for framing"
([OWASP Clickjacking Defense Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Clickjacking_Defense_Cheat_Sheet.html)).
The CSP Cheat Sheet is blunt that `frame-ancestors` is the modern mechanism:
"Historically the `X-Frame-Options` header has been used for this, but it has
been obsoleted by the `frame-ancestors` CSP directive … `X-Frame-Options` is
rendered obsolete by this directive and is ignored by the user agents"
([OWASP CSP Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Content_Security_Policy_Cheat_Sheet.html)).
It also notes `frame-ancestors` **does not** fall back to `default-src` and is
ignored if set via a `<meta>` tag — so it must be sent as a real response header.
OWASP's REST Security Cheat Sheet still lists `X-Frame-Options: DENY` as useful
"for compatibility with older browsers that do not support CSP Level 2"
([OWASP REST Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html)).

**ADOPT:** `frame-ancestors 'none'` as the primary control (sent as an HTTP
header, not `<meta>`), with `X-Frame-Options: DENY` as a legacy fallback
(security).
**REJECT:** `SAMEORIGIN`/`ALLOW-FROM` framing and relying on `<meta>`-delivered
`frame-ancestors` (scope — the operator console is never meant to be framed).

---

## 5. Holding the API key in the browser: in-memory vs `sessionStorage` vs `localStorage`

The risk is uniform and well-documented: anything in Web Storage is readable by
**any** script running in the origin, so a single XSS exfiltrates it. OWASP's
HTML5 Security Cheat Sheet: "A single Cross Site Scripting can be used to steal
all the data in these objects, so … it's recommended not to store sensitive
information in local storage," and "Do not store session identifiers in local
storage as the data is always accessible by JavaScript"
([OWASP HTML5 Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/HTML5_Security_Cheat_Sheet.html)).
OWASP's Session Management Cheat Sheet carries an explicit warning: "Do not store
authentication tokens, session IDs, JWTs, refresh tokens, or any credential in
`localStorage` or `sessionStorage`. These APIs are accessible to any JavaScript
executing in the origin, so a single XSS vulnerability discloses every token"
([OWASP Session Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)).

MDN confirms the persistence/scoping differences that make storage worse, not
better, for a secret: `localStorage` "persists even when the browser is closed
and reopened" while `sessionStorage` lasts only for the page session, but **both**
are origin-scoped and script-readable
([MDN — Web Storage API](https://developer.mozilla.org/en-US/docs/Web/API/Web_Storage_API),
[MDN — `Window.sessionStorage`](https://developer.mozilla.org/en-US/docs/Web/API/Window/sessionStorage)).
A plain JS variable is **not** persisted to disk, is cleared on reload/navigation,
and is not reachable from a fresh page load — so its exposure window is limited to
an already-running malicious script in the same document. Combined with the strict
CSP in §3 (which makes injecting such a script far harder) and in-memory-only
storage, the key's blast radius is minimized.

**ADOPT:** hold the API key **only** in a module-scoped JS variable in memory;
require re-entry after reload; never write it to storage or cookies; send it as a
request header to same-origin `connect-src 'self'` only (security).
**REJECT:** `localStorage`, `sessionStorage`, IndexedDB, and cookies for the key
(security — all are script-readable and/or persisted; one XSS or shared-machine
read discloses the key).

---

## 6. Consuming SSE over `fetch()` + `ReadableStream`

MDN documents reading a response body as a stream: `response.body` is a
`ReadableStream`; attach a reader with `ReadableStream.getReader()` and pull
chunks with `read()`, which resolves `{ done, value }` where `value` is a
`Uint8Array`
([MDN — Response.body](https://developer.mozilla.org/en-US/docs/Web/API/Response/body),
[MDN — Using readable streams](https://developer.mozilla.org/en-US/docs/Web/API/Streams_API/Using_readable_streams)).
MDN also shows the ergonomic async-iteration form, `for await (const chunk of
response.body)`
([MDN — ReadableStream](https://developer.mozilla.org/en-US/docs/Web/API/ReadableStream)).

For SSE specifically, the UI decodes each `Uint8Array` with `TextDecoder`,
buffers across chunk boundaries, splits on blank lines, and parses `data:` lines
(handling the OpenAI `data: [DONE]` sentinel). This uses `fetch()` — not the
`EventSource` API — because the operator console must send the API key as a
request **header**, which `EventSource` cannot do, and must POST a request body.
None of this requires inline code or any library, so it is fully compatible with
the strict CSP (`connect-src 'self'`) and the no-build constraint.

**ADOPT:** stream via `fetch()` + `response.body.getReader()` (or async
iteration) with `TextDecoder` + manual SSE frame parsing in an external JS file
(zero-deps + offline + works with header auth).
**REJECT:** the `EventSource` API (cannot set the `Authorization`/key header or
POST a body) and any third-party SSE/stream-parsing library (zero-deps).

---

## Adopt / Reject decisions

| # | Area | ADOPT | REJECT | Rationale |
|---|------|-------|--------|-----------|
| 1 | Playground feature set | Model list, streaming chat + completions, status/health, usage, load/unload/failover | SSO/RBAC, plugins, web search, voice, multi-user, persistence | scope / offline |
| 2 | Security headers | `CSP default-src 'self'`, `nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `Cache-Control: no-store` | HSTS as a UI requirement, COEP/COOP/CORP (for now) | security / scope |
| 3 | CSP strictness | `default-src 'self'`; external JS/CSS only; no `unsafe-inline`/`unsafe-eval`; no nonce/hash | inline script/style, inline handlers, `eval`, remote/`data:` script | security / zero-deps / offline |
| 4 | Clickjacking | `frame-ancestors 'none'` (header) + legacy `X-Frame-Options: DENY` | `SAMEORIGIN`/`ALLOW-FROM`, `<meta>`-delivered `frame-ancestors` | security |
| 5 | API-key storage | In-memory JS variable only; re-enter on reload | `localStorage`, `sessionStorage`, IndexedDB, cookies | security |
| 6 | SSE streaming | `fetch()` + `ReadableStream` reader + `TextDecoder` SSE parse | `EventSource`, third-party stream libs | zero-deps / offline / header-auth |
| 7 | Default state | `/ui` default-OFF → `404` when disabled; explicit opt-in flag | UI on by default | security (attack surface) |
| 8 | Asset delivery | Vanilla JS/CSS committed as plain same-origin files; no runtime build | CDN, external fonts/URLs, build toolchain at runtime | offline / zero-deps |

**Overall decision:** **ADOPT** a minimal, static, offline, default-off operator
UI with a strict same-origin CSP, in-memory-only API key, and `fetch()`-based SSE
streaming. **Confidence: High** — every control maps to primary guidance (OWASP
cheat sheets + Secure Headers Project, MDN HTTP/Streams/Web-Storage references),
and the security posture and the offline/zero-build constraints reinforce rather
than fight each other.

---

## Sources

1. [llama-swap README (mostlygeek/llama-swap)](https://raw.githubusercontent.com/mostlygeek/llama-swap/refs/heads/main/README.md) — Local model hot-swap proxy; documents `/ui` playground, `/running`, and unload endpoints; "no external dependencies."
2. [llama-swap-webui (drone540)](https://github.com/drone540/llama-swap-webui) — Single-file, no-build WebUI; enumerates operator features (health, running models, unload, metrics, live logs, streaming chat).
3. [LiteLLM — Admin UI docs](https://docs.litellm.ai/docs/proxy/ui) — Admin UI at `/ui` with model management and a hard `DISABLE_ADMIN_UI` off-switch.
4. [LiteLLM — Production Best Practices](https://docs.litellm.ai/docs/proxy/prod) — Proxy runs fine without the UI; UI is optional to the API.
5. [Open WebUI (open-webui/open-webui)](https://github.com/open-webui/open-webui) — Offline-capable, full-featured self-hosted AI UI; defines the maximal feature ceiling we scope out.
6. [OWASP Secure Headers Project — Best Practices](https://owasp.github.io/www-project-secure-headers/best-practices/) — Recommended baseline header values (CSP, nosniff, Referrer-Policy, X-Frame-Options, Cache-Control).
7. [OWASP Secure Headers Project — Response Headers](https://owasp.github.io/www-project-secure-headers/response-headers/) — Catalogue of the project's covered security headers.
8. [OWASP Content Security Policy Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Content_Security_Policy_Cheat_Sheet.html) — Strict CSP guidance; nonce vs hash; sample `default-src 'self'` / `'none'` policies; `frame-ancestors` obsoletes `X-Frame-Options`.
9. [MDN — Content-Security-Policy header](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy) — Confirms omitting `unsafe-inline`/`unsafe-eval` blocks inline scripts.
10. [MDN — CSP `default-src` directive](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/default-src) — `default-src` is the fallback for all fetch directives (script/style/connect/img/font/etc.).
11. [OWASP Clickjacking Defense Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Clickjacking_Defense_Cheat_Sheet.html) — `frame-ancestors 'none'` recommended to prevent all framing; `X-Frame-Options` values and limits.
12. [OWASP REST Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html) — `frame-ancestors 'none'` for API responses; `X-Frame-Options: DENY` as legacy compatibility.
13. [OWASP HTML5 Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/HTML5_Security_Cheat_Sheet.html) — One XSS steals all Web Storage; don't store sensitive data/session IDs there.
14. [OWASP Session Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html) — Explicit warning against storing tokens/credentials in `localStorage`/`sessionStorage`.
15. [MDN — Web Storage API](https://developer.mozilla.org/en-US/docs/Web/API/Web_Storage_API) — `localStorage` persists across sessions; `sessionStorage` scoped per tab/session; both origin-scoped and synchronous.
16. [MDN — Window.sessionStorage](https://developer.mozilla.org/en-US/docs/Web/API/Window/sessionStorage) — `sessionStorage` lifetime and origin+tab partitioning.
17. [MDN — Response.body](https://developer.mozilla.org/en-US/docs/Web/API/Response/body) — Response body is a `ReadableStream`; reader pump example.
18. [MDN — Using readable streams](https://developer.mozilla.org/en-US/docs/Web/API/Streams_API/Using_readable_streams) — `getReader()` + `read()` chunk-reading pattern.
19. [MDN — ReadableStream](https://developer.mozilla.org/en-US/docs/Web/API/ReadableStream) — `for await … of response.body` async-iteration form over fetch streams.

## Methodology

Conducted 2026-10-19 using the broker's web research tools (`web_search`,
`web_extract`) against official docs, source repositories, and specifications
(OWASP Cheat Sheet Series, OWASP Secure Headers Project, MDN Web Docs, and the
llama-swap / LiteLLM / Open WebUI project sources). All fetched content was
treated as untrusted data, scanned for agent-directed/injection text (none
found), and cited only where it could be attributed to a primary/authoritative
source.
