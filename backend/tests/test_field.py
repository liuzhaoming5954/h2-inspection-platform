from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from h2_backend import field
from h2_backend.app import create_app


FIELD_ROUTES = (
    ("GET", "/api/field/status", "status"),
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
    for name in ("reconnect_forwards", "start_pc2_agent", "start_pc3_gateway"):
        field_handlers[name].assert_called_once_with()


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
