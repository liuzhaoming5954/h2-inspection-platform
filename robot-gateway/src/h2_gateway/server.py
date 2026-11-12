"""Run the simulator-only gRPC gateway on loopback by default."""

from __future__ import annotations

import argparse
import json
import os
import signal
import threading
from concurrent.futures import ThreadPoolExecutor

import grpc

from h2_gateway.engine import SimulatorEngine
from h2_gateway.generated import robot_gateway_pb2 as pb
from h2_gateway.generated import robot_gateway_pb2_grpc as rpc


class GatewayService(rpc.RobotGatewayServicer):
    def __init__(self, engine: SimulatorEngine):
        self.engine = engine

    def GetState(self, request: pb.Empty, context: grpc.ServicerContext) -> pb.RobotState:
        state = self.engine.get_state()
        state["pose"] = pb.Pose(**state["pose"])
        return pb.RobotState(**state)

    def StartTask(self, request: pb.StartTaskRequest, context: grpc.ServicerContext) -> pb.CommandReply:
        points = [{"id": p.id, "label": p.label or p.id, "x": p.x, "y": p.y} for p in request.points]
        reply = self.engine.start_task(request.task_id, request.command_id, request.expires_at_ms, points)
        return pb.CommandReply(**reply)

    def StopTask(self, request: pb.StopTaskRequest, context: grpc.ServicerContext) -> pb.CommandReply:
        reply = self.engine.stop_task(request.task_id, request.command_id, request.expires_at_ms)
        return pb.CommandReply(**reply)

    def GetTask(self, request: pb.TaskQuery, context: grpc.ServicerContext) -> pb.TaskResult:
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
        return pb.TeleopReply(**self.engine.teleop_command(
            request.token, request.sequence, request.action, request.expires_at_ms,
        ))

    def TeleopRelease(self, request: pb.TeleopReleaseRequest, context: grpc.ServicerContext) -> pb.TeleopReply:
        return pb.TeleopReply(**self.engine.teleop_release(request.token))


def create_server(engine: SimulatorEngine, host: str = "127.0.0.1", port: int = 50051) -> tuple[grpc.Server, int]:
    server = grpc.server(ThreadPoolExecutor(max_workers=8), options=[
        ("grpc.max_send_message_length", 32 * 1024 * 1024),
        ("grpc.max_receive_message_length", 4 * 1024 * 1024),
    ])
    rpc.add_RobotGatewayServicer_to_server(GatewayService(engine), server)
    bound_port = server.add_insecure_port(f"{host}:{port}")
    if not bound_port:
        raise RuntimeError(f"Could not bind gRPC gateway to {host}:{port}")
    return server, bound_port


def main() -> None:
    parser = argparse.ArgumentParser(description="H2 simulated robot gRPC gateway (no hardware connected)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=50051)
    parser.add_argument("--db", default=os.environ.get("H2_GATEWAY_DB", ".local/state/gateway.sqlite3"))
    parser.add_argument("--step-delay", type=float, default=0.8, help="Simulated seconds per point")
    args = parser.parse_args()
    engine = SimulatorEngine(args.db, step_delay=args.step_delay)
    server, port = create_server(engine, args.host, args.port)
    stop = threading.Event()

    def request_stop(*_: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    server.start()
    print(json.dumps({"service": "h2-gateway", "simulated": True,
                      "target": f"{args.host}:{port}", "db": args.db}), flush=True)
    try:
        stop.wait()
    finally:
        server.stop(grace=2).wait(timeout=3)
        engine.close()


if __name__ == "__main__":
    main()
