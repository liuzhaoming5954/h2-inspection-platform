# H2 巡检上位机

面向 x86_64 Ubuntu 24.04 的巡检与遥控技术验证项目。默认用模拟机器人和合成数据运行巡检、人工复核、报告导出和遥控流程。H2 真机模式支持前进、后退、左右转向和停步。

| 目录 | 内容 |
| --- | --- |
| [`frontend/`](frontend/README.md) | Vue 3、TypeScript、Element Plus、Three.js、ECharts 验证台 |
| [`backend/`](backend/README.md) | FastAPI 业务 API、PostgreSQL 持久化、复核与 PDF、Excel、JSON 导出 |
| [`robot-gateway/`](robot-gateway/README.md) | gRPC 网关、模拟巡检和宇树 SDK2 高层遥控 |
| `contracts/` | 网关 Protobuf 接口定义 |
| `scripts/` | 本地开发启动与验证脚本 |

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

默认配置用于单机开发。API、前端和网关监听回环地址。数据库脚本根据 `/etc/os-release` 中的发行版代号选择 PostgreSQL 17 软件包源，下载并解压至 `.runtime/pgsql`。数据库使用 `127.0.0.1:55432`。也可以设置 `H2_DATABASE_URL` 后用 `dev.py start --skip-db` 连接已有数据库。运行日志保存在 `.runtime/logs/`。

```bash
.venv/bin/python scripts/dev.py status
.venv/bin/python scripts/dev.py stop
.venv/bin/python scripts/manage_postgres.py stop
```

## H2 真机遥控

真机模式在 H2 的 PC3 运行宇树 SDK2 网关。x86 上位机用两条独立的 SSH 本地转发承载 gRPC，`15052 → 50052` 用于遥控，`15051 → 50051` 用于状态和遥测。停步和释放控制权优先于排队的方向指令。PC3 上已经开始的 SDK 调用会先结束。上位机使用独立的 SQLite 数据库。

真机界面显示连接、运动服务状态、BMS 电量和 PC2 温湿度，并预留地图与相机窗口。地图、真机位姿和相机画面待接入。温湿度从 PC2 采样起最多显示 8 秒，重复轮询同一条样本不会续期。部署时同时更新 PC2 代理、PC3 网关和上位机前后端。各端用本机单调时钟判断有效期，无需同步三台设备的系统时钟。

- [设计方案](robot-gateway/H2_DESIGN.md)说明遥控优先级、指令有效期和温湿度采样年龄如何传递。
- [三端部署说明](robot-gateway/H2_COMMISSIONING.md)包含 PC2 代理、PC3 只读探测与网关、双 SSH 转发及上位机启动命令。
- [现场验收清单](robot-gateway/H2_ACCEPTANCE_CHECKLIST.md)包含通道隔离、温湿度过期、实际动作、停步与断链的验收判据。

## 运行验证

```bash
.venv/bin/python -m pytest -q backend/tests robot-gateway/tests tests
pnpm --dir frontend typecheck
pnpm --dir frontend build
```

Python 回归测试默认使用临时 SQLite 数据库。要验证 PostgreSQL，可通过 `H2_TEST_DATABASE_URL` 指向专用测试库，测试会清理该库的验证表。启动本地服务后可以运行 `.venv/bin/python scripts/validate_live.py` 验证巡检链路。

浏览器验证需要 Playwright 与 Chrome 或 Chromium。将 Playwright 安装到独立工具目录，通过 `H2_NODE_MODULES` 指定该目录下的 `node_modules`，通过 `H2_CHROME_PATH` 指定浏览器可执行文件，再运行 `node scripts/browser_validate.mjs`。

回归测试覆盖模拟巡检、人工复核、报告导出和遥控，以及本机回环下的双通道隔离、迟到与取消指令、停步优先和温湿度防续期。实际动作、端到端延迟和停稳时间需现场验收。
