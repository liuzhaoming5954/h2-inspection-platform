"""Run the H2 gRPC gateway on loopback by default."""

from __future__ import annotations

import argparse
import json
import os
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from ipaddress import ip_address

import grpc

from h2_gateway.engine import SimulatorEngine
from h2_gateway.generated import robot_gateway_pb2 as pb
from h2_gateway.generated import robot_gateway_pb2_grpc as rpc
from h2_gateway.hardware import H2HardwareGateway, UnitreeLoco


class GatewayService(rpc.RobotGatewayServicer):
    def __init__(self, engine: SimulatorEngine | H2HardwareGateway):
        self.engine = engine

    def GetState(self, request: pb.Empty, context: grpc.ServicerContext) -> pb.RobotState:
        state = self.engine.get_state()
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
        if not context.is_active():
            context.abort(grpc.StatusCode.CANCELLED, "teleop request no longer active")
        result = self.engine.teleop_acquire()
        if result["accepted"] and not context.is_active():
            self.engine.teleop_release(result["token"])
            context.abort(grpc.StatusCode.CANCELLED, "teleop request no longer active")
        return pb.TeleopReply(**result)

    def TeleopCommand(self, request: pb.TeleopCommandRequest, context: grpc.ServicerContext) -> pb.TeleopReply:
        if request.action != "stop" and not context.is_active():
            context.abort(grpc.StatusCode.CANCELLED, "teleop request no longer active")
        return pb.TeleopReply(**self.engine.teleop_command(
            request.token, request.sequence, request.action, request.expires_at_ms,
        ))

    def TeleopRelease(self, request: pb.TeleopReleaseRequest, context: grpc.ServicerContext) -> pb.TeleopReply:
        return pb.TeleopReply(**self.engine.teleop_release(request.token))


def create_server(engine: SimulatorEngine | H2HardwareGateway, host: str = "127.0.0.1",
                  port: int = 50051) -> tuple[grpc.Server, int]:
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
    rpc.add_RobotGatewayServicer_to_server(GatewayService(engine), server)
    bound_port = server.add_insecure_port(f"{address}:{port}")
    if not bound_port:
        raise RuntimeError(f"Could not bind gRPC gateway to {host}:{port}")
    return server, bound_port


def main() -> None:
    parser = argparse.ArgumentParser(description="H2 simulator or high-level SDK2 teleoperation gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=50051)
    parser.add_argument("--db", default=os.environ.get("H2_GATEWAY_DB", ".local/state/gateway.sqlite3"))
    parser.add_argument("--step-delay", type=float, default=0.8, help="Simulated seconds per point")
    parser.add_argument("--h2-interface", help="Explicitly enable H2 hardware via the named SDK2 DDS interface")
    args = parser.parse_args()
    if args.h2_interface:
        try:
            if not ip_address(args.host).is_loopback:
                parser.error("H2 hardware gateway requires a loopback IP address")
            engine = H2HardwareGateway(UnitreeLoco(args.h2_interface))
        except (RuntimeError, ValueError) as error:
            parser.error(str(error))
    else:
        engine = SimulatorEngine(args.db, step_delay=args.step_delay)
    stop = threading.Event()

    def request_stop(*_: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        server, port = create_server(engine, args.host, args.port)
        server.start()
        print(json.dumps({"service": "h2-gateway", "simulated": not args.h2_interface,
                          "target": f"{args.host}:{port}",
                          **({"interface": args.h2_interface} if args.h2_interface else {"db": args.db})}),
              flush=True)
        try:
            stop.wait()
        finally:
            server.stop(grace=0).wait(timeout=3)
    finally:
        engine.close()


if __name__ == "__main__":
    main()
