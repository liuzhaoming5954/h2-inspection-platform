# H2 真机遥控现场联调

浏览器、前端和 FastAPI 运行在 x86_64 Ubuntu 上位机。上位机通过 SSH 本地转发访问 PC3 上的 gRPC 网关，PC3 再用宇树 SDK2 和 CycloneDDS 调用 H2 高层运控服务。上位机以 Ubuntu 24.04 为项目基线。

上位机接 H2 的外部开发网络。PC3 使用运控侧网卡发现 `sport` 服务。现场先确认 PC3 的内外网口、SSH 登录地址和 H2 固件及运控版本。

## 1. 准备 PC3

确认 PC3 的系统、架构和 Python 版本。网关要求 Python 3.12 或更高版本。以下示例使用 Python 3.12，PC3 只需网关和 SDK 依赖。准备与 PC3 系统匹配的 C 编译器、CMake、Git 和所用 Python 的开发头文件，然后在 PC3 的仓库根目录执行。

```bash
python3.12 -m venv .venv
h2_sdk_root="$PWD/.runtime/h2-sdk2"
mkdir -p "$h2_sdk_root"
git clone --depth 1 --branch 0.10.2 https://github.com/eclipse-cyclonedds/cyclonedds.git "$h2_sdk_root/cyclonedds"
git clone https://github.com/unitreerobotics/unitree_sdk2_python.git "$h2_sdk_root/unitree_sdk2_python"
git -C "$h2_sdk_root/unitree_sdk2_python" checkout 814556d15970dd2ecf1c9984e845ca02ab07e206
cmake -S "$h2_sdk_root/cyclonedds" -B "$h2_sdk_root/cyclonedds-build" \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$h2_sdk_root/cyclonedds-install" \
  -DBUILD_TESTING=OFF -DBUILD_EXAMPLES=OFF -DBUILD_DDSPERF=OFF \
  -DCMAKE_DISABLE_FIND_PACKAGE_iceoryx_binding_c=TRUE
cmake --build "$h2_sdk_root/cyclonedds-build" --parallel 4
cmake --install "$h2_sdk_root/cyclonedds-build"
export CYCLONEDDS_HOME="$h2_sdk_root/cyclonedds-install"
.venv/bin/python -m pip install -e ./robot-gateway 'cyclonedds==0.10.2' numpy opencv-python-headless
.venv/bin/python -m pip install --no-deps "$h2_sdk_root/unitree_sdk2_python"
```

两个构建参数关闭 `ddsperf` 和 Iceoryx 自动发现，避免有 ROS 2 的机器从系统环境引入共享内存依赖。如果已有 CycloneDDS 构建缓存，用全新的构建目录和安装目录重新配置，并把 `CYCLONEDDS_HOME` 指向新的安装目录。重新打开终端后也要重新设置这个变量。

`opencv-python-headless` 提供 SDK 所需的 `cv2`。给 SDK 使用 `--no-deps` 可沿用已安装的 NumPy、CycloneDDS 和 `cv2`。

## 2. 在 PC3 只读探测并启动网关

1. 在 PC3 用 `ip -br link` 和 `ip -br addr` 核对接入运控网络的网卡。下面的 `eth10` 是示例，请替换成现场确认的网卡名。
2. 清空测试区域，准备防跌倒措施，确认原厂急停和控制权交接。由现场工程师按宇树流程将 H2 置于 **601 主运控站立模式**。
3. 在 PC3 执行只读探测。它先调用 `GetFsmId`，读到主运控 ID 后再查询 `GetFsmMode`。就绪时输出 `fsm_id: 601`、`fsm_mode: 0` 和 `teleop_ready: true`。如果出现 `3102`，先检查 PC3 所选网卡的链路与 DDS 发现。若返回其他 FSM 或状态未就绪，先确认现场运控状态。

```bash
.venv/bin/python -m h2_gateway.hardware --interface eth10
```

4. 只读探测就绪后，在 PC3 启动网关。保留前台终端输出以便回看，gRPC 监听 PC3 的回环地址。

```bash
.venv/bin/python -u -m h2_gateway.server --h2-interface eth10 --host 127.0.0.1 --port 50051
```

## 3. 在 x86 上位机建立转发并启动应用

在上位机按项目 README 安装 Python 和前端依赖。外置网关模式使用独立的本地 SQLite 数据库，可跳过 PostgreSQL 的下载和启动。先确认上位机与 PC3 的系统时钟已同步，网关会用 PC3 的时间判断遥控指令是否过期。[项目 README](../README.md)

