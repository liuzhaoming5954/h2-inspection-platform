"""现场遥控链路管理，提供手动探测和 SSH 操作。

上位机管理三个可独立恢复的部分，分别是 PC2 传感器代理、
PC3 网关进程和两条 SSH 转发通道。
本模块的现场操作由操作员点击界面按钮触发。
每项操作都是幂等的，已运行的组件会直接返回状态。
这些操作不发送运动指令。
"""

from __future__ import annotations

import contextlib
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
             # 2026-11-20 实测：前进阈值在 0.4–0.5 之间（0.4 不抬脚），转向阈值恰在
             # 0.55 附近（手动按住偶尔也只扭腰不迈步），现场反馈 1.0 偏快，取 0.7。
             "H2_V_FORWARD_SLOW=0.5 H2_V_BACKWARD_SLOW=0.5 H2_V_TURN=0.7 H2_V_TURN_SLOW=0.7 "
             "nohup .venv/bin/python -u -m h2_gateway.server --h2-interface eth10 "
             "--host 127.0.0.1 --port 50051 --teleop-port 50052 > gateway.log 2>&1 < /dev/null &")

# 数据源程序（区别于链路进程）：视觉程序在 PC2，建图栈在 PC3 容器。
# 打开数据阀门时若程序未运行则按需拉起；关闭阀门从不停止这些程序。
PC2_VISION_DIR = "~/OrbbecSDK_v2.10.6_202609291004_575347c_linux_x86_64/examples/build"
PC2_VISION_START = (f"cd {PC2_VISION_DIR} && OB_HEADLESS=1 nohup ./bin/ob_callback"
                    " > ob_headless.log 2>&1 < /dev/null &")
PC3_ROS_ENV = "source /opt/ros/noetic/setup.bash && source /workspace/G1Nav2D/devel/setup.bash"
# 语音识别服务（PC3 本地闭环：reSpeaker 采集 + Qwen3-ASR 转写，模型常驻）
PC3_ASR_START = ("cd ~/h2-asr && nohup ~/qwen3-asr-env/bin/python asr_service.py"
                 " > asr_service.log 2>&1 < /dev/null &")
# 语音播报服务（PC3 网关 venv：需要 CycloneDDS 与 unitree_sdk2py 驱动机器人扬声器，
# 与网关进程并行运行互不影响；HTTP :8099 提供 /say 与 /health）
PC3_VOICE_START = ("cd ~/h2-inspection-platform"
                   " && CYCLONEDDS_HOME=$PWD/.runtime/h2-sdk2/cyclonedds-install"
                   " nohup .venv/bin/python ~/h2-asr/voice_service.py"
                   " > ~/h2-asr/voice_service.log 2>&1 < /dev/null &")

# 演示前清零建图栈（2026-11-23 夜间运维结论固化为自动动作）：
# 桥的点云累积在行走建图时增长极快，/pose 间歇停顿会拖累 livox 反馈采样；
# 容器内的旧桥是 root 进程，宿主机 pkill 杀不掉，重启容器是唯一可靠手段。
# 就绪由调用方的位姿探测兜底（演示的 90 秒就绪时限覆盖重启全程 ~40s）。
PC3_MAPPING_RESTART = [
    "docker restart ros-noetic-dev",
    (f"docker exec -d ros-noetic-dev bash -c '{PC3_ROS_ENV}"
     " && roslaunch /workspace/h2_extra/h2_mapping_headless.launch"
     " > /tmp/mapping.log 2>&1'"),
    (f"docker exec -d ros-noetic-dev bash -c '{PC3_ROS_ENV}"
     " && python3 /workspace/h2_extra/h2_map_bridge.py"
     " > /tmp/bridge.log 2>&1'"),
]


def restart_mapping_stack() -> dict:
    """重启 PC3 建图容器并重拉建图栈与数据桥（见 PC3_MAPPING_RESTART 注释）。

    尽力而为：失败不抛出（旧栈可能仍在跑，演示照常），返回 ok=False 附原因；
    成功后需等待约 25~40 秒位姿才恢复，由调用方的就绪轮询覆盖。"""
    try:
        _ssh(PC3, PC3_MAPPING_RESTART[0], timeout=45)
    except (FieldError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "error": str(error)[:160]}
    time.sleep(2)
    for command in PC3_MAPPING_RESTART[1:]:
        _ssh_best_effort(PC3, command, timeout=15)
    return {"ok": True}

# 数据接入阀门：只存在于后端进程内存。相机/雷达默认开（显示是常态），
# 语音默认关（主动使用型功能，开机不自动采音），重启回到此默认。
DATA_VALVES = {"vision": True, "mapping": True, "asr": False}


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


