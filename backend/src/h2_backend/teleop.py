import asyncio
from contextlib import suppress
import time
from typing import Any, Literal

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, ValidationError


UI_ORIGINS = frozenset(
    f"http://{host}:{port}" for host in ("127.0.0.1", "localhost") for port in (5173, 4173)
)
LOCAL_HOSTS = frozenset(
    f"{host}:{port}" for host in ("127.0.0.1", "localhost") for port in (5173, 4173, 8000)
)
COMMAND_TTL_MS = 350
IDLE_TIMEOUT_SECONDS = 2.0
MAX_FRAME_LENGTH = 512
MAX_SAFE_INTEGER = 2**53 - 1


class TeleopInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    action: Literal["forward", "backward", "turn_left", "turn_right", "stop", "heartbeat", "release"]
    sequence: int = Field(gt=0, le=MAX_SAFE_INTEGER)
    sent_at_ms: int = Field(ge=0, le=MAX_SAFE_INTEGER)


# Send fixed public messages. Gateway replies can contain a private lease token
# or diagnostic text that should never appear in a browser frame.
MESSAGES = {
    "acquired": "已获得模拟遥控控制权。",
    "applied": "已应用模拟遥控指令。",
    "stopped": "已停止模拟机器人运动。",
    "renewed": "已续期模拟遥控控制权。",
    "released": "已释放模拟遥控控制权。",
    "teleop_busy": "已有操作员获得遥控控制权。",
    "robot_busy": "机器人正在巡检，无法进入遥控模式。",
    "invalid_token": "遥控控制权已失效，请重新获取。",
    "stale_sequence": "指令序号未递增，指令未执行。",
    "expired": "网关收到的指令已过期，未执行。",
    "invalid_expiry": "网关拒绝了指令有效期。",
    "invalid_action": "网关拒绝了不支持的动作。",
    "stale_command": "指令已过期，未发送给网关。",
    "future_command": "指令时间晚于服务器时间，未发送给网关。",
    "invalid_message": "遥控消息格式不正确。",
    "simulation_required": "当前入口仅支持模拟机器人遥控。",
    "gateway_timeout": "网关响应超时，遥控会话已结束。",
    "gateway_unavailable": "无法连接模拟机器人网关。",
    "gateway_protocol_error": "网关未返回有效的模拟遥控控制权。",
    "gateway_rejected": "网关拒绝了遥控请求。",
    "idle_timeout": "遥控会话长时间未收到指令，请重新获取控制权。",
    "ok": "网关已确认遥控请求。",
}
GATEWAY_CODES = frozenset({
    "acquired", "applied", "stopped", "renewed", "released", "teleop_busy", "robot_busy",
    "invalid_token", "stale_sequence", "expired", "invalid_expiry", "invalid_action",
})


def trusted_ui(websocket: WebSocket) -> bool:
    origins = websocket.headers.getlist("origin")
    hosts = websocket.headers.getlist("host")
    forwarded = websocket.headers.getlist("x-forwarded-host")
    return (len(origins) == 1 and origins[0] in UI_ORIGINS and len(hosts) == 1
            and all(host.lower() in LOCAL_HOSTS for host in hosts + forwarded))


async def reply(websocket: WebSocket, kind: str, accepted: bool, code: str, sequence: int | None = None) -> None:
    body: dict[str, Any] = {"type": kind, "accepted": accepted, "code": code, "message": MESSAGES[code]}
    if sequence is not None:
        body["sequence"] = sequence
    await websocket.send_json(body)


def gateway_outcome(result: dict[str, Any]) -> tuple[bool, str]:
    accepted = result.get("accepted") is True
    code = result.get("code")
    if not isinstance(code, str) or code not in GATEWAY_CODES:
        code = "ok" if accepted else "gateway_rejected"
    return accepted, code


def transport_code(error: Exception) -> str:
    status = getattr(error, "code", None)
    if isinstance(error, TimeoutError) or (callable(status) and getattr(status(), "name", None) == "DEADLINE_EXCEEDED"):
        return "gateway_timeout"
    return "gateway_unavailable"


async def teleop_socket(websocket: WebSocket, gateway: Any) -> None:
    if not trusted_ui(websocket):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    token = None
    try:
        try:
            state = await asyncio.to_thread(gateway.get_state)
            if state.get("simulated") is not True:
                await reply(websocket, "lease", False, "simulation_required")
                await websocket.close(code=1008)
                return
            result = await asyncio.to_thread(gateway.teleop_acquire)
            accepted, code = gateway_outcome(result)
        except WebSocketDisconnect:
            raise
        except Exception as error:
            await reply(websocket, "lease", False, transport_code(error))
            await websocket.close(code=1011)
            return

        if not accepted:
            await reply(websocket, "lease", False, code)
            await websocket.close(code=1008)
            return
        candidate = result.get("token")
        if not isinstance(candidate, str) or not candidate:
            await reply(websocket, "lease", False, "gateway_protocol_error")
            await websocket.close(code=1011)
            return
        token = candidate
        await reply(websocket, "lease", True, code)

        while True:
            try:
                frame = await asyncio.wait_for(websocket.receive(), timeout=IDLE_TIMEOUT_SECONDS)
            except TimeoutError:
                await reply(websocket, "error", False, "idle_timeout")
                await websocket.close(code=1008)
                return
            if frame["type"] == "websocket.disconnect":
                return
            try:
                raw = frame.get("text")
                if not isinstance(raw, str) or len(raw) > MAX_FRAME_LENGTH:
                    raise ValueError("Invalid text frame")
                command = TeleopInput.model_validate_json(raw)
            except (ValidationError, ValueError):
                await reply(websocket, "error", False, "invalid_message")
                continue

            now_ms = time.time_ns() // 1_000_000
            if command.action in ("stop", "release"):
                expires_at_ms = now_ms + COMMAND_TTL_MS
            else:
                if command.sent_at_ms > now_ms:
                    await reply(websocket, "ack", False, "future_command", command.sequence)
                    continue
                expires_at_ms = command.sent_at_ms + COMMAND_TTL_MS
                if expires_at_ms <= now_ms:
                    await reply(websocket, "ack", False, "stale_command", command.sequence)
                    continue

            try:
                if command.action == "release":
                    result = await asyncio.to_thread(gateway.teleop_release, token)
                else:
                    result = await asyncio.to_thread(
                        gateway.teleop_command, token, command.sequence, command.action, expires_at_ms,
                    )
                accepted, code = gateway_outcome(result)
            except Exception as error:
                await reply(websocket, "error", False, transport_code(error), command.sequence)
                await websocket.close(code=1011)
                return

            # A lost or expired lease is terminal for this socket. Never
            # silently reacquire or replay a movement command after it is lost.
            if code == "invalid_token" or (command.action == "release" and accepted):
                token = None
            await reply(websocket, "ack", accepted, code, command.sequence)
            if code == "invalid_token" or command.action == "release":
                await websocket.close(code=1000 if accepted else 1008)
                return
    except WebSocketDisconnect:
        pass
    finally:
        if token is not None:
            # The simulator watchdog remains authoritative if the link is down
            # or the browser vanishes before an explicit release reaches us.
            with suppress(Exception):
                await asyncio.to_thread(gateway.teleop_release, token)
