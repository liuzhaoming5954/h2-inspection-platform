"""使用命令替身验证一键启动的转发检查和上位机停启。"""

import os
import pty
from pathlib import Path
import shlex
import subprocess
import sys

import pytest


@pytest.fixture
def field_start(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "h2_field_start.sh"
    copied_script = tmp_path / "scripts" / script.name
    copied_script.parent.mkdir()
    copied_script.write_bytes(script.read_bytes())

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    commands = bin_dir / "test-command"
    commands.write_text("""#!/bin/bash
case "${0##*/}" in
  curl)
    case "$*" in
      *:8090/environment)
        if [[ "$TEST_START_COMPONENTS" == 1 ]]; then
          printf '%s\\n' "${!#}" >> "$TEST_SENSOR_CALLS"
          exit 1
        fi
        printf '%s\\n' '{"available":true}' ;;
      *:8000/api/health|*:5173/) grep -q '^scripts/dev.py start ' "$TEST_WORKSTATION_CALLS" ;;
      *) exit 98 ;;
    esac ;;
  sshpass)
    case "$*" in
      *h2_gateway.hardware*) printf '%s\\n' '{"teleop_ready": true}' ;;
      *pgrep*) [[ "$TEST_START_COMPONENTS" == 0 ]] ;;
      *nohup*)
        [[ "$TEST_START_COMPONENTS" == 1 ]] || exit 98
        printf '%s\\n' "${@: -2}" >> "$TEST_SENSOR_CALLS" ;;
      *) exit 98 ;;
    esac ;;
  ss)
    port=${2##*:}
    printf '%s\\n' "$port" > "$TEST_LAST_PORT"
    if [[ "$TEST_FAILURE" != ssh || "$port" != "$TEST_FAILED_PORT" ]]; then
      pid=$TEST_PROCESS_PID
      if [[ "$port" == 15052 ]]; then pid=$TEST_PARENT_PID; fi
      printf 'LISTEN 0 128 127.0.0.1:%s 0.0.0.0:* pid=%s,\\n' "$port" "$pid"
    fi ;;
  tr)
    read -r port < "$TEST_LAST_PORT"
    printf 'ssh -S none -L 127.0.0.1:%s:127.0.0.1:%s unitree@%s \\n' "$port" "$((port + 35000))" "$H2_PC3_HOST" ;;
  readlink) printf '%s\\n' /test/bin/ssh ;;
  timeout)
    case "$*" in
      *h2_gateway.server*) shift; exec "$@" ;;
      *) exit 1 ;;
    esac ;;
  sleep) exit 0 ;;
esac
""")
    commands.chmod(0o755)
    for name in ("curl", "sshpass", "ss", "tr", "readlink", "timeout", "sleep"):
        (bin_dir / name).symlink_to(commands.name)

    workstation = tmp_path / ".venv" / "bin" / "python"
    workstation.parent.mkdir(parents=True)
    workstation.write_text("""#!/bin/bash
if [[ "$1" == '-' ]]; then exec "$TEST_PYTHON" "$@"; fi
printf '%s\\n' "$*" >> "$TEST_WORKSTATION_CALLS"
[[ "$TEST_ALLOW_WORKSTATION" == 1 ]] || exit 97
[[ $(<"$TEST_PROBE_CALLS") == $'15051\\n15052' ]] || exit 96
case "$*" in
  'scripts/dev.py stop') exit 0 ;;
  # 记录上位机后端实际拿到的现场密码（未设置记为 <unset>）
  'scripts/dev.py start '*) printf '%s\\n' "${H2_FIELD_PASSWORD-<unset>}" >> "$TEST_WORKSTATION_PASSWORDS"; exit 0 ;;
  'scripts/dev.py status') printf '%s\\n' stopped ;;
  # 自检替身：TEST_SELF_CHECK=fail 模拟机器人未进入运控就绪（真机上是 h2_field_check 返回非零）
  'scripts/h2_field_check.py') grep -q '^scripts/dev.py start ' "$TEST_WORKSTATION_CALLS" && [[ "$TEST_SELF_CHECK" != fail ]] ;;
  *) exit 98 ;;
esac
""")
    workstation.chmod(0o755)
    gateway = tmp_path / "h2_gateway"
    gateway.mkdir()
    (gateway / "__init__.py").touch()
    (gateway / "client.py").write_text("""import os
from pathlib import Path

class GatewayClient:
    def __init__(self, target, timeout):
        assert timeout == 3
        self.port = target.rsplit(':', 1)[1]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get_state(self):
        with Path(os.environ['TEST_PROBE_CALLS']).open('a') as output:
            print(self.port, file=output)
        if os.environ['TEST_FAILURE'] == 'probe' and self.port == os.environ['TEST_FAILED_PORT']:
            raise OSError('转发对端未应答')
        return {'simulated': False, 'status': 'not_ready'}
""")
    workstation_calls = tmp_path / "workstation-calls"
    probe_calls = tmp_path / "probe-calls"
    workstation_passwords = tmp_path / "workstation-passwords"

    def run(*args, failure="", failed_port=0, allow_workstation=False, sensor_settings=None,
            password="fixture", typed_password=None, self_check="pass"):
        """password：环境变量里的密码（None 表示不设置）。
        typed_password：设置后经由伪终端输入密码，模拟操作员在终端提示下输入。"""
        stdin = None
        master = slave = None
        if typed_password is not None:
            # 脚本用 [ -t 0 ] 判断是否在终端运行；伪终端让交互输入路径成立
            master, slave = pty.openpty()
            os.write(master, f"{typed_password}\n".encode())
            stdin = slave
        environment = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "H2_PC2_HOST": "127.0.0.1",
            "H2_PC3_HOST": "127.0.0.1",
            "TEST_FAILURE": failure,
            "TEST_FAILED_PORT": str(failed_port),
            "TEST_LAST_PORT": str(tmp_path / "last-port"),
            "TEST_PROCESS_PID": str(os.getpid()),
            "TEST_PARENT_PID": str(os.getppid()),
            "TEST_PYTHON": sys.executable,
            "TEST_PROBE_CALLS": str(probe_calls),
            "TEST_ALLOW_WORKSTATION": "1" if allow_workstation else "0",
            "TEST_WORKSTATION_CALLS": str(workstation_calls),
            "TEST_WORKSTATION_PASSWORDS": str(workstation_passwords),
            "TEST_START_COMPONENTS": "1" if sensor_settings else "0",
            "TEST_SELF_CHECK": self_check,
            "TEST_SENSOR_CALLS": str(tmp_path / "sensor-calls"),
            **(sensor_settings or {}),
        }
        if password is not None:
            environment["H2_FIELD_PASSWORD"] = password
        try:
            result = subprocess.run(
                ["/bin/bash", str(copied_script), *args],
                env=environment, stdin=stdin,
                capture_output=True, text=True, timeout=5,
            )
        finally:
            for fd in (slave, master):
                if fd is not None:
                    os.close(fd)
        calls = workstation_calls.read_text().splitlines() if workstation_calls.exists() else []
        if not allow_workstation:
            assert calls == []
        probes = probe_calls.read_text().splitlines() if probe_calls.exists() else []
        # 上位机后端每次启动时拿到的现场密码
        passwords = (workstation_passwords.read_text().splitlines()
                     if workstation_passwords.exists() else [])
        return result, probes, calls, passwords

    return run


