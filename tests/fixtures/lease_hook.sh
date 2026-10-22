#!/bin/sh
# Stand-in lease hook for ADR 0026 tests. Appends "<device> <mode> <holder>" to
# the log file passed as the first literal argument, proving the broker invoked
# the acquire/release hook exactly once with the right substitutions.
# Usage (via BROKER_LEASE_*_COMMAND): lease_hook.sh <logfile> {device} {mode} {holder}
echo "$2 $3 $4" >> "$1"
