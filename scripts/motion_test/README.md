# H2 机器人功能测试台（motion bench）

逐条实测 `/home/sia/unitree_H2_docs` 文档声明的能力：话题名、IDL 类型、字段名、
参数格式一律以文档为准，调用方式照搬文档例程（初始化顺序、超时值、示例参数）。
与语音测试台（`scripts/voice_test/`）同款形态：PC3 上的单文件 Python 服务 +
单页 Web 界面，不依赖外部前端库。

## 覆盖范围

| 文档 | 测试项 |
| --- | --- |
| 04/02/01 RPC例程 | LocoClient 全部命令（FSM/速度/Move/查询），例程值做按钮默认值 |
| 04/02/02 高层运动服务接口 | 普通接口 + 专家接口全部 RPC；模式 ID 表；Move 持续语义开关 |
| 04/02/03 里程计服务接口 | rt/odommodestate(500Hz) + rt/lf/odommodestate(20Hz) 双话题、五组字段、行走跟踪录制分析 |
| 04/01/02 底层服务接口 | rt/lowstate 只读订阅：IMUState_、MotorState_[35]、wireless_remote |
| 04/01/01 关节电机顺序 | 35 槽位命名 + 限位核对（电机表超限标红） |
| 04/01/03 遥控器数据结构 | 40 字节按 xRockerBtnDataStruct 解码（摇杆/按键/L2 实时显示） |
| 05/02 音频灯光服务接口 | AudioClient（TtsMaker/音量/PlayStop/LedControl）、rt/audio_msg、麦克风组播 239.168.123.161:5555 |
| 05/04 多媒体服务接口 | VideoClient.GetImageSample() JPEG 拍照 |

未纳入项（原因见页面「覆盖清单」标签页）：rt/lowcmd 发布与 rt/arm_sdk 上肢
控制（主动控制，需调试模式/运控模式配合，风险高）、h264 图传（需 gstreamer
接收端）、双目推流（需手机端配合）、GMSL（H2Plus 专属）、ROS2 例程。

## 部署与启动（PC3）

```bash
# 工作站：部署到 PC3
sshpass -e scp scripts/motion_test/{motion_bench.py,page.html} \
  unitree@<PC3>:~/motion_bench/

# PC3：用网关 venv（内含 unitree_sdk2py 与 CycloneDDS）启动
mkdir -p ~/motion_bench
cd ~/h2-inspection-platform && \
CYCLONEDDS_HOME=$PWD/.runtime/h2-sdk2/cyclonedds-install \
nohup .venv/bin/python -u ~/motion_bench/motion_bench.py \
  > ~/motion_bench/bench.log 2>&1 &
```

页面：`http://<PC3-IP>:8110/`。环境变量：`H2_BENCH_PORT`（默认 8110）、
`H2_BENCH_IFACE`（默认 eth10，即 PC3 与运控通信的内部网口）。

## 使用注意

- 运动指令全部由操作员在页面亲手触发；危险动作（阻尼/零力矩）两段式确认。
- `Move(continous_move=true)` 与 `SetVelocity` 由测试台看护线程在时限到期强制
  StopMove（页面可关闭看护以复现文档原生持续语义）。
- 网关（h2_gateway）同时运行时，第二个客户端的 SetVelocity 可能不被运控服务
  应答（2026-11-22 实测）；页面顶栏有提示，测运动接口前先停网关。
- IMU 静态测试 / 行走录制分析 / 轨迹清零等结论建议随手记入页面「覆盖清单」
  标签页的结论记录（存浏览器 localStorage，可导出 Markdown）。

## 已知实测结论（2026-11-22，测试台前身脚本）

- 本机 State Estimator 不积分 SDK 指令驱动的行走：3m 实走 position 仅 0.33m，
  速度符号随步态交替；官方遥控器行走也只有约 1/3 尺度。文档要求
  State Estimator ≥ 2.1.0.12，疑似内置版本更低，需联系宇树技术支持确认。

## 本地验证记录

- 已知代价：sdk2py 在 Python 侧解码 rt/lowstate（800Hz × 35 电机 IDL）固有地
  占用约一个核；测试台已做「每 5 帧解包 1 帧」抽稀（显示 160Hz，频率统计仍
  按全帧），实测该负载下 RPC 应答与页面响应正常（0.1s 级）。
- 机器人关机期间在工作站用桩模块替代 unitree_sdk2py 做了 HTTP 层冒烟
  （19 项：页面/状态序列化/RPC 白名单与限幅/看护布置与到期触发/急停/
  录制分析空数据路径/IMU 测试快速失败/拍照与音频不可用路径），以及
  解包层单元验证（里程计五组字段、LowState 35 电机、遥控器 40 字节按
  `<2sH5f16s` 解码、ASR JSON、关节限位表）。
- 真机侧验证（订阅频率、RPC 真实回执、TTS/LED/拍照可用性）待机器人开机
  后在 PC3 上进行，结论记入页面「覆盖清单」标签页。
