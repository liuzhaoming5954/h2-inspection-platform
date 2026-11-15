# H2 巡检上位机

面向 x86_64 Ubuntu 24.04 的巡检与遥控技术验证项目。默认用模拟机器人和合成数据运行巡检、人工复核、报告导出和遥控流程。H2 真机模式支持前进、后退、左右转向和停步。

## 项目结构

```text
h2-inspection-platform/
├── .vscode/                             # VS Code 工作区设置
├── backend/                             # 上位机业务服务
│   ├── src/                             # Python 包源码根目录
│   │   └── h2_backend/                  # HTTP API、WebSocket、巡检业务、存储和报告
│   └── tests/                           # 后端 API 和遥控会话测试
├── contracts/                           # 上位机与机器人网关的 Protobuf 契约
├── frontend/                            # 上位机 Web 界面
│   ├── public/                          # Vite 原样提供的静态资源
│   └── src/                             # Vue 应用入口、页面、接口类型和样式
│       └── components/                  # 手动遥控、三维场景和温湿度图表
├── robot-gateway/                       # 本机模拟网关和 PC3 真机网关
│   ├── src/                             # Python 包源码根目录
│   │   └── h2_gateway/                  # gRPC 服务与客户端、模拟器和 H2 SDK2 适配
│   │       └── generated/               # 随仓库提交的 Python Protobuf 和 gRPC 代码
│   └── tests/                           # 模拟引擎、gRPC 和真机适配逻辑测试
├── scripts/                             # 本地启停、现场联调及验证工具
│   └── deploy/                          # PC2、PC3、上位机各自的环境准备脚本
├── tests/                               # 跨模块集成与端到端测试
├── requirements-validation.txt          # Python 验证依赖声明
└── requirements-validation.lock.txt     # 可重复安装的 Python 依赖版本
```

模拟模式下，浏览器经 Vite 的 `/api` 代理访问 FastAPI。后端通过 gRPC 向本机网关下发巡检任务，读取合成温湿度、图片和位姿，将任务、采样和复核记录保存到 PostgreSQL，并生成报告。

真机模式下，浏览器仍由上位机提供界面。上位机向 PC3 网关分别发起状态和遥控调用，PC3 通过 SDK2 和 DDS 连接 H2 运控，并通过 HTTP 读取 PC2 代理提供的温湿度。PC2 通过 RS-485 读取传感器。

### `frontend/` · 上位机界面

`frontend/` 使用 Vue 3、TypeScript、Vite 和 Element Plus。`public/` 存放站点图标，`src/` 存放应用源码，`src/components/` 存放遥控、三维场景和图表组件。[前端说明](frontend/README.md)

| 文件 | 职责 |
| --- | --- |
| [`index.html`](frontend/index.html)、[`src/main.ts`](frontend/src/main.ts) | HTML 和 Vue 入口，挂载应用并加载公共样式及组件库 |
| [`src/App.vue`](frontend/src/App.vue)、[`src/style.css`](frontend/src/style.css) | 根据后端模式显示巡检台或真机遥控台，组织状态轮询、链路管理、任务、复核、报告及页面布局 |
| [`src/api.ts`](frontend/src/api.ts) | 定义任务、采样、报告和机器人状态的 TypeScript 类型，封装 HTTP 请求与错误提示 |
| [`TeleopPanel.vue`](frontend/src/components/TeleopPanel.vue) | 申请和释放控制权、处理方向和停步输入、通过 WebSocket 发送遥控指令 |
| [`RoomScene.vue`](frontend/src/components/RoomScene.vue)、[`EnvironmentChart.vue`](frontend/src/components/EnvironmentChart.vue) | 用 Three.js 绘制场景和位姿，用 ECharts 展示巡检温湿度趋势 |
| [`vite.config.ts`](frontend/vite.config.ts)、[`package.json`](frontend/package.json)、[`pnpm-lock.yaml`](frontend/pnpm-lock.yaml) | 配置开发代理和构建，声明前端命令与依赖，锁定安装版本 |

