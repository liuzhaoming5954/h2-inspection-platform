"""用隔离目录和命令替身验证上位机安装流程。"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.fixture
def workstation(tmp_path):
    checkout = tmp_path / "H2 仓库 &'quote"
    scripts = checkout / "scripts" / "deploy"
    scripts.mkdir(parents=True)
    script = scripts / "setup_workstation.sh"
    source = Path(__file__).resolve().parents[1]
    shutil.copyfile(source / script.relative_to(checkout), script)
    home = tmp_path / "operator-home"
    home.mkdir()
    trace = tmp_path / "commands.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command = bin_dir / "test-command"
    command.write_text(f"#!{sys.executable}\n" + """
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
with Path(os.environ['TEST_TRACE']).open('a') as stream:
    print(json.dumps([name, *args]), file=stream)
if name == 'python3.12':
    target = Path(args[-1]) / 'bin' / 'python'
    target.parent.mkdir(parents=True)
    target.symlink_to(Path(__file__).resolve())
failure = os.environ['TEST_FAILURE']
if failure == name or (failure == 'import' and 'import h2_backend.app' in args[-1]):
    sys.exit(6)
""")
    command.chmod(0o755)
    for name in ("node", "python3.12", "pnpm"):
        (bin_dir / name).symlink_to(command.name)

    field_start = checkout / "scripts" / "h2_field_start.sh"
    field_start.write_text("""#!/bin/bash
printf '%s\\n' "$H2_PC2_HOST" "$H2_FIELD_PASSWORD" "$@"
""")
    field_start.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(home), "TEST_TRACE": str(trace)}
    return script, home / "h2-start.sh", trace, env


def run_setup(workstation, failure=""):
    script, _, _, env = workstation
    return subprocess.run(
        ["/bin/bash", str(script)], env={**env, "TEST_FAILURE": failure},
        cwd=script.parent, capture_output=True, text=True, timeout=5,
    )


def test_clean_workstation_installs_locked_dependencies_and_project_packages(workstation):
    script, wrapper, trace, env = workstation
    checkout = script.parents[2]
    result = run_setup(workstation)
    assert result.returncode == 0, result
    calls = [json.loads(line) for line in trace.read_text().splitlines()]
    assert calls[0][:2] == ["node", "-e"]
    assert calls[1] == ["python3.12", "-m", "venv", str(checkout / ".venv")]
    assert calls[2][:2] == ["python", "-c"]
    assert calls[3:6] == [
        ["python", "-m", "pip", "install", "-r", str(checkout / "requirements-validation.lock.txt")],
        ["python", "-m", "pip", "install", "--no-deps", "--no-build-isolation",
         "-e", str(checkout / "robot-gateway"), "-e", str(checkout / "backend")],
        ["python", "-m", "pip", "check"],
    ]
    assert calls[6][:2] == ["python", "-c"]
    assert "h2_backend.app, h2_gateway.server" in calls[6][2]
    assert calls[7] == ["pnpm", "--dir", str(checkout / "frontend"), "install", "--frozen-lockfile"]
    assert os.access(wrapper, os.X_OK)
    launch = subprocess.run(
        [str(wrapper), "--robot-only", "flag with spaces"],
        env={**env, "H2_PC2_HOST": "10.10.1.2", "H2_FIELD_PASSWORD": "fixture"},
        capture_output=True, text=True, check=True, timeout=5,
    )
    assert launch.stdout.splitlines() == ["10.10.1.2", "fixture", "--robot-only", "flag with spaces"]
    assert "H2_FIELD_PASSWORD" not in wrapper.read_text()


@pytest.mark.parametrize("failure", ["node", "import", "pnpm"])
def test_setup_failure_preserves_existing_workstation_wrapper(workstation, failure):
    _, wrapper, trace, _ = workstation
    wrapper.write_text("existing wrapper\n")
    result = run_setup(workstation, failure=failure)
    assert result.returncode != 0
    assert "== done ==" not in result.stdout
    assert wrapper.read_text() == "existing wrapper\n"
    if failure == "node":
        assert len(trace.read_text().splitlines()) == 1
