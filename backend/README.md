# H2巡检技术验证后端

真实FastAPI、SQLAlchemy/PostgreSQL和gRPC链路，机器人与所有传感数据为模拟。

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

`/api/ws`继续提供只读状态。模拟遥控单独使用`/api/teleop/ws`，仅接受本地 Vite 页面（`127.0.0.1`或`localhost`的5173、4173端口）及本地代理入口。连接时网关验证模拟状态并申请独占控制权，令牌保留在后端。收到`type: "lease"`且`accepted: true`后，浏览器才能发送`{action, sequence, sent_at_ms}`，其中序号严格递增，发送时间为`Date.now()`。支持前进、后退、左右转、停止、心跳和释放。

移动及心跳最多保留350毫秒，停止和释放不受客户端时钟过期影响。2秒未收到任何消息、连接断开或主动释放时结束会话；失去控制权后需要重新显式申请。移动失效和租约由模拟网关独立判断，遥控请求不依赖业务数据库；遥控期间被拒绝的巡检不会在释放后自动执行。

POST报告接口在线程池中生成PDF、Excel和JSON；用数据库记录作业状态。此轮没有接入Celery/RabbitMQ，也没有做报表作业自动恢复和自动生成验证。首期验证使用用户主动生成报告，报告作业状态可留存。

本地技术验证未实现登录、RBAC、CSRF或服务间TLS。服务必须绑定回环地址，不向生产网络开放。所有页面、API结果及报告标记模拟数据；本验证不能替代真实机器人安全、SDK、算法准确率及硬件验收。

测试：

```bash
pytest backend/tests
```

设置`H2_TEST_DATABASE_URL`时，测试使用该PostgreSQL数据库并清空本验证的表；应使用专门测试库。未设置时，测试使用临时SQLite，仅用于API业务逻辑检查，不能作为PostgreSQL验证证据。
