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

echo "== 0/4 clock: join the robot time domain (host follows PC3) =="
# PC2/PC3 run chrony against the robot-internal time source; manual sets on
# them are reverted within minutes. The host must therefore follow the robot
# clock. Measurement uses a shared SSH connection (ControlMaster) so the
# date query RTT is ~1 ms instead of the ~200 ms connect bias, and the host
# is placed 100 ms behind PC3: the centre of the command validity window.
CM_DIR="$LOG_DIR/ssh-cm"; mkdir -p "$CM_DIR"
SSH_CM=(-o ControlMaster=auto -o ControlPath="$CM_DIR/%r@%h-%p" -o ControlPersist=60)
sshpass -p "$PASS" ssh -o ConnectTimeout=5 "${SSH_CM[@]}" unitree@"$PC3" true \
  </dev/null >>"$LOG_DIR/h2-start.log" 2>&1

SYNC_RAW=$(sshpass -p "$PASS" ssh "${SSH_CM[@]}" unitree@"$PC3" \
  'chronyc tracking 2>/dev/null | grep "System time"' 2>/dev/null)
if [ -n "$SYNC_RAW" ]; then
  echo "   PC3 chrony: $SYNC_RAW"
  for _ in 1 2 3 4 5 6; do
    SYNC_VAL=$(echo "$SYNC_RAW" | grep -oE '[0-9]+\.[0-9]+ ' | head -1)
    OK=$(echo "$SYNC_VAL < 0.1" | bc 2>/dev/null)
    [ "$OK" = "1" ] && break
    echo "   time source still converging ($SYNC_VAL s), waiting 20s…"
    sleep 20
    SYNC_RAW=$(sshpass -p "$PASS" ssh "${SSH_CM[@]}" unitree@"$PC3" \
      'chronyc tracking 2>/dev/null | grep "System time"' 2>/dev/null)
  done
fi

T0=$(date +%s.%N)
P3=$(sshpass -p "$PASS" ssh "${SSH_CM[@]}" unitree@"$PC3" 'date +%s.%N' 2>/dev/null)
T1=$(date +%s.%N)
if [ -n "$P3" ]; then
  MID=$(echo "($T0+$T1)/2" | bc -l)
  NEW=$(echo "$T1 + ($P3 - $MID) - 0.1" | bc -l)
  echo "$PASS" | sudo -S timedatectl set-ntp false >/dev/null 2>&1
  echo "$PASS" | sudo -S date -s @"$NEW" >/dev/null 2>&1 \
    && echo "   host aligned 100ms behind PC3 (window centre)"
else
  echo "   WARN: PC3 unreachable, clock not aligned"
fi

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
for _ in $(seq 1 20); do
  sleep 1
  curl -s --max-time 2 http://127.0.0.1:8000/api/health >/dev/null 2>&1 && break
done
echo "== health =="
curl -s --max-time 3 http://127.0.0.1:8000/api/health || echo "(backend not ready yet)"
echo

echo "== self-check: teleop lease + heartbeat (no motion) =="
if .venv/bin/python scripts/h2_field_check.py; then
  echo "READY: open http://127.0.0.1:5173 and start teleoperation."
else
  echo "NOT READY: fix the issues above before touching the robot."
fi
