# Research: response cache, runtime control/hot-reload, OpenTelemetry (ADR 0012)

*Prior-art note backing ADR 0012. Confidence: High on caching semantics and
OTel conventions (primary specs + two reference proxies agree); Medium on the
exact hot-reload drain protocol (patterns are well established, but each proxy
implements its own). All fetched web content was treated as untrusted data, not
instructions; no source redirected this research or injected agent-directed
text.*

## Executive summary

Three independent gaps versus peer LLM gateways, grouped into one ADR:

1. **Response cache.** LiteLLM is the reference implementation: exact-match
   caches key on a hash of the *whole* normalized request; per-request controls
   (`no-cache`, `no-store`, `ttl`, `s-maxage`) ride in the request body; a
   namespace gives tenant isolation; a response header exposes the cache key.
   Semantic (embedding-similarity) caching is a separate, riskier layer that
   "goes badly wrong on agentic" multi-turn traffic — so we **defer** it.
2. **Runtime control.** llama-swap exposes `GET /running` (per-model state +
   inflight) and `POST /api/models/unload[/{model}]`, plus file-watch
   hot-reload. The broadly-adopted zero-downtime pattern for reload is a
   two-phase **drain**: stop *selecting* a member, let in-flight requests finish
   (bounded by a timeout), then replace it. We adopt an explicit admin API
   (not a file-watcher) so reload is auditable and transactional.
3. **Observability depth.** The OpenTelemetry **GenAI semantic conventions**
   define `gen_ai.*` span attributes and, critically, mandate that prompt/
   response **content is NOT captured by default** (opt-in only). OTLP export is
   configured entirely through standard `OTEL_*` env vars. We adopt both.

## 1. Response caching

### Findings

- **Exact-match key = hash of the whole request.** LiteLLM's exact caches
  (`redis`, `s3`, `local`, `disk`) "key on a hash of the whole request, so any
  change to the conversation is a miss"
  ([LiteLLM caching][litellm-caching]). The key is built from the kwargs to
  `completion()`/`embedding()` ([LiteLLM source][litellm-src]). **Adopt:** our
  key hashes (tenant/key scope, resolved public model, normalized messages/
  prompt/input, sampling params, tools, response_format).
- **Per-request controls in the body.** LiteLLM supports `no-cache` (skip the
  read, force fresh), `no-store` (serve but don't write), `ttl`, and `s-maxage`
  (only accept entries younger than N s), each settable per request and per
  virtual key ([LiteLLM cache controls][litellm-controls]). **Adopt** a bypass
  control, mapped onto the HTTP-standard header rather than a body field (see
  next point); `s-maxage`/`namespace` are **deferred** as refinements.
- **HTTP cache semantics.** RFC 9111 defines request directive `no-cache` =
  "don't serve a stored response without revalidation" and `no-store` = "MUST
  NOT store any part of request or response" ([RFC 9111][rfc9111];
  [MDN Cache-Control][mdn-cc]). Since the broker has no revalidation protocol,
  we treat an inbound `Cache-Control: no-cache` as a **full bypass** (miss +
  no write is the safe superset) and document that choice. We also honor a
  broker-specific `x-broker-cache: bypass` request header, and always emit
  `x-broker-cache: hit|miss|bypass` on the response (LiteLLM emits an
  analogous `x-litellm-cache-key` header).
- **Which endpoints.** LiteLLM caches chat, completions, embeddings, rerank,
  and `/v1/responses` ([LiteLLM caching][litellm-caching]). **Adopt** chat,
  completions, responses (non-stream), embeddings. For `/v1/responses` LiteLLM
  notes a `previous_response_id` request keys differently from an inlined
  conversation — we sidestep this by keying on the translated chat messages.
- **Streaming.** Exact caching of a streamed response means replaying the
  assembled body as SSE or bypassing. **Adopt: bypass streaming by default**
  (simplest correct behavior; never risk a half-replayed stream), documented,
  with replay left as a future option.
