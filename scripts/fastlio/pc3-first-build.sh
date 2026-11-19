#!/bin/bash
# 容器内首次编译（PC3 原生 arm64，依赖已在镜像中，无需网络）。
# 以 sudo 运行：sudo bash pc3-first-build.sh
set -euo pipefail
SDK_WS="$HOME/fastlio2_ros1_g1"
OFFLINE="${1:-$HOME/h2-fastlio-offline}"

echo "== 1/3 复制 h2_extra（headless launch 与数据桥，不改动既有代码树）=="
mkdir -p "$SDK_WS"
cp -r "$OFFLINE"/h2_extra "$SDK_WS"/h2_extra

# 雷达配置指向当前现场：雷达 192.168.124.170（开发网交换机），主机 PC3 开发网地址。
# 注意块名必须是 "Mid360s"（设备类型 35），经典 "MID360" 会被 SDK 静默忽略。
LIDAR_CFG="$SDK_WS/G1Nav2D/src/livox_ros_driver2-master/config/MID360_config.json"
if [ -f "$LIDAR_CFG" ] && ! grep -q 192.168.124.170 "$LIDAR_CFG"; then
  cp "$LIDAR_CFG" "$LIDAR_CFG.h2bak"
  sed -i -e 's/192\.168\.123\.120/192.168.124.170/g' \
         -e 's/192\.168\.123\.164/192.168.124.163/g' "$LIDAR_CFG"
  echo "雷达配置已指向 192.168.124.170 / host 192.168.124.163（备份 .h2bak）"
fi

echo "== 2/3 容器内编译建图工作区 =="
docker exec ros-noetic-dev /opt/build_ws.sh

echo "== 3/3 验证 =="
docker exec ros-noetic-dev bash -c \
  'source /opt/ros/noetic/setup.bash && source /workspace/G1Nav2D/devel/setup.bash \
   && rospack find fastlio && rospack find livox_ros_driver2'
echo "完成。启动方式见 scripts/fastlio/README.md"
