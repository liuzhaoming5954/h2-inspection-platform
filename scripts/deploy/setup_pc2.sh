#!/usr/bin/env bash
# PC2 setup: install pyserial and the sensor agent from this repository.
# Run ON PC2, from the repository checkout:
#   bash scripts/deploy/setup_pc2.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PYLIBS="$HOME/pylibs"

echo "== PC2 sensor agent setup =="
mkdir -p "$PYLIBS"
if ! PYTHONPATH="$PYLIBS" python3 -c "import serial" 2>/dev/null; then
  echo "   installing pyserial via python3 -m pip"
  python3 -m pip install --target "$PYLIBS" pyserial
fi

echo "== install ~/sensor_agent.py from the repository copy =="
cp "$ROOT/scripts/sensor_agent_pc2.py" "$HOME/sensor_agent.py"
chmod +x "$HOME/sensor_agent.py"

echo "== probe serial port (informational) =="
ls -l /dev/ttyACM* 2>/dev/null || echo "   no ttyACM* present - check the RS-485 USB module on interface 10"

echo "== done. Start/stop the agent from the workstation link panel, or manually: =="
echo "   PYTHONPATH=$PYLIBS nohup python3 ~/sensor_agent.py > ~/sensor_agent.log 2>&1 < /dev/null &"
