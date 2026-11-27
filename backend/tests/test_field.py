from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
import json
import shlex
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
import urllib.request

from fastapi.testclient import TestClient
import pytest

from h2_backend import field
from h2_backend.app import create_app


FIELD_ROUTES = (
    ("GET", "/api/field/status", "status"),
    ("GET", "/api/field/vision", "vision"),
    ("POST", "/api/field/reconnect-forwards", "reconnect_forwards"),
    ("POST", "/api/field/start-pc2", "start_pc2_agent"),
    ("POST", "/api/field/start-pc3", "start_pc3_gateway"),
)


@pytest.fixture
def field_handlers(monkeypatch):
    handlers = {}
    for _, _, name in FIELD_ROUTES:
        handlers[name] = Mock(return_value={"action": name})
        monkeypatch.setattr(field, name, handlers[name])
    return handlers


def test_simulation_does_not_reach_field_operations(client, app, field_handlers, monkeypatch):
    health = Mock(return_value={"gateway": "connected"})
    monkeypatch.setattr(app.state.service, "health", health)

    for method, path, name in FIELD_ROUTES:
        assert client.request(method, path).status_code == 404
        field_handlers[name].assert_not_called()
    health.assert_not_called()


def test_hardware_field_routes_delegate_operations(settings, gateway, field_handlers, monkeypatch):
    hardware_app = create_app(replace(settings, hardware_teleop=True), gateway)
    monkeypatch.setattr(hardware_app.state.service, "health", lambda: {"gateway": "connected"})

    with TestClient(hardware_app) as client:
        for method, path, name in FIELD_ROUTES:
            response = client.request(method, path)
            assert response.status_code == 200
            assert response.json() == {"action": name}

    field_handlers["status"].assert_called_once_with(True)
    for name in ("vision", "reconnect_forwards", "start_pc2_agent", "start_pc3_gateway"):
        field_handlers[name].assert_called_once_with()


def test_vision_forwards_latest_pc2_frame(monkeypatch):
    payload = {"available": True, "person": 2, "led": 1, "red": 0, "green": 1,
               "qr": "机房 A 巡检点 3", "center_mm": 1967, "age_ms": 120, "leds": []}
    urlopen = Mock(return_value=BytesIO(json.dumps(payload).encode()))
    monkeypatch.setattr(field, "urllib", SimpleNamespace(request=SimpleNamespace(urlopen=urlopen)))

    assert field.vision() == payload
    urlopen.assert_called_once_with(f"http://{field.PC2}:8090/vision", timeout=2)


def test_vision_folds_unreachable_pc2_and_non_object_payload(monkeypatch):
    urlopen = Mock(side_effect=OSError("connection refused"))
    monkeypatch.setattr(field, "urllib", SimpleNamespace(request=SimpleNamespace(urlopen=urlopen)))
    result = field.vision()
    assert result["available"] is False
    assert "refused" in result["error"]

    urlopen = Mock(return_value=BytesIO(b"[1, 2]"))
    monkeypatch.setattr(field, "urllib", SimpleNamespace(request=SimpleNamespace(urlopen=urlopen)))
    assert field.vision() == {"available": False, "error": "视觉代理返回了非对象响应"}


def _frame_response(payload: bytes, age_header: str | None, content_type: str = "image/png") -> MagicMock:
    # 模拟 PC2 /vision/frame 的二进制响应：上下文管理器 + headers 字典。
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = payload
    response.headers = {"Content-Type": content_type}
    if age_header is not None:
        response.headers["X-Frame-Age-Ms"] = age_header
    return response


def test_vision_frame_forwards_png_and_age_header(monkeypatch):
    urlopen = MagicMock(return_value=_frame_response(b"\x89PNG-frame", "120"))
    monkeypatch.setattr(field, "urllib", SimpleNamespace(request=SimpleNamespace(urlopen=urlopen)))

    assert field.vision_frame() == (b"\x89PNG-frame", 120, "image/png")
    urlopen.assert_called_once_with(f"http://{field.PC2}:8090/vision/frame", timeout=2)

    # 年龄头缺失时返回 None 年龄而不是报错。
    urlopen = MagicMock(return_value=_frame_response(b"\x89PNG-frame", None))
    monkeypatch.setattr(field, "urllib", SimpleNamespace(request=SimpleNamespace(urlopen=urlopen)))
    assert field.vision_frame() == (b"\x89PNG-frame", None, "image/png")


