#!/bin/sh
# Stand-in broker-managed runtime for ADR 0025 real-process tests.
# Writes a sentinel file (arg 1) to prove it started, then sleeps so a test can
# observe it via pgrep / the runtime ledger. The required-GPU pre-load gate must
# prevent the broker from ever spawning this when the GPU inventory is empty, so
# the sentinel must NOT appear in that case.
echo "started pid=$$" > "$1"
exec sleep 60
