# How-to: virtual keys, limits, budgets, tenants, and rotation

Govern access with hashed **virtual keys**: per-key rate limits (rpm/tpm/
concurrency), daily/monthly **token and cost budgets**, fail-closed
`allowed_models` and `placement`, hierarchical **tenant** budgets, and key
**rotation**. Governance is **opt-in** — it activates only when a keys file (or
per-key limits) is configured; defaults reproduce v0.5.0. Legacy flat
`BROKER_API_KEYS` keep working as unlimited keys.

Only **SHA-256 hashes** are stored; the secret is shown once at creation and never
logged.

## When to use

- Multiple clients/teams share one broker and you need per-client limits or cost
  caps.
- You need data-residency pinning (`placement`) or per-key model allow-lists.

## Minimal config

```bash
export BROKER_KEYS_PATH=keys.yaml
python -m broker.keys create --label team-a --scope user --rpm 60 --daily-cost 5
python -m broker.keys list          # never prints secrets or hashes
BROKER_KEYS_PATH=keys.yaml python -m broker
```

## Full config

```bash
python -m broker.keys create \
  --label team-alpha --tenant alpha --scope user \
  --rpm 60 --rpm-burst 10 --tpm 100000 --max-concurrency 4 \
  --daily-cost 5 --monthly-tokens 50000000 \
  --allowed-models chat,auto --placement local \
  --per-model chat:30:50000        # model:rpm:tpm (repeatable; blank axis = unlimited)

# Algorithm choices (optional):
export BROKER_RATE_LIMIT_ALGO=gcra        # sliding_log (default) | gcra token-bucket
export BROKER_RATE_LIMIT_TPM_ALGO=sliding # fixed (default) | sliding window
```

Add aggregate **tenant** budgets under a `tenants:` block in the keys file (see
[`examples/governance/keys.example.yaml`](../../examples/governance/keys.example.yaml));
they are enforced across every key of the tenant. **Cost** budgets require per-model
`pricing` in the registry; token budgets don't.

Rotate and revoke:

```bash
python -m broker.keys rotate brk_aaaa1111 --grace-days 7   # 0 = revoke now
python -m broker.keys revoke brk_aaaa1111
```

## Verify

Successful responses carry `x-ratelimit-*`; the request that trips a limit returns
`429`:

```bash
KEY="brk_aaaa1111_<secret-shown-once>"
curl -sD- -o/dev/null -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"model":"chat","messages":[{"role":"user","content":"hi"}]}' \
  http://127.0.0.1:8080/v1/chat/completions
# 200 + x-ratelimit-remaining-requests: N ; after the limit -> 429 rate_limit_exceeded + retry-after
```

Expected error types: `429 rate_limit_exceeded` (rpm/tpm), `429 insufficient_quota`
(token/cost budget), `403 model_not_allowed`, `403 placement_denied`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Limits not enforced | A keys file must be configured; a legacy `BROKER_API_KEYS` key is unlimited. |
| Cost budget never trips | Add `pricing` for the model in the registry; unpriced models are free. |
| `hmac` 500 on auth | Keys are compared as bytes; use ASCII secrets (the CLI generates safe ones). |

## Example

[examples/governance](../../examples/governance/) — the full flow, tested against
the fake backend (429 + headers, admin key round-trip).

## Reference & decisions

- Reference: [configuration](../configuration.md) · [API reference](../api-reference.md) ·
  [security model](../security-model.md)
- ADRs: [0008 per-key governance](../adr/0008-per-key-governance.md) ·
  [0016 governance v2](../adr/0016-governance-v2.md)
- See also: [admin API guide](admin-api.md) for managing keys over HTTP.
