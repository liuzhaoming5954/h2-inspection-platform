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
    # ip 替身：只认面向机器人的网口 enprobot，并模拟它的地址（TEST_LINK_ADDR 为空表示没有地址）
    ip = bin_dir / "ip"
    ip.write_text(f"#!{sys.executable}\n" + """
import os
import sys
args = [a for a in sys.argv[1:] if not a.startswith('-')]   # 去掉 -4 -o 这类开关
if args[0] == 'link':
    sys.exit(0 if args[2] == 'enprobot' else 1)
if args[0] == 'route':
    sys.exit(0)
if args[:2] == ['addr', 'show']:
    address = os.environ.get('TEST_LINK_ADDR')
    if 'dev' in args and args[args.index('dev') + 1] == 'enprobot' and address:
        print('2: enprobot    inet ' + address + ' scope global valid_lft forever preferred_lft forever')
    sys.exit(0)
sys.exit(1)
""")
    ip.chmod(0o755)
    # sudo 替身：免密时执行其后的 ip route 命令并记录；TEST_SUDO_ROUTE=deny 时模拟没有免密规则
    sudo = bin_dir / "sudo"
    sudo.write_text(f"#!{sys.executable}\n" + """
import json
import os
import subprocess
import sys
from pathlib import Path
args = sys.argv[1:]                      # -n ip route replace ...
if os.environ.get('TEST_SUDO_ROUTE') == 'deny':
    sys.exit(1)
if args[0] == '-n':
    args = args[1:]
if args[0] != 'ip':                      # 只放行 ip，其他命令一律拒绝，和真实 sudoers 规则一致
    sys.exit(1)
with Path(os.environ['TEST_ROUTES']).open('a') as stream:
    print(json.dumps(args[1:]), file=stream)
sys.exit(subprocess.run([os.environ['TEST_IP'], *args[1:]]).returncode)
""")
    sudo.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(home), "TEST_TRACE": str(trace),
           "TEST_ROUTES": str(tmp_path / "routes.jsonl"), "TEST_IP": str(ip)}
    return script, home / "h2-start.sh", trace, env


def run_setup(workstation, *options, failure="", link_address="192.168.124.10/24"):
    """options 原样传给安装脚本；failure：注入到替身命令里的失败点。"""
    script, _, _, env = workstation
    return subprocess.run(
        ["/bin/bash", str(script), *options],
        env={**env, "TEST_FAILURE": failure, "TEST_LINK_ADDR": link_address},
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


@pytest.mark.parametrize("args", [
    ["--robot-password", "s3cret"],                             # 指定了密码却没有网口
    ["--robot-password", "s3cret", "--robot-link", "eno0"],     # 网口不存在
    ["--robot-password"],                                       # 选项缺少值
    ["--robot-password", ""],                                   # 密码为空
    ["--robot-password", "s3cret", "--bogus"],                  # 未知参数
])
def test_invalid_robot_link_options_fail_before_touching_anything(workstation, args):
    """免密选项不完整、网口不存在或参数未知：直接报错退出，不装依赖也不改启动脚本。"""
    _, wrapper, trace, _ = workstation
    wrapper.write_text("existing wrapper\n")
    result = run_setup(workstation, *args)
    assert result.returncode == 2, result
    assert "ERROR:" in result.stderr
    assert wrapper.read_text() == "existing wrapper\n"
    assert not trace.exists()


def test_robot_password_setup_fails_when_link_has_no_address(workstation):
    _, wrapper, trace, _ = workstation
    wrapper.write_text("existing wrapper\n")
    result = run_setup(workstation, "--robot-password", "s3cret", "--robot-link", "enprobot",
                       link_address="")
    assert result.returncode == 2, result
    assert "no IPv4 address" in result.stderr
    assert wrapper.read_text() == "existing wrapper\n"
    assert not trace.exists()


def test_robot_password_setup_writes_private_wrapper_with_routes(workstation):
    """专机一键免密：密码写进仅本人可读的启动脚本，并把 PC2/PC3 钉到面向机器人的网口。"""
    script, wrapper, _, env = workstation
    checkout = script.parents[2]
    result = run_setup(workstation, "--robot-password", "p@ss 'word", "--robot-link", "enprobot")
    assert result.returncode == 0, result
    text = wrapper.read_text()
    assert os.access(wrapper, os.X_OK)
    assert wrapper.stat().st_mode & 0o077 == 0, "含密码的启动脚本不得让其他用户读取"
    # 路由先于正式启动脚本建立；重启后路由表清空，每次启动都要重建
    routes_at, launch_at = text.index("ip route replace"), text.index("exec ")
    assert routes_at < launch_at
    for host in ("192.168.124.162", "192.168.124.163"):
        assert f'ip route replace "$host/32" dev enprobot src 192.168.124.10' in text
    # 路径在脚本里经过 shell 转义（仓库目录名含空格与引号），实际运行时必须还原成正确路径
    exec_line = next(line for line in text.splitlines() if line.startswith("exec "))
    assert subprocess.run(["/bin/bash", "-c", f"printf '%s' {exec_line[len('exec '):].split(' "$@"')[0]}"],
                          capture_output=True, text=True, check=True).stdout == str(
        checkout / "scripts" / "h2_field_start.sh")
    # 实际运行：路由经免密 sudo 建立，正式脚本拿到的就是内置密码，参数原样透传
    launch_env = {k: v for k, v in env.items() if k != "TEST_LINK_ADDR"}
    launch = subprocess.run([str(wrapper), "--robot-only"], env=launch_env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=5)
    assert launch.returncode == 0, launch.stderr
    assert launch.stdout.splitlines()[-2:] == ["p@ss 'word", "--robot-only"]
    routes = [json.loads(line) for line in (wrapper.parent.parent / "routes.jsonl").read_text().splitlines()]
    assert routes == [
        ["route", "replace", "192.168.124.162/32", "dev", "enprobot", "src", "192.168.124.10"],
        ["route", "replace", "192.168.124.163/32", "dev", "enprobot", "src", "192.168.124.10"],
    ]


def test_robot_route_failure_stops_before_starting_the_stack(workstation):
    """路由建不起来（没有 sudo 免密规则）时必须中止：否则会拖到连 PC2 才失败，且看不出原因。"""
    _, wrapper, trace, env = workstation
    run_setup(workstation, "--robot-password", "s3cret", "--robot-link", "enprobot")
    launch = subprocess.run(
        [str(wrapper)], env={**env, "TEST_LINK_ADDR": "", "TEST_SUDO_ROUTE": "deny"},
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5,
    )
    assert launch.returncode == 1
    assert "无法把 192.168.124.162 指向网口 enprobot" in launch.stderr
    assert "sudo" in launch.stderr
    started = [json.loads(line) for line in trace.read_text().splitlines()]
    assert not any("h2_field_start" in " ".join(call) for call in started), "路由失败后不得启动现场链路"


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
