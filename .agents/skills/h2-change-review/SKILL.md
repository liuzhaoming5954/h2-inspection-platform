---
name: h2-change-review
description: 审查 H2 巡检上位机的提交、PR 或本地改动，检查遥控、遥测、跨端接口和部署变更，并按差异选择模拟回归。
---

# H2 变更审查

## 确定审查范围

按用户指定的提交、分支差异、PR 或工作区改动确定审查基线。结合差异与调用方，判断哪些变化影响上位机、PC2、PC3 或模拟器。保持只读，按改动选择下列检查项。

## 按影响检查

| 改动范围 | 审查重点 | 可选的本地验证 |
| --- | --- | --- |
| 遥控、网关、SSH 转发 | 顺着浏览器 WebSocket、后端、PC3 网关核对状态和遥控通道是否独立。检查状态阻塞、松手、停步、释放、帧过期和断链是否仍按预期处理。检查待发送帧的优先级和 PC3 单调时钟截止时间 | `tests/test_teleop_end_to_end.py`、`tests/test_h2_hardware_end_to_end.py`、`backend/tests/test_teleop.py`、`robot-gateway/tests/test_hardware.py` |
| PC2 温湿度、BMS、位姿 | 检查温湿度是否以 PC2 原始采样年龄限制在 8 秒内，同一样本重复读取时是否保留原截止时间。核对读数过期或缺失时是否清除页面旧值和「实测」标记，核对位姿坐标和三维视图重置行为 | `robot-gateway/tests/test_hardware_environment.py`、`robot-gateway/tests/test_hardware_battery.py`、`robot-gateway/tests/test_hardware_pose.py` |
| Protobuf 或 API | 对照 `.proto`、生成的 Python 桩、网关映射、HTTP 字段和前端类型。检查选填字段、单位、旧数据兼容性，指出哪些机器需要同步更新 | `robot-gateway/tests/test_grpc.py`、`backend/tests/test_api.py`、`pnpm --dir frontend build` |
| 巡检、复核、报告或页面布局 | 检查重复下发和机器人状态同步、巡检与遥控互斥、复核及报告导出。真机遥控页布局有变化时，用模拟响应检查 1440×820 下链路横条、控制权、四个方向和停步是否首屏可见 | `backend/tests/test_api.py`、`tests/test_teleop_end_to_end.py`、前端构建与本地浏览器 |
| 启动脚本和部署 | 核对模拟模式隔离、现场编排是否只由一键脚本触发，以及任一转发失败时上位机是否继续运行。检查各机启动参数、进程复用、转发和配置是否与受影响的 README、设计、联调和验收文档一致 | `tests/test_external_gateway_launcher.py`、`tests/test_h2_field_start.py`、对改过的 Shell 脚本运行 `bash -n` |

## 本地验证与报告

- 从仓库根目录按需用 `env -u H2_TEST_DATABASE_URL -u H2_TEST_SDK_DDS .venv/bin/python -m pytest -q <选中的测试>` 运行回归。`test_teleop_end_to_end.py` 使用模拟网关，`test_h2_hardware_end_to_end.py` 使用 SDK 替身，两者都使用本机回环和临时数据库。
- 审查验证限定在模拟器、本机回环和测试替身。`dev.py start` 的真机模式选项、现场启停脚本和界面链路恢复按钮会操作现场链路。真机动作、停稳和现场断链结论引用实际验收记录。
- 审查历史提交或对照基线时，在各自的独立快照验证，确认测试导入对应版本的项目代码。看到失败时，以同等环境复跑对应测试，区分新增回归、既有失败和环境限制。若无法复现基线，标明归因尚未确认。
- 按重要程度列出问题，每项给出文件行号、触发条件和影响。简要记录用到的验证及覆盖范围，标出需更新的部署端和受影响文档。没有发现问题时，说明已检查的范围和仍需的现场证据。
