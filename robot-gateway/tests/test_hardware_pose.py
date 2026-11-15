"""Pose telemetry: odometry watcher degradation and wire mapping."""

import time

from h2_gateway.client import GatewayClient
from h2_gateway.hardware import H2HardwareGateway
from h2_gateway.server import create_server

from test_hardware import RecordingLoco, wait_for


class FakeOdom:
    def __init__(self):
        self.value = {"x": 1.25, "y": -3.5, "yaw": 0.42}

    def pose(self):
        return self.value


def test_gateway_state_carries_pose_until_it_stales():
    odom = FakeOdom()
    gateway = H2HardwareGateway(RecordingLoco(), odom=odom)
    try:
        wait_for(gateway, lambda state: state["teleop_ready"])
        assert gateway.get_state()["pose"] == {"x": 1.25, "y": -3.5, "yaw": 0.42}

        odom.value = None  # odometry topic stale or absent
        assert "pose" not in gateway.get_state()
    finally:
        gateway.close()


def test_pose_over_grpc_maps_to_none_when_absent():
    odom = FakeOdom()
    gateway = H2HardwareGateway(RecordingLoco(), odom=odom)
    server, port = create_server(gateway, port=0)
    server.start()
    try:
        with GatewayClient(f"127.0.0.1:{port}") as client:
            deadline = time.monotonic() + 2
            state = client.get_state()
            while state["teleop_ready"] is not True and time.monotonic() < deadline:
                time.sleep(0.02)
                state = client.get_state()
            assert state["pose"] == {"x": 1.25, "y": -3.5, "yaw": 0.42}

            odom.value = None
            deadline = time.monotonic() + 2
            state = client.get_state()
            while state["pose"] is not None and time.monotonic() < deadline:
                time.sleep(0.02)
                state = client.get_state()
            assert state["pose"] is None
    finally:
        server.stop(grace=0).wait(timeout=3)
        gateway.close()
