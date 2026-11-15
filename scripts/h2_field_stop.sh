#!/usr/bin/env bash
# Stop the H2 field stack: workstation services and both SSH forwards.
# Robot-side services (PC2 agent, PC3 gateway) are left running so the
# next h2_field_start.sh reuses them; manual stop commands are printed.
#
# Usage: scripts/h2_field_stop.sh
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "== stopping workstation stack =="
.venv/bin/python scripts/dev.py stop

echo "== stopping SSH forwards =="
pkill -f '^ssh .* -L 127\.0\.0\.1:(15051:127\.0\.0\.1:50051|15052:127\.0\.0\.1:50052)( |$)' 2>/dev/null \
  && echo "stopped" || echo "no forward running"

echo "(PC2 agent and PC3 gateway keep running; to stop them fully:)"
echo "  ssh unitree@${H2_PC2_HOST:-192.168.124.162} 'pkill -f \"[s]ensor_agent\"'"
echo "  ssh unitree@${H2_PC3_HOST:-192.168.124.163} 'pkill -f \"[h]2_gateway.server\"'"
