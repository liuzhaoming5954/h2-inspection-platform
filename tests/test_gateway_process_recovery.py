"""Actual process termination/restart, persisted state and gRPC reconciliation."""
from __future__ import annotations

import socket
import subprocess
import sys
import time

from h2_gateway.client import GatewayClient


def test_killed_gateway_does_not_resume_or_replay_motion(tmp_path):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    database = tmp_path / 'gateway.sqlite'
    command = [sys.executable, '-m', 'h2_gateway.server', '--host', '127.0.0.1',
               '--port', str(port), '--db', str(database), '--step-delay', '0.2']
    processes = []

    def launch():
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        processes.append(process)
        client = GatewayClient(f'127.0.0.1:{port}', timeout=0.5)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert process.poll() is None, process.communicate()
            try:
                client.get_state()
                return process, client
            except Exception:
                time.sleep(0.1)
        raise AssertionError('Gateway failed to become ready')

    try:
        process, client = launch()
        points = [{'id': str(i), 'label': f'合成巡检点{i}', 'x': i, 'y': 0} for i in range(20)]
        deadline_ms = int(time.time() * 1000) + 60_000
        assert client.start_task('recovery-task', 'durable-command', deadline_ms, points)['accepted']
        deadline = time.monotonic() + 5
        while not client.get_task('recovery-task')['samples'] and time.monotonic() < deadline:
            time.sleep(0.05)
        assert client.get_task('recovery-task')['status'] == 'running'
        process.kill()
        process.wait(timeout=5)
        client.close()

        _, restarted = launch()
        result = restarted.get_task('recovery-task')
        assert result['status'] == 'interrupted'
        assert len(result['samples']) >= 1
        assert restarted.get_state()['start_count'] == 1
        # The original ACK is durable, but the task snapshot remains interrupted.
        assert restarted.start_task('recovery-task', 'durable-command', deadline_ms, points)['accepted']
        assert restarted.get_task('recovery-task')['status'] == 'interrupted'
        assert restarted.get_state()['start_count'] == 1
        assert not restarted.start_task('stale-task', 'expired-command', 1, points)['accepted']
        assert restarted.get_state()['start_count'] == 1
        restarted.close()
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
