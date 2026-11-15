#!/usr/bin/env bash
# One-command field startup for H2 hardware teleoperation.
#
# Brings up, in order: the PC2 RS-485 sensor agent, the PC3 gRPC gateway,
# the independent SSH local forwards for status (15051 -> 50051) and
# teleoperation (15052 -> 50052), then the workstation stack. The forwarded
# ports are bound to loopback on both sides. Running components are reused.
#
# SSH password is read from H2_FIELD_PASSWORD, or prompted once when run
# from a terminal. Never store robot passwords in this repository.
# Before upgrading, deploy the new PC2 script to ~/sensor_agent.py and stop
# the old PC2 and PC3 processes. See robot-gateway/H2_COMMISSIONING.md.
#
# Usage: scripts/h2_field_start.sh [--robot-only]
#   --robot-only: bring up PC2/PC3/forwards and stop there (no workstation
#   stack restart, no self-check) - used by dev.py's first-boot hook so the
#   stack start does not recurse.
set -u
ROBOT_ONLY=0
[ "${1:-}" = "--robot-only" ] && ROBOT_ONLY=1
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PC2="${H2_PC2_HOST:-192.168.124.162}"
PC3="${H2_PC3_HOST:-192.168.124.163}"
LOG_DIR="$ROOT/.runtime/logs"
mkdir -p "$LOG_DIR"

PASS="${H2_FIELD_PASSWORD:-}"
if [ -z "$PASS" ]; then
  if [ -t 0 ]; then read -rsp "SSH password for robot PCs: " PASS; echo
  else echo "set H2_FIELD_PASSWORD (or run from a terminal)"; exit 1; fi
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

echo "== 1.5/4 PC3 read-only FSM probe (checklist: switch H2 to 601 FIRST) =="
PROBE=$(sshpass -p "$PASS" ssh -o ConnectTimeout=5 unitree@"$PC3" \
  "cd ~/h2-inspection-platform && CYCLONEDDS_HOME=\$PWD/.runtime/h2-sdk2/cyclonedds-install timeout 20 .venv/bin/python -m h2_gateway.hardware --interface eth10 2>/dev/null" \
  </dev/null 2>/dev/null)
if echo "$PROBE" | grep -q '"teleop_ready": true'; then
  echo "   probe: $PROBE (601 standing, ready)"
else
  echo "   probe: ${PROBE:-unreachable} — H2 not in 601 yet."
  echo "   NOTE: the field checklist switches H2 to 601 BEFORE the gateway starts."
  echo "   Continuing (telemetry works regardless); teleop unlocks at 601."
fi

echo "== 2/4 PC3 gateway =="
if sshpass -p "$PASS" ssh -o ConnectTimeout=5 unitree@"$PC3" \
    'pgrep -f "[h]2_gateway.server" >/dev/null' 2>/dev/null; then
  echo "   already running"
else
  # nohup-over-ssh can hang the channel even with </dev/null, so bound it:
  # the backgrounded process still lands on PC3, the probe below decides.
  timeout 15 sshpass -p "$PASS" ssh unitree@"$PC3" \
    'cd ~/h2-inspection-platform && CYCLONEDDS_HOME=$PWD/.runtime/h2-sdk2/cyclonedds-install nohup .venv/bin/python -u -m h2_gateway.server --h2-interface eth10 --host 127.0.0.1 --port 50051 --teleop-port 50052 > gateway.log 2>&1 < /dev/null &' \
    </dev/null >>"$LOG_DIR/h2-start.log" 2>&1 \
    && echo "   started" || echo "   launch command timed out or failed - probing to confirm"
  sleep 3
fi

echo "== 3/4 SSH forwards (status and teleoperation) =="
previous_forward_pid=
for mapping in 15051:50051 15052:50052; do
  local_port=${mapping%:*}
  pc3_port=${mapping#*:}
  forward="127.0.0.1:$local_port:127.0.0.1:$pc3_port"
  listener=$(ss -Hltnp "sport = :$local_port" | awk -v addr="127.0.0.1:$local_port" '$4 == addr')
  if [ -z "$listener" ]; then
    timeout 15 sshpass -p "$PASS" ssh -N -T -f -S none \
      -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes \
      -o ServerAliveInterval=5 -o ServerAliveCountMax=2 \
      -L "$forward" unitree@"$PC3" || { echo "   forward $mapping failed (PC3 offline?)"; continue; }
    listener=$(ss -Hltnp "sport = :$local_port" | awk -v addr="127.0.0.1:$local_port" '$4 == addr')
  fi
  if [[ $listener =~ pid=([0-9]+), ]]; then
    forward_pid=${BASH_REMATCH[1]}
    forward_args=$(tr '\0' ' ' < "/proc/$forward_pid/cmdline") || exit 1
    if [[ $(readlink "/proc/$forward_pid/exe") == */ssh && $forward_args == *" -S none "* \
        && $forward_args == *" -L $forward "* && $forward_args == *" unitree@$PC3 "* \
        && $forward_pid != "$previous_forward_pid" ]]; then
      previous_forward_pid=$forward_pid
      echo "   $mapping forward up"
      continue
    fi
  fi
  echo "NOT READY: port $local_port must use its own SSH process for $mapping. Check the existing listener."
  exit 1
done

if [ "$ROBOT_ONLY" = 1 ]; then
  echo "== --robot-only: robot-side components up, workstation stack handled by caller =="
  exit 0
fi

echo "== 4/4 workstation stack (external gateway) =="
.venv/bin/python scripts/dev.py stop >>"$LOG_DIR/dev-supervisor.log" 2>&1 || exit 1
for _ in $(seq 1 20); do
  [ "$(.venv/bin/python scripts/dev.py status)" = stopped ] && break
  sleep 1
done
if [ "$(.venv/bin/python scripts/dev.py status)" != stopped ]; then
  echo "NOT READY: old workstation stack is still running. Check .runtime/logs/dev-supervisor.log."
  exit 1
fi
nohup .venv/bin/python scripts/dev.py start --external-gateway 127.0.0.1:15051 \
  --external-teleop-gateway 127.0.0.1:15052 \
  >>"$LOG_DIR/dev-supervisor.log" 2>&1 &
echo "   starting (log: .runtime/logs/dev-supervisor.log)"
for _ in $(seq 1 20); do
  sleep 1
  if curl --fail -s --max-time 2 http://127.0.0.1:8000/api/health >/dev/null 2>&1 \
      && curl --fail -s --max-time 2 http://127.0.0.1:5173/ >/dev/null 2>&1; then
    break
  fi
done
echo "== health =="
if ! curl --fail -s --max-time 3 http://127.0.0.1:8000/api/health \
    || ! curl --fail -s --max-time 3 http://127.0.0.1:5173/ >/dev/null; then
  echo "NOT READY: check the workstation service logs."
  exit 1
fi
echo

echo "== self-check: teleop lease, heartbeat, release (no motion) =="
if .venv/bin/python scripts/h2_field_check.py; then
  echo "READY: open http://127.0.0.1:5173 and start teleoperation."
else
  echo "NOT READY: fix the issues above before touching the robot."
  exit 1
fi