def map_data(kind: str) -> tuple[bytes, str, dict[str, str]] | None:
    """转发 PC3 建图桥（h2_map_bridge，容器 host 网络 :8097）的数据。

    kind 对应桥侧 /map（降采样点云，二进制 int16 三元组）、/pose（JSON 位姿）
    和 /path（轨迹折线）。桥未随建图启动或 PC3 不可达时返回 None，
    由路由折成 503。点云单帧可达 MB 级，超时放宽到 5 秒。
    """
    try:
        with urllib.request.urlopen(f"http://{PC3}:8097/{kind}", timeout=5) as response:
            data = response.read()
            content_type = response.headers.get("Content-Type") or "application/octet-stream"
            extra = {key: value for key, value in response.headers.items()
                     if key.startswith("X-Map-")}
    except Exception:
        return None
    if not data:
        return None
    return data, content_type, extra


def port_listening(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def forwards_status() -> dict[str, bool]:
    return {str(port): port_listening(port) for port in FORWARD_PORTS}


def status(gateway_connected: bool) -> dict:
    """提供界面所需的链路状态。PC3 网关监听 PC3 的回环地址，
    上位机通过两条转发的状态以及后端自身的网关连接
    判断网关是否可达。相机/雷达灯按数据就绪判定（上游全链有产出），
    与阀门独立：服务在跑而接入关闭时灯仍为绿。"""
    forwards = forwards_status()
    return {
        "pc2_agent": probe_pc2(),
        "vision_ready": vision_frame_ready(),
        "forwards": forwards,
        "gateway": gateway_connected and all(forwards.values()),
        "map_ready": map_bridge_ready(),
        "asr_ready": asr_ready(),
        "valves": dict(DATA_VALVES),
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


def vision_frame_ready() -> bool:
    """相机数据就绪：PC2 代理与视觉程序都在产出预览帧。"""
    try:
        with urllib.request.urlopen(pc2_vision_frame_url(PC2), timeout=1.5) as response:
            return response.status == 200
    except Exception:
        return False


def map_bridge_ready() -> bool:
    """雷达数据就绪：PC3 容器建图栈与数据桥在产出位姿。"""
    try:
        with urllib.request.urlopen(f"http://{PC3}:8097/pose", timeout=1.5) as response:
            return response.status == 200
    except Exception:
        return False


def asr_ready() -> bool:
    """语音服务就绪：PC3 上 ASR 服务进程在跑（模型常驻，与是否采音无关）。"""
    try:
        with urllib.request.urlopen(f"http://{PC3}:8098/health", timeout=1.5) as response:
            return response.status == 200
    except Exception:
        return False


def _asr_post(path: str) -> dict:
    request = urllib.request.Request(f"http://{PC3}:8098{path}", method="POST")
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.load(response)


def asr_text(since: int = 0) -> dict | None:
    """转发 ASR 服务的增量转写结果；服务不可达返回 None。"""
    try:
        with urllib.request.urlopen(f"http://{PC3}:8098/asr/text?since={since}",
                                    timeout=2) as response:
            payload = json.load(response)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def voice_ready() -> bool:
    """语音播报服务就绪：PC3 上 voice_service 进程可响应。"""
    try:
        with urllib.request.urlopen(f"http://{PC3}:8099/health", timeout=1.5) as response:
            return response.status == 200
    except Exception:
        return False


def start_pc3_voice() -> dict:
    """启动 PC3 语音播报服务（网关 venv + DDS），幂等。"""
    running = _ssh(PC3, 'pgrep -f "[v]oice_service.py" >/dev/null && echo running || echo stopped')
    if running != "running":
        _ssh_best_effort(PC3, PC3_VOICE_START)
    for _ in range(6):
        time.sleep(1.0)
        if voice_ready():
            break
    return {"ok": voice_ready(), "already": running == "running"}


def say(text: str) -> bool:
    """请求机器人本体扬声器播报文本（voice_service 的 /say）。

    演示流程中的提示性播报：失败不应中断流程，返回 False 由调用方记录。
    """
    body = json.dumps({"text": text}).encode()
    request = urllib.request.Request(f"http://{PC3}:8099/say", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        # /say 阻塞到播完（voice_service 按字数估算持锁），长句可达 20 秒以上
        with urllib.request.urlopen(request, timeout=40) as response:
            payload = json.load(response)
        return bool(payload.get("ok"))
    except Exception:
        return False


POSE_STALE_MS = 2000     # 里程计采样过期线（10Hz 源，抖动余量）

def calibrate_imu_bias() -> dict | None:
    """转向前校零：桥采样 1 秒陀螺取均值作为零偏扣除（消除温漂累计）。"""
    request = urllib.request.Request(
        f"http://{PC3}:8097/imu/calibrate", method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)
    except Exception:
        return None


def fastlio_pose(timeout: float = 1.0) -> dict | None:
    """轻量拉取 FastLIO 位姿，演示序列执行器高频轮询用。

    桥在尚未收到位姿时返回 age_ms=null，视为无效；里程计停更但 HTTP
    仍可达时 age_ms 持续增大，超过过期线同样视为无效（任务书第五节：
    HTTP 轮询本身不能刷新有效性）。IMU 字段（imu_yaw/imu_age_ms/
    imu_valid）透传，由调用方按需校验。
    """
    try:
        with urllib.request.urlopen(f"http://{PC3}:8097/pose", timeout=timeout) as response:
            payload = json.load(response)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    age = payload.get("age_ms")
    if not isinstance(age, (int, float)) or age > POSE_STALE_MS:
        return None
    return payload


def asr_health() -> dict | None:
    """识别服务全量健康（含 mic_recent 数据流心跳）；不可达返回 None。"""
    try:
        with urllib.request.urlopen(f"http://{PC3}:8098/health", timeout=1.5) as response:
            payload = json.load(response)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def reset_pc3_mic() -> dict:
    """麦克风 USB 设备复位（修复 XVF3800 挂死：句柄活着但零数据）。

    2026-11-21 现场实证：USB 总线 unbind/bind 后设备即恢复出数。
    作为「巡检准备」检查到麦克风无数据时的一键修复动作。
    固件级 REBOOT（更轻量）由 asr_service 内置，此处是兜底手段。"""
    try:
        password = shlex.quote(_password())
        _ssh(PC3, "pkill arecord 2>/dev/null; true", timeout=5)
        _ssh(PC3, f"echo {password} | sudo -S -p '' sh -c "
                  "'echo 1-2 > /sys/bus/usb/drivers/usb/unbind; "
                  "sleep 2; echo 1-2 > /sys/bus/usb/drivers/usb/bind' 2>/dev/null",
             timeout=10)
        time.sleep(2)
        return {"ok": True}
    except (FieldError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "error": str(error)[:120]}


INTERRUPT = "巡检中断，等待人工处理。"
def _asr_json_post(path: str, body: dict) -> dict | None:
    request = urllib.request.Request(f"http://{PC3}:8098{path}",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)
    except Exception:
        return None


def asr_arm_session() -> dict | None:
    """开启演示待命会话；服务不可用返回 None。"""
    return _asr_json_post("/asr/session", {"action": "arm"})


def asr_disarm_session() -> None:
    """解除待命会话（尽力而为；服务不在时无需解除）。"""
    with contextlib.suppress(Exception):
        _asr_json_post("/asr/session", {"action": "disarm"})


def asr_commands(since: int = 0) -> dict | None:
    """拉取口令命令事件（boot_id/会话 id/年龄由 DemoService 校验）。"""
    try:
        with urllib.request.urlopen(f"http://{PC3}:8098/asr/commands?since={since}",
                                    timeout=2) as response:
            payload = json.load(response)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def start_pc3_asr() -> dict:
    """启动 PC3 语音识别服务（模型常驻），幂等。"""
    running = _ssh(PC3, 'pgrep -f "[a]sr_service.py" >/dev/null && echo running || echo stopped')
    if running != "running":
        _ssh_best_effort(PC3, PC3_ASR_START)
    # 模型加载实测约 5 秒，留足余量
    for _ in range(10):
        time.sleep(1.5)
        if asr_ready():
            break
    return {"ok": asr_ready(), "already": running == "running"}


def start_pc2_vision() -> dict:
    """启动 PC2 视觉程序（headless，产出预览帧），幂等。"""
    running = _ssh(PC2, 'pgrep -f "[b]in/ob_callback" >/dev/null && echo running || echo stopped')
    if running == "running":
        return {"ok": vision_frame_ready(), "already": True}
    _ssh_best_effort(PC2, PC2_VISION_START)
    # 模型加载与首帧产出需要数秒
    for _ in range(8):
        time.sleep(1.5)
        if vision_frame_ready():
            break
    return {"ok": vision_frame_ready(), "already": False}


def ensure_usb_audio_power() -> None:
    """一次性修复 Jetson USB 节电（2026-11-21 确认根因：XVF3800 的 USB
    autosuspend 在 2 秒空闲后挂起设备，音频等时传输停止 = 麦克风挂死）。

    写 udev 规则 + 立即生效，每次「巡检准备」幂等调用。"""
    rule_file = "/etc/udev/rules.d/98-respeaker-usb-power.rules"
    check = _ssh(PC3, f"test -f {rule_file} && echo yes || echo no", timeout=5)
    if check == "yes":
        return    # 已修复过
    # udev 规则内容用 base64 传过去写文件，避开多层 shell 引号嵌套
    import base64
    rule_content = ('ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="2886", '
                    'ATTR{idProduct}=="001a", ATTR{power/autosuspend}="-1", '
                    'ATTR{power/control}="on"\n')
    encoded = base64.b64encode(rule_content.encode()).decode()
    password = shlex.quote(_password())
    _ssh(PC3, f"echo {password} | sudo -S -p '' sh -c "
              f"'echo -1 > /sys/bus/usb/devices/1-2/power/autosuspend 2>/dev/null; "
              f"echo on > /sys/bus/usb/devices/1-2/power/control 2>/dev/null; "
              f"echo on > /sys/bus/usb/devices/usb1/power/control 2>/dev/null; "
              f"echo on > /sys/bus/usb/devices/usb2/power/control 2>/dev/null; "
              f"echo {encoded} | base64 -d > {rule_file}; "
              f"udevadm control --reload-rules' 2>/dev/null", timeout=15)


def _ensure_docker_group() -> None:
    """首次使用时把 unitree 加入 PC3 的 docker 组（一次性系统变更），
    之后 docker 命令免 sudo；组权限在每次新建的 SSH 会话中生效。"""
    groups = _ssh(PC3, "id -nG unitree")
    if "docker" not in groups.split():
        password = shlex.quote(_password())
        _ssh(PC3, f"echo {password} | sudo -S -p '' usermod -aG docker unitree 2>/dev/null")


def start_pc3_mapping() -> dict:
    """启动 PC3 建图数据链（容器→建图栈→桥），幂等，返回各步是否新拉起。

    机器人重启后容器不会自启，此步最常触发；容器不存在时不自动创建，
    需先运行离线部署脚本。雷达握手与首帧位姿需要十秒级，以 :8097/pose 验证。
    """
    started = {"container": False, "mapping": False, "bridge": False}
    _ensure_docker_group()
    running = _ssh(PC3, "docker ps --format '{{.Names}}' | grep -qx ros-noetic-dev"
                       " && echo running || echo stopped")
    if running != "running":
        exists = _ssh(PC3, "docker ps -a --format '{{.Names}}' | grep -qx ros-noetic-dev"
                          " && echo yes || echo no")
        if exists != "yes":
            return {"ok": False, "error": "PC3 上没有建图容器，请先运行离线部署脚本 pc3-setup.sh"}
        _ssh(PC3, "docker start ros-noetic-dev", timeout=30)
        started["container"] = True
        time.sleep(2)
    # 进程检查用括号模式避免 pgrep 匹配到包装命令自身（docker exec bash -c 的命令行）
    mapping = _ssh(PC3, "docker exec ros-noetic-dev bash -c "
                        "'pgrep -f \"[h]2_mapping_headless\" >/dev/null && echo running || echo stopped'")
    if mapping != "running":
        _ssh_best_effort(PC3, "docker exec -d ros-noetic-dev bash -c '"
                         + PC3_ROS_ENV
                         + " && roslaunch /workspace/h2_extra/h2_mapping_headless.launch"
                           " > /tmp/mapping.log 2>&1'", timeout=20)
        started["mapping"] = True
    bridge = _ssh(PC3, "docker exec ros-noetic-dev bash -c "
                       "'pgrep -f \"[h]2_map_bridge\" >/dev/null && echo running || echo stopped'")
    if bridge != "running":
        # 建图栈可能刚拉起话题未就绪，桥先启动并自行等待数据
        _ssh_best_effort(PC3, "docker exec -d ros-noetic-dev bash -c 'sleep 3; "
                         + PC3_ROS_ENV
                         + " && python3 /workspace/h2_extra/h2_map_bridge.py"
                           " > /tmp/bridge.log 2>&1'", timeout=20)
        started["bridge"] = True
    for _ in range(12):
        time.sleep(2)
        if map_bridge_ready():
            break
    return {"ok": map_bridge_ready(), **started}


def set_data_valve(name: str, enabled: bool) -> dict:
    """切换数据接入阀门。开阀时若上游程序未运行则顺带拉起；
    关阀只动阀门，从不停止程序。语音额外控制服务内的采音开关。"""
    if name not in DATA_VALVES:
        raise FieldError(f"未知的数据阀门：{name}")
    if not enabled:
        if name == "asr":
            # 停采音但不杀模型进程；服务不在时停无可停，阀门照关
            try:
                _asr_post("/asr/stop")
            except Exception:
                pass
        DATA_VALVES[name] = False
        return {"ok": True, "enabled": False}
    DATA_VALVES[name] = True
    if name == "asr":
        result = start_pc3_asr()
        try:
            _asr_post("/asr/start")
        except Exception as error:
            return {"ok": False, "enabled": True, "error": f"采音启动失败：{error}"}
        result["enabled"] = True
        return result
    starter = {"vision": start_pc2_vision, "mapping": start_pc3_mapping}[name]
    result = starter()
    result["enabled"] = True
    return result
