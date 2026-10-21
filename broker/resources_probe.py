"""CLI: print one telemetry sample for a provider (ADR 0020).

    python -m broker.resources_probe host      # system RAM (CPU / Apple-silicon)
    python -m broker.resources_probe nvidia     # nvidia-smi query mode
    python -m broker.resources_probe amd        # amd-smi / rocm-smi
    python -m broker.resources_probe auto       # probe nvidia -> amd -> host

Used by ``examples/gpu-memory/`` and the operations runbook. Prints a JSON sample
and exits 0 when the provider is available, 2 when it degraded to ``unavailable``
(so a smoke script can tell "no measurement" from "crashed"). It never raises on a
missing tool — a failed probe is reported, not thrown.
"""

from __future__ import annotations

import json
import sys

from .resources import _PROVIDERS, OffTelemetry, ResourceTelemetry


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    name = args[0] if args else "auto"
    cls = _PROVIDERS.get(name)
    if cls is None or cls is OffTelemetry:
        choices = ", ".join(k for k in _PROVIDERS if k != "off")
        print(f"unknown provider {name!r}; choose one of: {choices}", file=sys.stderr)
        return 1
    provider: ResourceTelemetry = cls()
    sample = provider.sample()
    print(json.dumps(sample.to_dict(), indent=2))
    return 0 if sample.ok else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
