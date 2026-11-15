#!/usr/bin/env bash
# 配置上位机，安装项目依赖并生成 ~/h2-start.sh 启动脚本。
# 在上位机的仓库根目录运行：
#   bash scripts/deploy/setup_workstation.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

echo "== workstation setup =="
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  python3 -m venv "$ROOT/.venv"
fi
"$ROOT/.venv/bin/python" -m pip install -q -r "$ROOT/backend/requirements.txt" 2>/dev/null \
  || "$ROOT/.venv/bin/python" -m pip install -q fastapi uvicorn websockets pydantic grpcio pyserial

echo "== frontend dependencies =="
cd "$ROOT/frontend"
command -v npm >/dev/null || { echo "ERROR: npm not found - install Node.js first"; exit 1; }
npm install

echo "== generate ~/h2-start.sh wrapper =="
cat > "$HOME/h2-start.sh" << 'WRAPPER'
#!/usr/bin/env bash
# 现场启动脚本。将机器人计算机的密码配置在此文件中，
# 或在调用前导出 H2_FIELD_PASSWORD。密码仅保存在各机本地。
export H2_FIELD_PASSWORD="${H2_FIELD_PASSWORD:-}"   # 可在此填写，或在外部导出该变量。
exec "$(dirname "$0")/h2-inspection-platform/scripts/h2_field_start.sh" "$@"
WRAPPER
# 使用仓库的实际绝对路径，以适配不同的检出位置。
sed -i "s|\$(dirname \"\$0\")/h2-inspection-platform|$ROOT|" "$HOME/h2-start.sh"
chmod +x "$HOME/h2-start.sh"

echo "== done =="
echo "Next steps:"
echo "  1) export H2_FIELD_PASSWORD='<robot PC password>'   (add to ~/.bashrc to persist)"
echo "  2) ~/h2-start.sh                                    (one-command bring-up)"
echo "  3) open http://127.0.0.1:5173"
