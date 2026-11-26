# H2 巡检上位机协作指南

本文件适用于整个仓库。项目结构、依赖安装和本地启动步骤见 [README.md](README.md)。

## 代码入口

| 修改内容 | 从哪里开始 |
| --- | --- |
| 巡检台、真机界面、状态轮询和布局 | [`frontend/src/App.vue`](frontend/src/App.vue) |
| 浏览器遥控输入、控制权和停步交互 | [`frontend/src/components/TeleopPanel.vue`](frontend/src/components/TeleopPanel.vue) |
| HTTP API、巡检下发、人工复核和报告 | [`backend/src/h2_backend/`](backend/src/h2_backend/) |
| 后端遥控 WebSocket、指令排队和转发 | [`backend/src/h2_backend/teleop.py`](backend/src/h2_backend/teleop.py) |
| 模拟巡检和模拟运动 | [`robot-gateway/src/h2_gateway/engine.py`](robot-gateway/src/h2_gateway/engine.py)、[`teleop.py`](robot-gateway/src/h2_gateway/teleop.py) |
| PC3 真机网关、SDK2 运控和遥测 | [`robot-gateway/src/h2_gateway/hardware.py`](robot-gateway/src/h2_gateway/hardware.py) |
| PC2 温湿度和三机部署 | [`scripts/sensor_agent_pc2.py`](scripts/sensor_agent_pc2.py)、[`scripts/deploy/`](scripts/deploy/) |

## 代码编写规则

- 新增或修改的代码注释统一使用中文，包括行内注释和 Python 文档字符串。注释中引用的 API 名称、命令和协议字段保留原文。
- 注释优先说明关键逻辑的原因、业务约束和边界条件。

## 本地开发与验证

- 从仓库根目录执行命令。Python 使用 README 中的 3.12 虚拟环境和锁定依赖，前端使用 Node.js 24.12 至 24.x 和 `pnpm --dir frontend install --frozen-lockfile`。
- 本地联调默认使用 `.venv/bin/python scripts/dev.py start`，并使用本机模拟网关和开发数据库。页面在 `127.0.0.1:5173`，API 在 `127.0.0.1:8000`，运行日志在 `.runtime/logs/`。
- `dev.py start` 带 `--h2-interface` 会启动本机 SDK2 网关，带 `--external-gateway` 和 `--external-teleop-gateway` 会使用已建立的转发。一键现场编排由 `h2_field_start.sh` 管理。现场启停脚本与链路恢复按钮会操作现场连接，本地演示与回归测试使用模拟器或测试替身。
- Python 回归命令为 `.venv/bin/python -m pytest -q backend/tests robot-gateway/tests tests`。按改动选择对应测试目录，跨服务改动加跑根目录的 `tests/`。默认使用临时 SQLite，`H2_TEST_DATABASE_URL` 只指向专用测试库，测试会清理验证表。可选的 `H2_TEST_SDK_DDS=1` 测试需要已安装 SDK，使用独立的本机回环 DDS 域。
- 前端改动运行 `pnpm --dir frontend build`，其中包含 TypeScript 类型检查。涉及界面布局时，在浏览器中检查对应模式。真机桌面布局以 1440×820 验证状态带和地图、图像、语音转写三个工作面板一屏完整显示（主区无页头）；链路管理指示灯与恢复按钮、遥控与巡检演示的控制和进度都在抽屉侧栏中（宽 240px，默认收起，把手悬浮在屏幕左上角，呼出时主区让位，栏内内容可滚动），遥控会话或巡检演示执行期间收起按钮禁用，侧栏只由把手按钮开合、不随演示状态自动弹出。真机模式打开页面先进入登录页（初始密码 `123`，可在侧栏修改），浏览器验证前需先登录。
- 验证结果注明实际环境和范围，如模拟巡检、SDK 替身、回环 DDS 或现场真机。实际停稳时间和距离由现场验收记录。

## 接口与现场行为

- 修改 [`contracts/robot_gateway.proto`](contracts/robot_gateway.proto) 后，执行 `.venv/bin/python robot-gateway/generate_stubs.py`，把生成的 Python 代码和契约一起更新。涉及 HTTP 状态字段时，同时核对后端输出和 [`frontend/src/api.ts`](frontend/src/api.ts) 的类型。
- 真机状态与遥控使用各自独立的 gRPC 和 SSH 通道。停步、释放优先于待发送的方向指令。修改遥控时核对松手停步、主动停步、释放控制权和断链后的会话行为。模拟模式下保持巡检与手动遥控互斥。SDK 回执表示运控服务已应答，实际停稳由现场观察确认。
- 真机遥控的指令有效期使用 PC3 单调时钟。PC2 温湿度按原始采样年龄判断，从采样起最多显示 8 秒。同一采样重复读取时保留原有效截止时间，三机各自使用本机单调计时。

## 文档和提交范围

- 每天完成编码后，在结束当天工作前核对当天的改动，并同步更新所有受影响的文档。操作步骤、配置说明和验收记录以当天的实际实现与验证结果为准。
- 目录、启动命令和本地验证方法发生变化时更新 [README.md](README.md)。涉及三机部署、通道行为或现场判据时，同步对应的[设计方案](robot-gateway/H2_DESIGN.md)、[联调说明](robot-gateway/H2_COMMISSIONING.md)、[现场验收清单](robot-gateway/H2_ACCEPTANCE_CHECKLIST.md)和[部署脚本说明](scripts/deploy/README.md)。
- 将改动集中在当前任务，保留工作区已有的其他修改。`.runtime/`、`artifacts/` 和 `docs/` 按 `.gitignore` 留在本地，凭据由各机本地配置提供。
