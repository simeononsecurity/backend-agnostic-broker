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

# --- Admission queue (ADR 0023) ---
# New series only; the /metrics endpoint exposure itself is unchanged.
QUEUE_ENQUEUED = Counter(
    "broker_queue_enqueued_total",
    "Requests that entered the admission queue, by overload trigger",
    ["trigger"],
)
QUEUE_ADMITTED = Counter(
    "broker_queue_admitted_total",
    "Requests admitted out of the queue after waiting",
)
QUEUE_REJECTED = Counter(
    "broker_queue_rejected_total",
    "Requests rejected because the queue was full (429), by trigger",
    ["trigger"],
)
QUEUE_TIMEOUTS = Counter(
    "broker_queue_timeouts_total",
    "Queued requests that timed out waiting (503), by trigger",
    ["trigger"],
)
QUEUE_DEPTH = Gauge(
    "broker_queue_depth",
    "Current admission-queue depth (requests waiting)",
)
QUEUE_WAIT_SECONDS = Histogram(
    "broker_queue_wait_seconds",
    "Time a request spent waiting in the admission queue before admission",
)
