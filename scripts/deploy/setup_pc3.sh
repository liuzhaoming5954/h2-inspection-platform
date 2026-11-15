#!/usr/bin/env bash
# PC3 setup: verify/prepare the gateway runtime (Python, SDK2, CycloneDDS).
# Run ON PC3, from the repository checkout:
#   bash scripts/deploy/setup_pc3.sh
#
# The gateway process itself is started from the workstation (link panel /
# h2_field_start.sh); this script only prepares the environment.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SDK_DIR="$ROOT/.runtime/h2-sdk2"
SDK_PIN="814556d15970dd2ecf1c9984e845ca02ab07e206"

echo "== PC3 gateway runtime setup =="
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  echo "   creating .venv (python3.12+ required)"
  python3 -m venv "$ROOT/.venv"
fi
"$ROOT/.venv/bin/python" -m pip install -q -r "$ROOT/robot-gateway/requirements.txt" 2>/dev/null \
  || "$ROOT/.venv/bin/python" -m pip install -q grpcio "grpcio-tools" pyserial

echo "== unitree_sdk2_python (pinned $SDK_PIN) =="
if [ ! -d "$SDK_DIR/unitree_sdk2_python" ]; then
  mkdir -p "$SDK_DIR"
  git clone https://github.com/unitreerobotics/unitree_sdk2_python.git "$SDK_DIR/unitree_sdk2_python" \
    && git -C "$SDK_DIR/unitree_sdk2_python" checkout "$SDK_PIN" \
    && "$ROOT/.venv/bin/python" -m pip install -q -e "$SDK_DIR/unitree_sdk2_python"
else
  echo "   already present"
fi

echo "== CycloneDDS 0.10.2 (built without tracing; see H2_DESIGN.md) =="
if [ ! -x "$SDK_DIR/cyclonedds-install/bin/ddsls" ] && [ ! -d "$SDK_DIR/cyclonedds-install/lib" ]; then
  echo "   NOT FOUND. Build once (network + cmake required):"
  echo "     git clone -b 0.10.2 https://github.com/eclipse-cyclonedds/cyclonedds.git $SDK_DIR/cyclonedds"
  echo "     cmake -B $SDK_DIR/cyclonedds-build -S $SDK_DIR/cyclonedds -DCMAKE_INSTALL_PREFIX=$SDK_DIR/cyclonedds-install -DBUILD_DDSPERF=OFF -DCMAKE_DISABLE_FIND_PACKAGE_iceoryx_binding_c=TRUE"
  echo "     cmake --build $SDK_DIR/cyclonedds-build -j4 && cmake --install $SDK_DIR/cyclonedds-build"
  echo "   (机器人在 ROS 环境下必须带这两个开关，否则 DDS 初始化会 abort)"
else
  echo "   already present"
fi

echo "== verify =="
cd "$ROOT"
if CYCLONEDDS_HOME="$SDK_DIR/cyclonedds-install" .venv/bin/python -c "import h2_gateway" 2>/dev/null; then
  echo "   h2_gateway imports OK"
else
  echo "   WARNING: h2_gateway import failed - check venv deps above"
fi
echo "== done. The gateway is started from the workstation link panel =="
