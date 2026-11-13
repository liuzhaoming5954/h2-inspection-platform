# H2 机器人网关

默认启动模拟器，生成模拟状态、温湿度和带 SIM 标记的合成机柜图片。使用 `--h2-interface` 可切换到基于宇树高层运动服务的 H2 真机手动遥控。

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

gRPC 使用本机回环连接。真机模式只开放手动遥控和 FSM 状态查询，禁用巡检任务。SDK 的停步回执表示零速度请求已应答，物理停稳和断链后的表现以现场验收结果为准。
