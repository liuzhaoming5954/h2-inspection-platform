# 三机部署指南（H2 真机遥控链路）

> 目标：在任何一台新电脑上，拉取本仓库即可完成整条链路的部署。
> 原则：**仓库是唯一部署源**；机器特定信息走环境变量；凭据永不入库。

## 链路结构与部署顺序

```
[工作站] ──SSH转发(15051/15052)──> [PC3 网关] ──DDS──> [H2 运控]
    │                                 │
    └──HTTP──> [PC2 传感器代理] <──RS-485── 温湿度传感器（接 PC2 /dev/ttyACM0）
```

按 **PC2 → PC3 → 工作站** 顺序部署。三台机器的默认地址：PC2 `192.168.124.162`、PC3 `192.168.124.163`（不同网段用环境变量覆盖，见下）。

## 各机部署

### PC2（传感器代理）
```bash
# 仓库任意方式放到 PC2 的 ~/h2-inspection-platform（git clone 推荐）
bash ~/h2-inspection-platform/scripts/deploy/setup_pc2.sh
```
做三件事：装 pyserial（`~/pylibs`）、把仓库的 `scripts/sensor_agent_pc2.py` 安装为 `~/sensor_agent.py`、探针验证。串口默认 `/dev/ttyACM0`（机身接口 10 的 RS-485），现场不同改脚本顶部 `PORT`。

### PC3（遥控网关）
```bash
bash ~/h2-inspection-platform/scripts/deploy/setup_pc3.sh
```
指引安装 Python 3.12+、宇树 SDK2（pinned 版本）与 CycloneDDS 0.10.2（仓库内构建，`-DBUILD_DDSPERF=OFF`），并验证 `.venv` 与 `h2_gateway` 可导入。网关进程本身由工作站侧拉起，无需在 PC3 手动启动。

### 工作站（上位机 + 编排）
```bash
bash scripts/deploy/setup_workstation.sh   # 装 Python 依赖、前端依赖、生成 ~/h2-start.sh 包装器
export H2_FIELD_PASSWORD='<机器人PC密码>'   # 当前会话或写入个人 shell 配置（勿入库）
~/h2-start.sh                              # 一键起链：PC2 代理 → PC3 网关 → 双转发 → 上位机栈 → 自检
```

## 环境变量一览

| 变量 | 默认值 | 用途 |
|---|---|---|
| `H2_FIELD_PASSWORD` | （无，必设） | SSH 到 PC2/PC3 的密码 |
| `H2_PC2_HOST` | `192.168.124.162` | PC2 开发网地址 |
| `H2_PC3_HOST` | `192.168.124.163` | PC3 开发网地址 |
| `H2_GATEWAY_ADDRESS` | `127.0.0.1:15051` | 后端连网关的目标 |
| `H2_TELEOP_GATEWAY_ADDRESS` | `127.0.0.1:15052` | 后端连遥控通道的目标 |

## 运行期规则（重要）

- **一键启动**使用 `~/h2-start.sh` 或 `scripts/h2_field_start.sh`。脚本负责准备 PC2 代理、PC3 网关和两条 SSH 转发。每条转发都须由独立进程监听，并能收到 PC3 网关 `GetState` 的只读应答。任一路失败时显示对应端口并退出，已运行的上位机应用继续运行。`--robot-only` 在这两项检查完成后退出，上位机应用由调用方管理。
- **手动启动**先按[现场联调说明](../../robot-gateway/H2_COMMISSIONING.md)启动 PC2、PC3 并建立两条 SSH 转发，再用 `dev.py start --external-gateway … --external-teleop-gateway …` 启动上位机。本地启动器直接使用给定的网关地址。
- **运行中断线**：**永远手动**——用页面上「链路管理」的按钮（重连转发 / 启动 PC2 代理 / 启动 PC3 网关）恢复，系统绝不自动重连。断线恢复前必须经过操作员确认
- **收工**：`scripts/h2_field_stop.sh`（停上位机与转发，机器人侧进程保留复用）

## 故障排查

- 页面链路灯全红、PC2/PC3 ping 得通 → 多半是 `H2_FIELD_PASSWORD` 未设或转发进程残留，点「重连转发」
- 只 PC3 网关红灯 → 在工作站 `ssh unitree@$H2_PC3_HOST 'tail -20 ~/h2-inspection-platform/gateway.log'` 看网关日志
- 遥控就绪但温湿度空 → PC2 代理灯是否绿；绿则查 `~/sensor_agent.log`
