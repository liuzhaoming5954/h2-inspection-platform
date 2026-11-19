import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any

from fastapi import APIRouter, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from . import field
from .config import Settings
from .service import InspectionService, ServiceError
from .teleop import UI_ORIGINS, teleop_socket


ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class PointInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    label: ShortText
    x: float = Field(ge=-1000, le=1000, allow_inf_nan=False)
    y: float = Field(ge=-1000, le=1000, allow_inf_nan=False)


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: ShortText
    request_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    points: list[PointInput] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_point_ids(self) -> "TaskInput":
        if len({p.id for p in self.points}) != len(self.points):
            raise ValueError("point ids must be unique within a task")
        return self


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(min_length=1, max_length=4000)


def create_app(settings: Settings | None = None, gateway: Any = None) -> FastAPI:
    settings = settings or Settings.from_env()
    teleop_gateway = None
    if gateway is None:
        from h2_gateway.client import GatewayClient
        if settings.hardware_teleop:
            control_address = settings.teleop_gateway_address
            if not control_address or control_address == settings.gateway_address:
                raise ValueError("H2 hardware mode requires a dedicated H2_TELEOP_GATEWAY_ADDRESS")
            teleop_gateway = GatewayClient(target=control_address, timeout=0.75)
        gateway = GatewayClient(target=settings.gateway_address, timeout=3)
    if teleop_gateway is None:
        teleop_gateway = gateway
    service = InspectionService(settings, gateway)

    async def maintain() -> None:
        while True:
            await asyncio.sleep(1)
            try:
                await asyncio.to_thread(service.recover_outbox)
            except Exception:
                # /api/health 会反映数据库或网关故障。
                # 已持久化的待派发记录会保留，供下一轮核对处理。
                pass

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 耗时的状态查询应避开为遥控指令保留的工作线程。
        executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="h2-teleop") if settings.hardware_teleop else None
        app.state.teleop_executor = executor
        worker = None
        try:
            await asyncio.to_thread(service.initialize)
            if not settings.hardware_teleop:
                worker = asyncio.create_task(maintain())
            yield
        finally:
            if worker is not None:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
            try:
                await asyncio.to_thread(service.close)
            finally:
                if teleop_gateway is not gateway:
                    teleop_gateway.close()
                if executor is not None:
                    await asyncio.to_thread(executor.shutdown)

    app = FastAPI(title="H2巡检技术验证 API", version="0.1.0", lifespan=lifespan,
                  description="Local validation API. Inspection readings and images are simulated. "
                  "H2 hardware teleoperation requires an explicit local hardware configuration.")
    app.state.service = service
    app.add_middleware(CORSMiddleware,
                       allow_origins=list(UI_ORIGINS),
                       allow_methods=["GET", "POST"], allow_headers=["Content-Type"])

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, error: ServiceError):
        return JSONResponse(status_code=error.status_code, content={"detail": error.detail})

    @app.get("/api/health")
    def health():
        return service.health()

    # 现场链路的探测与恢复接口只在真机模式下注册。
    field_routes = APIRouter(prefix="/api/field")

    @field_routes.get("/status")
    def field_status():
        try:
            connected = service.health().get("gateway") == "connected"
        except Exception:
            connected = False
        return field.status(connected)

    @field_routes.post("/reconnect-forwards")
    def field_reconnect_forwards():
        try:
            return field.reconnect_forwards()
        except Exception as error:
            return {"ok": False, "error": str(error)[:200]}

    @field_routes.post("/start-pc2")
    def field_start_pc2():
        try:
            return field.start_pc2_agent()
        except Exception as error:
            return {"ok": False, "error": str(error)[:200]}

    @field_routes.post("/start-pc3")
    def field_start_pc3():
        try:
            return field.start_pc3_gateway()
        except Exception as error:
            return {"ok": False, "error": str(error)[:200]}

    @field_routes.get("/vision")
    def field_vision():
        # 视觉接口自身已经把不可达折叠为 available=False，这里不再包错。
        return field.vision()

    @field_routes.get("/vision/frame")
    def field_vision_frame():
        frame = field.vision_frame()
        if frame is None:
            # 预览帧尚不存在（视觉程序未运行或 PC2 离线），保留 503 语义供前端识别。
            raise ServiceError(503, "预览帧不可用（视觉程序未运行或 PC2 离线）")
        data, age_ms, content_type = frame
        headers = {"Cache-Control": "no-store"}
        if age_ms is not None:
            headers["X-Frame-Age-Ms"] = str(age_ms)
        return Response(content=data, media_type=content_type, headers=headers)

    @field_routes.get("/map/{kind}")
    def field_map(kind: str):
        # 建图桥数据转发（点云/位姿/轨迹），供「地图显示」模块轮询。
        if kind not in ("map", "pose", "path"):
            raise ServiceError(404, "未知的地图数据类型")
        payload = field.map_data(kind)
        if payload is None:
            raise ServiceError(503, "地图数据不可用（建图未运行或 PC3 离线）")
        data, content_type, extra = payload
        return Response(content=data, media_type=content_type,
                        headers={"Cache-Control": "no-store", **extra})

    if settings.hardware_teleop:
        app.include_router(field_routes)

    @app.get("/api/robot")
    def robot():
        return service.robot()

    @app.get("/api/tasks")
    def tasks():
        return service.list_tasks()

    @app.post("/api/tasks", status_code=201)
    def create_task(payload: TaskInput):
        return service.create_task(payload.model_dump())

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str):
        return service.get_task(task_id)

    @app.post("/api/tasks/{task_id}/stop")
    def stop_task(task_id: str):
        return service.stop_task(task_id)

    @app.post("/api/tasks/{task_id}/review")
    def review(task_id: str, payload: ReviewInput):
        return service.review(task_id, payload.note)

    @app.get("/api/tasks/{task_id}/reports")
    def reports(task_id: str):
        return service.reports(task_id)

    @app.post("/api/tasks/{task_id}/reports")
    def generate_reports(task_id: str):
        return service.generate_reports(task_id)

    @app.get("/api/files/{file_id}")
    def file(file_id: str):
        stored = service.file(file_id)
        return FileResponse(stored["path"], media_type=stored["media_type"], filename=stored["name"],
                            content_disposition_type="inline" if stored["media_type"].startswith("image/") else "attachment")

    @app.websocket("/api/ws")
    async def websocket(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                try:
                    state = await asyncio.to_thread(service.robot)
                    await websocket.send_json({"type": "robot_state", "data": state})
                except ServiceError:
                    await websocket.send_json({"type": "gateway_unavailable", "simulated": not settings.hardware_teleop})
                await asyncio.sleep(1)
        except WebSocketDisconnect:
            pass

    @app.websocket("/api/teleop/ws")
    async def teleop_websocket(websocket: WebSocket):
        await teleop_socket(websocket, teleop_gateway, hardware_teleop=settings.hardware_teleop,
                            executor=getattr(app.state, "teleop_executor", None))

    return app