def test_vision_frame_falls_back_to_magic_bytes_for_content_type(monkeypatch):
    # PC2 未声明类型时按文件魔数兜底，避免 PNG 被标成 jpeg。
    urlopen = MagicMock(return_value=_frame_response(b"\x89PNG-frame", "40", content_type=""))
    monkeypatch.setattr(field, "urllib", SimpleNamespace(request=SimpleNamespace(urlopen=urlopen)))
    assert field.vision_frame()[2] == "image/png"

    urlopen = MagicMock(return_value=_frame_response(b"\xff\xd8jpeg", "40", content_type=None))
    monkeypatch.setattr(field, "urllib", SimpleNamespace(request=SimpleNamespace(urlopen=urlopen)))
    assert field.vision_frame()[2] == "image/jpeg"


def test_vision_frame_folds_unreachable_pc2(monkeypatch):
    urlopen = MagicMock(side_effect=OSError("connection refused"))
    monkeypatch.setattr(field, "urllib", SimpleNamespace(request=SimpleNamespace(urlopen=urlopen)))
    assert field.vision_frame() is None


def test_hardware_vision_frame_route_serves_png(settings, gateway, monkeypatch):
    hardware_app = create_app(replace(settings, hardware_teleop=True), gateway)
    monkeypatch.setattr(field, "vision_frame", lambda: (b"\x89PNG-frame", 120, "image/png"))
    with TestClient(hardware_app) as client:
        response = client.get("/api/field/vision/frame")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.headers["X-Frame-Age-Ms"] == "120"
    assert response.content == b"\x89PNG-frame"


def test_hardware_vision_frame_route_reports_unavailable(settings, gateway, monkeypatch):
    hardware_app = create_app(replace(settings, hardware_teleop=True), gateway)
    monkeypatch.setattr(field, "vision_frame", lambda: None)
    with TestClient(hardware_app) as client:
        response = client.get("/api/field/vision/frame")
    assert response.status_code == 503


def _map_response(payload: bytes, headers: dict) -> MagicMock:
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = payload
    response.headers = headers
    return response


def test_map_data_forwards_binary_and_bridge_headers(monkeypatch):
    headers = {"Content-Type": "application/octet-stream",
               "X-Map-Points": "1234", "X-Map-Voxel": "0.050", "X-Map-Age-Ms": "300"}
    urlopen = MagicMock(return_value=_map_response(b"\x01\x02", headers))
    monkeypatch.setattr(field, "urllib", SimpleNamespace(request=SimpleNamespace(urlopen=urlopen)))

    assert field.map_data("map") == (b"\x01\x02", "application/octet-stream",
                                     {"X-Map-Points": "1234", "X-Map-Voxel": "0.050", "X-Map-Age-Ms": "300"})
    urlopen.assert_called_once_with(f"http://{field.PC3}:8097/map", timeout=5)


def test_map_data_folds_unreachable_bridge(monkeypatch):
    urlopen = MagicMock(side_effect=OSError("connection refused"))
    monkeypatch.setattr(field, "urllib", SimpleNamespace(request=SimpleNamespace(urlopen=urlopen)))
    assert field.map_data("pose") is None


def test_hardware_map_route_serves_bridge_payload(settings, gateway, monkeypatch):
    hardware_app = create_app(replace(settings, hardware_teleop=True), gateway)
    monkeypatch.setattr(field, "map_data", lambda kind: (
        (b"cloud", "application/octet-stream", {"X-Map-Points": "9"}) if kind == "map" else None))
    with TestClient(hardware_app) as client:
        ok = client.get("/api/field/map/map")
        assert ok.status_code == 200
        assert ok.headers["X-Map-Points"] == "9"
        assert ok.content == b"cloud"
        assert client.get("/api/field/map/path").status_code == 503
        assert client.get("/api/field/map/bogus").status_code == 404


