# H2 真机遥控现场联调

浏览器、前端和 FastAPI 运行在 x86_64 Ubuntu 上位机。上位机用两条 SSH 连接分别转发状态查询和遥控指令，PC3 网关使用独立的 gRPC 监听端口处理两类请求。PC3 通过宇树 SDK2 和 CycloneDDS 调用 H2 高层运控服务。上位机以 Ubuntu 24.04 为项目基线。

上位机接 H2 的外部开发网络。PC3 使用运控侧网卡发现 `sport` 服务。现场先确认 PC3 的内外网口、SSH 登录地址和 H2 固件及运控版本。

[设计方案](H2_DESIGN.md) · [现场验收清单](H2_ACCEPTANCE_CHECKLIST.md)

## 1. 核对部署版本

同时更新 PC2 温湿度代理、PC3 网关和上位机前后端。PC3 与上位机使用同一个代码提交，包含生成的 gRPC 代码。升级后重启各端进程，记录实际运行版本，便于复验。

上位机根据 PC3 返回的单调时钟参考计算指令截止时间。温湿度有效期从 PC2 的采样年龄传递到上位机。两者均无需同步三台设备的系统时钟。`scripts/h2_field_start.sh` 会启动 PC2 代理，在启动 PC3 网关前执行只读 FSM 探测，然后启动两条 SSH 转发和上位机服务。脚本完成真机模式、就绪状态、申请、心跳和释放自检后才显示 `READY`。

若使用一键脚本，先确认脚本中的现场地址、路径、网卡及 SSH 登录方式，再更新 PC2 的 `~/sensor_agent.py`，并停止旧的 PC2 代理和 PC3 网关。脚本会复用已在运行的进程。已监听的 SSH 端口须由当前账号的两个独立 SSH 进程按第 5 节的参数转发。探测未就绪时，可继续启动以查看遥测，由现场人员使运控就绪后再做第 5 节的无运动自检。

## 2. 在 PC2 启动温湿度代理

在 PC2 确认 RS-485 对应的串口设备和运行用户的访问权限。部署 `scripts/sensor_agent_pc2.py`，按现场配置确认脚本中的 `PORT`、`BIND` 和 `HTTP_PORT`。将 `BIND` 设为 PC3 可达的 PC2 开发网地址，限制代理只在受控开发网内访问。下面从仓库根目录创建环境并启动代理。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install pyserial
.venv/bin/python -u scripts/sensor_agent_pc2.py
```

在 PC3 上验证 `GET /environment`，将下面的 `pc2-host` 和端口替换为实际配置。

```bash
curl --fail --show-error 'http://pc2-host:8090/environment'
```

正常响应应包含 `available: true` 和 `sample`。`temperature_c`、`humidity_percent` 是原始读数，`captured_at` 是 PC2 原始采样时刻（Unix 秒），`age_seconds` 是 PC2 用单调时钟计算的采样年龄（秒）。`sample_id` 是样本标识，同一条样本被重复读取时保持不变。连续读不到有效样本时，可用 `consecutive_failures` 排查串口权限、线路及传感器配置。最后一条有效样本达到 8 秒时，代理返回 `available: false`。

## 3. 准备 PC3

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

## 4. 在 PC3 只读探测并启动网关

1. 在 PC3 用 `ip -br link` 和 `ip -br addr` 核对接入运控网络的网卡。下面的 `eth10` 是示例，请替换成现场确认的网卡名。
2. 清空测试区域，准备防跌倒措施，确认原厂急停和控制权交接。由现场工程师按宇树流程将 H2 置于 **601 主运控站立模式**。
3. 在 PC3 执行只读探测。它先调用 `GetFsmId`，读到主运控 ID 后再查询 `GetFsmMode`。就绪时输出 `fsm_id: 601`、`fsm_mode: 0` 和 `teleop_ready: true`。如果出现 `3102`，先检查 PC3 所选网卡的链路与 DDS 发现。若返回其他 FSM 或状态未就绪，先确认现场运控状态。

```bash
.venv/bin/python -m h2_gateway.hardware --interface eth10
```

4. 只读探测就绪后，在 PC3 启动网关。将 `H2_SENSOR_AGENT_URL` 指向第 2 步验证过的 PC2 代理地址。保留前台终端输出。两个 gRPC 端口均监听 PC3 回环地址，`50051` 用于读取状态，`50052` 用于遥控。

```bash
H2_SENSOR_AGENT_URL='http://pc2-host:8090/environment' \
  .venv/bin/python -u -m h2_gateway.server --h2-interface eth10 --host 127.0.0.1 \
  --port 50051 --teleop-port 50052
