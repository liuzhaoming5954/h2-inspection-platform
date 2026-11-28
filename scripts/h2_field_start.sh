#!/usr/bin/env bash
# 一键启动 H2 真机遥控的现场链路。
#
# 依次启动 PC2 的 RS-485 传感器代理、PC3 的 gRPC 网关，
# 建立状态（15051 -> 50051）和遥控（15052 -> 50052）各自独立的 SSH 本地转发，
# 最后启动上位机服务。转发两端的端口均绑定回环地址。
# 已在运行的组件继续复用。
#
# 从 H2_FIELD_PASSWORD 读取 SSH 密码，终端交互运行时也可提示输入一次。
# 两种来源的密码都会导出给上位机后端（链路恢复按钮与巡检准备要靠它远程操作）。
# 机器人密码应保存在现场机器上。
# 升级前，先将新版 PC2 脚本部署到 ~/sensor_agent.py，并停止 PC2 和 PC3 的旧进程。
# 详见 robot-gateway/H2_COMMISSIONING.md。
#
# 用法：scripts/h2_field_start.sh [--robot-only]
#   --robot-only：只启动 PC2、PC3 和转发，跳过上位机重启和自检。
set -u
ROBOT_ONLY=0
[ "${1:-}" = "--robot-only" ] && ROBOT_ONLY=1
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PC2="${H2_PC2_HOST:-192.168.124.162}"
PC2_BIND="${H2_PC2_BIND:-$PC2}"
PC2_AGENT_URL="http://$PC2:8090/environment"
PC3_SENSOR_AGENT_URL="${H2_SENSOR_AGENT_URL:-$PC2_AGENT_URL}"
PC3="${H2_PC3_HOST:-192.168.124.163}"
LOG_DIR="$ROOT/.runtime/logs"
mkdir -p "$LOG_DIR"

PASS="${H2_FIELD_PASSWORD:-}"
if [ -z "$PASS" ]; then
  if [ -t 0 ]; then read -rsp "SSH password for robot PCs: " PASS; echo
  else echo "set H2_FIELD_PASSWORD (or run from a terminal)"; exit 1; fi
fi

# SSH 由远端 shell 解释命令，配置值必须作为单个参数传递。
quote_for_ssh() {
  printf "'%s'" "${1//\'/\'\\\'\'}"
}

echo "== 1/4 PC2 sensor agent =="
if curl -s --max-time 3 "$PC2_AGENT_URL" | grep -q '"available"'; then
  echo "   already running"
else
  sshpass -p "$PASS" ssh -o ConnectTimeout=5 unitree@"$PC2" \
    "H2_PC2_BIND=$(quote_for_ssh "$PC2_BIND") PYTHONPATH=\$HOME/pylibs nohup python3 ~/sensor_agent.py > ~/sensor_agent.log 2>&1 < /dev/null &" \
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
  # 即使将标准输入重定向到 /dev/null，通过 SSH 运行 nohup 也可能挂起通道，因此设置超时。
  # 超时后 PC3 上的后台进程仍可能已启动，由后续探测确认。
  # H2_V_*：2026-11-20 实测——前进阈值在 0.4–0.5（0.4 不抬脚，取 0.5）；转向阈值恰在
  # 0.55 附近（手动按住偶尔只扭腰不迈步），取 1.0 rad/s 留余量（官方例程 ω=1.0–2.0）。
  # 与 field.py 的 PC3_START 模板保持同步。
  timeout 15 sshpass -p "$PASS" ssh unitree@"$PC3" \
    "cd ~/h2-inspection-platform && CYCLONEDDS_HOME=\$PWD/.runtime/h2-sdk2/cyclonedds-install H2_SENSOR_AGENT_URL=$(quote_for_ssh "$PC3_SENSOR_AGENT_URL") H2_V_FORWARD_SLOW=0.5 H2_V_BACKWARD_SLOW=0.5 H2_V_TURN=0.7 H2_V_TURN_SLOW=0.7 nohup .venv/bin/python -u -m h2_gateway.server --h2-interface eth10 --host 127.0.0.1 --port 50051 --teleop-port 50052 > gateway.log 2>&1 < /dev/null &" \
    </dev/null >>"$LOG_DIR/h2-start.log" 2>&1 \
    && echo "   started" || echo "   launch command timed out or failed - probing to confirm"
  sleep 3