@pytest.mark.parametrize("local_port,remote_port", [(15051, 50051), (15052, 50052)])
@pytest.mark.parametrize("args", [(), ("--robot-only",)])
def test_forward_failure_leaves_workstation_alone(field_start, local_port, remote_port, args):
    result, probes, _, _ = field_start(*args, failure="ssh", failed_port=local_port)
    assert result.returncode == 1, result
    assert f"NOT READY: SSH forward {local_port}:{remote_port} failed." in result.stderr
    assert probes == []


def test_forward_listeners_with_unreachable_gateway_leave_workstation_alone(field_start):
    result, probes, _, _ = field_start(failure="probe", failed_port=15052)
    assert result.returncode == 1, result
    assert "NOT READY: gRPC GetState failed through SSH forward 15052:50052" in result.stderr
    assert probes == ["15051", "15052"]


def test_robot_only_accepts_a_reachable_gateway_before_h2_is_ready(field_start):
    result, probes, _, _ = field_start("--robot-only")
    assert result.returncode == 0, result
    assert probes == ["15051", "15052"]


def test_full_start_restarts_workstation_once_after_both_forward_checks(field_start):
    result, probes, calls, _ = field_start(allow_workstation=True)
    assert result.returncode == 0, result
    assert "READY: hardware ready, teleoperation unlocked." in result.stdout
    assert "上位机页面：请在本机浏览器打开 http://127.0.0.1:5173" in result.stdout
    assert probes == ["15051", "15052"]
    assert [call for call in calls if call != "scripts/dev.py status"] == [
        "scripts/dev.py stop",
        "scripts/dev.py start --external-gateway 127.0.0.1:15051 --external-teleop-gateway 127.0.0.1:15052",
        "scripts/h2_field_check.py",
    ]


