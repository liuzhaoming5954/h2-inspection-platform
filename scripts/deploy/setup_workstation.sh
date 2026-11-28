#!/usr/bin/env bash
# 配置上位机，安装项目依赖并生成 ~/h2-start.sh 启动脚本。
# 在上位机的仓库根目录运行：
#   bash scripts/deploy/setup_workstation.sh
# 现场专机可免密一键启动，并固定机器人侧路由：
#   bash scripts/deploy/setup_workstation.sh --robot-password <PC2/PC3 的 SSH 密码> \
#        --robot-link <面向机器人的网口>
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"

# 一键免密启动的可选项。密码只写进本机的 ~/h2-start.sh，不进仓库。
ROBOT_PASSWORD=""
ROBOT_LINK=""
ROBOT_LINK_ADDRESS=""
while [ $# -gt 0 ]; do
  case "$1" in
    # 带值选项：值缺失时报错，避免把下一个参数或空串当成密码
    --robot-password|--robot-link)
      [ $# -ge 2 ] && [ -n "${2:-}" ] || { echo "ERROR: $1 needs a value" >&2; exit 2; }
      case "$1" in --robot-password) ROBOT_PASSWORD="$2" ;; --robot-link) ROBOT_LINK="$2" ;; esac
      shift 2 ;;
    --robot-password=*) ROBOT_PASSWORD="${1#*=}"; shift ;;
    --robot-link=*) ROBOT_LINK="${1#*=}"; shift ;;
    *) echo "ERROR: unknown option: $1" >&2; exit 2 ;;
  esac
done
if [ -n "$ROBOT_PASSWORD" ]; then
  : "${H2_PC2_HOST:=192.168.124.162}" "${H2_PC3_HOST:=192.168.124.163}"
  if [ -z "$ROBOT_LINK" ]; then
    echo "ERROR: --robot-password needs --robot-link <interface> (the port facing the robot)." >&2
    exit 2
  fi
  if ! ip link show "$ROBOT_LINK" >/dev/null 2>&1; then
    echo "ERROR: interface $ROBOT_LINK does not exist on this machine." >&2
    exit 2
  fi
  ROBOT_LINK_ADDRESS=$(ip -4 -o addr show dev "$ROBOT_LINK" scope global | awk '{print $4}' | cut -d/ -f1 | head -1)
  if [ -z "$ROBOT_LINK_ADDRESS" ]; then
    echo "ERROR: interface $ROBOT_LINK has no IPv4 address." >&2
    exit 2
  fi
fi

echo "== workstation setup =="
if ! command -v node >/dev/null || ! node -e '
  const version = process.versions.node
  const [major, minor] = version.split(".").map(Number)
  process.exit(major === 24 && minor >= 12 && !version.includes("-") ? 0 : 1)
'; then
  echo "ERROR: Node.js 24.12 or newer within 24.x is required." >&2
  exit 1
fi
if ! command -v pnpm >/dev/null; then
  echo "ERROR: pnpm is required to install the locked frontend dependencies." >&2
  exit 1
fi
if [ ! -x "$PYTHON" ]; then
  if ! command -v python3.12 >/dev/null; then
    echo "ERROR: Python 3.12 with venv support is required." >&2
    exit 1
  fi
  python3.12 -m venv "$ROOT/.venv"
fi
if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))'; then
  echo "ERROR: .venv must use Python 3.12. Recreate it with python3.12 -m venv .venv." >&2
  exit 1
fi

echo "== Python dependencies and project packages =="
"$PYTHON" -m pip install -r "$ROOT/requirements-validation.lock.txt"
"$PYTHON" -m pip install --no-deps --no-build-isolation -e "$ROOT/robot-gateway" -e "$ROOT/backend"
"$PYTHON" -m pip check
"$PYTHON" -c 'import h2_backend.app, h2_gateway.server, psycopg, uvicorn, websockets'

echo "== frontend dependencies =="
pnpm --dir "$ROOT/frontend" install --frozen-lockfile

echo "== generate ~/h2-start.sh wrapper =="
wrapper=$(mktemp "$HOME/.h2-start.sh.XXXXXX")
trap 'rm -f -- "$wrapper"' EXIT
{
  printf '%s\n' '#!/usr/bin/env bash' '# 使用当前进程继承的现场配置启动链路。'
  if [ -n "$ROBOT_PASSWORD" ]; then
    # 这台机器横在本机局域网与机器人之间，两个网口同在 192.168.124.0/24：
    # 内核按 metric 选路，发往机器人的包会走错网口。这里每次启动前用主机路由
    # 把 PC2/PC3 钉到面向机器人的网口上（重启后路由表会清空，所以放在这里）。
    # 添加路由需要 root；失败必须中止，否则启动会在连 PC2 时才失败且看不出原因。
    # 免密执行依赖 install-robot-route-sudoers.sh 写入的 sudoers 规则。
    printf '%s\n' '# 现场专机：内置机器人 PC 的 SSH 密码，并把发往机器人的流量固定到指定网口。'
    printf 'export H2_FIELD_PASSWORD=%q\n' "$ROBOT_PASSWORD"
    printf '%s\n' 'for host in "${H2_PC2_HOST:-192.168.124.162}" "${H2_PC3_HOST:-192.168.124.163}"; do'
    printf '\tsudo -n ip route replace "$host/32" dev %q src %q || {\n' "$ROBOT_LINK" "$ROBOT_LINK_ADDRESS"
    printf '\t\techo "无法把 $host 指向网口 %s（需要 sudo 免密执行 ip route，见 scripts/deploy/README.md）" >&2\n' "$ROBOT_LINK"
    printf '\t\texit 1\n\t}\n'
    printf '%s\n' 'done'
  fi
  printf 'exec %q "$@"\n' "$ROOT/scripts/h2_field_start.sh"
} > "$wrapper"
chmod 755 "$wrapper"
mv -fT -- "$wrapper" "$HOME/h2-start.sh"
[ -z "$ROBOT_PASSWORD" ] || chmod 700 "$HOME/h2-start.sh"   # 含密码，仅本人可读

echo "== done =="
echo "Next steps:"
echo "  1) set the site configuration and SSH credentials in the current environment"
echo "  2) ~/h2-start.sh                                    (one-command bring-up)"
echo "  3) open http://127.0.0.1:5173"
