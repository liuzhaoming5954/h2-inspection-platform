"""基于版本化 gRPC 契约的同步 Python 客户端。"""

from __future__ import annotations

from ipaddress import ip_address
import time
from typing import Any

import grpc

from h2_gateway.generated import robot_gateway_pb2 as pb
from h2_gateway.generated import robot_gateway_pb2_grpc as rpc


def command_dict(reply: pb.CommandReply) -> dict[str, Any]:
    return {"accepted": reply.accepted, "code": reply.code, "message": reply.message,
            "task_id": reply.task_id, "command_id": reply.command_id, "simulated": reply.simulated}


def teleop_dict(reply: pb.TeleopReply) -> dict[str, Any]:
    result = {"accepted": reply.accepted, "code": reply.code, "message": reply.message, "token": reply.token}
    if reply.HasField("gateway_monotonic_ms"):
        result["gateway_monotonic_ms"] = reply.gateway_monotonic_ms
        result["received_monotonic_ns"] = time.monotonic_ns()
    return result


class GatewayClient:
    def __init__(self, target: str = "127.0.0.1:50051", timeout: float = 3):
        self.timeout = timeout
        options = [("grpc.max_receive_message_length", 32 * 1024 * 1024)]
        host, separator, _ = target.rpartition(":")
        if separator:
            try:
                if ip_address(host.strip("[]")).is_loopback:
                    # 本地 SSH 转发优先于环境变量配置的代理。
                    options.append(("grpc.enable_http_proxy", 0))
            except ValueError:
                pass
        self.channel = grpc.insecure_channel(target, options=options)
        self.stub = rpc.RobotGatewayStub(self.channel)

    def get_state(self) -> dict[str, Any]:
        started = time.monotonic_ns()
        state = self.stub.GetState(pb.Empty(), timeout=self.timeout)
        transit_ms = (time.monotonic_ns() - started + 999_999) // 1_000_000
        remaining = 0
        if state.HasField("environment_remaining_ms") and state.HasField("environment_sample_id"):
            remaining = max(0, state.environment_remaining_ms - transit_ms)
        return {"robot_id": state.robot_id, "status": state.status,
                "task_id": state.task_id, "progress": state.progress,
                "sequence": state.sequence,
                "battery_percent": state.battery_percent if state.HasField("battery_percent") else None,
                "pose": {"x": state.pose.x, "y": state.pose.y, "yaw": state.pose.yaw} if state.HasField("pose") else None,
                "simulated": state.simulated,
                "start_count": state.start_count, "task_status": state.task_status,
                "updated_at": state.updated_at, "teleop_active": state.teleop_active,
                "motion": state.motion or ("stop" if state.simulated else "unknown"),
                "teleop_ready": state.teleop_ready,
                "fsm_id": state.fsm_id if state.HasField("fsm_id") else None,
                "fsm_mode": state.fsm_mode if state.HasField("fsm_mode") else None,
                "temperature_c": state.temperature_c if remaining and state.HasField("temperature_c") else None,
                "humidity_percent": state.humidity_percent if remaining and state.HasField("humidity_percent") else None,
                "environment_captured_at": state.environment_captured_at if remaining and state.HasField("environment_captured_at") else None,
                "environment_remaining_ms": remaining or None,
                "environment_sample_id": state.environment_sample_id if remaining else None}

    def teleop_acquire(self) -> dict[str, Any]:
        return teleop_dict(self.stub.TeleopAcquire(pb.Empty(), timeout=min(self.timeout, 0.75)))

    def teleop_command(self, token: str, sequence: int, action: str, expires_at_ms: int = 0, *,
                       gateway_deadline_ms: int | None = None, timeout: float = 0.75) -> dict[str, Any]:
        request = pb.TeleopCommandRequest(token=token, sequence=sequence, action=action, expires_at_ms=expires_at_ms)
        if gateway_deadline_ms is not None:
            request.gateway_deadline_ms = gateway_deadline_ms
        return teleop_dict(self.stub.TeleopCommand(request, timeout=min(self.timeout, timeout)))

    def teleop_release(self, token: str) -> dict[str, Any]:
        return teleop_dict(self.stub.TeleopRelease(pb.TeleopReleaseRequest(token=token), timeout=min(self.timeout, 0.75)))

    def start_task(self, task_id: str, command_id: str, expires_at_ms: int,
                   points: list[dict[str, Any]]) -> dict[str, Any]:
        message = pb.StartTaskRequest(
            task_id=task_id, command_id=command_id, expires_at_ms=expires_at_ms,
            points=[pb.Point(id=str(p["id"]), label=str(p.get("label", p["id"])),
                             x=float(p.get("x", 0)), y=float(p.get("y", 0))) for p in points],
        )
        return command_dict(self.stub.StartTask(message, timeout=self.timeout))

    def stop_task(self, task_id: str, command_id: str, expires_at_ms: int) -> dict[str, Any]:
        return command_dict(self.stub.StopTask(
            pb.StopTaskRequest(task_id=task_id, command_id=command_id, expires_at_ms=expires_at_ms),
            timeout=self.timeout,
        ))

    def get_task(self, task_id: str) -> dict[str, Any]:
        result = self.stub.GetTask(pb.TaskQuery(task_id=task_id), timeout=self.timeout)
        samples = [{"point_id": s.point_id, "label": s.label, "device_id": s.device_id,
                    "temperature_c": s.temperature_c, "humidity_percent": s.humidity_percent,
                    "image_png": bytes(s.image_png), "captured_at": s.captured_at,
                    "simulated": s.simulated, "x": s.x, "y": s.y} for s in result.samples]
        return {"task_id": result.task_id, "status": result.status, "progress": result.progress,
                "samples": samples, "reason": result.reason, "simulated": result.simulated,
                "sequence": result.sequence, "updated_at": result.updated_at}

    def close(self) -> None:
        self.channel.close()

    def __enter__(self) -> GatewayClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
