# H2 真机遥控现场联调

上位机运行在 x86_64 Ubuntu 24.04。指令从浏览器经 WebSocket、FastAPI 和本机 gRPC 到达机器人网关，由宇树 SDK2 通过 CycloneDDS 调用 H2 自带的高层运控服务。

首次联调使用一台 H2 独占的有线网段。SDK2 按网卡和 DDS 域发现 `sport` 服务，同一网段内的多台 H2 无法由当前界面区分。上位机程序和浏览器都在现场 Ubuntu 主机运行。

## 1. 准备上位机

先按[项目 README](../README.md)安装 Python、前端依赖。真机遥控用独立的本地 SQLite 数据库，可跳过 PostgreSQL 的下载和启动。

在 Ubuntu 安装构建工具 `build-essential`、`cmake`、`git` 和 `python3.12-dev`。从项目根目录运行以下命令，SDK 和 CycloneDDS 安装在项目环境中。

```bash
h2_sdk_root="$PWD/.runtime/h2-sdk2"
mkdir -p "$h2_sdk_root"
git clone --depth 1 --branch 0.10.2 https://github.com/eclipse-cyclonedds/cyclonedds.git "$h2_sdk_root/cyclonedds"
git clone https://github.com/unitreerobotics/unitree_sdk2_python.git "$h2_sdk_root/unitree_sdk2_python"
git -C "$h2_sdk_root/unitree_sdk2_python" checkout 814556d15970dd2ecf1c9984e845ca02ab07e206
cmake -S "$h2_sdk_root/cyclonedds" -B "$h2_sdk_root/cyclonedds-build" \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$h2_sdk_root/cyclonedds-install" \
  -DBUILD_TESTING=OFF -DBUILD_EXAMPLES=OFF
cmake --build "$h2_sdk_root/cyclonedds-build" --parallel 4
cmake --install "$h2_sdk_root/cyclonedds-build"
export CYCLONEDDS_HOME="$h2_sdk_root/cyclonedds-install"
.venv/bin/python -m pip install 'cyclonedds==0.10.2'
.venv/bin/python -m pip install --no-deps "$h2_sdk_root/unitree_sdk2_python"
```

项目环境已安装 NumPy 和 `opencv-python-headless`，使用 `--no-deps` 可以避免宇树 SDK 再安装另一个提供 `cv2` 的包。重新打开终端后，从仓库根目录设置 `export CYCLONEDDS_HOME="$PWD/.runtime/h2-sdk2/cyclonedds-install"`。

## 2. 查询状态并启动

1. 按宇树手册和现场配置接好专用网线，确认 H2 和 Ubuntu 有线口的地址配置。用 `ip -br link` 和 `ip -br addr` 确定实际网卡。记录 H2 固件和运控版本。
2. 由现场工程师按宇树流程将 H2 置于 **601 主运控站立模式**。先清空测试区域，准备防跌倒措施并确认原厂急停和控制权交接。避免在两个控制端同时发送运动指令。
3. 先查询只读状态。下面的 `enp2s0` 是示例，请替换为连接该 H2 的实际网卡。成功且就绪时，输出 `fsm_id: 601`、`fsm_mode: 0` 和 `teleop_ready: true`。站立准备状态 4 与其他 FSM 会显示未就绪。

```bash
.venv/bin/python -m h2_gateway.hardware --interface enp2s0
```

4. 停止已运行的模拟开发服务后启动真机模式，在同一台 Ubuntu 主机打开[上位机](http://127.0.0.1:5173)。页面显示「H2 真机遥控」，FSM 就绪后才允许申请控制权。

```bash
.venv/bin/python scripts/dev.py stop
.venv/bin/python scripts/dev.py start --h2-interface enp2s0
```

网关、API 和前端监听回环地址。现场可查看 `.runtime/logs/gateway.log`，以及[机器人状态接口](http://127.0.0.1:8000/api/robot)。停步回执表示 H2 高层运控服务已应答零速度请求，实际停稳由现场观察。若返回「停步未确认」，先用现场原厂控制手段确认安全，再检查链路和日志，确认已停稳后重新启动上位机。

## 3. 现场验收

每次只按住一个方向，确认动作后松开并观察实际停稳。初始请求限幅为前后 0.15 m/s 和左右 0.25 rad/s，非零速度单帧有效期最多 250 ms。固件实际动作、速度和停步时间由现场记录。

| 项目 | 操作与记录 |
| --- | --- |
| 方向 | 依次短按前进、后退、左转、右转，记录实际方向和 SDK 回执 |
| 松手与主动停步 | 分别松开方向键和点击「停步」，记录回执、实际停稳时间及距离 |
| 释放控制 | 释放后方向按钮禁用，重新申请后需要重新按键 |
| 会话中断 | 在原厂急停和防跌倒措施就绪后，按现场方案分别测试浏览器断开、网关退出及网线断开，记录实际停稳。重新连接后确认动作未自动恢复 |

首轮在受控场地逐项确认。若零速度后仍原地踏步，按厂商手册检查是否处于持续踏步模式，并记录现场结果。

## 不接机器人时复现

安装 SDK 后执行下面的测试。测试用独立的回环 DDS 域和仿真的 H2 高层服务，验证 WebSocket、真实 gRPC、真实 SDK/DDS 到速度应答的链路。

```bash
H2_TEST_SDK_DDS=1 .venv/bin/python -m pytest -q tests/test_h2_sdk_dds.py
```

官方资料：[关于 H2](https://support.unitree.com/home/zh/H2_developer/About_H2)、[高层运动开发](https://support.unitree.com/home/zh/H2_developer/high_motion_development)、[高层运动服务接口](https://support.unitree.com/home/zh/H2_developer/sport_services_interface)、[SDK2 Python](https://github.com/unitreerobotics/unitree_sdk2_python)。
