#!/bin/sh
# Device-lease hook example (ADR 0026).
#
# Invoked by the broker as:  pause-cotenant.sh <device> <mode> <holder>
# on lease acquire AND release. In production this is where you would pause the
# co-tenant that uses the device when the broker is idle, e.g.:
#
#     systemctl kill --signal=STOP "cotenant@$1"   # on acquire
#     systemctl kill --signal=CONT "cotenant@$1"   # on release
#
# It is side-effect-free here (only appends to a log) so it is safe to run in CI
# and as a copy-paste starting point. Exit non-zero to FAIL the lease operation
# (an acquire failure makes the model load return device_busy).
set -eu

DEVICE="${1:?usage: pause-cotenant.sh <device> <mode> <holder>}"
MODE="${2:?missing mode}"
HOLDER="${3:?missing holder}"
LOG="${LEASE_HOOK_LOG:-/tmp/lease-hook.log}"

echo "acquire-or-release device=${DEVICE} mode=${MODE} holder=${HOLDER}" >> "$LOG"
