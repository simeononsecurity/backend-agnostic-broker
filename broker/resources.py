"""Vendor-neutral resource telemetry (ADR 0020).

A small, pluggable provider interface that reports **per-device memory** and, where
the driver exposes it, **which PIDs hold memory on which device**. Providers shell
out to the stock driver CLI with an **argv list** (never a shell string), resolve
the binary to an **absolute, allow-listed path**, apply a **timeout**, and parse
defensively. A missing tool or a failed/garbled probe degrades to a sample with
``status="unavailable"`` and a human reason — never a crash and never an invented
number. Every measured sample carries ``source="measured"``.

Selected by ``BROKER_GPU_TELEMETRY``:

* ``off``    — telemetry disabled (DEFAULT); behavior equals v0.6.0.
* ``nvidia`` — ``nvidia-smi`` query mode (``--query-gpu`` / ``--query-compute-apps``).
* ``amd``    — ``amd-smi ... --json`` with a ``rocm-smi --json`` fallback.
* ``host``   — system RAM (Linux ``/proc/meminfo``; macOS ``sysctl`` + ``vm_stat``).
* ``auto``   — probe ``nvidia`` → ``amd`` → ``host``, first that works.

The parsing functions are pure (operate on captured text) so every one is unit
tested against recorded fixtures, including malformed, multi-GPU and
MIG/partitioned output. See ``docs/adr/0020-resource-telemetry-and-measured-release.md``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess  # noqa: S404 - argv-only, absolute path, no shell (see _run)
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("broker.resources")

# Candidate absolute locations for each tool. The first that exists and is
# executable is used; PATH is never consulted (an attacker-controlled PATH must
# not be able to redirect a privileged query). An operator may override with the
# matching BROKER_GPU_TELEMETRY_*_PATH setting (validated absolute).
_TOOL_CANDIDATES: dict[str, tuple[str, ...]] = {
    "nvidia-smi": (
        "/usr/bin/nvidia-smi",
        "/bin/nvidia-smi",
        "/usr/local/bin/nvidia-smi",
        "/opt/nvidia/bin/nvidia-smi",
    ),
    "amd-smi": (
        "/usr/bin/amd-smi",
        "/usr/local/bin/amd-smi",
        "/opt/rocm/bin/amd-smi",
    ),
    "rocm-smi": (
        "/usr/bin/rocm-smi",
        "/opt/rocm/bin/rocm-smi",
        "/usr/local/bin/rocm-smi",
    ),
    "sysctl": ("/usr/sbin/sysctl", "/sbin/sysctl"),
    "vm_stat": ("/usr/bin/vm_stat",),
}


@dataclass(frozen=True)
class DeviceMemory:
    """Memory for one device (a GPU ordinal, a MIG instance, or the host)."""

    index: str
    name: str
    total_mib: int | None = None
    used_mib: int | None = None
    free_mib: int | None = None
    #: pid -> MiB held on this device, where the driver exposes per-process usage.
    processes: dict[int, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "total_mib": self.total_mib,
            "used_mib": self.used_mib,
            "free_mib": self.free_mib,
            "processes": [{"pid": p, "mib": m} for p, m in sorted(self.processes.items())],
        }


@dataclass(frozen=True)
class TelemetrySample:
    """One telemetry read. ``status`` is ``ok`` or ``unavailable`` (never a crash)."""

    provider: str
    status: str  # "ok" | "unavailable"
    devices: tuple[DeviceMemory, ...] = ()
    reason: str | None = None
    source: str = "measured"
    captured_at: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def device(self, index: str) -> DeviceMemory | None:
        for d in self.devices:
            if str(d.index) == str(index):
                return d
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status,
            "source": self.source,
            "reason": self.reason,
            "captured_at": self.captured_at,
            "devices": [d.to_dict() for d in self.devices],
        }


# --------------------------------------------------------------------------- #
# Tool resolution + execution (argv-only, absolute path, timeout, no shell)
# --------------------------------------------------------------------------- #
def resolve_tool(name: str, override: str | None = None) -> str | None:
    """Return an absolute executable path for ``name`` or None.

    An ``override`` is honored only when it is an absolute path to an existing
    executable file. Otherwise the fixed allow-list in :data:`_TOOL_CANDIDATES`
    is searched. PATH is intentionally never consulted.
    """
    if override:
        if os.path.isabs(override) and os.path.isfile(override) and os.access(override, os.X_OK):
            return override
        log.warning(
            "telemetry tool override %r for %s is not an absolute executable", override, name
        )
        return None
    for cand in _TOOL_CANDIDATES.get(name, ()):  # pragma: no branch
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    # Last resort: shutil.which, but only accept an absolute result.
    found = shutil.which(name)
    if found and os.path.isabs(found):
        return found
    return None


def run_tool(argv: Sequence[str], timeout: float) -> str:
    """Run ``argv`` (list, no shell) with a timeout; return stdout text.

    Raises :class:`TelemetryError` on non-zero exit, timeout, or spawn failure so
    the provider can degrade to ``status="unavailable"`` with a reason.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, absolute path, shell=False
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise TelemetryError(f"tool not found: {argv[0]!r}") from exc
    except subprocess.TimeoutExpired as exc:
        raise TelemetryError(f"tool timed out after {timeout}s: {argv[0]!r}") from exc
    except OSError as exc:  # pragma: no cover - defensive
        raise TelemetryError(f"tool failed to run: {exc}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
        raise TelemetryError(f"{argv[0]!r} exited {proc.returncode}: {tail[0][:200]}")
    return proc.stdout


class TelemetryError(Exception):
    """Internal: a probe could not produce a measurement (degrade, never crash)."""


# --------------------------------------------------------------------------- #
# Parsers (pure; unit-tested against recorded fixtures)
# --------------------------------------------------------------------------- #
def _to_int_mib(token: str) -> int | None:
    token = token.strip()
    if not token or token.lower() in ("[n/a]", "n/a", "", "[not supported]"):
        return None
    # Strip a trailing unit if present (we request nounits, but be defensive).
    for unit in (" MiB", " MB", "MiB", "MB"):
        if token.endswith(unit):
            token = token[: -len(unit)].strip()
    try:
        return int(float(token))
    except ValueError:
        return None


def parse_nvidia_smi_memory(text: str) -> list[DeviceMemory]:
    """Parse ``nvidia-smi --query-gpu=index,name,memory.total,memory.free,memory.used``.

    Expected CSV (``--format=csv,noheader,nounits``) rows::

        0, NVIDIA A100-SXM4-40GB, 40960, 39000, 1960

    Rows that don't parse are skipped (defensive); an all-unparseable body yields
    an empty list so the provider reports ``unavailable`` rather than inventing data.
    """
    out: list[DeviceMemory] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        idx, name = parts[0], parts[1]
        if not idx:
            continue
        total = _to_int_mib(parts[2])
        free = _to_int_mib(parts[3])
        used = _to_int_mib(parts[4])
        out.append(
            DeviceMemory(
                index=idx, name=name or f"GPU {idx}", total_mib=total, free_mib=free, used_mib=used
            )
        )
    return out


def parse_nvidia_compute_apps(text: str) -> dict[str, dict[int, int]]:
    """Parse ``nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory``.

    Returns ``{gpu_uuid_or_index: {pid: mib}}``. Older/alt query forms that omit
    the gpu column (``pid,used_memory``) map every PID under the sentinel key
    ``"*"`` (applies to all devices) so the caller can still attribute holders.
    """
    out: dict[str, dict[int, int]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            key, pid_s, mem_s = parts[0], parts[1], parts[2]
        elif len(parts) == 2:
            key, pid_s, mem_s = "*", parts[0], parts[1]
        else:
            continue
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        mib = _to_int_mib(mem_s) or 0
        out.setdefault(key or "*", {})[pid] = mib
    return out


def parse_amd_smi_json(mem_text: str, proc_text: str | None = None) -> list[DeviceMemory]:
    """Parse ``amd-smi metric --mem-usage --json`` (+ optional ``process --json``).

    amd-smi emits a JSON list, one object per GPU. Memory fields appear under a
    ``mem_usage`` block with ``total_vram`` / ``used_vram`` / ``free_vram`` whose
    values are ``{"value": N, "unit": "MB"}``. Process attribution (when supplied)
    comes from ``amd-smi process --json`` whose per-GPU ``process_list`` carries
    ``pid`` and a ``memory_usage``/``vram_usage`` MB figure.
    """
    out: list[DeviceMemory] = []
    try:
        data = json.loads(mem_text)
    except (ValueError, TypeError):
        return out
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return out

    procs_by_gpu = _parse_amd_processes(proc_text) if proc_text else {}

    for i, dev in enumerate(data):
        if not isinstance(dev, dict):
            continue
        gpu_id = dev.get("gpu") if dev.get("gpu") is not None else dev.get("gpu_id", i)
        idx = str(gpu_id)
        mem = dev.get("mem_usage") or dev.get("mem") or {}
        total = _amd_mib(mem.get("total_vram") or mem.get("total"))
        used = _amd_mib(mem.get("used_vram") or mem.get("used"))
        free = _amd_mib(mem.get("free_vram") or mem.get("free"))
        if free is None and total is not None and used is not None:
            free = max(total - used, 0)
        out.append(
            DeviceMemory(
                index=idx,
                name=str(dev.get("name") or f"GPU {idx}"),
                total_mib=total,
                used_mib=used,
                free_mib=free,
                processes=procs_by_gpu.get(idx, {}),
            )
        )
    return out


def _amd_mib(val: Any) -> int | None:
    if val is None:
        return None
    if isinstance(val, dict):
        value = val.get("value")
        unit = str(val.get("unit", "MB")).upper()
    else:
        value, unit = val, "MB"
    if value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if unit in ("GB", "GIB"):
        n *= 1024
    elif unit in ("B", "BYTES"):
        n /= 1024 * 1024
    elif unit in ("KB", "KIB"):
        n /= 1024
    return int(n)


def _parse_amd_processes(text: str) -> dict[str, dict[int, int]]:
    out: dict[str, dict[int, int]] = {}
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return out
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return out
    for i, dev in enumerate(data):
        if not isinstance(dev, dict):
            continue
        idx = str(dev.get("gpu", dev.get("gpu_id", i)))
        plist = dev.get("process_list") or dev.get("processes") or []
        for entry in plist:
            info = entry.get("process_info", entry) if isinstance(entry, dict) else {}
            if not isinstance(info, dict):
                continue
            pid_raw = info.get("pid")
            if pid_raw is None:
                continue
            try:
                pid = int(pid_raw)
            except (TypeError, ValueError):
                continue
            mib = _amd_mib(info.get("memory_usage") or info.get("vram_usage") or info.get("vram"))
            out.setdefault(idx, {})[pid] = mib or 0
    return out


def parse_rocm_smi_json(text: str) -> list[DeviceMemory]:
    """Parse ``rocm-smi --showmeminfo vram --json`` (fallback for older ROCm).

    rocm-smi keys devices as ``card0``/``card1`` with ``VRAM Total Memory (B)``
    and ``VRAM Total Used Memory (B)`` in **bytes**.
    """
    out: list[DeviceMemory] = []
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return out
    if not isinstance(data, dict):
        return out
    for key in sorted(data.keys()):
        if not key.lower().startswith("card"):
            continue
        dev = data[key]
        if not isinstance(dev, dict):
            continue
        total_b = _first_num(dev, ("VRAM Total Memory (B)", "VRAM Total Memory (B) ", "vram_total"))
        used_b = _first_num(dev, ("VRAM Total Used Memory (B)", "vram_used"))
        total = int(total_b / (1024 * 1024)) if total_b is not None else None
        used = int(used_b / (1024 * 1024)) if used_b is not None else None
        free = (total - used) if (total is not None and used is not None) else None
        idx = key[4:] if key[4:].isdigit() else key
        out.append(DeviceMemory(index=idx, name=key, total_mib=total, used_mib=used, free_mib=free))
    return out


def _first_num(d: dict[str, Any], keys: Sequence[str]) -> float | None:
    for k in keys:
        if k in d:
            try:
                return float(str(d[k]).strip())
            except (ValueError, TypeError):
                continue
    return None


def parse_proc_meminfo(text: str) -> DeviceMemory:
    """Parse Linux ``/proc/meminfo`` into a single host 'device'.

    Uses ``MemTotal`` and ``MemAvailable`` (the kernel's honest estimate of memory
    available for new apps without swapping). Values are in kB.
    """
    vals: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        tok = rest.strip().split()
        if not tok:
            continue
        try:
            vals[name.strip()] = int(tok[0])
        except ValueError:
            continue
    total_kb = vals.get("MemTotal")
    avail_kb = vals.get("MemAvailable")
    if avail_kb is None and "MemFree" in vals:
        avail_kb = vals["MemFree"] + vals.get("Buffers", 0) + vals.get("Cached", 0)
    total = total_kb // 1024 if total_kb is not None else None
    free = avail_kb // 1024 if avail_kb is not None else None
    used = (total - free) if (total is not None and free is not None) else None
    return DeviceMemory(index="host", name="host", total_mib=total, used_mib=used, free_mib=free)


def parse_macos_memory(memsize_bytes: str, pagesize_bytes: str, vm_stat_text: str) -> DeviceMemory:
    """Parse macOS ``sysctl hw.memsize`` + ``hw.pagesize`` + ``vm_stat``.

    Available ≈ (free + inactive + speculative-less-double-count) × pagesize, the
    same basis Activity Monitor / psutil use. Total is ``hw.memsize``.
    """
    try:
        total = int(memsize_bytes.strip()) // (1024 * 1024)
    except ValueError:
        total = None
    try:
        pagesize = int(pagesize_bytes.strip())
    except ValueError:
        pagesize = 4096
    pages: dict[str, int] = {}
    for line in vm_stat_text.splitlines():
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        digits = "".join(ch for ch in rest if ch.isdigit())
        if digits:
            pages[name.strip().lower()] = int(digits)
    free_pages = pages.get("pages free", 0)
    inactive = pages.get("pages inactive", 0)
    speculative = pages.get("pages speculative", 0)
    avail_pages = free_pages + inactive + speculative
    free = (avail_pages * pagesize) // (1024 * 1024) if pages else None
    used = (total - free) if (total is not None and free is not None) else None
    return DeviceMemory(index="host", name="host", total_mib=total, used_mib=used, free_mib=free)


def _attribute_nvidia(
    devices: list[DeviceMemory], apps: dict[str, dict[int, int]]
) -> list[DeviceMemory]:
    """Attach per-PID holders to NVIDIA devices keyed by index (uuid keys unknown here)."""
    if not apps:
        return devices
    wildcard = apps.get("*", {})
    out: list[DeviceMemory] = []
    for d in devices:
        procs = dict(wildcard)
        procs.update(apps.get(d.index, {}))
        out.append(
            DeviceMemory(
                index=d.index,
                name=d.name,
                total_mib=d.total_mib,
                used_mib=d.used_mib,
                free_mib=d.free_mib,
                processes=procs,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #
class ResourceTelemetry(ABC):
    """A pluggable telemetry provider. ``sample()`` is cheap, synchronous, and
    NEVER raises — a failed probe returns an ``unavailable`` sample with a reason."""

    name = "base"

    def __init__(self, *, timeout: float = 4.0, overrides: dict[str, str] | None = None) -> None:
        self.timeout = timeout
        self.overrides = overrides or {}

    @abstractmethod
    def sample(self) -> TelemetrySample:  # pragma: no cover - abstract
        ...

    def _tool(self, name: str) -> str | None:
        return resolve_tool(name, self.overrides.get(name))


class OffTelemetry(ResourceTelemetry):
    name = "off"

    def sample(self) -> TelemetrySample:
        return TelemetrySample(provider="off", status="unavailable", reason="telemetry disabled")


class NvidiaTelemetry(ResourceTelemetry):
    name = "nvidia"

    def sample(self) -> TelemetrySample:
        exe = self._tool("nvidia-smi")
        if not exe:
            return TelemetrySample(
                provider="nvidia", status="unavailable", reason="nvidia-smi not found"
            )
        try:
            mem_text = run_tool(
                [
                    exe,
                    "--query-gpu=index,name,memory.total,memory.free,memory.used",
                    "--format=csv,noheader,nounits",
                ],
                self.timeout,
            )
        except TelemetryError as exc:
            return TelemetrySample(provider="nvidia", status="unavailable", reason=str(exc))
        devices = parse_nvidia_smi_memory(mem_text)
        if not devices:
            return TelemetrySample(
                provider="nvidia", status="unavailable", reason="no GPUs parsed from nvidia-smi"
            )
        try:
            apps_text = run_tool(
                [
                    exe,
                    "--query-compute-apps=gpu_uuid,pid,used_memory",
                    "--format=csv,noheader,nounits",
                ],
                self.timeout,
            )
            apps = parse_nvidia_compute_apps(apps_text)
        except TelemetryError:
            apps = {}
        devices = _attribute_nvidia(devices, apps)
        return TelemetrySample(provider="nvidia", status="ok", devices=tuple(devices))


class AmdTelemetry(ResourceTelemetry):
    name = "amd"

    def sample(self) -> TelemetrySample:
        exe = self._tool("amd-smi")
        if exe:
            try:
                mem_text = run_tool([exe, "metric", "--mem-usage", "--json"], self.timeout)
                try:
                    proc_text: str | None = run_tool([exe, "process", "--json"], self.timeout)
                except TelemetryError:
                    proc_text = None
                devices = parse_amd_smi_json(mem_text, proc_text)
                if devices:
                    return TelemetrySample(provider="amd", status="ok", devices=tuple(devices))
            except TelemetryError as exc:
                log.debug("amd-smi probe failed, trying rocm-smi: %s", exc)
        rocm = self._tool("rocm-smi")
        if rocm:
            try:
                text = run_tool([rocm, "--showmeminfo", "vram", "--json"], self.timeout)
                devices = parse_rocm_smi_json(text)
                if devices:
                    return TelemetrySample(provider="amd", status="ok", devices=tuple(devices))
            except TelemetryError as exc:
                return TelemetrySample(provider="amd", status="unavailable", reason=str(exc))
        return TelemetrySample(
            provider="amd", status="unavailable", reason="amd-smi/rocm-smi not found or no GPUs"
        )


class HostTelemetry(ResourceTelemetry):
    name = "host"

    def sample(self) -> TelemetrySample:
        if sys.platform == "darwin":
            return self._sample_macos()
        return self._sample_linux()

    def _sample_linux(self) -> TelemetrySample:
        try:
            with open("/proc/meminfo", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            return TelemetrySample(
                provider="host", status="unavailable", reason=f"/proc/meminfo: {exc}"
            )
        dev = parse_proc_meminfo(text)
        if dev.total_mib is None:
            return TelemetrySample(
                provider="host", status="unavailable", reason="could not parse /proc/meminfo"
            )
        return TelemetrySample(provider="host", status="ok", devices=(dev,))

    def _sample_macos(self) -> TelemetrySample:
        sysctl = self._tool("sysctl")
        vm_stat = self._tool("vm_stat")
        if not sysctl or not vm_stat:
            return TelemetrySample(
                provider="host", status="unavailable", reason="sysctl/vm_stat not found"
            )
        try:
            memsize = run_tool([sysctl, "-n", "hw.memsize"], self.timeout)
            pagesize = run_tool([sysctl, "-n", "hw.pagesize"], self.timeout)
            vm = run_tool([vm_stat], self.timeout)
        except TelemetryError as exc:
            return TelemetrySample(provider="host", status="unavailable", reason=str(exc))
        dev = parse_macos_memory(memsize, pagesize, vm)
        if dev.total_mib is None:
            return TelemetrySample(
                provider="host", status="unavailable", reason="could not read macOS memory"
            )
        return TelemetrySample(provider="host", status="ok", devices=(dev,))


class AutoTelemetry(ResourceTelemetry):
    name = "auto"

    def __init__(self, *, timeout: float = 4.0, overrides: dict[str, str] | None = None) -> None:
        super().__init__(timeout=timeout, overrides=overrides)
        self._chain = [
            NvidiaTelemetry(timeout=timeout, overrides=overrides),
            AmdTelemetry(timeout=timeout, overrides=overrides),
            HostTelemetry(timeout=timeout, overrides=overrides),
        ]
        self._chosen: ResourceTelemetry | None = None

    def sample(self) -> TelemetrySample:
        # Stick to the first provider that worked; otherwise re-probe the chain.
        if self._chosen is not None:
            s = self._chosen.sample()
            if s.ok:
                return s
            self._chosen = None
        reasons = []
        for prov in self._chain:
            s = prov.sample()
            if s.ok:
                self._chosen = prov
                return s
            reasons.append(f"{prov.name}: {s.reason}")
        return TelemetrySample(
            provider="auto", status="unavailable", reason="; ".join(reasons) or "no provider"
        )


class FakeTelemetry(ResourceTelemetry):
    """Fully scriptable provider for tests (and reused by ADR 0023).

    Set ``devices`` to a list of :class:`DeviceMemory` and flip ``available`` to
    simulate a probe failure. ``sample()`` returns a snapshot of the current
    scripted state, so a test can mutate ``devices`` between polls (e.g. a PID that
    keeps holding memory for N reads, then frees).
    """

    name = "fake"

    def __init__(
        self, devices: list[DeviceMemory] | None = None, *, available: bool = True
    ) -> None:
        super().__init__()
        self.devices: list[DeviceMemory] = devices or []
        self.available = available
        self.reason: str | None = None
        self.samples_taken = 0

    def sample(self) -> TelemetrySample:
        self.samples_taken += 1
        if not self.available:
            return TelemetrySample(
                provider="fake", status="unavailable", reason=self.reason or "scripted unavailable"
            )
        return TelemetrySample(provider="fake", status="ok", devices=tuple(self.devices))


_PROVIDERS: dict[str, type[ResourceTelemetry]] = {
    "off": OffTelemetry,
    "nvidia": NvidiaTelemetry,
    "amd": AmdTelemetry,
    "host": HostTelemetry,
    "auto": AutoTelemetry,
}


def make_telemetry(settings: Any) -> ResourceTelemetry:
    """Construct the telemetry provider named by ``settings.gpu_telemetry``."""
    name = getattr(settings, "gpu_telemetry", "off")
    cls = _PROVIDERS.get(name, OffTelemetry)
    overrides: dict[str, str] = {}
    for tool, attr in (
        ("nvidia-smi", "gpu_telemetry_nvidia_smi_path"),
        ("amd-smi", "gpu_telemetry_amd_smi_path"),
        ("rocm-smi", "gpu_telemetry_rocm_smi_path"),
    ):
        val = getattr(settings, attr, None)
        if val:
            overrides[tool] = val
    timeout = float(getattr(settings, "gpu_telemetry_timeout_seconds", 4.0))
    if cls in (OffTelemetry,):
        return cls()
    return cls(timeout=timeout, overrides=overrides)
