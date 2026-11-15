"""在不启动服务的情况下验证开发启动器的模式选择。"""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts" / "dev.py"
    spec = spec_from_file_location("h2_dev_launcher", script)
    assert spec and spec.loader
    dev = module_from_spec(spec)
    spec.loader.exec_module(dev)

    runtime = tmp_path / ".runtime"
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.setattr(dev, "RUNTIME", runtime)
    monkeypatch.setattr(dev, "PID_FILE", runtime / "dev.pid")
    monkeypatch.setattr(dev, "PYTHON", python)
    monkeypatch.setattr(dev, "shutil", SimpleNamespace(which=lambda *args, **kwargs: "/test/bin/node"))
    return dev


def start(launcher, monkeypatch, *args):
    services = {}
    ready_checks = {}
    database_commands = []
    signal_handlers = {}

    def spawn(command, **kwargs):
        assert command[0] in {str(launcher.PYTHON), "/test/bin/node"}, command
        name = Path(kwargs["stdout"].name).stem
        process = SimpleNamespace(pid=len(services) + 100, poll=lambda: None, wait=lambda timeout: None)
        services[name] = (process, command, kwargs["env"])
        return process

    def check_ready(url, process):
        ready_checks[url] = process

    def register_signal(number, handler):
        signal_handlers[number] = handler

    monkeypatch.setattr(launcher, "subprocess", SimpleNamespace(
        Popen=spawn,
        STDOUT=-2,
        DEVNULL=-3,
        run=lambda command, **kwargs: database_commands.append(command),
    ))
    monkeypatch.setattr(launcher, "wait_url", check_ready)
    monkeypatch.setattr(launcher.signal, "signal", register_signal)
    monkeypatch.setattr(launcher.os, "killpg", lambda pid, number: None)
    monkeypatch.setattr(launcher, "time", SimpleNamespace(
        sleep=lambda _: signal_handlers[launcher.signal.SIGTERM](launcher.signal.SIGTERM, None),
    ))
    monkeypatch.setattr(launcher.sys, "argv", ["dev.py", "start", *args])
    launcher.main()

    assert ready_checks == {
        "http://127.0.0.1:8000/api/health": services["api"][0],
        "http://127.0.0.1:5173": services["frontend"][0],
    }
    assert not launcher.PID_FILE.exists()
    return services, database_commands


@pytest.mark.parametrize("endpoint", ["127.0.0.1:15051", "[::1]:15051"])
def test_external_gateway_launches_only_local_api_and_frontend(launcher, monkeypatch, endpoint):
    monkeypatch.setenv("H2_GATEWAY_ADDRESS", "127.0.0.1:50051")
    monkeypatch.setenv("H2_HARDWARE_TELEOP", "0")
    monkeypatch.setenv("H2_DATABASE_URL", "postgresql://example.invalid/inspection")
    monkeypatch.setenv("H2_DATA_DIR", "/inspection-data")

    teleop_endpoint = endpoint.rsplit(":", 1)[0] + ":15052"
    services, database_commands = start(launcher, monkeypatch, "--external-gateway", endpoint,
                                        "--external-teleop-gateway", teleop_endpoint)

    assert list(services) == ["api", "frontend"]
    assert database_commands == []
    for _, command, environment in services.values():
        assert command[command.index("--host") + 1] == "127.0.0.1"
        assert environment["H2_GATEWAY_ADDRESS"] == endpoint
        assert environment["H2_TELEOP_GATEWAY_ADDRESS"] == teleop_endpoint
        assert environment["H2_HARDWARE_TELEOP"] == "1"
        assert environment["H2_DATABASE_URL"] == f"sqlite:///{launcher.RUNTIME / 'hardware.sqlite'}"
        assert environment["H2_DATA_DIR"] == str(launcher.RUNTIME / "hardware-data")
    assert "--no-proxy-headers" in services["api"][1]


@pytest.mark.parametrize("args", [
    ["start", "--external-gateway", "0.0.0.0:15051"],
    ["start", "--external-gateway", "192.0.2.1:15051"],
    ["start", "--external-gateway", "localhost:15051"],
    ["start", "--external-gateway", "127.0.0.1"],
    ["start", "--external-gateway", "127.0.0.1:0"],
    ["start", "--external-gateway", "127.0.0.1:65536"],
    ["start", "--external-gateway", "127.0.0.1:15051"],
    ["start", "--external-teleop-gateway", "127.0.0.1:15052"],
    ["start", "--external-gateway", "127.0.0.1:15051", "--external-teleop-gateway", "127.0.0.1:15051"],
    ["start", "--external-gateway", "127.0.0.1:15051", "--h2-interface", "eth0"],
    ["status", "--external-gateway", "127.0.0.1:15051"],
    ["stop", "--external-gateway", "127.0.0.1:15051"],
])
def test_invalid_external_gateway_options_fail_before_startup(launcher, monkeypatch, args):
    def unexpected_start(*args, **kwargs):
        pytest.fail("Invalid command attempted to start a process")

    monkeypatch.setattr(launcher, "subprocess", SimpleNamespace(Popen=unexpected_start, run=unexpected_start, STDOUT=-2))
    monkeypatch.setattr(launcher.sys, "argv", ["dev.py", *args])
    with pytest.raises(SystemExit) as error:
        launcher.main()

    assert error.value.code == 2
    assert not launcher.RUNTIME.exists()


def test_simulation_still_launches_gateway_and_database(launcher, monkeypatch):
    services, database_commands = start(launcher, monkeypatch)

    assert set(services) == {"gateway", "api", "frontend"}
    assert "--db" in services["gateway"][1]
    assert services["api"][2]["H2_HARDWARE_TELEOP"] == "0"
    assert len(database_commands) == 1
    assert database_commands[0][1:] == [str(launcher.ROOT / "scripts/manage_postgres.py"), "start"]


def test_local_hardware_still_launches_gateway_with_sdk_interface(launcher, monkeypatch):
    services, database_commands = start(launcher, monkeypatch, "--h2-interface", "eth0")

    assert set(services) == {"gateway", "api", "frontend"}
    assert services["gateway"][1][-2:] == ["--h2-interface", "eth0"]
    assert services["api"][2]["H2_GATEWAY_ADDRESS"] == "127.0.0.1:50051"
    assert services["api"][2]["H2_TELEOP_GATEWAY_ADDRESS"] == "127.0.0.1:50052"
    assert services["api"][2]["H2_HARDWARE_TELEOP"] == "1"
    assert database_commands == []
