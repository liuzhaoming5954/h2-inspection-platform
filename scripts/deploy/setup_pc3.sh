#!/usr/bin/env bash
# 配置 PC3，准备并检查网关运行环境（Python、SDK2、CycloneDDS）。
# 在 PC3 的仓库根目录运行：
#   bash scripts/deploy/setup_pc3.sh
#
# 此脚本只准备运行环境。随后从上位机的链路管理面板或
# h2_field_start.sh 启动网关进程。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SDK_DIR="$ROOT/.runtime/h2-sdk2"
SDK_PIN="814556d15970dd2ecf1c9984e845ca02ab07e206"
DDS_VERSION="0.10.2"
DDS_SOURCE="$SDK_DIR/cyclonedds"
DDS_BUILD="$SDK_DIR/cyclonedds-build"
DDS_INSTALL="$SDK_DIR/cyclonedds-install"
SDK_SOURCE="$SDK_DIR/unitree_sdk2_python"
PYTHON="$ROOT/.venv/bin/python"

check_python() {
  "$1" -c 'import sys
if sys.version_info < (3, 12):
    sys.exit("ERROR: PC3 requires Python 3.12 or newer.")'
}

dds_installed() {
  [ -f "$DDS_INSTALL/lib/libddsc.so" ] \
    && [ -f "$DDS_INSTALL/lib/libddsc.so.$DDS_VERSION" ] \
    && [ -f "$DDS_INSTALL/include/dds/dds.h" ]
}

if ! command -v git >/dev/null; then
  echo "ERROR: install Git before setting up PC3." >&2
  exit 1
fi

echo "== PC3 gateway runtime setup =="
if [ ! -x "$PYTHON" ]; then
  python_command=$(command -v python3.12 || command -v python3) || {
    echo "ERROR: install Python 3.12 or newer with venv support." >&2
    exit 1
  }
  check_python "$python_command"
  "$python_command" -m venv "$ROOT/.venv"
fi
check_python "$PYTHON"
mkdir -p "$SDK_DIR"

echo "== CycloneDDS $DDS_VERSION =="
if ! dds_installed; then
  if ! command -v cmake >/dev/null; then
    echo "ERROR: install CMake and a C build toolchain before building CycloneDDS." >&2
    exit 1
  fi
  if [ ! -d "$DDS_SOURCE" ]; then
    git clone --depth 1 --branch "$DDS_VERSION" https://github.com/eclipse-cyclonedds/cyclonedds.git "$DDS_SOURCE"
  fi
  dds_head=$(git -C "$DDS_SOURCE" rev-parse HEAD)
  dds_tag=$(git -C "$DDS_SOURCE" rev-parse "refs/tags/$DDS_VERSION^{commit}")
  if [ "$dds_head" != "$dds_tag" ]; then
    echo "ERROR: $DDS_SOURCE must be at the CycloneDDS $DDS_VERSION tag." >&2
    exit 1
  fi
  # 关闭共享内存与 Iceoryx 自动发现，避免复用 ROS 环境的共享内存依赖。
  cmake -S "$DDS_SOURCE" -B "$DDS_BUILD" \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$DDS_INSTALL" -DCMAKE_INSTALL_LIBDIR=lib \
    -DBUILD_SHARED_LIBS=ON -DBUILD_TESTING=OFF -DBUILD_EXAMPLES=OFF -DBUILD_DDSPERF=OFF \
    -DENABLE_SHM=OFF -DCMAKE_DISABLE_FIND_PACKAGE_iceoryx_binding_c=TRUE
  cmake --build "$DDS_BUILD" --parallel 4
  cmake --install "$DDS_BUILD"
  if ! dds_installed; then
    echo "ERROR: CycloneDDS installation is incomplete at $DDS_INSTALL." >&2
    exit 1
  fi
fi

echo "== unitree_sdk2_python (pinned $SDK_PIN) =="
if [ ! -d "$SDK_SOURCE" ]; then
  git clone https://github.com/unitreerobotics/unitree_sdk2_python.git "$SDK_SOURCE"
  git -C "$SDK_SOURCE" checkout --detach "$SDK_PIN"
fi
if [ "$(git -C "$SDK_SOURCE" rev-parse HEAD)" != "$SDK_PIN" ]; then
  echo "ERROR: $SDK_SOURCE must be at the SDK commit $SDK_PIN." >&2
  exit 1
fi

echo "== install Python dependencies and gateway =="
export CYCLONEDDS_HOME="$DDS_INSTALL"
# 预编译 Python wheel 会优先加载内置 DDS 库，使用源码安装以采用上述构建配置。
dds_binding_matches() {
  "$PYTHON" - "$DDS_VERSION" 2>/dev/null <<'PY'
import os
from importlib.metadata import version
from pathlib import Path
import sys

from cyclonedds.__library__ import in_wheel, library_path

expected = Path(os.environ["CYCLONEDDS_HOME"]) / "lib/libddsc.so"
same_library = not in_wheel and Path(library_path).resolve() == expected.resolve()
sys.exit(not (same_library and version("cyclonedds") == sys.argv[1]))
PY
}
if ! dds_binding_matches; then
  "$PYTHON" -m pip install --no-binary=cyclonedds --no-cache-dir --force-reinstall "cyclonedds==$DDS_VERSION"
  if ! dds_binding_matches; then
    echo "ERROR: the CycloneDDS Python binding does not match $DDS_INSTALL." >&2
    exit 1
  fi
fi
"$PYTHON" -m pip install -e "$ROOT/robot-gateway" numpy opencv-python-headless
# SDK 通过 cv2 导入 OpenCV，使用上一步已安装的无界面版本。
"$PYTHON" -m pip install --no-deps "$SDK_SOURCE"

echo "== verify imports =="
# 导入模块即可检查 Python 依赖和原生 DDS 库，安装过程不初始化运控通道。
"$PYTHON" -c 'import cv2, cyclonedds, numpy
from h2_gateway import server
from unitree_sdk2py.h2.loco.h2_loco_client import LocoClient'
echo "== done. The gateway is started from the workstation link panel =="