### `backend/` · 业务服务

`backend/src/h2_backend/` 是 Python 包 `h2_backend` 的源码。`backend/tests/` 验证 API、任务与报告逻辑、遥控会话及异常处理。[后端说明](backend/README.md)

| 文件 | 职责 |
| --- | --- |
| [`app.py`](backend/src/h2_backend/app.py) | FastAPI 应用入口，注册健康检查、状态、任务、复核、报告下载和遥控 WebSocket 等接口 |
| [`config.py`](backend/src/h2_backend/config.py) | 从环境变量读取数据库地址、数据目录、网关地址与运行模式 |
| [`service.py`](backend/src/h2_backend/service.py) | 处理任务去重、待派发记录和恢复、网关状态同步，协调采样、复核和报告存储 |
| [`models.py`](backend/src/h2_backend/models.py)、[`reports.py`](backend/src/h2_backend/reports.py) | 定义 SQLAlchemy 数据表，生成 PDF、Excel 和 JSON 报告 |
| [`teleop.py`](backend/src/h2_backend/teleop.py) | 管理浏览器遥控会话、控制权令牌、指令有效期与优先转发 |
| [`field.py`](backend/src/h2_backend/field.py) | 提供现场链路状态，以及界面触发的转发重连和 PC2、PC3 进程启动操作 |

### `robot-gateway/` 与 `contracts/` · 机器人接入

`robot-gateway/src/h2_gateway/` 是 Python 包 `h2_gateway` 的源码。模拟器和真机适配共享同一套 gRPC 契约。`robot-gateway/tests/` 覆盖模拟巡检、gRPC 通信、控制权以及真机模式的遥测、指令时效和停步逻辑。[网关说明](robot-gateway/README.md)

| 文件或目录 | 职责 |
| --- | --- |
| [`server.py`](robot-gateway/src/h2_gateway/server.py)、[`client.py`](robot-gateway/src/h2_gateway/client.py) | 网关服务入口和后端使用的 Python 客户端。服务按启动模式使用模拟引擎或 H2 适配器 |
| [`engine.py`](robot-gateway/src/h2_gateway/engine.py)、[`teleop.py`](robot-gateway/src/h2_gateway/teleop.py) | 模拟巡检、合成采样与图片、SQLite 命令记录，以及模拟机器人运动、位姿和独占租约 |
| [`hardware.py`](robot-gateway/src/h2_gateway/hardware.py) | 在 PC3 对接宇树 SDK2，读取运控状态、BMS 和里程计，接收 PC2 温湿度并处理真机遥控 |
| [`contracts/robot_gateway.proto`](contracts/robot_gateway.proto) | 定义状态查询、任务下发和停止、结果读取、遥控申请和指令等 RPC 及消息字段 |
| [`generated/`](robot-gateway/src/h2_gateway/generated/) | 保存由 `.proto` 生成的 Python 消息类和 gRPC 桩。修改契约后用 [`generate_stubs.py`](robot-gateway/generate_stubs.py) 同步更新 |

### `scripts/` · 启停、部署和验证

