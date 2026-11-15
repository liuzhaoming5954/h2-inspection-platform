"""运行 H2 gRPC 网关，默认监听回环地址。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from ipaddress import ip_address

import grpc

from h2_gateway.engine import SimulatorEngine
from h2_gateway.generated import robot_gateway_pb2 as pb
from h2_gateway.generated import robot_gateway_pb2_grpc as rpc
from h2_gateway.hardware import (
    AgentEnvironment,
    BmsWatcher,
    H2HardwareGateway,
    OdomWatcher,
    UnitreeLoco,
    emit_event,
)
from h2_gateway.sensor_config import DEFAULT_PC2_HOST, pc3_sensor_agent_url


class GatewayService(rpc.RobotGatewayServicer):
    def __init__(self, engine: SimulatorEngine | H2HardwareGateway, lane: str = "both"):
        self.engine = engine
        self.lane = lane

    def _require_teleop(self, context: grpc.ServicerContext) -> None:
        if self.lane == "state":
            context.abort(grpc.StatusCode.UNIMPLEMENTED, "Use the dedicated H2 teleoperation endpoint")

    def _teleop_reply(self, result: dict) -> pb.TeleopReply:
        if result["accepted"] and isinstance(self.engine, H2HardwareGateway):
            return pb.TeleopReply(**result, gateway_monotonic_ms=time.monotonic_ns() // 1_000_000)
        return pb.TeleopReply(**result)

    def GetState(self, request: pb.Empty, context: grpc.ServicerContext) -> pb.RobotState:
        state = (self.engine.get_state(include_telemetry=False) if self.lane == "teleop"
                 and isinstance(self.engine, H2HardwareGateway) else self.engine.get_state())
        if "pose" in state:
            state["pose"] = pb.Pose(**state["pose"])
        return pb.RobotState(**state)

    def StartTask(self, request: pb.StartTaskRequest, context: grpc.ServicerContext) -> pb.CommandReply:
        if isinstance(self.engine, H2HardwareGateway):
            context.abort(grpc.StatusCode.UNIMPLEMENTED, "H2 hardware mode supports manual teleoperation only")
        points = [{"id": p.id, "label": p.label or p.id, "x": p.x, "y": p.y} for p in request.points]
        reply = self.engine.start_task(request.task_id, request.command_id, request.expires_at_ms, points)
        return pb.CommandReply(**reply)

    def StopTask(self, request: pb.StopTaskRequest, context: grpc.ServicerContext) -> pb.CommandReply:
        if isinstance(self.engine, H2HardwareGateway):
            context.abort(grpc.StatusCode.UNIMPLEMENTED, "H2 hardware mode supports manual teleoperation only")
        reply = self.engine.stop_task(request.task_id, request.command_id, request.expires_at_ms)
        return pb.CommandReply(**reply)

    def GetTask(self, request: pb.TaskQuery, context: grpc.ServicerContext) -> pb.TaskResult:
        if isinstance(self.engine, H2HardwareGateway):
            context.abort(grpc.StatusCode.UNIMPLEMENTED, "H2 hardware mode supports manual teleoperation only")
        result = self.engine.get_task(request.task_id)
        result["samples"] = [pb.Sample(**sample) for sample in result["samples"]]
        return pb.TaskResult(**result)

    def TeleopAcquire(self, request: pb.Empty, context: grpc.ServicerContext) -> pb.TeleopReply:
        self._require_teleop(context)
        if not context.is_active():
            context.abort(grpc.StatusCode.CANCELLED, "teleop request no longer active")
        result = self.engine.teleop_acquire()
        if result["accepted"] and not context.is_active():
            self.engine.teleop_release(result["token"])
            context.abort(grpc.StatusCode.CANCELLED, "teleop request no longer active")
        return self._teleop_reply(result)

    def TeleopCommand(self, request: pb.TeleopCommandRequest, context: grpc.ServicerContext) -> pb.TeleopReply:
        self._require_teleop(context)
        if request.action != "stop" and not context.is_active():
            context.abort(grpc.StatusCode.CANCELLED, "teleop request no longer active")
        if isinstance(self.engine, H2HardwareGateway):
            deadline = request.gateway_deadline_ms if request.HasField("gateway_deadline_ms") else None
            result = self.engine.teleop_command(request.token, request.sequence, request.action, deadline,
                                               is_active=context.is_active)
        else:
            result = self.engine.teleop_command(request.token, request.sequence, request.action, request.expires_at_ms)
        return self._teleop_reply(result)

    def TeleopRelease(self, request: pb.TeleopReleaseRequest, context: grpc.ServicerContext) -> pb.TeleopReply:
        self._require_teleop(context)
        return self._teleop_reply(self.engine.teleop_release(request.token))


def create_server(engine: SimulatorEngine | H2HardwareGateway, host: str = "127.0.0.1",
                  port: int = 50051, *, lane: str = "both") -> tuple[grpc.Server, int]:
    address = host
    if isinstance(engine, H2HardwareGateway):
        try:
            loopback = ip_address(host)
        except ValueError:
            raise ValueError("H2 hardware gateway requires a loopback IP address") from None
        if not loopback.is_loopback:
            raise ValueError("H2 hardware gateway requires a loopback IP address")
        if loopback.version == 6:
            address = f"[{host}]"
    server = grpc.server(ThreadPoolExecutor(max_workers=8), options=[
        ("grpc.so_reuseport", 0),
        ("grpc.max_send_message_length", 32 * 1024 * 1024),
        ("grpc.max_receive_message_length", 4 * 1024 * 1024),
    ])
    rpc.add_RobotGatewayServicer_to_server(GatewayService(engine, lane), server)
    bound_port = server.add_insecure_port(f"{address}:{port}")
    if not bound_port:
        raise RuntimeError(f"Could not bind gRPC gateway to {host}:{port}")
    return server, bound_port


def main() -> None:
    parser = argparse.ArgumentParser(description="H2 simulator or high-level SDK2 teleoperation gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=50051)
    parser.add_argument("--teleop-port", type=int, default=50052, help="Separate loopback port for H2 control")
    parser.add_argument("--db", default=os.environ.get("H2_GATEWAY_DB", ".local/state/gateway.sqlite3"))
    parser.add_argument("--step-delay", type=float, default=0.8, help="Simulated seconds per point")
    parser.add_argument("--h2-interface", help="Explicitly enable H2 hardware via the named SDK2 DDS interface")
    args = parser.parse_args()
    if args.h2_interface:
        try:
            if not ip_address(args.host).is_loopback:
                parser.error("H2 hardware gateway requires a loopback IP address")
            if args.port == args.teleop_port or not 1 <= args.teleop_port <= 65535:
                parser.error("H2 teleoperation requires a separate, valid --teleop-port")
            loco = UnitreeLoco(args.h2_interface)
            battery = None
            try:
                battery = BmsWatcher()
            except Exception as error:  # SDK 已加载，BMS 不可用时省略电量。
                print(json.dumps({"warning": f"bms watcher unavailable: {error}"}), flush=True)
            odom = None
            try:
                odom = OdomWatcher()
            except Exception as error:  # 里程计不可用时，位姿保持为空。
                print(json.dumps({"warning": f"odom watcher unavailable: {error}"}), flush=True)
            engine = H2HardwareGateway(
                loco,
                AgentEnvironment(
                    pc3_sensor_agent_url(os.getenv("H2_PC2_HOST") or DEFAULT_PC2_HOST)
                ).start(),
                battery,
                odom,
            )
        except (RuntimeError, ValueError) as error:
            parser.error(str(error))
    else:
        engine = SimulatorEngine(args.db, step_delay=args.step_delay)
    if args.h2_interface:
        # 仅网关包记录这些事件，grpc 保持 WARNING 级别。
        # 系统时钟时间戳为单调事件时间线提供参照，
        # 便于离线分析指令帧连续性和停步延迟。
        events = logging.getLogger("h2_gateway")
        events.setLevel(logging.INFO)
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        events.addHandler(handler)
        emit_event({"event": "clock_base", "unix_ms": time.time_ns() // 1_000_000})
    stop = threading.Event()

    def request_stop(*_: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    servers = []
    try:
        server, port = create_server(engine, args.host, args.port, lane="state" if args.h2_interface else "both")
        servers.append(server)
        server.start()
        targets = {}
        if args.h2_interface:
            control, control_port = create_server(engine, args.host, args.teleop_port, lane="teleop")
            servers.append(control)
            control.start()
            targets = {"interface": args.h2_interface, "teleop_target": f"{args.host}:{control_port}"}
        print(json.dumps({"service": "h2-gateway", "simulated": not args.h2_interface,
                          "target": f"{args.host}:{port}",
                          **(targets if args.h2_interface else {"db": args.db})}),
              flush=True)
        stop.wait()
    finally:
        for running in reversed(servers):
            running.stop(grace=0).wait(timeout=3)
        engine.close()


if __name__ == "__main__":
    main()
