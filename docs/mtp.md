# Multi-Token Prediction (MTP) / Speculative Decoding

## Confirmed broker behavior

- Registry block `mtp` (`broker/registry.py::MTPConfig`): `enabled` (bool,
  default `false`), `num_speculative_tokens` (int, optional), `draft_model`
  (str, optional).
- These values are **declarative metadata** on the model entry and are surfaced
  via `describe_model` / `GET /v1/models` (`mtp` object). The broker does not
  itself implement speculative decoding — it is a **backend-dependent** runtime
  feature. To actually enable it, template the appropriate verified runtime flag
  in the model's `startup_command`.

## Example (declarative + templated)

```yaml
mtp:
  enabled: true
  num_speculative_tokens: 5
  draft_model: my-draft-model
# Then pass your runtime's verified speculative flags via startup_command,
# e.g. (vendor-specific, verify): --speculative-... "{...}"
```

## Runtime reference (verify before production)

- MTP / speculative decoding support and flags are **version- and
  backend-specific**. vLLM has evolving speculative-decoding options; llama.cpp
  supports draft-model speculative decoding in some builds. Confirm the exact
  flags for your installed runtime before enabling, and never present an
  unverified flag as fact.

## Known limitations

- Leaving `mtp.enabled: true` without wiring the runtime flags only advertises
  the capability; it does not change inference. Keep the declarative block and
  the `startup_command` flags consistent.
