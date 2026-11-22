# PC3 建图环境（FastLIO2 · ROS1 noetic 容器）

在 PC3（aarch64，Ubuntu 24.04 + 原生 ROS2）上用 noetic 容器运行 `~/fastlio2_ros1_g1`
的建图部分。只建图不导航；对既有源码零改动（`h2_extra/` 与容器为新增物）。

## 组成

| 文件 | 用途 |
| --- | --- |
| `Dockerfile` | `deps` 阶段 = noetic + PCL/tf2/eigen_conversions/GTSAM(源码编译，focal 无包)/Livox-SDK2；`verify` 阶段在工作站 QEMU 下全量验证依赖 |
| `build_ws.sh` | 容器内首次编译：清残留 toplevel、导航包 CATKIN_IGNORE、三段编译（消息头→驱动→建图节点，上游未挂消息依赖） |
| `h2_extra/h2_mapping_headless.launch` | 只启动雷达驱动 + map_builder（无 rviz/octomap） |
| `h2_map_bridge.py` | 建图数据桥：订阅 `/body_cloud`+`/slam_odom`+`/livox/imu`，世界系体素累计（与是否移动无关），HTTP :8097 提供 `/map /pose /path`；`/pose` 含里程计与 IMU 各自的采样年龄、`imu_valid` 有效标志和 `bridge_boot` 启动标识（2026-11-20 任务书第五节：位姿与 IMU 独立计龄，HTTP 轮询不刷新有效性） |
| `pc3-setup.sh` / `pc3-first-build.sh` | PC3 侧安装/首编脚本（用 `sudo bash` 运行） |

## 镜像构建（工作站，需外网与 docker/qemu-user-static）

```bash
mkdir -p build-context && cd build-context
git clone --depth 1 https://github.com/Livox-SDK/Livox-SDK2.git
git clone --depth 1 --branch 4.1.1 https://github.com/borglab/gtsam.git
# Dockerfile/build_ws.sh/h2_extra 从本目录复制；ws/ 放 fastlio2 与 livox 两包源码（验证用）
docker build --platform linux/arm64 -t h2-mapping:noetic-deps --target deps .
docker save h2-mapping:noetic-deps | gzip > h2-mapping-image.tar.gz
```

## PC3 部署（离线）

```bash
# 材料放 ~/h2-fastlio-offline/：h2-mapping-image.tar.gz、pc3-debs/（docker arm64 四件套）、h2_extra/
sudo bash pc3-setup.sh        # 装 docker → load 镜像 → 建 ros-noetic-dev 容器
sudo bash pc3-first-build.sh  # 复制 h2_extra、雷达配置改现场 IP、容器内首编（约 20-40 分钟）
```

## 现场 IP 与雷达型号注意

- 雷达配置 `MID360_config.json`：雷达块名必须是 **`"Mid360s"`**（本机雷达为 Mid360s，
  设备类型 35；写成经典 `"MID360"`(9) 时 SDK 会静默丢弃广播，不报任何错），
  `host_net_info` 五个 IP 全部指向 PC3 开发网地址（当前 `192.168.124.163`）
- 容器为 `--network host --privileged`，雷达发现依赖 UDP 广播 56000 端口，
  同机只允许一个驱动实例（多实例会互相卡死握手）

## 运行

```bash
# 建图栈（headless）
docker exec -d ros-noetic-dev bash -c \
  'source /opt/ros/noetic/setup.bash && source /workspace/G1Nav2D/devel/setup.bash \
   && roslaunch /workspace/h2_extra/h2_mapping_headless.launch > /tmp/mapping.log 2>&1'
# 数据桥
docker exec -d ros-noetic-dev bash -c \
  'source /opt/ros/noetic/setup.bash && source /workspace/G1Nav2D/devel/setup.bash \
   && python3 /workspace/h2_extra/h2_map_bridge.py > /tmp/bridge.log 2>&1'
```

上位机「地图显示」模块经 `/api/field/map/{map,pose,path}`（后端转发 PC3:8097）实时渲染。

## 排障速查

- 雷达数据不通、日志无报错 → 先查 `MID360_config.json` 块名与 IP；再查是否残留双驱动
  （`ss -ulnp | grep 56000` 应只有一个进程的两条记录）
- `libmetis-gtsam.so` 找不到 → 容器内 `ldconfig`（新版镜像已在安装后执行）
- 编译报缺 `CustomMsg.h`/`SaveMap.h` → 未按三段顺序，直接跑 `/opt/build_ws.sh`
- 地图停在关键帧不累计 → 用的是旧版桥；确认 `/workspace/h2_extra/h2_map_bridge.py` 为 v2
