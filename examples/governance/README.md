# Governance walkthrough (ADR 0008 + ADR 0016)

End-to-end example of per-key governance: virtual keys with per-model rpm/tpm
limits, hierarchical tenant budgets, per-key placement, GCRA rate limiting, key
rotation, and the admin key-management API. Every value here is a **fake
placeholder**; `keys.example.yaml` documents the schema.

> Defaults reproduce v0.5.0 exactly. Governance activates only when a keys file
> (or per-key limits) is configured. `keys.yaml` is **git-ignored** — only hashes
> are ever written to it.

## 1. Create keys with the CLI

The secret is printed **once** and never stored; only its SHA-256 hash is written.

```bash
export BROKER_KEYS_PATH=keys.yaml

# A team key: per-model cap on `chat`, pinned to `local` placement, in tenant alpha.
python -m broker.keys create \
  --label team-alpha --tenant alpha --scope user \
  --rpm 60 --rpm-burst 10 --tpm 100000 --max-concurrency 4 \
  --daily-cost 5 --allowed-models chat,auto \
  --placement local \
  --per-model chat:30:50000      # model:rpm:tpm  (repeatable; blank axis = unlimited)

# An admin key for the management API.
python -m broker.keys create --label ops-admin --scope admin

python -m broker.keys list       # never prints secrets or hashes
```

Add aggregate tenant budgets by hand under a `tenants:` block (see
`keys.example.yaml`) — they are enforced across **every** key of the tenant.

## 2. Select the algorithms (optional)

```bash
# rpm: exact sliding-log (default) or atomic GCRA token-bucket with per-key burst.
export BROKER_RATE_LIMIT_ALGO=gcra
# tpm: fixed windowed counter (default) or weighted sliding-window counter.
export BROKER_RATE_LIMIT_TPM_ALGO=sliding
```

## 3. Hit the rate limit (429 + headers)

Successful responses carry `x-ratelimit-*` so clients can self-pace; the request
that trips the limit returns **429** with `Retry-After`.

```bash
KEY="brk_aaaa1111_<the-secret-shown-once>"

curl -sD- -o/dev/null http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"chat","messages":[{"role":"user","content":"hi"}]}'
# HTTP/1.1 200 OK
# x-ratelimit-limit-requests: 30
# x-ratelimit-remaining-requests: 29
# x-ratelimit-reset-requests: 60

# ...after exceeding the per-model `chat` rpm of 30:
# HTTP/1.1 429 Too Many Requests
# retry-after: 60
# {"error":{"type":"rate_limit_exceeded",
#           "message":"request rate limit of 30/min for model 'chat' reached", ...}}
```

A model the key may not use returns **403 `model_not_allowed`**; a member that
fails the key's `placement` returns **403 `placement_denied`**; an exhausted
token/cost budget (per-key or tenant) returns **429 `insufficient_quota`**.

## 4. Rotate and revoke

```bash
# Mint a successor; the old key stays valid for a 7-day grace window (0 = revoke now).
python -m broker.keys rotate brk_aaaa1111 --grace-days 7

python -m broker.keys revoke brk_aaaa1111
```

## 5. Admin key-management API

With `BROKER_ADMIN_ENABLED=true` and an admin-scope key (or
`BROKER_ADMIN_API_KEYS`), manage keys over HTTP. The secret is returned **once**
in the create/rotate response and never logged.

```bash
ADMIN="brk_bbbb2222_<admin-secret>"

# List (never returns secrets or hashes)
curl -s http://127.0.0.1:8000/admin/keys -H "X-Admin-Key: $ADMIN"

# Create (secret shown once)
curl -s -X POST http://127.0.0.1:8000/admin/keys -H "X-Admin-Key: $ADMIN" \
  -H 'Content-Type: application/json' \
  -d '{"label":"ci","tenant":"alpha","limits":{"rpm":10,"allowed_models":["chat"]}}'

# Rotate (secret shown once; old key grace-overlaps)
curl -s -X POST http://127.0.0.1:8000/admin/keys/brk_aaaa1111/rotate \
  -H "X-Admin-Key: $ADMIN" -H 'Content-Type: application/json' -d '{"grace_days":7}'

# Revoke
curl -s -X DELETE http://127.0.0.1:8000/admin/keys/brk_aaaa1111 -H "X-Admin-Key: $ADMIN"
```

This example is covered by `tests/test_examples_governance.py` (config loads; the
429 + headers and the admin flow run against the fake backend).
