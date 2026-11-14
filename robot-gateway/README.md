# H2 机器人网关

默认启动模拟器，生成模拟状态、温湿度和带 SIM 标记的合成机柜图片。现场真机联调时，在 H2 的 PC3 上使用 `--h2-interface` 启动 SDK2 网关，上位机使用独立的 SSH 转发连接状态和遥控的回环 gRPC 端口。 [设计方案](H2_DESIGN.md) · [三端部署说明](H2_COMMISSIONING.md) · [现场验收清单](H2_ACCEPTANCE_CHECKLIST.md)

从项目根目录安装并启动：

```bash
python -m pip install -e './robot-gateway[dev]'
python robot-gateway/generate_stubs.py
python -m h2_gateway.server --host 127.0.0.1 --port 50051 --db .local/state/gateway.sqlite3
python -m pytest robot-gateway/tests -q
```

gRPC契约在`contracts/robot_gateway.proto`，生成的Python代码随工程保存。Python同步客户端：

```python
import time
from h2_gateway.client import GatewayClient

with GatewayClient('127.0.0.1:50051') as gateway:
    print(gateway.get_state())
    print(gateway.start_task(
        task_id='demo-001', command_id='start-demo-001',
        expires_at_ms=int(time.time() * 1000) + 10_000,
        points=[{'id': 'rack-a', 'label': 'A01', 'x': 1, 'y': 2}],
    ))
    print(gateway.get_task('demo-001'))
```

- `get_state()`：机器人快照、最近任务、0～100进度、持久化`sequence`和累计`start_count`。
- `start_task(task_id, command_id, expires_at_ms, points)`：收到、验证并持久化后接受；图片和采集数据按点逐步产生。
- `stop_task(task_id, command_id, expires_at_ms)`：立即中断模拟等待，不等待下一点采集。
- `get_task(task_id)`：状态、进度、采样数据与合成PNG字节；不存在时返回`status='not_found'`。

命令回执包括`accepted/code/message/task_id/command_id/simulated`；同一命令标识同一载荷始终返回原回执，换载荷返回`idempotency_conflict`。缓存命令跨网关重启保留，过期的首次运动请求拒绝执行。任务标识不可重复启动。

SQLite仅为网关本地命令/结果日志；业务平台使用PostgreSQL。重启将未结束任务标记为`interrupted`，并保留已采集结果，不自动继续或重放运动。后台报告及模型任务不进入网关执行线程。

模拟遥控使用 `teleop_acquire()`、`teleop_command(token, sequence, action, expires_at_ms)` 和 `teleop_release(token)`。网关独占仲裁人工遥控与巡检；只有有效会话能发送严格递增的序号，状态快照不携带令牌。支持 `forward`、`backward`、`turn_left`、`turn_right`、`stop`、`heartbeat`。各请求只接受未来最多500毫秒的截止时间。租约2秒，心跳只续租约，方向动作最多450毫秒；动作失效即停止并撤销租约。独立监视线程无需轮询请求即可生效。停止后保持当前位置，`yaw`为弧度，零沿+x、左转增加。租约与动作不写入SQLite，重启不会恢复。

gRPC 使用本机回环连接。真机模式用独立端口处理状态请求和遥控指令，默认分别为 `50051` 和 `50052`。真机模式支持手动遥控、FSM 状态查询、PC2 温湿度及 PC3 BMS 电量遥测，巡检任务仅在模拟模式开放。

- PC2 运行 `scripts/sensor_agent_pc2.py`，通过 `pyserial` 读取 RS-485 传感器。PC3 通过 `H2_SENSOR_AGENT_URL` 轮询代理的 `/environment`。`get_state()` 中的 `environment_captured_at` 保留 PC2 原始采样时间（Unix 秒），`environment_remaining_ms` 提供剩余有效毫秒数。真机页面在有效期届满时清除温湿度和「实测」标记，重复轮询同一条样本不会续期。
- PC3 通过 SDK2 订阅 DDS 话题 `rt/lf/bmsstate`，将 `soc` 作为 `battery_percent` 上报。无数据或读数超过 5 秒时，电量显示 `—`。

部署时同时更新 PC2 代理、PC3 网关和上位机前后端。各端用本机单调时钟判断有效期，无需同步系统时钟。SDK 的停步回执表示零速度请求已应答。温湿度到期与恢复、遥控方向、实际停稳、释放控制权和断链行为仍待真机验收。[H2 真机遥控现场联调](H2_COMMISSIONING.md)
