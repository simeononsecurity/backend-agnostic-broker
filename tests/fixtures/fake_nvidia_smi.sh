#!/usr/bin/env bash
# Recorded-fixture nvidia-smi stand-in for ADR 0027 GPU drills (D7/D8).
#
# Emits deterministic CSV matching the two queries broker/resources.py NvidiaTelemetry
# makes, with NO real GPU required. Content is driven by env so one script serves
# both the "no device in the inventory" (D7) and "runtime holds no device memory"
# (D8) drills:
#
#   FAKE_NVIDIA_GPUS  CSV lines for --query-gpu=index,name,memory.total,memory.free,memory.used
#                     (one line per device; empty => no GPUs => empty inventory).
#   FAKE_NVIDIA_APPS  CSV lines for --query-compute-apps=gpu_uuid,pid,used_memory
#                     (empty => nothing attributed => a required model verifies as CPU-placed).
#
# No shell metacharacters are interpreted: values are printed verbatim.
set -eu

args="$*"
case "$args" in
  *query-compute-apps*)
    printf '%s' "${FAKE_NVIDIA_APPS:-}"
    [ -n "${FAKE_NVIDIA_APPS:-}" ] && printf '\n' || true
    ;;
  *query-gpu*)
    printf '%s' "${FAKE_NVIDIA_GPUS:-}"
    [ -n "${FAKE_NVIDIA_GPUS:-}" ] && printf '\n' || true
    ;;
  *)
    # Any other invocation: behave like a present-but-idle nvidia-smi.
    printf ''
    ;;
esac
exit 0
