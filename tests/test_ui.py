"""Web UI / playground tests (ADR 0015).

Covers: default-off 404; enabled serving of index + assets with EXACT strict
security headers; CSP has no ``unsafe-*`` and no external origins; shipped assets
reference no external ``http(s)://`` URLs and carry no inline ``<script>``/style;
enabling the UI adds NO new unauthenticated data routes (only ``/ui`` statics);
path traversal is blocked; content types are correct; and a functional smoke that
streams a chat (the pipeline the UI drives) against the fake backend.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from broker.app import create_app
from broker.config import Settings
from broker.ui import _STATIC_DIR

KEY = "ui-key"
AUTH = {"Authorization": f"Bearer {KEY}"}

EXPECTED_SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "object-src 'none'; form-action 'self'"
    ),
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "cache-control": "no-store",
}


def _settings(tmp_path: Path, *, ui_enabled: bool) -> Settings:
    reg = tmp_path / "registry.yaml"
    reg.write_text(
        textwrap.dedent(
            """
            models:
              - alias: echo-fake
                backend: fake
                runtime_model_id: fake-echo-v1
                idle_timeout_seconds: 60
            """
        ).strip()
    )
    return Settings(
        auth_enabled=True,
        api_keys=[KEY],
        registry_path=str(reg),
        enabled_backends=["fake"],
        ui_enabled=ui_enabled,
    )


@pytest.fixture
def ui_client(tmp_path):
    app = create_app(_settings(tmp_path, ui_enabled=True))
    with TestClient(app) as c:
        yield c


@pytest.fixture
def noui_client(tmp_path):
    app = create_app(_settings(tmp_path, ui_enabled=False))
    with TestClient(app) as c:
        yield c


# --- default-off ----------------------------------------------------------
def test_ui_disabled_index_404(noui_client):
    assert noui_client.get("/ui").status_code == 404


def test_ui_disabled_asset_404(noui_client):
    assert noui_client.get("/ui/app.js").status_code == 404
    assert noui_client.get("/ui/styles.css").status_code == 404


# --- enabled serving ------------------------------------------------------
def test_ui_index_served(ui_client):
    r = ui_client.get("/ui")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "broker playground" in r.text
    assert '<script src="/ui/app.js"' in r.text


def test_ui_index_trailing_slash(ui_client):
    r = ui_client.get("/ui/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


def test_ui_app_js_served(ui_client):
    r = ui_client.get("/ui/app.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript")
    assert len(r.content) > 0


def test_ui_styles_css_served(ui_client):
    r = ui_client.get("/ui/styles.css")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")


def test_ui_favicon_served(ui_client):
    r = ui_client.get("/ui/favicon.svg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/svg+xml"


# --- security headers -----------------------------------------------------
def test_ui_index_security_headers_exact(ui_client):
    r = ui_client.get("/ui")
    for name, value in EXPECTED_SECURITY_HEADERS.items():
        assert r.headers.get(name) == value, name


def test_ui_asset_security_headers_exact(ui_client):
    r = ui_client.get("/ui/app.js")
    for name, value in EXPECTED_SECURITY_HEADERS.items():
        assert r.headers.get(name) == value, name


def test_ui_404_still_carries_security_headers(ui_client):
    r = ui_client.get("/ui/does-not-exist.js")
    assert r.status_code == 404
    assert (
        r.headers.get("content-security-policy")
        == EXPECTED_SECURITY_HEADERS["content-security-policy"]
    )


def test_ui_csp_has_no_unsafe_or_external(ui_client):
    csp = ui_client.get("/ui").headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert "frame-ancestors 'none'" in csp
    assert "http://" not in csp and "https://" not in csp
    # No scheme that would permit a third-party origin.
    assert "*" not in csp


# --- shipped-asset hygiene (scan files on disk) ---------------------------
def _ui_files() -> list[Path]:
    return [p for p in _STATIC_DIR.rglob("*") if p.is_file()]


# The SVG XML namespace is a non-fetchable identifier, not a network reference.
_ALLOWED_URI = "http://www.w3.org/2000/svg"


def test_assets_have_no_external_urls():
    offenders = {}
    for p in _ui_files():
        text = p.read_text(encoding="utf-8")
        cleaned = text.replace(_ALLOWED_URI, "")
        found = re.findall(r"https?://[^\s\"'<>)]+", cleaned)
        if found:
            offenders[p.name] = found
    assert not offenders, f"external URLs in shipped UI assets: {offenders}"


def test_index_has_no_inline_script():
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", html, re.DOTALL | re.IGNORECASE):
        attrs, body = m.group(1), m.group(2)
        assert "src=" in attrs, "inline <script> without src is forbidden under strict CSP"
        assert body.strip() == "", "inline <script> body is forbidden under strict CSP"


def test_index_has_no_inline_style():
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "<style" not in html.lower(), "inline <style> blocks are forbidden under strict CSP"
    assert not re.search(r"\sstyle\s*=", html), "inline style= attributes are forbidden"


def test_app_js_has_no_eval():
    js = (_STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "eval(" not in js
    assert "new Function" not in js


# --- no new unauthenticated data routes -----------------------------------
def _route_paths(app) -> set[str]:
    return {getattr(r, "path", "") for r in app.routes}


def test_ui_adds_only_static_routes(tmp_path):
    base = create_app(_settings(tmp_path, ui_enabled=False))
    withui = create_app(_settings(tmp_path, ui_enabled=True))
    added = _route_paths(withui) - _route_paths(base)
    assert added <= {"/ui", "/ui/", "/ui/{path:path}"}, added


def test_data_endpoints_still_require_auth(ui_client):
    # Enabling the UI must not open any data endpoint to anonymous callers.
    assert ui_client.get("/v1/models").status_code == 401
    assert ui_client.get("/status").status_code == 401
    assert ui_client.get("/usage").status_code == 401


# --- path traversal -------------------------------------------------------
def test_ui_path_traversal_encoded_blocked(ui_client):
    # %2e%2e%2f = ../  — httpx does not normalize these, so they reach routing.
    r = ui_client.get("/ui/%2e%2e%2f%2e%2e%2fconfig.py")
    assert r.status_code == 404
    assert "ui_enabled" not in r.text  # never leaked config.py contents


def test_ui_path_traversal_to_package_blocked(ui_client):
    r = ui_client.get("/ui/%2e%2e%2fapp.py")
    assert r.status_code == 404


def test_ui_unknown_asset_404(ui_client):
    assert ui_client.get("/ui/nope.js").status_code == 404


def test_ui_disallowed_extension_404(ui_client):
    # Even a real file with a non-allow-listed extension is not served.
    assert ui_client.get("/ui/../pyproject.toml").status_code == 404


# --- content types --------------------------------------------------------
@pytest.mark.parametrize(
    ("path", "prefix"),
    [
        ("/ui/app.js", "text/javascript"),
        ("/ui/styles.css", "text/css"),
        ("/ui/index.html", "text/html"),
        ("/ui/favicon.svg", "image/svg+xml"),
    ],
)
def test_ui_content_types(ui_client, path, prefix):
    r = ui_client.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(prefix)


# --- functional smoke (the pipeline the UI drives) ------------------------
def test_ui_smoke_streaming_chat(ui_client):
    """UI-equivalent streaming chat against the fake backend: assert the stream
    yields deltas, [DONE], and the x-broker-route header the UI displays."""
    body = {
        "model": "echo-fake",
        "messages": [{"role": "user", "content": "hello from the playground"}],
        "stream": True,
    }
    with ui_client.stream("POST", "/v1/chat/completions", headers=AUTH, json=body) as r:
        assert r.status_code == 200
        assert r.headers.get("x-broker-route")
        saw_done = False
        saw_delta = False
        for line in r.iter_lines():
            if line == "data: [DONE]":
                saw_done = True
            elif line.startswith("data: "):
                import json

                chunk = json.loads(line[6:])
                if chunk.get("choices", [{}])[0].get("delta", {}).get("content"):
                    saw_delta = True
    assert saw_done and saw_delta


# --- runnable example (examples/web-ui) -----------------------------------
def test_example_web_ui_config():
    """The examples/web-ui registry loads and serves /ui + a chat against the
    fake backend, so the shipped example cannot drift from the code."""
    example_dir = Path(__file__).resolve().parents[1] / "examples" / "web-ui"
    registry = example_dir / "registry.yaml"
    assert registry.is_file(), "examples/web-ui/registry.yaml is missing"
    assert (example_dir / ".env.example").is_file()
    assert (example_dir / "README.md").is_file()
    settings = Settings(
        auth_enabled=True,
        api_keys=[KEY],
        registry_path=str(registry),
        enabled_backends=["fake"],
        ui_enabled=True,
        usage_log_path=":memory:",
    )
    app = create_app(settings)
    with TestClient(app) as c:
        assert c.get("/ui").status_code == 200
        models = c.get("/v1/models", headers=AUTH).json()["data"]
        assert any(m["id"] == "echo-fake" for m in models)
        r = c.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={"model": "echo-fake", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["role"] == "assistant"


# --- hardened path handling: never 500, always security headers (regression) --
# Follow-up to ADR 0015: `_serve_asset` used to let an embedded NUL raise
# ValueError and an over-long name raise OSError (ENAMETOOLONG), surfacing as a
# header-less 500. Every hostile path below must degrade to a hardened 404.
CSP = EXPECTED_SECURITY_HEADERS["content-security-policy"]

MALFORMED_PATHS = [
    # Embedded NUL byte, three placements.
    "/ui/index.html%00.js",
    "/ui/%00",
    "/ui/a%00b",
    # One over-long path segment (> NAME_MAX) -> would OSError on resolve/is_file.
    "/ui/" + "a" * 5000,
    # Over-long *total* path built from many in-spec-length segments.
    "/ui/" + "/".join(["a" * 200] * 10),
    # C0 control characters and DEL.
    "/ui/a%01b.js",
    "/ui/%1f",
    "/ui/%7f.css",
    # Backslashes (Windows-style separators / drive hints).
    "/ui/..%5c..%5capp.py",
    "/ui/a%5cb.js",
    # Drive-like / colon prefixes.
    "/ui/c:/windows/system32",
    # Double-encoded traversal (%252e%252e -> %2e%2e -> ..).
    "/ui/%252e%252e%252fapp.py",
    "/ui/%252e%252e%252f%252e%252e%252fconfig.py",
]


@pytest.mark.parametrize("path", MALFORMED_PATHS)
def test_ui_malformed_path_is_hardened_404(ui_client, path):
    r = ui_client.get(path)
    assert r.status_code == 404, (path, r.status_code)
    # Security headers — crucially the CSP — must be present on the error path.
    assert r.headers.get("content-security-policy") == CSP, path
    for name, value in EXPECTED_SECURITY_HEADERS.items():
        assert r.headers.get(name) == value, (path, name)


def test_ui_malformed_paths_never_leak_source(ui_client):
    # Defense-in-depth: none of the traversal attempts may return package source.
    for path in MALFORMED_PATHS:
        body = ui_client.get(path).text
        assert "ui_enabled" not in body
        assert "_serve_asset" not in body


def _fuzz_paths(n: int) -> list[str]:
    """Generate ~`n` deterministic odd request paths under /ui.

    Mixes percent-encoded NUL/control bytes, traversal fragments, over-long
    segments, backslashes, colons, allow-listed and disallowed extensions, and
    occasionally a real asset name so some requests legitimately hit 200.
    """
    import random

    rng = random.Random(0xB0CA)
    real = ["index.html", "app.js", "styles.css", "favicon.svg"]
    pieces = [
        "%00",
        "%01",
        "%1f",
        "%7f",
        "..",
        "%2e%2e",
        "%252e%252e",
        "%2f",
        "%5c",
        "\\",
        ":",
        "a" * rng.randint(1, 400),
        "x" * rng.randint(250, 300),
        ".py",
        ".html",
        ".js",
        ".css",
        ".svg",
        ".txt",
        "/",
        "foo",
    ]
    out = []
    for _ in range(n):
        if rng.random() < 0.1:
            out.append("/ui/" + rng.choice(real))
            continue
        k = rng.randint(1, 6)
        seg = "".join(rng.choice(pieces) for _ in range(k))
        out.append("/ui/" + seg)
    return out


@pytest.mark.parametrize("path", _fuzz_paths(200))
def test_ui_fuzz_paths_never_500(ui_client, path):
    r = ui_client.get(path)
    # The only acceptable outcomes are a served asset or a hardened 404; never a
    # 500 and never a response missing the CSP header.
    assert r.status_code in {200, 404}, (path, r.status_code)
    # httpx performs RFC 3986 dot-segment removal client-side, so a path with a
    # literal ``..`` can normalize out of /ui before it is sent; only assert the
    # CSP when the request that actually reached the server stayed under /ui.
    if r.request.url.path.startswith("/ui"):
        assert r.headers.get("content-security-policy") == CSP, path
