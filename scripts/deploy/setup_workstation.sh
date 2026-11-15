#!/usr/bin/env bash
# 配置上位机，安装项目依赖并生成 ~/h2-start.sh 启动脚本。
# 在上位机的仓库根目录运行：
#   bash scripts/deploy/setup_workstation.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"

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
  printf 'exec %q "$@"\n' "$ROOT/scripts/h2_field_start.sh"
} > "$wrapper"
chmod 755 "$wrapper"
mv -fT -- "$wrapper" "$HOME/h2-start.sh"

echo "== done =="
echo "Next steps:"
echo "  1) set the site configuration and SSH credentials in the current environment"
echo "  2) ~/h2-start.sh                                    (one-command bring-up)"
echo "  3) open http://127.0.0.1:5173"
