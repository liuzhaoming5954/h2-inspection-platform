# H2 真机联调验收清单

启动顺序为 **PC2 温湿度代理 → PC3 只读探测和网关 → 上位机两条 SSH 转发和应用 → 就绪检查**。就绪后依次验收温湿度、遥控方向和停步、通道隔离及两段断链。地图、真机位姿和相机画面待接入。[三端部署说明](H2_COMMISSIONING.md) · [设计方案](H2_DESIGN.md)

## 三端更新与开测条件

| 设备 | 更新或复用 | 开测前确认 |
| --- | --- | --- |
| PC2 | 更新并重启 `scripts/sensor_agent_pc2.py`，使用 `pyserial`。脚本启动方式下更新 PC2 的 `~/sensor_agent.py`。 | 确认 RS-485 串口、运行账号权限、PC2 开发网地址和 HTTP 端口。 |
| PC3 | 更新并重启网关，一并更新生成的 Protobuf 文件。复用已验证的 Python、SDK2 和 CycloneDDS 环境。 | 确认运控侧网卡、PC3 开发网地址和 SSH 账号。首次安装参照现场联调手册第 3 节。 |
| 上位机 | 前端和后端一并更新，安装项目 Python 和前端依赖。 | 上位机接 H2 外部开发网，浏览器在上位机本机运行。通过可信渠道核对并登记 PC3 的 SSH 主机密钥。 |

