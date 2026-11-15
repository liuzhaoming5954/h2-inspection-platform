#!/usr/bin/env bash
# Workstation setup: project dependencies plus the ~/h2-start.sh wrapper.
# Run ON THE WORKSTATION, from the repository checkout:
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
# Field bring-up wrapper. Set your robot PC password here or export
# H2_FIELD_PASSWORD before calling; it is never stored in the repository.
export H2_FIELD_PASSWORD="${H2_FIELD_PASSWORD:-}"   # e.g. '...' - fill or export outside
exec "$(dirname "$0")/h2-inspection-platform/scripts/h2_field_start.sh" "$@"
WRAPPER
# Resolve the repository path robustly (this checkout may live anywhere).
sed -i "s|\$(dirname \"\$0\")/h2-inspection-platform|$ROOT|" "$HOME/h2-start.sh"
chmod +x "$HOME/h2-start.sh"

echo "== done =="
echo "Next steps:"
echo "  1) export H2_FIELD_PASSWORD='<robot PC password>'   (add to ~/.bashrc to persist)"
echo "  2) ~/h2-start.sh                                    (one-command bring-up)"
echo "  3) open http://127.0.0.1:5173"