def test_page_address_is_shown_even_when_self_check_fails(field_start):
    """机器人未进入运控就绪时自检失败，但上位机与链路已经起来：
    脚本要以非零退出提醒现场，同时仍然给出页面地址（2026-11-28 现场反馈，
    自检不过时原来直接退出，操作员看不到该打开哪个网址）。"""
    result, probes, calls, _ = field_start(allow_workstation=True, self_check="fail")
    assert result.returncode == 1, result
    assert "NOT READY: fix the issues above before touching the robot." in result.stdout
    last_line = result.stdout.strip().splitlines()[-1]
    assert "http://127.0.0.1:5173" in last_line, last_line
    # 失败只影响自检结论，不回滚已经拉起的现场链路与上位机
    assert probes == ["15051", "15052"]
    assert "scripts/dev.py start --external-gateway 127.0.0.1:15051 --external-teleop-gateway 127.0.0.1:15052" in calls
    assert all("dev.py stop" not in call or i == 0 for i, call in enumerate(calls) if "dev.py" in call)


def test_full_start_passes_environment_password_to_workstation(field_start):
    """密码来自环境变量：上位机后端原样拿到它（链路恢复按钮与巡检准备靠它远程操作）。"""
    result, _, _, passwords = field_start(allow_workstation=True)
    assert result.returncode == 0, result
    assert passwords == ["fixture"]


def test_full_start_passes_prompted_password_to_workstation(field_start):
    """密码来自终端输入：过去它只是脚本内的普通变量，后端继承不到，表现为链路正常
    但「巡检准备」报「H2_FIELD_PASSWORD 未设置」（2026-11-27 现场）。现在同样传给后端。"""
    result, _, _, passwords = field_start(allow_workstation=True, password=None,
                                          typed_password="typed-secret")
    assert result.returncode == 0, result
    assert passwords == ["typed-secret"]


@pytest.mark.parametrize("override_url", [None, "http://pc2-private.example:8090/environment?name='h2'&lane=pc3"])
def test_one_click_passes_configured_sensor_addresses(field_start, tmp_path, override_url):
    settings = {"H2_PC2_HOST": "10.42.8.22", "H2_PC2_BIND": "10.42.9.22", "H2_PC3_HOST": "10.42.8.23"}
    if override_url is not None:
        settings["H2_SENSOR_AGENT_URL"] = override_url

    result, probes, _, _ = field_start("--robot-only", sensor_settings=settings)
    assert result.returncode == 0, result
    assert probes == ["15051", "15052"]
    monitor_url, pc2_host, pc2_command, pc3_host, pc3_command = (tmp_path / "sensor-calls").read_text().splitlines()
    assert monitor_url == "http://10.42.8.22:8090/environment"
    assert pc2_host == "unitree@10.42.8.22"
    assert "H2_PC2_BIND=10.42.9.22" in shlex.split(pc2_command)
    assert pc3_host == "unitree@10.42.8.23"
    assert f"H2_SENSOR_AGENT_URL={override_url or monitor_url}" in shlex.split(pc3_command)