@pytest.fixture
def valves_default(monkeypatch):
    """恢复阀门初始状态，避免用例间相互污染。"""
    monkeypatch.setattr(field, "DATA_VALVES",
                        {"vision": True, "mapping": True, "asr": False})


def test_asr_valve_close_stops_capture_and_keeps_service(monkeypatch, valves_default):
    stop = Mock(return_value={"ok": True})
    monkeypatch.setattr(field, "_asr_post", stop)
    result = field.set_data_valve("asr", False)
    assert result == {"ok": True, "enabled": False}
    stop.assert_called_once_with("/asr/stop")
    assert field.DATA_VALVES["asr"] is False


def test_asr_valve_open_pulls_service_and_starts_capture(monkeypatch, valves_default):
    posts = []
    monkeypatch.setattr(field, "_asr_post",
                        lambda path: posts.append(path) or {"ok": True})
    monkeypatch.setattr(field, "start_pc3_asr", Mock(return_value={"ok": True, "already": False}))
    result = field.set_data_valve("asr", True)
    assert result == {"ok": True, "already": False, "enabled": True}
    assert posts == ["/asr/start"]
    assert field.DATA_VALVES["asr"] is True


def test_asr_text_forwards_and_folds(monkeypatch):
    urlopen = MagicMock(return_value=BytesIO(json.dumps(
        {"items": [{"id": 3, "ts": 1.0, "text": "你好"}], "last_id": 3}).encode()))
    monkeypatch.setattr(field, "urllib", SimpleNamespace(request=SimpleNamespace(urlopen=urlopen)))
    assert field.asr_text(2)["last_id"] == 3
    urlopen.assert_called_once_with("http://192.168.124.163:8098/asr/text?since=2", timeout=2)

    urlopen = MagicMock(side_effect=OSError("refused"))
    monkeypatch.setattr(field, "urllib", SimpleNamespace(request=SimpleNamespace(urlopen=urlopen)))
    assert field.asr_text() is None


def test_asr_text_route_gated_by_valve(settings, gateway, monkeypatch, valves_default):
    hardware_app = create_app(replace(settings, hardware_teleop=True), gateway)
    monkeypatch.setattr(field, "asr_text", Mock(return_value={"items": [], "last_id": 0}))
    with TestClient(hardware_app) as client:
        # 默认关：503 且不触达 PC3
        assert "已关闭" in client.get("/api/field/asr/text").json()["detail"]
        field.asr_text.assert_not_called()
        # 开阀后正常转发
        monkeypatch.setattr(field, "DATA_VALVES", {"vision": True, "mapping": True, "asr": True})
        ok = client.get("/api/field/asr/text?since=5")
        assert ok.status_code == 200 and ok.json() == {"items": [], "last_id": 0}
        field.asr_text.assert_called_once_with(5)


def test_start_pc3_voice_is_idempotent(monkeypatch):
    monkeypatch.setattr(field, "_ssh", Mock(return_value="running"))
    monkeypatch.setattr(field, "voice_ready", Mock(return_value=True))
    assert field.start_pc3_voice() == {"ok": True, "already": True}


def test_start_pc3_voice_launches_with_gateway_venv(monkeypatch):
    monkeypatch.setattr(field, "time", SimpleNamespace(sleep=lambda _: None))
    monkeypatch.setattr(field, "_ssh", Mock(return_value="stopped"))
    launcher = Mock()
    monkeypatch.setattr(field, "_ssh_best_effort", launcher)
    monkeypatch.setattr(field, "voice_ready", Mock(return_value=True))
    assert field.start_pc3_voice() == {"ok": True, "already": False}
    command = launcher.call_args[0][1]
    assert "voice_service.py" in command and "CYCLONEDDS_HOME" in command


