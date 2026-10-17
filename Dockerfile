# syntax=docker/dockerfile:1
# Multi-stage build: wheels in builder, minimal non-root runtime image.

ARG PYTHON_VERSION=3.11-slim

# ---- builder ---------------------------------------------------------------
FROM python:${PYTHON_VERSION} AS builder
WORKDIR /build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt .
RUN python -m pip install --upgrade pip && \
    pip wheel --wheel-dir /wheels -r requirements.txt

# ---- runtime ---------------------------------------------------------------
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
    BROKER_TMP_DIR=/tmp/broker

# Non-root user.
RUN groupadd --system --gid 10001 broker && \
    useradd --system --uid 10001 --gid broker --home /app --shell /usr/sbin/nologin broker

WORKDIR /app

# Install deps from prebuilt wheels (no network at runtime-layer build).
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt && \
    rm -rf /wheels && \
    # Drop build-only tooling (setuptools/wheel) from the runtime image. The app
    # runs via `python -m broker` and needs none of it; this clears vendored
    # build-tool CVEs from the image surface.
    pip uninstall -y setuptools wheel pip || true

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

ENTRYPOINT ["python", "-m", "broker"]
