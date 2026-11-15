"""使用命令替身检查 PC3 新装和重复部署流程。"""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


SDK_PIN = "814556d15970dd2ecf1c9984e845ca02ab07e206"


@pytest.fixture
def pc3_setup(tmp_path):
    checkout = tmp_path / "h2 checkout"
    script = checkout / "scripts/deploy/setup_pc3.sh"
    script.parent.mkdir(parents=True)
    root = Path(__file__).resolve().parents[1]
    shutil.copyfile(root / "scripts/deploy/setup_pc3.sh", script)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    event_log = tmp_path / "events.jsonl"
    stub = fake_bin / "stub"
    stub.write_text(f'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import shutil
import sys

command = Path(sys.argv[0]).name
args = sys.argv[1:]
runtime = Path(os.environ["FIXTURE_RUNTIME"])
dds_home = runtime / "cyclonedds-install"
binding = runtime / "binding-installed"
with open(os.environ["FIXTURE_LOG"], "a") as log:
    print(json.dumps((command, args, os.environ.get("CYCLONEDDS_HOME"))), file=log)

if command in ("python3.12", "python"):
    if args[:2] == ["-m", "venv"]:
        target = Path(args[2]) / "bin/python"
        target.parent.mkdir(parents=True)
        shutil.copy2(sys.argv[0], target)
    elif args == ["-", "0.10.2"]:
        sys.stdin.read()
        sys.exit(not binding.is_file())
    elif "cyclonedds==0.10.2" in args:
        if os.environ["FIXTURE_FAIL"] != "binding":
            binding.touch()
    elif args[0] == "-c" and "LocoClient" in args[1]:
        sys.exit(31 if os.environ["FIXTURE_FAIL"] == "imports" else 0)
elif command == "git":
    if args[0] == "clone":
        Path(args[-1]).mkdir()
    elif args[0] == "-C" and args[2] == "rev-parse":
        print("{SDK_PIN}" if args[1].endswith("unitree_sdk2_python") else "cyclonedds-release")
elif command == "cmake":
    if args[0] == "--build" and os.environ["FIXTURE_FAIL"] == "cmake":
        sys.exit(32)
    if args[0] == "--install":
        (dds_home / "lib").mkdir(parents=True)
        (dds_home / "lib/libddsc.so").touch()
        (dds_home / "lib/libddsc.so.0.10.2").touch()
        (dds_home / "include/dds").mkdir(parents=True)
        (dds_home / "include/dds/dds.h").touch()
''')
    stub.chmod(0o755)
    for command in ("python3.12", "git", "cmake"):
        (fake_bin / command).symlink_to(stub.name)

    def run(fail=""):
        event_log.unlink(missing_ok=True)
        result = subprocess.run(
            ["/bin/bash", str(script)],
            env={
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "FIXTURE_FAIL": fail,
                "FIXTURE_LOG": str(event_log),
                "FIXTURE_RUNTIME": str(checkout / ".runtime/h2-sdk2"),
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result, [json.loads(line) for line in event_log.read_text().splitlines()]

    return checkout, run


def test_clean_pc3_install_builds_dds_and_installs_gateway_and_sdk(pc3_setup):
    checkout, run = pc3_setup
    runtime = checkout / ".runtime/h2-sdk2"
    dds_home = str(runtime / "cyclonedds-install")
    result, events = run()
    assert result.returncode == 0, result.stderr
    assert "== done." in result.stdout
    configure = next(args for command, args, _ in events if command == "cmake" and args[0] == "-S")
    assert f"-DCMAKE_INSTALL_PREFIX={dds_home}" in configure
    assert "-DENABLE_SHM=OFF" in configure
    assert "-DBUILD_DDSPERF=OFF" in configure
    assert "-DCMAKE_DISABLE_FIND_PACKAGE_iceoryx_binding_c=TRUE" in configure
    sdk_source = str(runtime / "unitree_sdk2_python")
    assert ["git", ["-C", sdk_source, "checkout", "--detach", SDK_PIN], None] in events
    assert [
        "python",
        ["-m", "pip", "install", "-e", str(checkout / "robot-gateway"), "numpy", "opencv-python-headless"],
        dds_home,
    ] in events
    assert ["python", ["-m", "pip", "install", "--no-deps", sdk_source], dds_home] in events
    bindings = next((args, env) for _, args, env in events if "cyclonedds==0.10.2" in args)
    assert "--no-binary=cyclonedds" in bindings[0]
    assert bindings[1] == dds_home

    result, repeated = run()
    assert result.returncode == 0, result.stderr
    assert not any(command in {"cmake", "python3.12"} or args[0] == "clone" for command, args, _ in repeated)
    assert not any("cyclonedds==0.10.2" in args for _, args, _ in repeated)


@pytest.mark.parametrize("failure, code", [("cmake", 32), ("binding", 1), ("imports", 31)])
def test_pc3_setup_exits_on_failure(pc3_setup, failure, code):
    _, run = pc3_setup
    result, _ = run(fail=failure)
    assert result.returncode == code
    assert "== done." not in result.stdout
