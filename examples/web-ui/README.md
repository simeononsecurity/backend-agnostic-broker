# Web UI playground — runnable example (ADR 0015)

Launches the broker with the optional, offline [`/ui` playground](../../docs/web-ui.md)
enabled against the dependency-free **fake backend** — no model weights, no network.

## Run

```bash
cd examples/web-ui
cp .env.example .env
# edit .env: replace YOUR_API_KEY (and YOUR_ADMIN_KEY) with values you choose
broker                     # or: python -m broker
```

Then open <http://127.0.0.1:8080/ui> and:

1. Leave **Broker base URL** blank (same origin).
2. Paste your `BROKER_API_KEYS` value into **API key** (and the admin key into
   **Admin key** to unlock the Admin tab).
3. Click **Connect** — the `echo-fake` model appears. Open **Chat**, type a
   message, and send (streaming is on by default).

## Verify with curl

```bash
# UI served with strict security headers (replace KEY with your BROKER_API_KEYS value):
curl -s -D - -o /dev/null http://127.0.0.1:8080/ui

# The chat pipeline the UI drives (streaming SSE):
curl -s -N -H "Authorization: Bearer KEY" -H 'Content-Type: application/json' \
  -d '{"model":"echo-fake","messages":[{"role":"user","content":"hi"}],"stream":true}' \
  http://127.0.0.1:8080/v1/chat/completions
```

## Notes

- The UI is **off unless `BROKER_UI_ENABLED=true`**; with it unset, `/ui` returns
  404 and nothing is served.
- Keys you paste into the page are held in browser memory only — never stored.
- This example is exercised in CI by `tests/test_ui.py::test_example_web_ui_config`,
  which loads `registry.yaml` here, serves `/ui` with the fake backend, and runs a
  chat — so the example cannot silently drift from the code.
