"""现场遥控链路管理，提供手动探测和 SSH 操作。

上位机管理三个可独立恢复的部分，分别是 PC2 传感器代理、
PC3 网关进程和两条 SSH 转发通道。
本模块的现场操作由操作员点击界面按钮触发。
每项操作都是幂等的，已运行的组件会直接返回状态。
这些操作不发送运动指令。
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import time
import urllib.request

from h2_gateway.sensor_config import (DEFAULT_PC2_HOST, pc2_agent_url, pc2_vision_frame_url,
                                       pc2_vision_url, pc3_sensor_agent_url)


PC2 = os.getenv("H2_PC2_HOST") or DEFAULT_PC2_HOST
PC3 = os.environ.get("H2_PC3_HOST", "192.168.124.163")
PC2_BIND = os.getenv("H2_PC2_BIND") or PC2
PC2_AGENT_URL = pc2_agent_url(PC2)
FORWARD_PORTS = (15051, 15052)

PC2_START = (f"H2_PC2_BIND={shlex.quote(PC2_BIND)} PYTHONPATH=$HOME/pylibs "
             "nohup python3 ~/sensor_agent.py > ~/sensor_agent.log 2>&1 < /dev/null &")
PC3_START = ("cd ~/h2-inspection-platform && CYCLONEDDS_HOME=$PWD/.runtime/h2-sdk2/cyclonedds-install "
             f"H2_SENSOR_AGENT_URL={shlex.quote(pc3_sensor_agent_url(PC2))} "
             "nohup .venv/bin/python -u -m h2_gateway.server --h2-interface eth10 "
             "--host 127.0.0.1 --port 50051 --teleop-port 50052 > gateway.log 2>&1 < /dev/null &")


class FieldError(RuntimeError):
    pass


def _password() -> str:
    password = os.environ.get("H2_FIELD_PASSWORD")
    if not password:
        raise FieldError("H2_FIELD_PASSWORD 未设置，无法执行远程操作")
    return password


def _ssh(host: str, command: str, timeout: float = 15) -> str:
    """执行一次 SSH 命令。通过 nohup 启动的进程就绪后，SSH 通道仍可能阻塞，
    调用方通过探测确认结果。"""
    result = subprocess.run(
        ["sshpass", "-p", _password(), "ssh", "-n", "-o", "ConnectTimeout=5",
         "-o", "StrictHostKeyChecking=accept-new", f"unitree@{host}", command],
        capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        message = (result.stderr or "").strip()
        raise FieldError(message[:200] or f"ssh 退出码 {result.returncode}")
    return result.stdout.strip()


def _ssh_best_effort(host: str, command: str, timeout: float = 15) -> None:
    """执行启动命令，允许 SSH 通道阻塞，以后续探测结果为准。"""
    try:
        _ssh(host, command, timeout=timeout)
    except (FieldError, subprocess.TimeoutExpired):
        pass


def probe_pc2() -> bool:
    try:
        with urllib.request.urlopen(PC2_AGENT_URL, timeout=2) as response:
            payload = json.load(response)
        return payload.get("available") is True
    except Exception:
        return False


def vision() -> dict:
    """转发 PC2 /vision 的最新一帧识别结果，供真机界面展示。

    PC2 不可达或返回异常时回 available=False，界面据此区分
    「PC2 离线」和「视觉程序未运行」（available 但 age_ms 过大）。
    该接口只读，不触发任何远程操作。
    """
    try:
        with urllib.request.urlopen(pc2_vision_url(PC2), timeout=2) as response:
            payload = json.load(response)
    except Exception as error:
        return {"available": False, "error": str(error)[:120]}
    if not isinstance(payload, dict):
        return {"available": False, "error": "视觉代理返回了非对象响应"}
    return payload


def vision_frame() -> tuple[bytes, int | None, str] | None:
    """转发 PC2 /vision/frame 的最新预览帧，返回 (图像字节, 采样年龄毫秒, Content-Type)。

    PC2 不可达或尚无帧时返回 None，由路由折成 503，
    界面据此保留上一帧并提示画面来源离线。
    """
    try:
        with urllib.request.urlopen(pc2_vision_frame_url(PC2), timeout=2) as response:
            data = response.read()
            age_header = response.headers.get("X-Frame-Age-Ms")
            content_type = response.headers.get("Content-Type")
    except Exception:
        return None
    if not data:
        return None
    # 年龄头由 PC2 写入，缺失时返回 None，界面不显示年龄而不是显示错误值。
    age_ms = int(age_header) if age_header is not None and age_header.lstrip("-").isdigit() else None
    # 类型以 PC2 声明为准，缺失或异常时按文件魔数兜底，保证前端拿到正确 MIME。
    if not isinstance(content_type, str) or not content_type.startswith("image/"):
        content_type = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
    return data, age_ms, content_type


def port_listening(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def forwards_status() -> dict[str, bool]:
    return {str(port): port_listening(port) for port in FORWARD_PORTS}


def status(gateway_connected: bool) -> dict:
    """提供界面所需的链路状态。PC3 网关监听 PC3 的回环地址，
    上位机通过两条转发的状态以及后端自身的网关连接
    判断网关是否可达。"""
    forwards = forwards_status()
    return {
        "pc2_agent": probe_pc2(),
        "forwards": forwards,
        "gateway": gateway_connected and all(forwards.values()),
    }


def reconnect_forwards() -> dict:
    """终止异常的转发监听进程并重建两条隧道。"""
    password = _password()
    # 匹配范围仅限两条转发的 ssh 命令行。
    subprocess.run(["pkill", "-f", r"127\.0\.0\.1:1505[12]:127\.0\.0\.1:5005[12]"],
                   capture_output=True)
    time.sleep(0.5)
    for port, target in ((15051, 50051), (15052, 50052)):
        subprocess.run(
            ["sshpass", "-p", password, "ssh", "-N", "-T", "-f", "-S", "none",
             "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=5",
             "-o", "ServerAliveCountMax=2",
             "-L", f"127.0.0.1:{port}:127.0.0.1:{target}", f"unitree@{PC3}"],
            capture_output=True, text=True, timeout=20)
    time.sleep(1.5)
    forwards = forwards_status()
    return {"ok": all(forwards.values()), "forwards": forwards}


def start_pc2_agent() -> dict:
    running = _ssh(PC2, 'pgrep -f "[s]ensor_agent.py" >/dev/null && echo running || echo stopped')
    if running == "running":
        return {"ok": True, "already": True}
    _ssh_best_effort(PC2, PC2_START)
    time.sleep(2.5)
    return {"ok": probe_pc2(), "already": False}


def start_pc3_gateway() -> dict:
    running = _ssh(PC3, 'pgrep -f "[h]2_gateway.server" >/dev/null && echo running || echo stopped')
    if running == "running":
        return {"ok": True, "already": True}
    _ssh_best_effort(PC3, PC3_START, timeout=20)
    time.sleep(3.5)
    ports = _ssh(PC3, "ss -tln | grep -c '5005[12]'")
    return {"ok": ports == "2", "already": False}
