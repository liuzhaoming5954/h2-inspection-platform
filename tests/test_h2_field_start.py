"""使用命令替身验证一键启动的转发检查和上位机停启。"""

import os
from pathlib import Path
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
      *:8090/environment) printf '%s\\n' '{"available":true}' ;;
      *:8000/api/health|*:5173/) grep -q '^scripts/dev.py start ' "$TEST_WORKSTATION_CALLS" ;;
      *) exit 98 ;;
    esac ;;
  sshpass)
    case "$*" in
      *h2_gateway.hardware*) printf '%s\\n' '{"teleop_ready": true}' ;;
      *pgrep*) exit 0 ;;
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
  timeout) exit 1 ;;
esac
""")
    commands.chmod(0o755)
    for name in ("curl", "sshpass", "ss", "tr", "readlink", "timeout"):
        (bin_dir / name).symlink_to(commands.name)

    workstation = tmp_path / ".venv" / "bin" / "python"
    workstation.parent.mkdir(parents=True)
    workstation.write_text("""#!/bin/bash
if [[ "$1" == '-' ]]; then exec "$TEST_PYTHON" "$@"; fi
printf '%s\\n' "$*" >> "$TEST_WORKSTATION_CALLS"
[[ "$TEST_ALLOW_WORKSTATION" == 1 ]] || exit 97
[[ $(<"$TEST_PROBE_CALLS") == $'15051\\n15052' ]] || exit 96
case "$*" in
  'scripts/dev.py stop'|'scripts/dev.py start '*) exit 0 ;;
  'scripts/dev.py status') printf '%s\\n' stopped ;;
  'scripts/h2_field_check.py') grep -q '^scripts/dev.py start ' "$TEST_WORKSTATION_CALLS" ;;
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

    def run(*args, failure="", failed_port=0, allow_workstation=False):
        result = subprocess.run(
            ["/bin/bash", str(copied_script), *args],
            env={
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "H2_PC2_HOST": "127.0.0.1",
                "H2_PC3_HOST": "127.0.0.1",
                "H2_FIELD_PASSWORD": "fixture",
                "TEST_FAILURE": failure,
                "TEST_FAILED_PORT": str(failed_port),
                "TEST_LAST_PORT": str(tmp_path / "last-port"),
                "TEST_PROCESS_PID": str(os.getpid()),
                "TEST_PARENT_PID": str(os.getppid()),
                "TEST_PYTHON": sys.executable,
                "TEST_PROBE_CALLS": str(probe_calls),
                "TEST_ALLOW_WORKSTATION": "1" if allow_workstation else "0",
                "TEST_WORKSTATION_CALLS": str(workstation_calls),
            },
            capture_output=True,
            text=True,
            timeout=5,
        )
        calls = workstation_calls.read_text().splitlines() if workstation_calls.exists() else []
        if not allow_workstation:
            assert calls == []
        probes = probe_calls.read_text().splitlines() if probe_calls.exists() else []
        return result, probes, calls

    return run


@pytest.mark.parametrize("local_port,remote_port", [(15051, 50051), (15052, 50052)])
@pytest.mark.parametrize("args", [(), ("--robot-only",)])
def test_forward_failure_leaves_workstation_alone(field_start, local_port, remote_port, args):
    result, probes, _ = field_start(*args, failure="ssh", failed_port=local_port)
    assert result.returncode == 1, result
    assert f"NOT READY: SSH forward {local_port}:{remote_port} failed." in result.stderr
    assert probes == []


def test_forward_listeners_with_unreachable_gateway_leave_workstation_alone(field_start):
    result, probes, _ = field_start(failure="probe", failed_port=15052)
    assert result.returncode == 1, result
    assert "NOT READY: gRPC GetState failed through SSH forward 15052:50052" in result.stderr
    assert probes == ["15051", "15052"]


def test_robot_only_accepts_a_reachable_gateway_before_h2_is_ready(field_start):
    result, probes, _ = field_start("--robot-only")
    assert result.returncode == 0, result
    assert probes == ["15051", "15052"]


def test_full_start_restarts_workstation_once_after_both_forward_checks(field_start):
    result, probes, calls = field_start(allow_workstation=True)
    assert result.returncode == 0, result
    assert "READY: open http://127.0.0.1:5173" in result.stdout
    assert probes == ["15051", "15052"]
    assert [call for call in calls if call != "scripts/dev.py status"] == [
        "scripts/dev.py stop",
        "scripts/dev.py start --external-gateway 127.0.0.1:15051 --external-teleop-gateway 127.0.0.1:15052",
        "scripts/h2_field_check.py",
    ]