def test_say_posts_text_and_folds_failures(monkeypatch):
    posts = []

    def urlopen(request, timeout=None):
        posts.append((request.full_url, request.data))
        return BytesIO(json.dumps({"ok": True, "code": 0}).encode())
    # 只替换 urlopen，Request 仍用真实实现（say 需要它构造 POST）
    monkeypatch.setattr(field, "urllib", SimpleNamespace(
        request=SimpleNamespace(urlopen=urlopen, Request=urllib.request.Request)))
    assert field.say("语音链路正常。") is True
    url, data = posts[0]
    assert url == f"http://{field.PC3}:8099/say"
    assert json.loads(data) == {"text": "语音链路正常。"}

    refused = MagicMock(side_effect=OSError("connection refused"))
    monkeypatch.setattr(field, "urllib", SimpleNamespace(
        request=SimpleNamespace(urlopen=refused, Request=urllib.request.Request)))
    assert field.say("再试一次") is False


def test_fastlio_pose_requires_fresh_age(monkeypatch):
    fresh = {"x": 1.0, "y": 2.0, "z": 0.0, "yaw": 0.5, "age_ms": 20}
    urlopen = MagicMock(return_value=BytesIO(json.dumps(fresh).encode()))
    monkeypatch.setattr(field, "urllib", SimpleNamespace(request=SimpleNamespace(urlopen=urlopen)))
    assert field.fastlio_pose() == fresh
    urlopen.assert_called_once_with(f"http://{field.PC3}:8097/pose", timeout=1.0)

    # 桥尚未收到位姿时 age_ms=null，执行器必须视为无效
    empty = BytesIO(json.dumps({"x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0, "age_ms": None}).encode())
    urlopen = MagicMock(return_value=empty)
    monkeypatch.setattr(field, "urllib", SimpleNamespace(request=SimpleNamespace(urlopen=urlopen)))
    assert field.fastlio_pose() is None


def test_fastlio_pose_rejects_stale_odometry(monkeypatch):
    # 里程计停更但 HTTP 正常：age_ms 持续增大，超过过期线视为无效
    stale = BytesIO(json.dumps({"x": 0.1, "y": 0.2, "z": 0.0, "yaw": 0.3,
                                "age_ms": 3000, "imu_yaw": 0.01,
                                "imu_valid": True, "imu_age_ms": 50}).encode())
    urlopen = MagicMock(return_value=stale)
    monkeypatch.setattr(field, "urllib", SimpleNamespace(request=SimpleNamespace(urlopen=urlopen)))
    assert field.fastlio_pose() is None

    fresh = BytesIO(json.dumps({"x": 0.1, "y": 0.2, "z": 0.0, "yaw": 0.3,
                                "age_ms": 90, "imu_yaw": 0.01,
                                "imu_valid": True, "imu_age_ms": 40,
                                "bridge_boot": "20261120T120000"}).encode())
    urlopen = MagicMock(return_value=fresh)
    monkeypatch.setattr(field, "urllib", SimpleNamespace(request=SimpleNamespace(urlopen=urlopen)))
    pose = field.fastlio_pose()
    assert pose is not None and pose["imu_valid"] is True


INTERRUPT = "巡检中断，等待人工处理。"
def test_asr_session_and_commands_passthrough(monkeypatch):
    posts = []
    gets = []

    def urlopen(request, timeout=None):
        # GET 传字符串 URL，POST 传 Request 对象，两种形态都要兼容
        if isinstance(request, str):
            gets.append(request)
            return BytesIO(json.dumps({"items": [], "last_seq": 0, "boot_id": "b1"}).encode())
        posts.append((request.full_url, json.loads(request.data)))
        return BytesIO(json.dumps({"ok": True, "session_id": "s9",
                                   "boot_id": "b1"}).encode())

    monkeypatch.setattr(field, "urllib", SimpleNamespace(
        request=SimpleNamespace(urlopen=urlopen, Request=urllib.request.Request)))
    assert field.asr_arm_session()["session_id"] == "s9"
    assert posts[0][0] == f"http://{field.PC3}:8098/asr/session"
    assert posts[0][1] == {"action": "arm"}
    assert field.asr_commands(5)["boot_id"] == "b1"
    assert gets[0].endswith("/asr/commands?since=5")


def test_data_valve_close_gates_data_and_keeps_services(settings, gateway, monkeypatch, valves_default):
    hardware_app = create_app(replace(settings, hardware_teleop=True), gateway)
    monkeypatch.setattr(field, "vision", Mock(return_value={"available": True}))
    monkeypatch.setattr(field, "vision_frame", Mock(return_value=(b"\x89PNG", 1, "image/png")))
    monkeypatch.setattr(field, "map_data",
                        Mock(return_value=(b"cloud", "application/octet-stream", {})))
    with TestClient(hardware_app) as client:
        # 默认开：数据端点正常
        assert client.get("/api/field/vision").status_code == 200
        assert client.get("/api/field/vision/frame").status_code == 200
        assert client.get("/api/field/map/map").status_code == 200

        # 关阀：只改状态，不触发任何拉起动作
        stop_spy = Mock()
        monkeypatch.setattr(field, "start_pc2_vision", stop_spy)
        monkeypatch.setattr(field, "start_pc3_mapping", stop_spy)
        closed = client.post("/api/field/data/vision", json={"enabled": False})
        assert closed.status_code == 200 and closed.json() == {"ok": True, "enabled": False}
        stop_spy.assert_not_called()

        # 关闭后相机数据端点 503，雷达端点不受影响
        assert "已关闭" in client.get("/api/field/vision").json()["detail"]
        assert client.get("/api/field/vision/frame").status_code == 503
        assert client.get("/api/field/map/map").status_code == 200


def test_data_valve_open_pulls_services_once(settings, gateway, monkeypatch, valves_default):
    hardware_app = create_app(replace(settings, hardware_teleop=True), gateway)
    with TestClient(hardware_app) as client:
        # 开阀触发对应服务的幂等拉起
        monkeypatch.setattr(field, "start_pc2_vision", Mock(return_value={"ok": True, "already": False}))
        opened = client.post("/api/field/data/vision", json={"enabled": True})
        assert opened.status_code == 200
        assert opened.json() == {"ok": True, "already": False, "enabled": True}

        # 未知阀门名回错误而不是 500
        bogus = client.post("/api/field/data/bogus", json={"enabled": True})
        assert bogus.status_code == 200
        assert bogus.json()["ok"] is False


def test_start_pc2_vision_is_idempotent(monkeypatch):
    monkeypatch.setattr(field, "_ssh", Mock(return_value="running"))
    monkeypatch.setattr(field, "vision_frame_ready", Mock(return_value=True))
    assert field.start_pc2_vision() == {"ok": True, "already": True}


def test_start_pc3_mapping_starts_stopped_container(monkeypatch):
    monkeypatch.setattr(field, "time", SimpleNamespace(sleep=lambda _: None))
    monkeypatch.setattr(field, "_ensure_docker_group", Mock())
    # 依次返回：容器未跑、容器存在、docker start 应答、建图栈未跑、桥未跑
    states = iter(["stopped", "yes", "started", "stopped", "stopped"])
    monkeypatch.setattr(field, "_ssh", Mock(side_effect=lambda *a, **k: next(states)))
    monkeypatch.setattr(field, "_ssh_best_effort", Mock())
    monkeypatch.setattr(field, "map_bridge_ready", Mock(return_value=True))
    result = field.start_pc3_mapping()
    assert result == {"ok": True, "container": True, "mapping": True, "bridge": True}


def test_start_pc3_mapping_reports_missing_container(monkeypatch):
    monkeypatch.setattr(field, "_ensure_docker_group", Mock())
    states = iter(["stopped", "no"])
    monkeypatch.setattr(field, "_ssh", Mock(side_effect=lambda *a, **k: next(states)))
    result = field.start_pc3_mapping()
    assert result["ok"] is False
    assert "pc3-setup" in result["error"]


def test_reconnect_requires_credentials_before_terminating_forwards(monkeypatch):
    process = Mock()
    monkeypatch.delenv("H2_FIELD_PASSWORD", raising=False)
    monkeypatch.setattr(field, "subprocess", SimpleNamespace(run=process))

    with pytest.raises(field.FieldError, match="H2_FIELD_PASSWORD"):
        field.reconnect_forwards()

    process.assert_not_called()


def test_reconnect_disables_ssh_connection_sharing_for_both_forwards(monkeypatch):
    process = Mock()
    forwards = {"15051": True, "15052": True}
    monkeypatch.setenv("H2_FIELD_PASSWORD", "fixture")
    monkeypatch.setattr(field, "subprocess", SimpleNamespace(run=process))
    monkeypatch.setattr(field, "time", SimpleNamespace(sleep=lambda _: None))
    monkeypatch.setattr(field, "forwards_status", lambda: forwards)

    assert field.reconnect_forwards() == {"ok": True, "forwards": forwards}

    cleanup, status, teleop = (call.args[0] for call in process.call_args_list)
    assert cleanup[0] == "pkill"
    for command, local_port, remote_port in ((status, 15051, 50051), (teleop, 15052, 50052)):
        assert command[command.index("-S") + 1] == "none"
        assert command[command.index("-L") + 1] == f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}"


@pytest.mark.parametrize("override_url", [None, "http://pc2-private.example:8090/environment?name='h2'&lane=pc3"])
def test_manual_field_start_uses_configured_sensor_addresses(monkeypatch, override_url):
    pc2_host = "10.42.8.22"
    pc2_bind = "10.42.9.22"
    pc3_host = "10.42.8.23"
    monitor_url = f"http://{pc2_host}:8090/environment"
    monkeypatch.setenv("H2_PC2_HOST", pc2_host)
    monkeypatch.setenv("H2_PC2_BIND", pc2_bind)
    monkeypatch.setenv("H2_PC3_HOST", pc3_host)
    if override_url is None:
        monkeypatch.delenv("H2_SENSOR_AGENT_URL", raising=False)
    else:
        monkeypatch.setenv("H2_SENSOR_AGENT_URL", override_url)

    spec = spec_from_file_location("configured_field", field.__file__)
    assert spec is not None and spec.loader is not None
    configured = module_from_spec(spec)
    spec.loader.exec_module(configured)
    urlopen = Mock(return_value=BytesIO(b'{"available":true}'))
    start = Mock()
    monkeypatch.setattr(configured, "urllib", SimpleNamespace(request=SimpleNamespace(urlopen=urlopen)))
    monkeypatch.setattr(configured, "_ssh", Mock(side_effect=["stopped", "stopped", "2"]))
    monkeypatch.setattr(configured, "_ssh_best_effort", start)
    monkeypatch.setattr(configured, "time", SimpleNamespace(sleep=lambda _: None))

    assert configured.start_pc2_agent() == {"ok": True, "already": False}
    assert configured.start_pc3_gateway() == {"ok": True, "already": False}
    urlopen.assert_called_once_with(monitor_url, timeout=2)
    (agent, gateway) = [call.args for call in start.call_args_list]
    assert agent[0] == pc2_host
    assert f"H2_PC2_BIND={pc2_bind}" in shlex.split(agent[1])
    assert gateway[0] == pc3_host
    assert f"H2_SENSOR_AGENT_URL={override_url or monitor_url}" in shlex.split(gateway[1])


def test_backend_http_never_uses_environment_proxy(settings, gateway, monkeypatch):
    """后端的对外 HTTP 一律直连：启动终端里的代理变量不得把发往现场设备的请求送进代理。

    urllib 不识别 no_proxy 里的网段写法，http_proxy 被继承时 PC2/PC3 的请求会等到
    超时才失败（2026-11-27 现场）。"""
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    # 没有进程监听的代理地址：请求一旦走代理必然失败
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    assert not urllib.request.proxy_bypass("192.168.124.163")
    # 清掉其他用例留下的全局 opener，保证这里验证的是本次 create_app 的效果
    monkeypatch.setattr(urllib.request, "_opener", None)

    served = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server 接口命名
            served.set()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format, *args):  # noqa: A002 - 基类签名
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        create_app(settings, gateway)
        url = f"http://127.0.0.1:{server.server_port}/probe"
        with urllib.request.urlopen(url, timeout=3) as response:
            assert response.status == 200
        assert served.is_set()
    finally:
        server.shutdown()
        server.server_close()
