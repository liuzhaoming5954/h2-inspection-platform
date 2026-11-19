#!/bin/bash
# H2 建图环境一键部署（在 PC3 上以 sudo 运行，无需外网）：
#   sudo bash pc3-setup.sh
# 前置：/home/unitree/h2-fastlio-offline/ 内含 pc3-debs/ 与 h2-mapping-image.tar.gz
# 作用：安装 Docker → 导入镜像 → 创建 ros-noetic-dev 容器
#（沿用既有脚本的容器约定，挂载 ~/fastlio2_ros1_g1 → /workspace，host 网络
#  + privileged 供雷达 UDP 使用）。以 root 运行，内部命令不加 sudo。
set -euo pipefail

OFFLINE="${1:-$HOME/h2-fastlio-offline}"
SDK_WS="$HOME/fastlio2_ros1_g1"

echo "== 1/3 安装 Docker（离线 deb）=="
if ! command -v docker >/dev/null; then
  dpkg -i "$OFFLINE"/pc3-debs/*.deb || apt-get install -f -y
fi
systemctl enable --now docker
docker --version

echo "== 2/3 导入建图镜像 =="
docker load -i "$OFFLINE"/h2-mapping-image.tar.gz

echo "== 3/3 创建容器 ros-noetic-dev（不自动启动）=="
if docker ps -a --format '{{.Names}}' | grep -qx ros-noetic-dev; then
  echo "容器已存在，跳过创建"
else
  docker run -d --name ros-noetic-dev --network host --privileged \
    -v "$SDK_WS":/workspace \
    h2-mapping:noetic-deps sleep infinity
fi

echo "== 完成 =="
echo "下一步：sudo bash pc3-first-build.sh   # 容器内首次编译（约 20-40 分钟）"
