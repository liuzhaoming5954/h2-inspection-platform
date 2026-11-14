#!/usr/bin/env bash
# One-command field startup for H2 hardware teleoperation.
#
# Brings up, in order: robot-PC clock sync (host as reference), the PC2
# RS-485 sensor agent, the PC3 gRPC gateway, the SSH local forward
# (127.0.0.1:15051 -> PC3 127.0.0.1:50051), and the workstation stack in
# external-gateway mode. Components already running are skipped.
#
# SSH password is read from H2_FIELD_PASSWORD, or prompted once when run
# from a terminal. Never store robot passwords in this repository.
#
# Usage: scripts/h2_field_start.sh   then open http://127.0.0.1:5173
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PC2=192.168.124.162
PC3=192.168.124.163
LOG_DIR="$ROOT/.runtime/logs"
mkdir -p "$LOG_DIR"

PASS="${H2_FIELD_PASSWORD:-}"
if [ -z "$PASS" ]; then
  if [ -t 0 ]; then read -rsp "SSH password for robot PCs: " PASS; echo
  else echo "set H2_FIELD_PASSWORD (or run from a terminal)"; exit 1; fi
fi

echo "== 0/4 clock sync against this host (per machine) =="
for H in $PC3 $PC2; do
  T=$(date +%s.%N)
  sshpass -p "$PASS" ssh -o ConnectTimeout=5 unitree@"$H" \
    "echo "$PASS" | sudo -S date -s @$T >/dev/null 2>&1" \
    </dev/null >>"$LOG_DIR/h2-start.log" 2>&1 && echo "   $H synced"
done

echo "== 1/4 PC2 sensor agent =="
if curl -s --max-time 3 "http://$PC2:8090/environment" | grep -q '"available"'; then
  echo "   already running"
else
  sshpass -p "$PASS" ssh -o ConnectTimeout=5 unitree@"$PC2" \
    'PYTHONPATH=$HOME/pylibs nohup python3 ~/sensor_agent.py > ~/sensor_agent.log 2>&1 < /dev/null &' \
    </dev/null >>"$LOG_DIR/h2-start.log" 2>&1 \
    && echo "   started" || echo "   FAILED (PC2 offline?)"
fi

echo "== 2/4 PC3 gateway =="
if sshpass -p "$PASS" ssh -o ConnectTimeout=5 unitree@"$PC3" \
    'pgrep -f "[h]2_gateway.server" >/dev/null' 2>/dev/null; then
  echo "   already running"
else
  sshpass -p "$PASS" ssh unitree@"$PC3" \
    'cd ~/h2-inspection-platform && CYCLONEDDS_HOME=$PWD/.runtime/h2-sdk2/cyclonedds-install nohup .venv/bin/python -u -m h2_gateway.server --h2-interface eth10 --host 127.0.0.1 --port 50051 > gateway.log 2>&1 < /dev/null &' \
    </dev/null >>"$LOG_DIR/h2-start.log" 2>&1 \
    && echo "   started" || echo "   FAILED (PC3 offline?)"
  sleep 3
fi

echo "== 3/4 SSH forward 127.0.0.1:15051 -> PC3:50051 =="
if ss -tln | grep -q '127.0.0.1:15051'; then
  echo "   already listening"
else
  sshpass -p "$PASS" ssh -N -T -f -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=5 -o ServerAliveCountMax=2 \
    -L 127.0.0.1:15051:127.0.0.1:50051 unitree@"$PC3" \
    && echo "   forward up" || echo "   FAILED"
fi

echo "== 4/4 workstation stack (external gateway) =="
.venv/bin/python scripts/dev.py stop >/dev/null 2>&1
nohup .venv/bin/python scripts/dev.py start --external-gateway 127.0.0.1:15051 \
  >>"$LOG_DIR/dev-supervisor.log" 2>&1 &
echo "   starting (log: .runtime/logs/dev-supervisor.log)"
sleep 8
echo "== health =="
curl -s --max-time 3 http://127.0.0.1:8000/api/health || echo "(backend not ready yet)"
echo
echo "open http://127.0.0.1:5173"