| 文件或目录 | 运行位置与职责 |
| --- | --- |
| [`dev.py`](scripts/dev.py) | 上位机服务启动器，管理 Vite、FastAPI 和可选的本机网关。默认启动模拟模式，支持 `start`、`status`、`stop` |
| [`fetch_postgres.py`](scripts/fetch_postgres.py)、[`manage_postgres.py`](scripts/manage_postgres.py) | 在上位机下载解压 PostgreSQL 17，并初始化、启动或停止本地验证数据库 |
| [`sensor_agent_pc2.py`](scripts/sensor_agent_pc2.py) | 在 PC2 读取 RS-485 温湿度传感器，通过 `/environment` 提供读数、样本标识和采样年龄 |
| [`h2_field_start.sh`](scripts/h2_field_start.sh)、[`h2_field_stop.sh`](scripts/h2_field_stop.sh) | 在上位机编排现场链路，启用 PC2 代理、PC3 网关、两条 SSH 转发和上位机服务。收工脚本停止上位机服务和转发，PC2、PC3 进程保留供复用 |
| [`h2_field_check.py`](scripts/h2_field_check.py) | 在机器人静止时检查真机模式、运控就绪、控制权申请、心跳和释放回执 |
| [`deploy/`](scripts/deploy/README.md) | [`setup_pc2.sh`](scripts/deploy/setup_pc2.sh) 安装传感器代理，[`setup_pc3.sh`](scripts/deploy/setup_pc3.sh) 安装网关和 SDK2 运行依赖，[`setup_workstation.sh`](scripts/deploy/setup_workstation.sh) 安装上位机包与锁定依赖并生成启动包装器 |
| [`validate_live.py`](scripts/validate_live.py)、[`browser_validate.mjs`](scripts/browser_validate.mjs) | 验证本地模拟系统。前者经 HTTP 验证 PostgreSQL、gRPC、巡检和报告，后者通过浏览器验证下发、人工复核、报告入口和刷新后数据恢复 |
| [`validate_inference.py`](scripts/validate_inference.py) | 用小型合成模型检查 OpenCV、NumPy 和 ONNX Runtime 的 CPU 推理环境 |

### `tests/` · 跨模块测试

根目录的 `tests/` 检查上位机启动参数、网关进程异常退出后的恢复、模拟遥控以及使用 SDK 替身的真机模式端到端链路。[`test_h2_sdk_dds.py`](tests/test_h2_sdk_dds.py) 需要显式设置 `H2_TEST_SDK_DDS=1`，使用已安装的宇树 SDK 和本机回环上的模拟 DDS 服务。

### 本地运行目录

以下目录或文件由安装、运行、构建或本地整理产生，均由 Git 忽略。

| 路径 | 内容 |
| --- | --- |
| `.venv/`、`frontend/node_modules/` | Python 虚拟环境和前端依赖 |
| `frontend/dist/` | `pnpm --dir frontend build` 生成的前端静态文件 |
| `.runtime/` | 开发服务的运行状态、日志、数据库和文件存储 |
| `.runtime/logs/` | 前端、后端、网关和 PostgreSQL 的本地运行日志 |
| `.runtime/pg-packages/`、`.runtime/pgsql/` | PostgreSQL 软件包及解压后的运行文件 |
| `.runtime/postgres-data/`、`.runtime/pg-socket/` | 本地 PostgreSQL 数据和套接字目录 |
| `.runtime/gateway.sqlite`、`.runtime/hardware.sqlite` | 本机模拟网关的命令与结果记录，以及真机模式下上位机使用的独立数据库 |
| `.runtime/data/`、`.runtime/hardware-data/` | 按运行模式隔离的后端文件存储目录 |
| `artifacts/` | 本地交付产物。`artifacts/validation/` 存放验证脚本生成的报告样例、证据图、截图和结果记录 |
| `docs/` | 本地文档和交付材料。随仓库发布的现场文档位于 [`robot-gateway/`](robot-gateway/README.md) 和 [`scripts/deploy/`](scripts/deploy/README.md) |

## 本地启动

