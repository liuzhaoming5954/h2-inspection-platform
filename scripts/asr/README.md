# PC3 语音识别服务（Qwen3-ASR 本地闭环）

`asr_service.py` 部署在 PC3 `~/h2-asr/`，以 `~/qwen3-asr-env/bin/python` 运行：
reSpeaker XVF3800 四麦阵列采集 → 能量 VAD 切段 → Qwen3-ASR-1.7B 本地转写 → HTTP :8098。
采声与识别全在 PC3，上位机只经 `/api/field/asr/text` 拉取文本；模型常驻，
阀门开关只控制采音（见上位机「语音识别」面板）。

## 现场标定要点（实测 2026-11-19）

- **双路不对称**：USB 输出的 ch1 是波束语音路、ch0 近乎静默（疑似参考路），
  服务按「每窗取两路较高能量」判 VAD、按段择路送推理，勿改回双路平均
- 麦克风电平偏低（说话峰值约 0.05，硬件增益已 60/60 顶格），推理前做峰值归一化
- 噪声底取 6 秒滑动窗的 20 百分位（`FLOOR_WINDOWS`），语音窗不参与学习，
  防止门限追着说话声上涨
- `GET :8098/asr/debug` 提供窗能量分布与 VAD 事件，调门限先看它

## 手动操作

```bash
# 启动（模型加载约 5 秒）
cd ~/h2-asr && nohup ~/qwen3-asr-env/bin/python asr_service.py > asr_service.log 2>&1 &
curl -X POST http://192.168.124.163:8098/asr/start   # 开始采音
curl http://192.168.124.163:8098/asr/debug            # 标定诊断
```

上位机阀门（「语音识别」面板开关）即上述 start/stop 的界面化封装，首次开阀会自动拉起服务。

## 后续增强（未做）

- reSpeaker Console / xvf_host 开启固件侧 AGC（60dB）与非平稳噪声抑制，
  可显著改善远场低电平——见 Seeed wiki（respeaker_xvf3800_introduction）
- 断句后可传上文（qwen-asr 的 context 参数）提升连续说话的准确率