```

PC3 后台轮询 PC2 代理，并从 DDS 话题 `rt/lf/bmsstate` 订阅 BMS 电量。网关将 BMS 的 `soc` 作为 `battery_percent` 上报。无 BMS 读数或最新读数超过 5 秒时，电量显示 `—`。温湿度和电量可独立降级，排查遥测时可继续查询运控状态。

## 5. 在 x86 上位机建立转发并启动应用

在上位机按项目 README 安装 Python 和前端依赖。外置网关模式使用独立的本地 SQLite 数据库，可跳过 PostgreSQL 的下载和启动。[项目 README](../README.md)

1. 用现场分配的 PC3 SSH 账号建立本地转发。把 `pc3-user@pc3-host` 替换为实际账号和开发口地址，并通过可信渠道核对并登记 PC3 主机密钥。下面两个命令分别在两个前台终端保持运行。上位机的 `15051` 转发到 PC3 的 `50051`，上位机的 `15052` 转发到 PC3 的 `50052`。`-S none` 禁止复用 SSH 连接。[OpenSSH 本地转发说明](https://man.openbsd.org/ssh.1)

```bash
ssh -N -T -S none -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=5 -o ServerAliveCountMax=2 \
  -L 127.0.0.1:15051:127.0.0.1:50051 pc3-user@pc3-host
```

```bash
ssh -N -T -S none -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=5 -o ServerAliveCountMax=2 \
  -L 127.0.0.1:15052:127.0.0.1:50052 pc3-user@pc3-host
```

2. 在上位机另开终端，停止原开发服务。用 `status` 确认返回 `stopped` 后，指定两个外部网关地址启动前端和后端。

```bash
.venv/bin/python scripts/dev.py stop
.venv/bin/python scripts/dev.py status
.venv/bin/python scripts/dev.py start --external-gateway 127.0.0.1:15051 \
  --external-teleop-gateway 127.0.0.1:15052
