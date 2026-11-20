from dataclasses import replace
from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
import json
import shlex
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

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
    monkeypatch.setattr(field, "DATA_VALVES", {"vision": True, "mapping": True})


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
