"""Battery telemetry: bms watcher degradation and wire mapping."""

import time

from h2_gateway.client import GatewayClient
from h2_gateway.hardware import H2HardwareGateway
from h2_gateway.server import create_server

from test_hardware import RecordingLoco, wait_for


class FakeBattery:
    def __init__(self):
        self.percent = 21.0

    def battery(self):
        return self.percent


def test_gateway_state_carries_battery_until_it_stales():
    battery = FakeBattery()
    gateway = H2HardwareGateway(RecordingLoco(), battery=battery)
    try:
        wait_for(gateway, lambda state: state["teleop_ready"])
        assert gateway.get_state()["battery_percent"] == 21.0

        battery.percent = None  # BMS topic stale or absent
        assert "battery_percent" not in gateway.get_state()
    finally:
        gateway.close()


def test_battery_over_grpc_maps_to_none_when_absent():
    battery = FakeBattery()
    gateway = H2HardwareGateway(RecordingLoco(), battery=battery)
    server, port = create_server(gateway, port=0)
    server.start()
    try:
        with GatewayClient(f"127.0.0.1:{port}") as client:
            deadline = time.monotonic() + 2
            state = client.get_state()
            while state["teleop_ready"] is not True and time.monotonic() < deadline:
                time.sleep(0.02)
                state = client.get_state()
            assert state["battery_percent"] == 21.0

            battery.percent = None
            deadline = time.monotonic() + 2
            state = client.get_state()
            while state["battery_percent"] is not None and time.monotonic() < deadline:
                time.sleep(0.02)
                state = client.get_state()
            assert state["battery_percent"] is None
    finally:
        server.stop(grace=1).wait()
        gateway.close()
