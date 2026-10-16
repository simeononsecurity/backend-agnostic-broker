"""Prometheus metrics."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

REQUESTS = Counter(
    "broker_requests_total",
    "Total API requests",
    ["route", "model", "status"],
)
REQUEST_LATENCY = Histogram(
    "broker_request_latency_seconds",
    "Request latency",
    ["route", "model"],
)
MODEL_LOADS = Counter(
    "broker_model_loads_total",
    "Model load events",
    ["model", "backend"],
)
MODEL_UNLOADS = Counter(
    "broker_model_unloads_total",
    "Model unload events",
    ["model", "backend"],
)
LOADED_MODELS = Gauge(
    "broker_loaded_models",
    "Currently loaded models",
)
INFLIGHT = Gauge(
    "broker_inflight_requests",
    "In-flight requests",
)
