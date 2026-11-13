#!/usr/bin/env python3
"""Start or stop the H2 workstation services and optional local gateway."""
from __future__ import annotations

import argparse
from ipaddress import ip_address
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.runtime'
PID_FILE = RUNTIME / 'dev.pid'
PYTHON = ROOT / '.venv' / 'bin' / 'python'


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def wait_url(url: str, process: subprocess.Popen, timeout: float = 30) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if process.poll() is not None:
            raise RuntimeError(f'Process exited with {process.returncode}; see .runtime/logs')
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(0.2)
    raise RuntimeError(f'Timed out waiting for {url}')


def loopback_endpoint(value: str) -> str:
    host, separator, port = value.rpartition(':')
    try:
        address = ip_address(host.removeprefix('[').removesuffix(']'))
    except ValueError:
        address = None
    if (not separator or address is None or not address.is_loopback or '%' in host
            or (address.version == 6) != (host.startswith('[') and host.endswith(']'))
            or (address.version == 4 and ('[' in host or ']' in host))
            or not 1 <= len(port) <= 5 or not port.isascii() or not port.isdecimal()
            or not 1 <= int(port) <= 65535):
        raise argparse.ArgumentTypeError('use a loopback IP and port, e.g. 127.0.0.1:15051 or [::1]:15051')
    return f'[{address}]:{int(port)}' if address.version == 6 else f'{address}:{int(port)}'


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('command', nargs='?', default='start', choices=['start', 'stop', 'status'])
    parser.add_argument('--skip-db', action='store_true', help='Use an already running/external database')
    gateway_mode = parser.add_mutually_exclusive_group()
    gateway_mode.add_argument('--h2-interface', help='Use a local H2 SDK2 gateway on the named interface')
    gateway_mode.add_argument('--external-gateway', type=loopback_endpoint, metavar='LOOPBACK:PORT',
                              help='Use an external H2 gateway via a local SSH forward, without starting a gateway')
    args = parser.parse_args()
    hardware_mode = args.h2_interface is not None or args.external_gateway is not None
    if hardware_mode and args.command != 'start':
        parser.error('--h2-interface and --external-gateway are only used with start')
    RUNTIME.mkdir(exist_ok=True)
    pid = int(PID_FILE.read_text()) if PID_FILE.exists() else None
    if args.command in {'stop', 'status'}:
        running = bool(pid and alive(pid))
        if args.command == 'stop' and running:
            command_line = Path(f'/proc/{pid}/cmdline').read_bytes()
            if b'dev.py' not in command_line:
                raise SystemExit('PID does not belong to this development supervisor')
            os.kill(pid, signal.SIGTERM)
            print('Stop requested. PostgreSQL data is retained; database stays running.')
        else:
            print('running' if running else 'stopped')
        return
    if pid and alive(pid):
        raise SystemExit(f'Validation supervisor already running (PID {pid})')
    if not PYTHON.exists():
        raise SystemExit('Install project dependencies first; see README.md')
    if not args.skip_db and not hardware_mode:
        subprocess.run([sys.executable, str(ROOT / 'scripts/manage_postgres.py'), 'start'], check=True)
    environment = os.environ.copy()
    environment['PATH'] = os.pathsep.join((str(PYTHON.parent), environment.get('PATH', os.defpath)))
    environment['H2_HARDWARE_TELEOP'] = '1' if hardware_mode else '0'
    gateway_args = ['--db', str(RUNTIME / 'gateway.sqlite')]
    mode = 'SIMULATION ONLY'
    if hardware_mode:
        environment['H2_DATABASE_URL'] = f'sqlite:///{RUNTIME / "hardware.sqlite"}'
        environment['H2_GATEWAY_ADDRESS'] = args.external_gateway or '127.0.0.1:50051'
        environment['H2_DATA_DIR'] = str(RUNTIME / 'hardware-data')
        if args.external_gateway:
            mode = 'H2 EXTERNAL GATEWAY'
        else:
            mode = 'H2 HARDWARE TELEOP'
            gateway_args = ['--h2-interface', args.h2_interface]
    else:
        environment.setdefault('H2_DATABASE_URL', 'postgresql+psycopg://h2@127.0.0.1:55432/h2_validation')
        environment.setdefault('H2_GATEWAY_ADDRESS', '127.0.0.1:50051')
        environment.setdefault('H2_DATA_DIR', str(RUNTIME / 'data'))
    node = shutil.which('node', path=environment['PATH'])
    if not node:
        raise SystemExit('Node.js not found; install/use Node.js 24')
    log_dir = RUNTIME / 'logs'
    log_dir.mkdir(exist_ok=True)
    commands = [
        ('api', [str(PYTHON), '-m', 'uvicorn', 'h2_backend.app:create_app', '--factory',
                 '--host', '127.0.0.1', '--port', '8000', '--no-proxy-headers'], ROOT),
        ('frontend', [node, str(ROOT / 'frontend/node_modules/vite/bin/vite.js'),
                      '--host', '127.0.0.1', '--port', '5173', '--strictPort'], ROOT / 'frontend'),
    ]
    if not args.external_gateway:
        commands.insert(0, ('gateway', [str(PYTHON), '-m', 'h2_gateway.server',
                                        '--host', '127.0.0.1', '--port', '50051', *gateway_args], ROOT))
    processes: dict[str, subprocess.Popen] = {}
    files = []
    stopping = False

    def stop(_signum: int, _frame) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    PID_FILE.write_text(str(os.getpid()))
    try:
        for name, command, cwd in commands:
            output = (log_dir / f'{name}.log').open('a')
            files.append(output)
            child = subprocess.Popen(command, cwd=cwd, env=environment,
                                     stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            processes[name] = child
            print(f'{name}: PID {child.pid}, log {log_dir / (name + ".log")}', flush=True)
        wait_url('http://127.0.0.1:8000/api/health', processes['api'])
        wait_url('http://127.0.0.1:5173', processes['frontend'])
        print(json.dumps({'page': 'http://127.0.0.1:5173',
                          'api': 'http://127.0.0.1:8000/docs',
                          'mode': mode},
                         ensure_ascii=False), flush=True)
        while not stopping:
            for process in processes.values():
                if process.poll() is not None:
                    raise RuntimeError(f'Service PID {process.pid} exited; see logs')
            time.sleep(0.5)
    finally:
        for process in reversed(processes.values()):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process in processes.values():
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        for output in files:
            output.close()
        PID_FILE.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
