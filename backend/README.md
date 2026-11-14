# H2巡检技术验证后端

FastAPI 通过 gRPC 连接机器人网关，巡检任务和报告记录使用 SQLAlchemy 持久化。默认模式使用模拟机器人和合成巡检数据。H2 真机模式提供高层遥控和状态读取，使用本地启动器时由独立的 SQLite 数据库承载。

环境变量：

```bash
export H2_DATABASE_URL='postgresql+psycopg://h2:h2_validation@127.0.0.1:5432/h2_validation'
export H2_GATEWAY_ADDRESS='127.0.0.1:50051'
export H2_DATA_DIR="$PWD/.local/backend-data"
uvicorn h2_backend.app:create_app --factory --host 127.0.0.1 --port 8000
```

依赖还包括本仓库 `robot-gateway/` 的 `h2_gateway` 包。PDF使用WeasyPrint系统运行库及中文字体。首次启动通过SQLAlchemy创建验证表；正式工程需采用Alembic迁移。

API契约见 `/docs`。任务创建以`request_id`去重，相同键对应不同内容返回409。创建任务及outbox在同一事务中提交，网关用持久command_id二次去重；恢复先核对机器人已有任务，过期启动请求不重放。停止指令直接到网关，不进入报告队列。样本图片通过文件标识下载，原始结果和人工复核记录分别保存。

停止路径先请求网关再同步业务数据库。数据库暂时不可用时，网关确认停止的响应会带`database_status: unavailable`，不能据此认为数据库已经保存停止状态；恢复后重新同步。

## 机器人状态和遥控

`GET /api/health` 返回模式标记 `hardware_teleop`、`simulated` 及网关状态。`GET /api/robot` 和只读 WebSocket `/api/ws` 提供机器人状态。模拟模式返回模拟位姿和巡检进度，真机模式读取高层运动状态、控制权状态、最近指令应答，以及可用的电量和环境遥测。真机模式要求连接真机网关，拒绝下发或停止巡检任务，也不会回放待派发的巡检任务。

`/api/robot` 的真机环境字段包括 `temperature_c`（℃）、`humidity_percent`（%RH）、`environment_captured_at`（PC2 原始采集时刻，Unix 秒）、`environment_sample_id`（样本标识）和 `environment_remaining_ms`（样本剩余有效毫秒数）。PC2 用单调时钟计算采样年龄，网关和页面据此维持从采样起最多 8 秒的有效期，重复读到同一采样不会刷新期限。`received_at` 是后端收到网关状态后记录的时间。网关没有传回的遥测字段在 API 中为 `null`。旧网关缺少样本剩余有效时间时，页面显示「等待传感器数据」。现场地图和相机画面目前仅有前端占位，状态 API 未接入相应数据。

模拟和真机遥控均使用 `/api/teleop/ws`。入口仅接受本地 Vite 页面（`127.0.0.1` 或 `localhost` 的 5173、4173 端口）及本地代理，真机模式另要求 WebSocket 对端为回环地址。后端核对机器人模式，真机模式还要求高层运动服务就绪。网关授予独占控制权，令牌保留在后端。收到 `type: "lease"` 且 `accepted: true` 后，浏览器才能发送 `{action, sequence, sent_at_ms}`，其中序号严格递增，发送时间为 `Date.now()`。支持前进、后退、左右转、停步、心跳和释放。

移动及心跳指令的有效期为 350 毫秒，停步和释放不受客户端时钟过期影响。真机模式下，后端使用独立的 gRPC 连接传递遥控，按网关返回的单调时钟参考计算 PC3 侧指令截止时间。停步和释放优先于尚未发送的方向指令。2 秒未收到任何消息、连接断开或主动释放时结束会话，失去控制权后需要重新申请。遥控请求不依赖业务数据库，遥控期间被拒绝的模拟巡检不会在释放后自动执行。真机运动和停步应答只表示指令已获确认，实际运动和停稳需要现场验证。

POST报告接口在线程池中生成PDF、Excel和JSON；用数据库记录作业状态。此轮没有接入Celery/RabbitMQ，也没有做报表作业自动恢复和自动生成验证。首期验证使用用户主动生成报告，报告作业状态可留存。

本地技术验证未实现登录、RBAC、CSRF 或服务间 TLS。上位机服务应绑定回环地址。模拟页面、巡检数据和报告均标明合成来源。当前改动的验证范围为本地模拟和演示数据，真机动作及现场遥测尚未实测。现场配置与验收见 [H2 真机遥控现场联调](../robot-gateway/H2_COMMISSIONING.md)。

测试：

```bash
pytest backend/tests
```

设置`H2_TEST_DATABASE_URL`时，测试使用该PostgreSQL数据库并清空本验证的表；应使用专门测试库。未设置时，测试使用临时SQLite，仅用于API业务逻辑检查，不能作为PostgreSQL验证证据。
