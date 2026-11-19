# H2 建图环境镜像（linux/arm64，PC3 原生运行）
# deps 阶段：ROS1 noetic + FastLIO2/livox 驱动的全部编译依赖 + Livox-SDK2
# verify 阶段：在工作站以 QEMU 模拟完整编译一遍，验证依赖清单齐全；
#               正式导出给 PC3 的是 deps 阶段镜像（编译由 PC3 首启原生完成）。
FROM --platform=linux/arm64 ros:noetic-ros-base AS deps

# 容器内 apt 换清华 ports 镜像（focal arm64，国内直连不依赖代理）。
RUN sed -i -e 's|http://ports.ubuntu.com/ubuntu-ports|https://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports|g' \
            -e 's|http://archive.ubuntu.com/ubuntu|https://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports|g' \
            -e 's|http://security.ubuntu.com/ubuntu|https://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports|g' \
       /etc/apt/sources.list \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential cmake \
      ros-noetic-pcl-ros ros-noetic-pcl-conversions \
      ros-noetic-tf2 ros-noetic-tf2-ros ros-noetic-cv-bridge \
      ros-noetic-eigen-conversions \
      ros-noetic-message-generation ros-noetic-message-runtime \
      libeigen3-dev \
 && rm -rf /var/lib/apt/lists/*

# GTSAM：focal 无预编译包（groovy 起才收录），按原 G1 部署方式源码编译。
# 关掉测试/示例/不稳定模块，用系统 Eigen，压掉大部分无关编译量。
COPY gtsam /tmp/gtsam
RUN cd /tmp/gtsam && mkdir build && cd build \
 && cmake .. -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local \
    -DGTSAM_USE_SYSTEM_EIGEN=ON -DGTSAM_BUILD_TESTS=OFF \
    -DGTSAM_BUILD_EXAMPLES_ALWAYS=OFF -DGTSAM_BUILD_UNSTABLE=OFF \
    -DGTSAM_WITH_TBB=OFF \
 && cmake --build . --parallel 4 && cmake --install . \
 && ldconfig \
 && rm -rf /tmp/gtsam

# Livox-SDK2 装入 /usr/local（livox_ros_driver2 的 CMake 按 find_library 查找）
COPY Livox-SDK2 /tmp/Livox-SDK2
RUN cd /tmp/Livox-SDK2 && mkdir build && cd build \
 && cmake .. -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local \
 && cmake --build . --parallel 4 && cmake --install . \
 && ldconfig \
 && rm -rf /tmp/Livox-SDK2

# 首启编译脚本（挂载 /workspace 后执行一次）
COPY build_ws.sh /opt/build_ws.sh
RUN chmod +x /opt/build_ws.sh

# ---- 验证阶段：完整编译两包，证明依赖齐全（不随镜像导出） ----
FROM deps AS verify
COPY ws /workspace/G1Nav2D
RUN ln -s /opt/ros/noetic/share/catkin/cmake/toplevel.cmake /workspace/G1Nav2D/src/CMakeLists.txt
RUN /opt/build_ws.sh
