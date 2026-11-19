# 三机部署指南（H2 真机遥控链路）

在各机准备系统工具后，从同一版本的仓库安装项目依赖。现场地址和 SSH 凭据通过环境变量提供。

## 链路结构与部署顺序

```
[上位机] ──双 SSH 转发──> [PC3 网关] ──DDS──> [H2 运控]
[上位机] ──HTTP 探测──> [PC2 传感器代理]
[PC3 网关] ──HTTP 采样──> [PC2 传感器代理] ──RS-485──> [温湿度传感器]
```

按 **PC2 → PC3 → 上位机** 顺序部署。

## 各机部署

### PC2（传感器代理）
```bash
# 仓库任意方式放到 PC2 的 ~/h2-inspection-platform（git clone 推荐）
bash ~/h2-inspection-platform/scripts/deploy/setup_pc2.sh
```
脚本把 `pyserial` 安装到 `~/pylibs`，把代理复制到 `~/sensor_agent.py`，并列出本机串口。串口默认 `/dev/ttyACM0`（机身接口 10 的 RS-485），如有差异，在部署前调整代理脚本顶部的 `PORT`。

代理同时提供三个接口：`GET /environment` 返回温湿度采样；`GET /vision` 解析头部相机视觉程序（`ob_callback`，位于 `~/OrbbecSDK_v2.10.6_202609291004_575347c_linux_x86_64/examples/build`）追加写入的 `detect_log.txt`，返回最新一帧的人数、红绿灯、二维码和中心深度；`GET /vision/frame` 返回该程序每 500ms 原子写入 `/dev/shm/latest_annot.png` 的带检测框预览图（该机 OpenCV 未编入 JPEG 编码器，故为 PNG），响应头 `X-Frame-Age-Ms` 给出帧年龄。视觉程序支持 `OB_HEADLESS=1` 无窗口运行（SSH 常驻），界面按 8 秒采样年龄判断其是否在运行。日志与预览路径分别可用 `H2_VISION_LOG`、`H2_VISION_FRAME` 覆盖，SDK 升级导致路径变化时同步修改并重新部署。

### PC3（遥控网关）

准备 Python 3.12 或更高版本、`venv` 和该版本的开发头文件、Git、CMake 3.16 或更高版本、C 编译器和构建工具。首次安装需要能下载源码和 Python 依赖。

```bash
bash ~/h2-inspection-platform/scripts/deploy/setup_pc3.sh
```

脚本把 CycloneDDS 0.10.2 准备到 `.runtime/h2-sdk2/cyclonedds-install`，使用该目录编译 Python 绑定，并安装 NumPy、OpenCV headless、本仓库的 `h2_gateway` 和固定版本的宇树 SDK2。最后检查 Python 绑定使用的库路径和模块是否能导入。网关启动由一键脚本或现场手动步骤执行。[CycloneDDS 库加载顺序](https://github.com/eclipse-cyclonedds/cyclonedds-python/blob/0.10.2/cyclonedds/internal.py)

### 工作站（上位机 + 编排）

准备 Python 3.12（含 `venv`）、Node.js 24.12 至 24.x、pnpm、Pango 和中文字体。现场启动需要 `curl`、OpenSSH 客户端、`sshpass` 和 `iproute2`。

```bash
bash scripts/deploy/setup_workstation.sh
```

脚本使用项目的 Python 锁定依赖和前端锁文件，安装本仓库的 `h2_backend` 和 `h2_gateway`，检查 Python 依赖和模块导入后生成 `~/h2-start.sh`。启动脚本继承当前终端的配置。

## 换网段与启动

以下以 PC2 为 `192.168.50.162`、PC3 为 `192.168.50.163` 为例。在**上位机**终端按现场实际地址设置后启动。一键脚本会将 PC2 的监听配置传给 PC2，将采样地址传给 PC3。

```bash
export H2_PC2_HOST=192.168.50.162
export H2_PC3_HOST=192.168.50.163
read -rsp '机器人 PC 的 SSH 密码：' H2_FIELD_PASSWORD
printf '\n'
export H2_FIELD_PASSWORD
~/h2-start.sh
```

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `H2_PC2_HOST` | `192.168.124.162` | 上位机访问 PC2 的 SSH 和 HTTP 地址，也用作 PC2 默认监听地址和 PC3 默认采样主机 |
| `H2_PC2_BIND` | `H2_PC2_HOST` | PC2 监听的本机地址。访问地址与本机网卡地址不同时单独设置 |
| `H2_SENSOR_AGENT_URL` | `http://$H2_PC2_HOST:8090/environment` | PC3 采样 URL。PC3 通过另一个地址访问 PC2 时可单独设置 |
| `H2_PC3_HOST` | `192.168.124.163` | 上位机连接 PC3 的开发网地址 |
| `H2_FIELD_PASSWORD` | 由现场账号提供 | 当前终端和上位机进程使用的 SSH 凭据 |
| `H2_GATEWAY_ADDRESS` | `127.0.0.1:15051`（一键启动） | 由启动器按网关参数写入，供后端连接状态通道 |
| `H2_TELEOP_GATEWAY_ADDRESS` | `127.0.0.1:15052`（一键启动） | 由启动器按网关参数写入，供后端连接遥控通道 |

手动部署时分别在 PC2、PC3 和上位机配置 `H2_PC2_HOST`。如需在界面恢复现场进程，在启动上位机前从同一终端导出 `H2_FIELD_PASSWORD` 和现场实际使用的 `H2_PC2_BIND`、`H2_SENSOR_AGENT_URL` 覆盖项。更换地址前按各端原部署方式停止旧进程，使用新配置重启。若 PC3 地址也变了，先清理指向旧 PC3 的转发。一键启动会复用已运行的代理和网关，进程启动时读取配置。[三端手动部署](../../robot-gateway/H2_COMMISSIONING.md)

## 运行期规则（重要）

- **一键启动**使用 `~/h2-start.sh` 或 `scripts/h2_field_start.sh`。脚本负责准备 PC2 代理、PC3 网关和两条 SSH 转发。每条转发都须由独立进程监听，并能收到 PC3 网关 `GetState` 的只读应答。任一路失败时显示对应端口并退出，已运行的上位机应用继续运行。`--robot-only` 在这两项检查完成后退出，上位机应用由调用方管理。
- **手动启动**先按[现场联调说明](../../robot-gateway/H2_COMMISSIONING.md)启动 PC2、PC3 并建立两条 SSH 转发，再用 `dev.py start --external-gateway … --external-teleop-gateway …` 启动上位机。本地启动器直接使用给定的网关地址。
- **运行中断线**：**永远手动**——用页面上「链路管理」的按钮（重连转发 / 启动 PC2 代理 / 启动 PC3 网关）恢复，系统绝不自动重连。断线恢复前必须经过操作员确认
- **收工**：`scripts/h2_field_stop.sh`（停上位机与转发，机器人侧进程保留复用）

## 故障排查

- 页面链路灯全红、PC2/PC3 ping 得通 → 多半是 `H2_FIELD_PASSWORD` 未设或转发进程残留，点「重连转发」
- 只 PC3 网关红灯 → 在工作站 `ssh unitree@$H2_PC3_HOST 'tail -20 ~/h2-inspection-platform/gateway.log'` 看网关日志
- 遥控就绪但温湿度空 → PC2 代理灯是否绿；绿则查 `~/sensor_agent.log`