1. 用现场分配的 PC3 SSH 账号建立本地转发。把 `pc3-user@pc3-host` 替换为实际账号和开发口地址，并通过可信渠道核对并登记 PC3 主机密钥。以下命令在前台运行，把上位机的 `127.0.0.1:15051` 转发至 PC3 的 `127.0.0.1:50051`。[OpenSSH 本地转发说明](https://man.openbsd.org/ssh.1)

```bash
ssh -N -T -S none -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=5 -o ServerAliveCountMax=2 \
  -L 127.0.0.1:15051:127.0.0.1:50051 pc3-user@pc3-host
```

2. 在上位机另开终端，停止原开发服务后以外置网关模式启动。此模式启动前端和后端，由 PC3 运行机器人网关。

```bash
.venv/bin/python scripts/dev.py stop
.venv/bin/python scripts/dev.py start --external-gateway 127.0.0.1:15051
```

3. 在同一台上位机打开页面，确认显示「H2 真机遥控」，并检查机器人状态接口返回 `simulated: false` 和 `teleop_ready: true` 后再申请控制权。日志分别来自 PC3 的网关终端、上位机的 SSH 终端和上位机的 `.runtime/logs/api.log`。[上位机页面](http://127.0.0.1:5173) · [机器人状态接口](http://127.0.0.1:8000/api/robot)

## 4. 现场验收

每次只按住一个方向，松开并观察实际停稳后再进行下一项。初始请求限幅为前后 0.15 m/s 和左右 0.25 rad/s，非零速度单帧有效期最多 250 ms。停步回执表示 H2 高层运控服务已应答零速度请求，停稳时间和距离由现场实测。若显示「停步未确认」，先用原厂控制手段处置，确认已停稳后检查链路和 PC3 网关日志。若网关保持故障状态，排除原因后在 PC3 重新做只读探测，再重启网关。

| 项目 | 操作与记录 |
| --- | --- |
| 方向 | 依次短按前进、后退、左转、右转，记录实际方向和 SDK 回执 |
| 松手与主动停步 | 分别松开方向键和点击「停步」，记录回执、实际停稳时间及距离 |
| 释放控制 | 释放后方向按钮禁用，重新申请后需要重新按键 |

两段断链分别测试。每项开始前确认原厂急停和防跌倒措施可用，并按现场批准的测试条件执行。

1. **上位机到 PC3。** 保持 PC3 网关和运控侧通信正常，从上位机结束前台 SSH 转发。记录页面连接状态、PC3 网关日志、实际停稳时间和距离。恢复转发后确认旧会话失效且运动没有自动恢复。再次测试需重新申请控制权。
2. **PC3 到 H2 运控。** 保持上位机 SSH 转发和 PC3 网关运行，只用宇树批准的方法中断 PC3 SDK 与运控服务之间的 DDS 通信，保持本体安全与急停链路完整。记录运控状态丢失、停步请求或超时日志，以及机器人实际停稳时间和距离。恢复后先核对只读状态并确认旧会话失效，再申请新会话。现场尚无批准方法时，记录这一项待厂商支持。

反馈请附 H2 固件与运控版本、上位机和 PC3 的系统与 Python 版本、两端代码提交、各项实测时间和结果。异常附发生时间与两端日志。若零速度后仍原地踏步，按厂商手册检查是否处于持续踏步模式，并记录现场结果。

## 不接机器人时复现

安装 SDK 后执行下面的测试。测试用独立的回环 DDS 域和仿真的 H2 高层服务，验证 WebSocket、真实 gRPC、真实 SDK/DDS 到速度应答的链路。

```bash
H2_TEST_SDK_DDS=1 .venv/bin/python -m pytest -q tests/test_h2_sdk_dds.py
```

[关于 H2](https://support.unitree.com/home/zh/H2_developer/About_H2) · [高层运动开发](https://support.unitree.com/home/zh/H2_developer/high_motion_development) · [高层运动服务接口](https://support.unitree.com/home/zh/H2_developer/sport_services_interface) · [SDK2 Python](https://github.com/unitreerobotics/unitree_sdk2_python) · [CycloneDDS 0.10.2 构建配置](https://github.com/eclipse-cyclonedds/cyclonedds/blob/0.10.2/src/CMakeLists.txt#L38)
