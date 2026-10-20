# How-to: the web UI playground

Enable an optional, offline **`/ui`** playground — a single static page for trying
chat, inspecting models, and (with an admin key) managing keys. It's **off by
default**, serves no unauthenticated data endpoints, and ships with a strict
Content-Security-Policy.

## When to use

- You want a quick human-facing way to test the broker without curl or the SDK.
- Demos and local exploration.

Leave it off for headless production unless you deliberately want the page.

## Minimal config

```bash
BROKER_UI_ENABLED=true
python -m broker
# open http://127.0.0.1:8080/ui
```

With `BROKER_UI_ENABLED` unset, `/ui` returns **404** and nothing is served.

## Using it

1. Leave **Broker base URL** blank (same origin).
2. Paste a `BROKER_API_KEYS` value into **API key** (and an admin key into **Admin
   key** to unlock the Admin tab).
3. Click **Connect** — your models appear. Open **Chat** and send a message
   (streaming is on by default).

Keys you paste are held in **browser memory only** — never stored or sent anywhere
but the broker.

## Verify

```bash
# Served with strict security headers when enabled:
curl -s -D - -o /dev/null http://127.0.0.1:8080/ui | grep -iE 'content-security-policy|^HTTP'
# -> HTTP/1.1 200 OK and a Content-Security-Policy header
```

Expected: `200 text/html` with a CSP header when enabled; `404` when disabled.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/ui` is 404 | Set `BROKER_UI_ENABLED=true`. |
| Models don't appear | Paste a valid API key and click Connect; check the browser console for CORS/base-URL. |
| Admin tab locked | Paste an admin-scope key into **Admin key**. |

## Example

[examples/web-ui](../../examples/web-ui/) — runs `/ui` against the fake backend,
tested in CI.

## Reference & decisions

- Reference: [web UI](../web-ui.md) · [configuration](../configuration.md)
- ADR: [0015 web UI playground](../adr/0015-web-ui-playground.md)