需要 Python 3.12（含 `venv`）、Node.js 24.12 或同系列更高版本、pnpm、curl、dpkg-deb，以及 WeasyPrint 所需的 Pango 和中文字体。

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-validation.lock.txt
.venv/bin/python -m pip install --no-deps --no-build-isolation -e ./robot-gateway -e ./backend
pnpm --dir frontend install --frozen-lockfile
.venv/bin/python scripts/fetch_postgres.py
.venv/bin/python scripts/dev.py start
```

启动后打开 [巡检验证台](http://127.0.0.1:5173)，也可访问 [API 文档](http://127.0.0.1:8000/docs)。在「手动遥控」面板申请控制权，按住方向按钮移动模拟机器人，松开按钮停步。操作结束后释放控制权。巡检任务与手动遥控互斥。

默认配置用于单机开发。API、前端和网关监听回环地址。模拟模式的页面使用本机模拟网关，现场链路接口仅在真机模式开放。数据库脚本根据 `/etc/os-release` 中的发行版代号选择 PostgreSQL 17 软件包源，下载并解压至 `.runtime/pgsql`。数据库使用 `127.0.0.1:55432`。也可以设置 `H2_DATABASE_URL` 后用 `dev.py start --skip-db` 连接已有数据库。运行日志保存在 `.runtime/logs/`。

```bash
.venv/bin/python scripts/dev.py status
.venv/bin/python scripts/dev.py stop
.venv/bin/python scripts/manage_postgres.py stop
```

## H2 真机遥控

真机模式在 H2 的 PC3 运行宇树 SDK2 网关。x86 上位机用两条独立的 SSH 本地转发承载 gRPC，`15052 → 50052` 用于遥控，`15051 → 50051` 用于状态和遥测。停步和释放控制权优先于排队的方向指令。PC3 上已经开始的 SDK 调用会先结束。上位机使用独立的 SQLite 数据库。

真机界面顶部是链路管理，左侧固定遥控面板。页面显示机器人连接、运动服务状态、BMS 电量、PC2 温湿度和 PC3 里程计位姿。位姿来自 DDS 话题 `rt/odommodestate`，以开机点为原点，超过 2 秒未收到新读数时停止上报。三维场景的「重置视图」只改变显示基准。地图和相机窗口目前为占位。

温湿度从 PC2 采样起最多显示 8 秒，重复轮询同一条样本不会续期。部署时同时更新 PC2 代理、PC3 网关和上位机前后端。各端用本机单调时钟判断有效期，无需同步三台设备的系统时钟。

现场一键入口 `scripts/h2_field_start.sh` 负责准备 PC2、PC3 和双 SSH 转发。两条转发均通过独立进程和只读网关查询检查后，才进入上位机应用的停启步骤。转发失败会报告对应端口并保留已运行的应用。手动部署先建立两条转发，再用 `dev.py` 启动本地应用。运行中的连接恢复由操作员在链路管理区触发。

换网段时从 `H2_PC2_HOST` 指定 PC2 开发网地址，上位机探测、PC2 默认监听和 PC3 默认采样共用该配置。具体覆盖方式和三端重启步骤见[部署指南](scripts/deploy/README.md#换网段与启动)。

- [设计方案](robot-gateway/H2_DESIGN.md)说明遥控优先级、指令有效期和温湿度采样年龄如何传递。
- [三端部署说明](robot-gateway/H2_COMMISSIONING.md)包含 PC2 代理、PC3 只读探测与网关、双 SSH 转发及上位机启动命令。
- [现场验收清单](robot-gateway/H2_ACCEPTANCE_CHECKLIST.md)包含通道隔离、温湿度过期、实际动作、停步与断链的验收判据。
- [三机部署脚本说明](scripts/deploy/README.md)列出各机环境准备脚本、启动顺序及运行期恢复方式。

## 运行验证

```bash
.venv/bin/python -m pytest -q backend/tests robot-gateway/tests tests
pnpm --dir frontend typecheck
pnpm --dir frontend build
```

Python 回归测试默认使用临时 SQLite 数据库。要验证 PostgreSQL，可通过 `H2_TEST_DATABASE_URL` 指向专用测试库，测试会清理该库的验证表。以默认 PostgreSQL 配置启动模拟服务后，可以运行 `.venv/bin/python scripts/validate_live.py` 验证巡检链路。

浏览器验证需要 Playwright 与 Chrome 或 Chromium。将 Playwright 安装到独立工具目录，通过 `H2_NODE_MODULES` 指定该目录下的 `node_modules`，通过 `H2_CHROME_PATH` 指定浏览器可执行文件，再运行 `node scripts/browser_validate.mjs`。

回归测试覆盖模拟巡检、人工复核、报告导出和遥控，以及本机回环下的双通道隔离、迟到与取消指令、停步优先和温湿度防续期。实际动作、端到端延迟和停稳时间需现场验收。
