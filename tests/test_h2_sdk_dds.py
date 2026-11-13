"""Opt-in WebSocket, gRPC and Unitree DDS integration with a loopback controller."""

import json
import os
import time
from queue import Queue

from fastapi.testclient import TestClient
import pytest

from h2_backend.app import create_app
from h2_backend.config import Settings
from h2_gateway.client import GatewayClient
from h2_gateway.hardware import H2HardwareGateway, UnitreeLoco
from h2_gateway.server import create_server


@pytest.mark.skipif(os.getenv("H2_TEST_SDK_DDS") != "1", reason="requires the official Unitree SDK and local DDS")
def test_h2_hardware_path_over_loopback_dds(tmp_path, monkeypatch):
    from unitree_sdk2py.core import channel
    from unitree_sdk2py.h2.loco.h2_loco_api import (
        LOCO_SERVICE_NAME,
        ROBOT_API_ID_LOCO_GET_FSM_ID,
        ROBOT_API_ID_LOCO_GET_FSM_MODE,
        ROBOT_API_ID_LOCO_SET_VELOCITY,
    )
    from unitree_sdk2py.rpc.server import Server

    # Unitree's factory silently reuses its first domain and interface. Fail if
    # another test initialized it before this loopback configuration.
    assert not channel.ChannelFactory._ChannelFactory__initialized
    monkeypatch.setattr(channel, "ChannelConfigHasInterface", """
        <CycloneDDS>
          <Domain Id="any">
            <General>
              <Interfaces><NetworkInterface name="$__IF_NAME__$" multicast="false"/></Interfaces>
              <AllowMulticast>false</AllowMulticast>
            </General>
            <Discovery><Peers><Peer address="127.0.0.1"/></Peers></Discovery>
          </Domain>
        </CycloneDDS>
    """)
    domain = 81
    loco = UnitreeLoco("lo", domain=domain)
    commands = Queue()

    def accept_velocity(parameter):
        commands.put(json.loads(parameter))
        return 0, ""

    controller = Server(LOCO_SERVICE_NAME)
    controller._RegistHandler(ROBOT_API_ID_LOCO_GET_FSM_ID, lambda _: (0, '{"data":601}'), False)
    controller._RegistHandler(ROBOT_API_ID_LOCO_GET_FSM_MODE, lambda _: (0, '{"data":0}'), False)
    controller._RegistHandler(ROBOT_API_ID_LOCO_SET_VELOCITY, accept_velocity, False)
    controller.Start()

    def assert_velocity(expected):
        received = commands.get(timeout=2)
        assert tuple(received["velocity"]) == expected
        assert 0 < received["duration"] <= 0.25

    engine = H2HardwareGateway(loco)
    gateway_server, port = create_server(engine, port=0)
    gateway_server.start()
    address = f"127.0.0.1:{port}"
    settings = Settings(f"sqlite:///{tmp_path / 'platform.sqlite'}", address, tmp_path / "data",
                        hardware_teleop=True)
    try:
        with GatewayClient(address) as gateway:
            deadline = time.monotonic() + 3
            state = gateway.get_state()
            while not state["teleop_ready"] and time.monotonic() < deadline:
                time.sleep(0.02)
                state = gateway.get_state()
            assert state["teleop_ready"], state
            assert (state["simulated"], state["fsm_id"], state["fsm_mode"]) == (False, 601, 0)

            with TestClient(create_app(settings), client=("127.0.0.1", 50000)) as browser:
                with browser.websocket_connect(
                    "ws://127.0.0.1:5173/api/teleop/ws", headers={"origin": "http://127.0.0.1:5173"},
                ) as ws:
                    lease = ws.receive_json()
                    assert lease["accepted"] and lease["code"] == "acquired" and "token" not in lease
                    steps = (
                        ("forward", (0.15, 0.0, 0.0), "applied"),
                        ("backward", (-0.15, 0.0, 0.0), "applied"),
                        ("turn_left", (0.0, 0.0, 0.25), "applied"),
                        ("turn_right", (0.0, 0.0, -0.25), "applied"),
                        ("stop", (0.0, 0.0, 0.0), "stopped"),
                        ("forward", (0.15, 0.0, 0.0), "applied"),
                    )
                    for sequence, (action, expected, code) in enumerate(steps, start=1):
                        ws.send_json({"action": action, "sequence": sequence,
                                      "sent_at_ms": time.time_ns() // 1_000_000})
                        ack = ws.receive_json()
                        assert ack["accepted"] and ack["code"] == code and ack["sequence"] == sequence, ack
                        assert_velocity(expected)
                assert_velocity((0.0, 0.0, 0.0))
    finally:
        gateway_server.stop(grace=0).wait()
        engine.close()
