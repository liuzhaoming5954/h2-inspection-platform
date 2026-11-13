# H2 巡检上位机

面向 x86_64 Ubuntu 24.04 的巡检与遥控技术验证项目。默认用模拟机器人和合成数据运行巡检、人工复核、报告导出和遥控流程。H2 真机模式支持前进、后退、左右转向和停步。

| 目录 | 内容 |
| --- | --- |
| `frontend/` | Vue 3、TypeScript、Element Plus、Three.js、ECharts 验证台 |
| `backend/` | FastAPI 业务 API、PostgreSQL 持久化、复核与 PDF、Excel、JSON 导出 |
| `robot-gateway/` | gRPC 网关、模拟巡检和宇树 SDK2 高层遥控 |
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

默认配置用于单机开发。API、前端和网关监听回环地址。数据库脚本下载 PostgreSQL 17 并解压至 `.runtime/pgsql`，数据库使用 `127.0.0.1:55432`。也可以设置 `H2_DATABASE_URL` 后用 `dev.py start --skip-db` 连接已有数据库。运行日志保存在 `.runtime/logs/`。

```bash
.venv/bin/python scripts/dev.py status
.venv/bin/python scripts/dev.py stop
.venv/bin/python scripts/manage_postgres.py stop
```

## H2 真机遥控

真机模式使用宇树 SDK2。启动器使用独立 SQLite 数据库。本机回环测试覆盖方向指令、停步请求和会话断开。实际动作和停稳需现场验收。

## 运行验证

```bash
.venv/bin/python -m pytest -q backend/tests robot-gateway/tests tests
pnpm --dir frontend typecheck
pnpm --dir frontend build
```

Python 回归测试默认使用临时 SQLite 数据库。要验证 PostgreSQL，可通过 `H2_TEST_DATABASE_URL` 指向专用测试库，测试会清理该库的验证表。启动本地服务后可以运行 `.venv/bin/python scripts/validate_live.py` 验证巡检链路。

浏览器验证需要 Playwright 与 Chrome 或 Chromium。将 Playwright 安装到独立工具目录，通过 `H2_NODE_MODULES` 指定该目录下的 `node_modules`，通过 `H2_CHROME_PATH` 指定浏览器可执行文件，再运行 `node scripts/browser_validate.mjs`。
