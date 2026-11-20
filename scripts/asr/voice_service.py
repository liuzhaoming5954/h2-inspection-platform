#!/usr/bin/env python3
"""H2 语音服务（PC3 上以网关 venv 运行）：关键词应答 + HTTP 播报接口。

上位机演示编排的播报出口。两条职责共用一个扬声器互斥锁：
- HTTP :8099
    GET  /health        存活探测（上位机拉起判据）
    POST /say {"text"}  请求机器人本体扬声器播报指定文本
- 关键词应答线程：轮询同机 asr_service 的 /asr/text，命中意图后播报
  （沿用原 voice_reply 的行为，现场按需在 INTENTS 扩展）

初始化需复用 h2_gateway.hardware 的 CycloneDDS Tracing 规避，否则
CycloneDDS 0.10.2 构建在 Ubuntu 24.04 上缓冲区溢出。
运行方式见同目录 README；与 PC3 网关进程并行，互不影响。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.etree import ElementTree

ASR_TEXT_URL = "http://127.0.0.1:8098/asr/text"
POLL_SECONDS = 1.0
REPLY_COOLDOWN = 8.0
HTTP_PORT = 8099

# 意图规则：转写文本包含任一关键词即应答（现场按需在此扩展）。
# 注意：应答文本应避开演示触发词「开始」，防止机器人自听触发运动。
INTENTS = [
    {"keywords": ["介绍", "自己", "你是谁", "名字"],
     "reply": "我是中核404研发的数据中心巡检机器人。"},
]

_speak_lock = threading.Lock()   # 串行化 TtsMaker，避免两条播报抢扬声器
_client = None                   # 主线程初始化后供各线程复用


def build_tts_client():
    from unitree_sdk2py.core import channel
    # 摘除 DDS 配置的 Tracing 段再初始化（详见 h2_gateway.hardware 同款注释）
    original = channel.ChannelConfigHasInterface
    config = ElementTree.fromstring(original)
    for entry in config.findall("./Domain"):
        tracing = entry.find("Tracing")
        if tracing is not None:
            entry.remove(tracing)
    channel.ChannelConfigHasInterface = ElementTree.tostring(config, encoding="unicode")
    try:
        channel.ChannelFactoryInitialize(0, "eth10")
    finally:
        channel.ChannelConfigHasInterface = original
    from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
    client = AudioClient()
    client.Init()
    return client


TTS_SECONDS_PER_CHAR = 0.22   # 中文播报语速估算（约 4.5 字/秒），2026-11-20 现场标定


def speak(text: str) -> int:
    """播报一段文本并等待播完，返回 TtsMaker 回执码（0 为成功）。

    TtsMaker 即发即回（实测 10ms），调用方无从得知播报时长；这里按字数
    估算持锁等待，使 /say 具有阻塞到播完的语义——上位机据此在播报期间
    暂停采音，避免机器人自听挤占识别队列。"""
    import time as _time
    with _speak_lock:
        code = _client.TtsMaker(text, 0)
        print(f"[voice] 播报 code={code}: {text[:60]}", flush=True)
        if code == 0:
            _time.sleep(min(30.0, TTS_SECONDS_PER_CHAR * len(text) + 1.0))
        return code


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:   # noqa: N802 - http.server 接口命名
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - http.server 接口命名
        if self.path != "/say":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            text = str(payload.get("text", "")).strip()
        except Exception:
            self.send_error(400)
            return
        if not text or len(text) > 200:
            body = json.dumps({"ok": False, "error": "text 需为 1–200 字"}).encode()
            self.send_response(400)
        else:
            code = speak(text)
            body = json.dumps({"ok": code == 0, "code": code}).encode()
            self.send_response(200 if code == 0 else 500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:   # noqa: A002 - 基类签名
        # 常驻服务只保留播报日志（speak 内打印），HTTP 访问日志静默
        return


def fetch_text(since: int | None) -> dict | None:
    url = ASR_TEXT_URL if since is None else f"{ASR_TEXT_URL}?since={since}"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return json.load(response)
    except Exception:
        return None


def keyword_loop() -> None:
    """关键词应答线程：与上位机演示编排共用同一份转写（接口无状态）。"""
    last_id = None
    last_reply_at = 0.0
    last_reply_text = ""
    while True:
        payload = fetch_text(last_id)
        if payload is None:
            time.sleep(2.0)   # ASR 服务未就绪时静待
            continue
        last_id = payload.get("last_id", last_id or 0)
        now = time.time()
        for item in payload.get("items", []):
            text = item.get("text", "")
            for intent in INTENTS:
                if any(keyword in text for keyword in intent["keywords"]):
                    if now - last_reply_at < REPLY_COOLDOWN and intent["reply"] == last_reply_text:
                        continue
                    print(f"[voice] {text} -> {intent['reply']}", flush=True)
                    speak(intent["reply"])
                    last_reply_at, last_reply_text = now, intent["reply"]
                    break
        time.sleep(POLL_SECONDS)


def main() -> None:
    global _client
    _client = build_tts_client()
    print("[voice] TTS 就绪", flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    threading.Thread(target=keyword_loop, daemon=True, name="voice-keywords").start()
    print(f"[voice] HTTP :{HTTP_PORT} (/health, /say)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
