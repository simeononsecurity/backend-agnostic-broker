"""ADR 0020 resource-telemetry parsers and providers.

Parser tests run against recorded fixtures in ``tests/data/resources`` (multi-GPU,
MIG/unsupported, malformed, AMD, rocm-smi, Linux /proc, macOS vm_stat). Provider
tests prove the degrade-never-crash contract and the host provider on the real
machine. No GPU is required.
"""

from __future__ import annotations

import json
from pathlib import Path

from broker.config import Settings
from broker.resources import (
    AmdTelemetry,
    FakeTelemetry,
    HostTelemetry,
    NvidiaTelemetry,
    OffTelemetry,
    _attribute_nvidia,
    make_telemetry,
    parse_amd_smi_json,
    parse_macos_memory,
    parse_nvidia_compute_apps,
    parse_nvidia_smi_memory,
    parse_proc_meminfo,
    parse_rocm_smi_json,
    resolve_tool,
)
from broker.resources_probe import main as probe_main

DATA = Path(__file__).parent / "data" / "resources"


def _read(name: str) -> str:
    return (DATA / name).read_text()


# --- nvidia parsers --------------------------------------------------------
def test_parse_nvidia_multi_gpu():
    devices = parse_nvidia_smi_memory(_read("nvidia_smi_memory.csv"))
    assert [d.index for d in devices] == ["0", "1"]
    assert devices[0].total_mib == 40960
    assert devices[0].free_mib == 39000
    assert devices[0].used_mib == 1960
    assert devices[1].free_mib == 100


def test_parse_nvidia_mig_unsupported_fields_are_none_not_zero():
    devices = parse_nvidia_smi_memory(_read("nvidia_smi_memory_mig.csv"))
    # [N/A] memory must become None (never an invented 0).
    assert devices[0].total_mib is None
    assert devices[0].free_mib is None
    assert devices[1].free_mib == 80000


def test_parse_nvidia_malformed_yields_empty():
    assert parse_nvidia_smi_memory(_read("nvidia_smi_memory_malformed.txt")) == []


def test_parse_nvidia_compute_apps():
    apps = parse_nvidia_compute_apps(_read("nvidia_compute_apps.csv"))
    assert apps["GPU-5f8e2c1a-0000-0000-0000-000000000000"] == {12345: 1900}
    assert apps["GPU-9a7b3d2e-1111-1111-1111-111111111111"] == {67890: 40000}


def test_compute_apps_two_column_form_maps_to_wildcard():
    apps = parse_nvidia_compute_apps("111, 500\n222, 600\n")
    assert apps == {"*": {111: 500, 222: 600}}


def test_attribute_nvidia_by_index_and_wildcard():
    devices = parse_nvidia_smi_memory(_read("nvidia_smi_memory.csv"))
    out = _attribute_nvidia(devices, {"0": {111: 100}, "*": {999: 5}})
    assert out[0].processes == {111: 100, 999: 5}
    assert out[1].processes == {999: 5}


# --- amd parsers -----------------------------------------------------------
def test_parse_amd_smi_with_processes():
    devices = parse_amd_smi_json(_read("amd_smi_mem.json"), _read("amd_smi_process.json"))
    assert [d.index for d in devices] == ["0", "1"]
    assert devices[0].total_mib == 196592
    assert devices[0].free_mib == 195568
    assert devices[0].processes == {4321: 1024}
    assert devices[1].processes == {8888: 190000}


def test_parse_amd_free_derived_when_absent():
    text = json.dumps(
        [
            {
                "gpu": 0,
                "mem_usage": {
                    "total_vram": {"value": 1000, "unit": "MB"},
                    "used_vram": {"value": 400, "unit": "MB"},
                },
            }
        ]
    )
    devices = parse_amd_smi_json(text)
    assert devices[0].free_mib == 600


def test_parse_amd_malformed_yields_empty():
    assert parse_amd_smi_json("not json") == []


def test_parse_rocm_smi_bytes_to_mib():
    devices = parse_rocm_smi_json(_read("rocm_smi_meminfo.json"))
    assert devices[0].index == "0"
    assert devices[0].total_mib == 65520
    assert devices[0].used_mib == 1024
    assert devices[1].free_mib is not None and devices[1].free_mib < 2000


# --- host parsers ----------------------------------------------------------
def test_parse_proc_meminfo_uses_memavailable():
    dev = parse_proc_meminfo(_read("proc_meminfo.txt"))
    assert dev.index == "host"
    assert dev.total_mib == 32047
    assert dev.free_mib == 16000  # MemAvailable // 1024
    assert dev.used_mib == 32047 - 16000


def test_parse_macos_memory():
    dev = parse_macos_memory("17179869184", "16384", _read("vm_stat.txt"))
    assert dev.total_mib == 16384
    # free = (65536 + 120000 + 10000) * 16384 bytes
    assert dev.free_mib == (65536 + 120000 + 10000) * 16384 // (1024 * 1024)


# --- providers: degrade-never-crash ---------------------------------------
def test_off_provider_is_unavailable():
    s = OffTelemetry().sample()
    assert s.status == "unavailable" and s.provider == "off"


def test_nvidia_unavailable_when_tool_missing(monkeypatch):
    monkeypatch.setattr("broker.resources.resolve_tool", lambda *a, **k: None)
    s = NvidiaTelemetry().sample()
    assert s.status == "unavailable"
    assert "not found" in (s.reason or "")


def test_amd_unavailable_when_tools_missing(monkeypatch):
    monkeypatch.setattr("broker.resources.resolve_tool", lambda *a, **k: None)
    s = AmdTelemetry().sample()
    assert s.status == "unavailable"


def test_host_provider_reports_measured_memory():
    s = HostTelemetry().sample()
    assert s.status == "ok"
    assert s.source == "measured"
    assert s.devices and s.devices[0].total_mib and s.devices[0].total_mib > 0


def test_resolve_tool_rejects_relative_override():
    assert resolve_tool("nvidia-smi", override="nvidia-smi") is None


def test_make_telemetry_selects_provider():
    s = Settings(gpu_telemetry="host", registry_path=__file__)
    assert make_telemetry(s).name == "host"
    s2 = Settings(gpu_telemetry="off", registry_path=__file__)
    assert isinstance(make_telemetry(s2), OffTelemetry)


# --- fake provider (reused by ADR 0023) -----------------------------------
def test_fake_telemetry_is_scriptable():
    from broker.resources import DeviceMemory

    fake = FakeTelemetry([DeviceMemory(index="0", name="GPU 0", free_mib=1000, processes={7: 500})])
    s = fake.sample()
    assert s.ok and s.device("0").processes == {7: 500}
    fake.available = False
    assert fake.sample().status == "unavailable"


# --- probe CLI -------------------------------------------------------------
def test_probe_cli_host_exit_zero(capsys):
    rc = probe_main(["host"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["status"] == "ok" and out["source"] == "measured"


def test_probe_cli_unknown_provider():
    assert probe_main(["bogus"]) == 1


def test_probe_cli_off_is_rejected():
    assert probe_main(["off"]) == 1