fi

echo "== 3/4 SSH forwards (status and teleoperation) =="
previous_forward_pid=
mappings=(15051:50051 15052:50052)
for mapping in "${mappings[@]}"; do
  local_port=${mapping%:*}
  pc3_port=${mapping#*:}
  forward="127.0.0.1:$local_port:127.0.0.1:$pc3_port"
  listener=$(ss -Hltnp "sport = :$local_port" | awk -v addr="127.0.0.1:$local_port" '$4 == addr')
  if [ -z "$listener" ]; then
    if ! timeout 15 sshpass -p "$PASS" ssh -N -T -f -S none \
      -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes \
      -o ServerAliveInterval=5 -o ServerAliveCountMax=2 \
      -L "$forward" unitree@"$PC3"; then
      echo "NOT READY: SSH forward $mapping failed. Existing workstation services were left unchanged." >&2
      exit 1
    fi
    listener=$(ss -Hltnp "sport = :$local_port" | awk -v addr="127.0.0.1:$local_port" '$4 == addr')
  fi
  if [[ $listener =~ pid=([0-9]+), ]]; then
    forward_pid=${BASH_REMATCH[1]}
    if forward_args=$(tr '\0' ' ' 2>/dev/null < "/proc/$forward_pid/cmdline") \
      && [[ $(readlink "/proc/$forward_pid/exe") == */ssh && $forward_args == *" -S none "* \
        && $forward_args == *" -L $forward "* && $forward_args == *" unitree@$PC3 "* \
        && $forward_pid != "$previous_forward_pid" ]]; then
      previous_forward_pid=$forward_pid
      echo "   $mapping forward up"
      continue
    fi
  fi
  echo "NOT READY: port $local_port must use its own SSH process for $mapping. Check the existing listener." >&2
  exit 1
done

# 本地端口监听成功后，再确认两条转发都能收到 PC3 网关的只读应答。
if ! .venv/bin/python - "${mappings[@]}" <<'PY'
import sys

from h2_gateway.client import GatewayClient

for mapping in sys.argv[1:]:
    local_port = mapping.split(":", 1)[0]
    try:
        with GatewayClient(f"127.0.0.1:{local_port}", timeout=3) as gateway:
            if gateway.get_state().get("simulated") is not False:
                raise ValueError("expected an H2 hardware gateway")
    except Exception as error:
        sys.exit(f"NOT READY: gRPC GetState failed through SSH forward {mapping}: {error}")
PY
then
  echo "NOT READY: SSH forward checks failed. Existing workstation services were left unchanged." >&2
  exit 1
fi

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
# 上位机后端靠 H2_FIELD_PASSWORD 执行现场远程操作（链路恢复按钮、巡检准备）。
# 密码来自终端输入时 PASS 只是普通 shell 变量，后端继承不到，会出现链路正常
# 但「准备」报「H2_FIELD_PASSWORD 未设置」。统一导出，两种来源行为一致。
export H2_FIELD_PASSWORD="$PASS"
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
READY=0
if .venv/bin/python scripts/h2_field_check.py; then
  echo "READY: hardware ready, teleoperation unlocked."
else
  # 自检不过多半是运控还没就绪（未进入 601），上位机与链路本身已经可用，
  # 页面照常打开；机器人就绪后刷新页面或重新运行本脚本即可。
  echo "NOT READY: fix the issues above before touching the robot."
  READY=1
fi

# 无论自检是否通过，最后都单独一行给出页面地址：
# 上位机已在本机 127.0.0.1 上监听，机器人就绪后刷新页面即可继续。
echo
echo "上位机页面：请在本机浏览器打开 http://127.0.0.1:5173   （接口文档 http://127.0.0.1:8000/docs）"
exit "$READY"