```

3. 在同一台上位机打开页面，确认显示「H2 真机遥控」，并检查机器人状态接口返回 `simulated: false` 和 `teleop_ready: true` 后再申请控制权。空闲时可以运行 `.venv/bin/python scripts/h2_field_check.py`，检查真机模式、运控状态和申请、心跳、释放的回执，全程不发方向指令。日志分别来自 PC3 的网关终端、上位机的 SSH 终端和 `.runtime/logs/api.log`。[上位机页面](http://127.0.0.1:5173) · [机器人状态接口](http://127.0.0.1:8000/api/robot)

4. 在机器人状态接口核对温湿度和 `battery_percent`。`environment_captured_at` 和 `environment_sample_id` 对应 PC2 原始采样时间和标识，`environment_remaining_ms` 是样本剩余有效毫秒数。温湿度从采样起最多显示 8 秒，重复轮询同一条样本不会延长有效期。遥测断开或样本缺少有效的剩余时间时，页面清除读数及「实测」标记。

## 6. 现场验收

PC2 温湿度和 PC3 电量已有现场联调记录。本版本的温湿度到期与恢复，以及方向、停步、释放控制权和两段断链，仍需在真机上验收。

在静止状态下核对页面温湿度和电量与原始遥测相符。保持 PC3 网关和上位机运行，停止 PC2 代理。在上位机记录最后一条样本的 `captured_at` 和接口返回的剩余有效时间，用本机计时确认期限届满时页面清除两项读数及「实测」标记。恢复 PC2 代理，确认新采样到达后页面恢复显示。

每次只按住一个方向，松开并观察实际停稳后再进行下一项。请求限幅为前后 0.5 m/s 和左右 0.55 rad/s（2026-11-15 真机实测：H2 的 601 HybridWalk 运控在速度低于迈步阈值时只做姿态调整不抬脚，0.15 m/s / 0.25 rad/s 全程无迈步；0.5 / 0.55 为实测可用值，见 motion_test 记录），非零速度单帧有效期最多 250 ms。停步和释放优先于尚未发送的方向请求。PC3 保持速度 SDK 调用串行，停步仍可能等待正在执行的 SDK 调用结束。停步回执表示 H2 高层运控服务已应答零速度请求，停稳时间和距离由现场实测——限幅提高后，松手停稳距离会比早期低速测试明显变长，现场判据需按新速度重新约定。若显示「停步未确认」，先用原厂控制手段处置，确认已停稳后检查链路和 PC3 网关日志。若网关保持故障状态，排除原因后在 PC3 重新做只读探测，再重启网关。

真机网关以 `h2_event` 单行 JSON 记录事件，时间戳 `mono_ms` 为 PC3 单调时钟毫秒（启动时的 `clock_base` 行给出与墙钟的换算锚点）。每条被采纳的方向帧记录 `teleop_frame`（序号、动作、入场剩余有效期 `remaining_ms`、实际下发的 `duration_ms`、SDK 回执耗时 `sdk_ms`、结果）；被拒绝的指令记录 `teleop_reject` 与原因码；每次零速度请求记录 `zero_velocity` 及原因（`stop` / `frame_expired` / `late_frame` / `lease_expired` / `not_ready` / `release` / `sdk_rejected`）与 SDK 回执；控制权变化记录 `lease`。软件链路的帧连续性与停步响应直接用这些事件测量，例如：

```bash
grep h2_event gateway.log | tail -20
```

| 项目 | 操作与记录 |
| --- | --- |
| 方向 | 依次短按前进、后退、左转、右转，记录实际方向和 SDK 回执 |
| 松手与主动停步 | 分别松开方向键和点击「停步」，记录回执、实际停稳时间及距离 |
| 释放控制 | 释放后方向按钮禁用，重新申请后需要重新按键 |

两段断链分别测试。每项开始前确认原厂急停和防跌倒措施可用，并按现场批准的测试条件执行。

在正式断链测试前，先静止持权，只关闭 `15051` 状态转发。确认页面状态和遥测降级，当前遥控会话仍可通过 `15052` 接收心跳和释放回执。恢复状态转发后，从 `/api/robot` 确认 `teleop_active: false`。本项全程无需发送方向指令。

1. **上位机到 PC3。** 保持 PC3 网关和运控侧通信正常，从上位机结束 `15052` 的遥控 SSH 转发，保留 `15051` 的状态转发以观察会话。记录页面连接状态、PC3 网关日志、实际停稳时间和距离。恢复遥控转发后确认旧会话失效且运动没有自动恢复。再次测试需重新申请控制权。
2. **PC3 到 H2 运控。** 保持上位机两条 SSH 转发和 PC3 网关运行，只用宇树批准的方法中断 PC3 SDK 与运控服务之间的 DDS 通信，保持本体安全与急停链路完整。记录运控状态丢失、停步请求或超时日志，以及机器人实际停稳时间和距离。恢复后先核对只读状态并确认旧会话失效，再申请新会话。现场尚无批准方法时，记录这一项待厂商支持。

反馈请附 H2 固件与运控版本、PC2、PC3 和上位机的系统与 Python 版本、各端代码提交、各项实测时间和结果。异常附发生时间与相关日志。若零速度后仍原地踏步，按厂商手册检查是否处于持续踏步模式，并记录现场结果。

## 不接机器人时复现

安装 SDK 后执行下面的测试。测试用独立的回环 DDS 域和仿真的 H2 高层服务，验证 WebSocket、真实 gRPC、真实 SDK/DDS 到速度应答的链路。

```bash
H2_TEST_SDK_DDS=1 .venv/bin/python -m pytest -q tests/test_h2_sdk_dds.py
```

[关于 H2](https://support.unitree.com/home/zh/H2_developer/About_H2) · [高层运动开发](https://support.unitree.com/home/zh/H2_developer/high_motion_development) · [高层运动服务接口](https://support.unitree.com/home/zh/H2_developer/sport_services_interface) · [SDK2 Python](https://github.com/unitreerobotics/unitree_sdk2_python) · [CycloneDDS 0.10.2 构建配置](https://github.com/eclipse-cyclonedds/cyclonedds/blob/0.10.2/src/CMakeLists.txt#L38)
