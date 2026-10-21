# syntax=docker/dockerfile:1
# Multi-stage build. The compile toolchain (build-essential/cmake) and all build
# wheels live ONLY in build stages; the final `runtime` image receives just the
# installed Python packages (a relocatable prefix) and, optionally, the router
# model — so the final image never carries compilers or a wheels layer.

ARG PYTHON_VERSION=3.11-slim

# ---- base Python deps (installed into a relocatable /install prefix) --------
FROM python:${PYTHON_VERSION} AS pydeps
WORKDIR /build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt .
RUN python -m pip install --upgrade pip && \
    pip install --prefix=/install -r requirements.txt

# ---- optional router classifier (compile llama.cpp + fetch the GGUF) --------
# Everything heavy (apt build tools, the compile) stays in THIS stage. The dirs
# are always created so the runtime COPY succeeds even when the router is off.
FROM python:${PYTHON_VERSION} AS router
ARG WITH_ROUTER_LLM=0
ARG ROUTER_MODEL_URL=https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN mkdir -p /install-router /models
RUN if [ "$WITH_ROUTER_LLM" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends build-essential cmake && \
      rm -rf /var/lib/apt/lists/* && \
      CMAKE_ARGS="-DGGML_NATIVE=OFF" pip install --prefix=/install-router "llama-cpp-python==0.3.2" && \
      python -c "import urllib.request; urllib.request.urlretrieve('${ROUTER_MODEL_URL}', '/models/router.gguf')" ; \
    fi

# ---- runtime (minimal, non-root) -------------------------------------------
FROM python:${PYTHON_VERSION} AS runtime

# OCI image metadata (version + source revision injected at build time).
ARG VERSION=0.1.0
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="backend-agnostic-broker" \
      org.opencontainers.image.description="OpenAI-compatible local AI broker" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="https://github.com/simeononsecurity/backend-agnostic-broker/" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BROKER_PORT=8080 \
    BROKER_HOST=0.0.0.0 \
    BROKER_CACHE_DIR=/var/cache/broker \
    BROKER_TMP_DIR=/tmp/broker \
    BROKER_ROUTER_MODEL_PATH=/models/router.gguf

# Non-root user.
RUN groupadd --system --gid 10001 broker && \
    useradd --system --uid 10001 --gid broker --home /app --shell /usr/sbin/nologin broker

WORKDIR /app

# Installed Python packages from the build stages (no wheels layer, no toolchain).
# The router prefix + model dir are empty unless built with WITH_ROUTER_LLM=1, in
# which case the final image also carries the compiled wheel's package + model.
COPY --from=pydeps /install /usr/local
COPY --from=router /install-router /usr/local
COPY --from=router /models /models
# The compiled llama.cpp shared lib needs the OpenMP runtime (libgomp). Install it
# only for the LLM variant so the lean image stays apt-free.
ARG WITH_ROUTER_LLM=0
RUN if [ "$WITH_ROUTER_LLM" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends libgomp1 && \
      rm -rf /var/lib/apt/lists/* ; \
    fi
# Drop build-only tooling (setuptools/wheel/pip) from the runtime image. The app
# runs via `python -m broker` and needs none of it; this clears vendored
# build-tool CVEs from the image surface.
RUN pip uninstall -y setuptools wheel pip || true

# App code.
COPY broker/ ./broker/
COPY registry.example.yaml ./registry.example.yaml

# Writable dirs (image root fs can be mounted read-only; these are tmpfs/volumes).
RUN mkdir -p "${BROKER_CACHE_DIR}" "${BROKER_TMP_DIR}" && \
    chown -R broker:broker /app "${BROKER_CACHE_DIR}" "${BROKER_TMP_DIR}"

USER broker
EXPOSE 8080

# Liveness probe (uses the broker's own /healthz; no shell tricks).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
    urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"BROKER_PORT\",\"8080\")}/healthz', timeout=3); sys.exit(0)" || exit 1

# Exec-form entrypoint so python is PID 1 and receives SIGTERM directly; uvicorn's
# SIGTERM handler drives the FastAPI lifespan shutdown, which stops every runtime
# and clears the runtime ledger (ADR 0021). No init shim / signal-forwarding is
# needed. STOPSIGNAL is the default but set explicitly for clarity.
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-m", "broker"]
