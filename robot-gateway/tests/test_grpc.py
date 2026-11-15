"""验证实际的 Protobuf 序列化、TCP 通信及图像字节传输。"""

import time

from h2_gateway.client import GatewayClient
from h2_gateway.engine import SimulatorEngine, now_ms
from h2_gateway.server import create_server


def test_real_grpc_round_trip_and_idempotency(tmp_path):
    engine = SimulatorEngine(tmp_path / "gateway.sqlite", step_delay=0.03)
    server, port = create_server(engine, port=0)
    server.start()
    try:
        with GatewayClient(f"127.0.0.1:{port}") as client:
            assert client.get_state()["robot_id"] == "H2-SIM-001"
            points = [{"id": "rack", "label": "机柜A01", "x": 1, "y": 2}]
            reply = client.start_task("g1", "cmd1", now_ms() + 10_000, points)
            assert reply["accepted"] is True
            assert client.start_task("g1", "cmd1", now_ms() + 10_000, points) == reply
            end = time.monotonic() + 3
            while time.monotonic() < end:
                result = client.get_task("g1")
                if result["status"] == "completed":
                    break
                time.sleep(0.02)
            assert result["status"] == "completed"
            assert result["samples"][0]["label"] == "机柜A01"
            assert result["samples"][0]["image_png"].startswith(b"\x89PNG")
            assert client.get_state()["start_count"] == 1
            assert client.get_task("missing")["status"] == "not_found"
    finally:
        server.stop(grace=1).wait()
        engine.close()