- [ ] PC3 和上位机分别在仓库根目录运行 `git rev-parse --short HEAD`，确认部署同一提交，并记录对应进程的重启结果。PC2 部署同一提交中的代理。有效期使用单调时钟计算，三台设备无需同步系统时钟。
- [ ] 现场负责人确认测试区已清场、防跌倒措施及原厂急停可用。安排操作员和观察员，先约定可接受的实际停稳时间和距离。切换运控模式及真机动作由现场人员按厂商流程执行。[宇树开发文档中心](https://support.unitree.com/home/)

## 三端启动与就绪检查

以下命令在对应设备的项目根目录执行。先把示例 `pc2-host` 和 `8090` 换成 PC2 代理的实际地址和端口，把 `eth10` 换成 PC3 的运控侧网卡，把 `pc3-user@pc3-host` 换成现场 SSH 账号和 PC3 开发网地址。

### 1. PC2 启动温湿度代理

- [ ] 在 `scripts/sensor_agent_pc2.py` 中核对 `PORT`、`BIND`、`HTTP_PORT`。`PORT` 是现场 RS-485 串口，`BIND` 是 PC3 可达的 PC2 开发网地址。确认代理账号可以访问串口。已确认运行新版代理时，直接做下面的 PC3 查询。
- [ ] 首次部署或缺少依赖时，先准备 Python 环境。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install pyserial
```

- [ ] 在 PC2 启动代理并保留前台终端。若现场用服务管理器部署，沿用该服务启停。

```bash
.venv/bin/python -u scripts/sensor_agent_pc2.py
```

- [ ] 在 **PC3** 查询 PC2。确认返回 `available: true`，`sample` 包含 `temperature_c`、`humidity_percent`、`sample_id`、数值型 `captured_at` 和 `age_seconds`。间隔几秒查询一次，确认样本标识随新采样更新。

```bash
curl --fail --show-error --max-time 5 'http://pc2-host:8090/environment'
```

### 2. PC3 只读探测并启动网关

- [ ] 现场人员按宇树流程将 H2 置于主运控站立模式，用 `ip -br link` 和 `ip -br addr` 核对运控侧网卡。若按项目文档在仓库内构建 CycloneDDS，先在当前终端设置 `CYCLONEDDS_HOME`。已有部署使用现场安装路径。

```bash
ip -br link
ip -br addr
export CYCLONEDDS_HOME="$PWD/.runtime/h2-sdk2/cyclonedds-install"
.venv/bin/python -m h2_gateway.hardware --interface eth10
```

- [ ] 只读探测应返回 `fsm_id: 601`、`fsm_mode: 0`、`teleop_ready: true`。若返回 `3102`，检查 PC3 运控网卡和 DDS 服务发现。若运控未就绪，先由现场人员确认 H2 状态，再复测。
- [ ] 探测就绪后启动网关。若旧网关仍在运行，先按原部署方式停止并确认退出。使用上一步在 PC3 验证过的 PC2 代理地址，保留 PC3 网关终端及日志。

```bash
H2_SENSOR_AGENT_URL='http://pc2-host:8090/environment' \
  .venv/bin/python -u -m h2_gateway.server \
  --h2-interface eth10 --host 127.0.0.1 --port 50051 --teleop-port 50052
```

- [ ] 确认 PC3 的状态端口 `127.0.0.1:50051` 和遥控端口 `127.0.0.1:50052` 均已监听。运控是否就绪以只读探测和上位机状态接口为准。

### 3. 上位机建立 SSH 转发并启动应用

- [ ] **状态终端** 建立 SSH 本地转发并保持运行。上位机 `127.0.0.1:15051` 转发到 PC3 `127.0.0.1:50051`。若报主机密钥错误，先向现场管理员核对。

```bash
ssh -N -T -S none \
  -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=5 -o ServerAliveCountMax=2 \
  -L 127.0.0.1:15051:127.0.0.1:50051 pc3-user@pc3-host
```

- [ ] **遥控终端** 单独建立另一条 SSH 本地转发并保持运行。上位机 `127.0.0.1:15052` 转发到 PC3 `127.0.0.1:50052`。两条连接均使用 `-S none`，各自建立 SSH 会话。

```bash
ssh -N -T -S none \
  -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=5 -o ServerAliveCountMax=2 \
  -L 127.0.0.1:15052:127.0.0.1:50052 pc3-user@pc3-host
```

- [ ] **应用终端** 若旧服务在运行，先发出停止请求，再查状态直到返回 `stopped`。

```bash
.venv/bin/python scripts/dev.py stop
.venv/bin/python scripts/dev.py status
```

- [ ] 在应用终端启动外置网关模式。上位机运行前后端并使用独立 SQLite。首次部署按项目 README 安装 Python 和前端依赖，外置网关模式可跳过 PostgreSQL 下载和启动。[上位机依赖说明](../README.md#本地启动)

```bash
.venv/bin/python scripts/dev.py start --external-gateway 127.0.0.1:15051 \
  --external-teleop-gateway 127.0.0.1:15052
```

- [ ] 启动器应显示 `H2 EXTERNAL GATEWAY`。保持应用终端运行，使用本机浏览器打开 `http://127.0.0.1:5173`。

### 4. 上位机核对真机模式与遥控就绪

- [ ] 页面标题是「H2 真机遥控验证台」，「机器人连接」为「已连接」，「高层运动服务」为「可遥控」。在上位机另开终端，查询两个只读接口。

```bash
curl --fail --silent --show-error http://127.0.0.1:8000/api/health \
  | .venv/bin/python -m json.tool
curl --fail --silent --show-error http://127.0.0.1:8000/api/robot \
  | .venv/bin/python -m json.tool
```

- [ ] `/api/health` 显示 `hardware_teleop: true` 和 `gateway: "connected"`。空闲待命时，`/api/robot` 显示 `simulated: false`、`teleop_ready: true`、`teleop_active: false`、`fsm_id: 601`、`fsm_mode: 0`。页面显示真机模式后仍需核对运控就绪。
- [ ] 机器人保持静止、没有其他会话时，运行以下自检，核对申请、心跳和释放回执。也可在页面申请控制权，看到「本页持权」后直接释放。用 `/api/robot` 确认 `teleop_active` 恢复为 `false`。

```bash
.venv/bin/python scripts/h2_field_check.py
```

已完成三端部署的现场可在上位机项目根目录运行 `scripts/h2_field_start.sh`，用其启动输出和本节的判据复核。脚本会复用已运行的代理和网关。升级时先替换 PC2 的 `~/sensor_agent.py`，停止旧的 PC2、PC3 进程，再执行脚本。停止上位机应用及两条转发可用 `scripts/h2_field_stop.sh`。PC2 和 PC3 的服务按各自部署方式停止。[现场启动说明](H2_COMMISSIONING.md#1-核对部署版本)

若应用未启动，查看上位机 `.runtime/logs/api.log` 和 `.runtime/logs/frontend.log`。若页面未连接或 `/api/robot` 返回 `503`，先查看状态 SSH 终端及 PC3 网关日志。若能读取状态但不能申请控制权，检查遥控 SSH 终端。运控未就绪时回到 PC3 做只读探测。温湿度缺失时核对 PC2 的响应、采样年龄和网关剩余有效时间。

## 温湿度过期与恢复

1. [ ] 机器人静止，保持 PC2、PC3 和上位机运行。用 `sample_id` 与 `/api/robot` 的 `environment_sample_id` 对照同一条样本，核对温湿度和原始采样时刻 `environment_captured_at`。`environment_remaining_ms` 应大于 0 且不超过 8000，页面显示数值和「机身传感器 · 实测」。
2. [ ] 保持浏览器在前台。仅停止 PC2 温湿度代理，前台启动的代理可在 PC2 终端按 `Ctrl+C`，由服务管理器启动的代理按原方式停止。保持 PC3、SSH 和上位机运行。记录 `/api/robot` 末次样本的标识、剩余有效时间及上位机观测时刻，按同一台上位机的经过时间核对。剩余有效期届满后，页面数值变为「—」，状态改为「等待传感器数据」。过期后 API 的 `temperature_c`、`humidity_percent`、`environment_captured_at`、`environment_remaining_ms` 和 `environment_sample_id` 均为 `null`。状态连接应保持，BMS 正常时电量仍可见。
3. [ ] 恢复 PC2 代理。确认收到新的 `environment_sample_id`，温湿度和「实测」标记重新出现。记录末次旧样本标识、页面清除时点和首条恢复样本标识。有效期从 PC2 采样起算，重复接收同一条数据不会续期。

若现场能在保持 PC2 代理可访问的情况下暂停采样，可再测一次。记录连续响应中的同一个 `sample_id` 和递增的 `age_seconds`，确认 PC3 反复读到缓存时，样本剩余有效时间没有增加。

## 遥控方向、停步与释放

| 勾选 | 验收项 | 通过判据 |
| --- | --- | --- |
| ☐ | 四个方向 | 申请控制权后，依次短按前进、后退、左转、右转。逐项记录实际方向和回执，每次确认停稳后再测下一项。 |
| ☐ | 松手停步 | 观察到短时运动后松开方向按钮，分别记录松手时刻、零速度回执、实际停稳时刻和距离。实际结果应满足现场事先约定的标准。 |
| ☐ | 主动停步 | 将焦点放在遥控面板内，短按住键盘方向键，在松键前点击页面「停步」按钮。记录按钮触发、回执和实际停稳。 |
| ☐ | 释放控制权 | 停稳后点击「释放控制权」。方向按钮应禁用，`/api/robot` 应恢复 `teleop_active: false`。重新申请后必须重新按方向才会运动。 |

「停步请求已应答」表示 SDK 高层运控服务已应答零速度请求。实际停稳时间和距离由观察员记录。停步和释放控制权优先于尚未发送的方向指令，仍可能等待正在执行的 SDK 调用。出现「停步未确认」、持续踏步、异常位移或失稳时，暂停测试，按现场应急流程处置并记录异常。

## 两段断链分别验收

- [ ] 静止持权时，仅关闭 `15051` 状态转发。页面的状态与遥测应降级，当前会话仍可通过 `15052` 续期并释放。恢复状态转发后，核对 `/api/robot` 的 `teleop_active: false`。此项无需发送方向指令。

每段先在静止且已持权的状态下验证会话失效和恢复。运动中的停稳验证须在现场负责人批准的条件下单独进行，有观察员和原厂急停待命。记录断链触发时刻，避免浏览器先失焦或先松开方向造成停步，影响对断链本身的判断。

| 勾选 | 断链位置 | 操作与证据 |
| --- | --- | --- |
| ☐ | 上位机 ↔ PC3 | 保持 PC3 网关和运控侧通信正常，结束上位机的 `15052` 遥控 SSH 转发，保留 `15051` 状态转发。安排另一人触发断链，让遥控浏览器保持焦点。记录会话结束、`teleop_active: false` 和状态接口是否继续可读，并保留 SSH 与 PC3 日志。如果获准做运动验证，记录实际停稳时间和距离。恢复转发后确认旧会话失效，机器人没有自动恢复运动。 |
| ☐ | PC3 ↔ H2 运控 | 保持 SSH 转发和 PC3 网关运行。按宇树批准的方法验证该段 DDS 链路中断，保持本体安全与急停链路完整。记录运控状态丢失、停步请求或超时日志和实际停稳结果。恢复后先做只读检查，确认旧会话失效、运动未自动恢复。没有批准的测试方法时，记录「待厂商支持」。 |

两段恢复后均应确认 `teleop_active: false`，待 `teleop_ready: true` 后才重新申请控制权。若只完成静止验证，单独标记「运动中断链停稳待验收」。

## 回传记录

每项填写「通过 / 失败 / 待验收」，并附时间、页面或 API 截图、相关日志与现场观察记录。运动项记录触发时刻、回执时刻、实际停稳时刻和距离。联调记录附 H2 固件和运控版本、三端系统及 Python 版本、PC2 代理版本、PC3 与上位机代码提交。现场允许时附停稳视频，便于复核。