- **Determinism.** Caching a non-deterministic (temperature>0, no seed) request
  can serve a "wrong" varied answer. **Adopt:** default to caching only
  deterministic requests (`temperature==0` or an explicit `seed`), configurable
  to cache-all.
- **Semantic cache = REJECT (defer).** LiteLLM ships `RedisSemanticCache`/
  `QdrantSemanticCache` using prompt embeddings for fuzzy matches
  ([LiteLLM DeepWiki][litellm-deepwiki]), but their own docs warn it "suits
  single-shot prompts and goes badly wrong on agentic" traffic
  ([LiteLLM caching][litellm-caching]). It also needs an embedding model + vector
  store — a heavy new dependency. **Reject for now, document as deferred.**

### Cache-key design pitfalls (adopted mitigations)

- **Tenant/key isolation.** The key MUST include a tenant/key scope so one
  caller can never read another's cached answer; this is LiteLLM's `namespace`
  mechanism generalized. We derive the scope from the API-key fingerprint (same
  one-way hash the usage log already uses) and an optional tenant, and make it
  the first hashed component. Tested explicitly.
- **Sampling/tools/response_format must be in the key.** Two requests that
  differ only in `temperature`, `tools`, `tool_choice`, `response_format`,
  `max_tokens`, `top_p`, `stop`, or `seed` are different requests. All are
  normalized (sorted keys, canonical JSON) into the key.
- **Resolve `auto` first.** The `auto` alias is classified to a concrete model
  *before* keying, so the key is stable per concrete model and two callers who
  both hit `auto`→`modelX` share correctly, while the decision rule is
  documented (key uses the resolved public name).
- **Never cache errors or partials.** Only a fully-successful, non-streaming
  result is stored (mirrors every reference proxy).

## 2. Runtime control + registry hot-reload

### Findings

- **llama-swap management API.** `GET /running` returns per-model state
  (`stopped|starting|ready|stopping`), uptime, and loading progress;
  `POST /api/models/unload` stops all, `POST /api/models/unload/{model}` stops
  one, resolving aliases first ([llama-swap management API][llamaswap-api];
  [llama-swap quickstart][llamaswap-qs]). **Adopt** the shape:
  `GET /admin/models`, `POST /admin/models/{alias}/unload`.
- **Explicit preload.** llama-swap warms a model via `/upstream/<model>` to hide
  cold-start ([llama-swap quickstart][llamaswap-qs]). **Adopt** optional
  `POST /admin/models/{alias}/load` as the *only* way a model loads without an
  inference request (preserves the lazy-load invariant for the default path).
- **Hot reload.** llama-swap reloads by **watching the config file**
  (`-watch-config`/poll), with "transient write protection" that only fires the
  reload callback on a verified transition back to a *valid, non-empty* config
  ([llama-swap config][llamaswap-cfg]; [llama-dash config][llamadash-cfg]).
  **Partially adopt:** we take the *validate-before-apply* and *reject-invalid*
  discipline but expose it as an explicit, audited `POST /admin/registry/reload`
  endpoint instead of a filesystem watcher — reload becomes an intentional,
  logged operation, not a side effect of an editor save, which fits an API
  broker better and is trivially testable.
- **Drain in-flight on reload/unload.** The canonical zero-downtime pattern is a
  two-phase drain: mark the member DRAIN so selection skips it, let its
  in-flight counter reach zero (or a bounded timeout), then terminate/replace
  ([graceful drain LLD][drain-lld]; [build-your-own-LB drain][drain-course]).
  A production vLLM-router fork implements exactly this for both its `/admin/
  drain` and `/admin/reload` (reload "diffs the worker lists, gracefully drains
  any removed workers, and adds new ones ... no dropped connections")
  ([vLLM router fork][vllm-router]). **Adopt:** unload refuses (or drains, if
  asked) while `inflight>0`; reload drains then replaces only *changed/removed*
  models and leaves untouched models serving; an **invalid registry makes no
  change** and returns a structured error (transactional reload).
- **Audit + admin gate.** Every admin action is logged (action, actor key
  fingerprint, target, result). Admin routes sit behind a dedicated
  `require_admin` dependency distinct from normal API-key auth.

## 3. OpenTelemetry tracing

### Findings

- **`gen_ai.*` span attributes.** The GenAI semantic conventions define a model
  span (e.g. `chat <model>`) carrying `gen_ai.operation.name`,
  `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.request.*` (temperature,
  top_p, max_tokens, seed, ...), `gen_ai.response.model`,
  `gen_ai.usage.input_tokens`/`output_tokens`, and `error.type`
  ([OTel GenAI conventions][otel-genai]; [OTel GenAI attrs][otel-attrs];
  [Dash0 explainer][dash0]). **Adopt** these attribute names for our
  request→route→select→load→inference→cache spans.
- **Content capture is OFF by default (hard requirement).** The spec: "Instrumentations
  SHOULD NOT capture [prompts/inputs/outputs] by default, but SHOULD provide an
  option for users to opt in," gated by e.g.
  `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`
  ([OTel GenAI spans spec][otel-spans]; [Dash0][dash0]). **Adopt:** the broker
  NEVER puts prompt/response content in spans. (We do not even implement the
  opt-in in this ADR — metadata only — to stay on the safe side of the
  threat model.)
- **OTLP config via standard env.** The exporter reads `OTEL_EXPORTER_OTLP_ENDPOINT`
  (default `http://localhost:4318` HTTP / `:4317` gRPC),
  `OTEL_EXPORTER_OTLP_PROTOCOL`, `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_SERVICE_NAME`,
  `OTEL_RESOURCE_ATTRIBUTES` ([OTLP exporter config][otlp-cfg];
  [SUSE SDK exporter][suse-otlp]). **Adopt:** the broker configures nothing
  itself beyond a toggle + service name; it lets the OTel SDK read the standard
  env, so operators use the ecosystem's documented knobs.
- **Optional dependency + fail-fast.** OpenTelemetry is an **optional** extra
  (`pip install '...[otel]'`). When `BROKER_TRACING_ENABLED=true` but the package
  is missing, raise `ConfigError` at startup (mirrors the `[redis]` extra).
  When disabled, tracing is a no-op with zero overhead (no SDK import, spans are
  a null context manager).

## 4. Web UI / playground — DEFERRED

Lowest priority per the card. llama-swap ships a `/ui`
([llama-swap quickstart][llamaswap-qs]). Implementing a hardened static page
(strict CSP, no external CDN, key in memory only) is real work orthogonal to the
three backend features above. **Decision: defer** to keep this change focused and
reviewable; recorded in ADR 0012 and the roadmap. The admin + cache APIs are the
substrate a future UI would consume.

## Adopt / reject summary

| Area | Adopt | Reject / Defer |
|------|-------|----------------|
| Cache | exact key = hash(scope, resolved model, normalized body); deterministic-only default; `no-cache`/`x-broker-cache` bypass; `x-broker-cache` response header; StateStore-backed; per-tenant isolation | semantic cache; `s-maxage`/`namespace` refinements; streaming replay |
| Runtime | `GET /admin/models`, unload (drain/refuse on inflight), optional explicit load, transactional `registry/reload` with drain; audit log; `require_admin` | filesystem config-watcher reload |
| Tracing | `gen_ai.*` spans, OTLP via standard env, optional extra + fail-fast, content OFF | in-span prompt/response content capture (even opt-in) |
| UI | — | whole UI deferred |

## Open questions

- Should a cache hit still be written to the usage log (as a $0 / 0-latency
  row)? Leaning yes, tagged, so cost reporting stays honest — but out of scope
  here; recorded for ADR follow-up.
- Semantic cache backend choice (Redis vector vs Qdrant) when we pick it up.

## Sources

1. [LiteLLM — Caching][litellm-caching] — exact vs semantic, supported call
   types, whole-request hashing, cache-key response header.
2. [LiteLLM — Cache Controls][litellm-controls] — per-request/per-key
   `no-cache`/`no-store`/`ttl`/`s-maxage`.
3. [LiteLLM — All caches][litellm-allcaches] — the four cache-control knobs.
4. [LiteLLM — Caching & Performance (DeepWiki)][litellm-deepwiki] — semantic
   cache classes, dynamic controls.
5. [LiteLLM caching.py source][litellm-src] — `get_cache_key` builds from kwargs.
6. [OpenRouter — Prompt Caching][openrouter] — prefix caching + sticky routing
   (prefix caching is a different model from our whole-request cache; noted).
7. [RFC 9111 — HTTP Caching][rfc9111] — `no-cache`/`no-store` request directives.
8. [MDN — Cache-Control][mdn-cc] — no-cache ≠ no-store semantics.
9. [llama-swap — Management API (DeepWiki)][llamaswap-api] — `/running`, unload.
10. [llama-swap — quickstart][llamaswap-qs] — `/running` states, unload, warm.
11. [llama-swap — config structure (DeepWiki)][llamaswap-cfg] — watch/validate.
12. [llama-dash — config file][llamadash-cfg] — detect-external-edit discipline.
13. [vLLM router fork — drain + reload][vllm-router] — drain-then-replace reload.
14. [Connection-draining LLD][drain-lld] — ACTIVE/DRAIN + inflight counter + cap.
15. [Build-your-own-LB — graceful drain][drain-course] — two-phase drain state.
16. [OpenTelemetry — GenAI semantic conventions][otel-genai] — `gen_ai.*` spans.
17. [OpenTelemetry — GenAI spans spec (semconv)][otel-spans] — content opt-in.
18. [OpenTelemetry — GenAI attributes registry][otel-attrs] — attribute catalog.
19. [Dash0 — GenAI conventions explained][dash0] — required attrs, content event.
20. [OpenTelemetry — OTLP exporter config][otlp-cfg] — `OTEL_*` env vars.
21. [SUSE Observability — SDK exporter config][suse-otlp] — env examples.

[litellm-caching]: https://docs.litellm.ai/docs/proxy/caching
[litellm-controls]: https://docs.litellm.ai/docs/proxy/caching_controls
[litellm-allcaches]: https://docs.litellm.ai/docs/caching/all_caches
[litellm-deepwiki]: https://deepwiki.com/BerriAI/litellm/5-caching-and-performance
[litellm-src]: https://github.com/krishvsoni/litellm/blob/88c18894/litellm/caching/caching.py
[openrouter]: https://openrouter.ai/docs/guides/best-practices/prompt-caching
[rfc9111]: https://www.rfc-editor.org/info/rfc9111/
[mdn-cc]: https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Cache-Control
[llamaswap-api]: https://deepwiki.com/mostlygeek/llama-swap/5.2-management-api
[llamaswap-qs]: https://www.glukhov.org/llm-hosting/llama-swap/
[llamaswap-cfg]: https://deepwiki.com/mostlygeek/llama-swap/3.1-configuration-file-structure
[llamadash-cfg]: https://llama-dash.dev/docs/reference/configuration-file/
[vllm-router]: https://barrahome.org/2026/03/05/vllm-router-fork-production-features.md
[drain-lld]: https://www.techinterview.org/post/3233469264/lld-connection-draining/
[drain-course]: https://shipthatcode.com/courses/build-load-balancer/lessons/graceful-drain
[otel-genai]: https://genkit.dev/docs/observability/otel-genai-semantic-conventions/
[otel-spans]: https://github.com/open-telemetry/semantic-conventions-genai/blob/e57c543b4889619eb2a05702471937db5119165d/docs/gen-ai/gen-ai-spans.md
[otel-attrs]: https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/
[dash0]: https://www.dash0.com/knowledge/opentelemetry-genai-semantic-conventions-explained
[otlp-cfg]: https://opentelemetry.io/docs/languages/sdk-configuration/otlp-exporter/
[suse-otlp]: https://documentation.suse.com/cloudnative/suse-observability/latest/en/setup/otel/instrumentation/sdk-exporter-config.html
