import asyncio
import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any, Literal
import urllib.request

from fastapi import APIRouter, FastAPI, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from . import field
from .auth_gate import AuthGate
from .config import Settings
from .demo import DemoService
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


class RouteStepInput(BaseModel):
    """演示路线的单个步骤：前进(米)/转向(度，左转为正)/停留(秒)。

    停留步可挂一个识别任务（task）：qr=朗读二维码内容、led=播报信号灯
    数量与颜色、person=播报人数。同一时刻只执行当前步的任务，行进中
    和其他任务字段一律忽略（2026-11-21 确认的每站单任务设计）。
    兼容旧字段 read_aloud=True 等价于 task="qr"。
    """
    model_config = ConfigDict(extra="forbid")
    type: Literal["forward", "turn", "wait"]
    value: float = Field(allow_inf_nan=False)
    task: Literal["qr", "led", "person"] | None = None
    read_aloud: bool = False   # 已废弃，等价于 task="qr"

    @model_validator(mode="after")
    def step_bounds(self) -> "RouteStepInput":
        if self.type == "forward" and not 0.05 <= self.value <= 5:
            raise ValueError("前进距离需在 0.05–5 米之间")
        if self.type == "turn" and not (1 <= abs(self.value) <= 180):
            raise ValueError("转向角度需在 ±180° 之间且不为零")
        if self.type == "wait" and not 0.5 <= self.value <= 60:
            raise ValueError("停留时长需在 0.5–60 秒之间")
        if self.task and self.type != "wait":
            raise ValueError("只有停留步支持识别任务")
        if self.read_aloud and self.type != "wait":
            raise ValueError("只有停留步支持朗读识别结果")
        return self


class RouteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: ShortText
    steps: list[RouteStepInput] = Field(min_length=1, max_length=50)


class DemoStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    route_id: str = Field(min_length=1, max_length=64)


def create_app(settings: Settings | None = None, gateway: Any = None) -> FastAPI:
    # 后端的对外 HTTP 只访问回环与现场设备，一律不经过代理。启动终端里的
    # http_proxy 会被进程继承，而 urllib 不识别 no_proxy 中的网段写法
    # （如 192.168.0.0/16），发往 PC2/PC3 的请求会被送进代理、等到超时才失败。
    urllib.request.install_opener(
        urllib.request.build_opener(urllib.request.ProxyHandler({})))
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
    demo = DemoService(settings, service)
    app_instance_id = secrets.token_urlsafe(12)

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
            await asyncio.to_thread(demo.initialize)
            if not settings.hardware_teleop:
                worker = asyncio.create_task(maintain())
            yield
        finally:
            demo.shutdown()
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
    app.state.demo = demo
    app.add_middleware(CORSMiddleware,
                       allow_origins=list(UI_ORIGINS),
                       allow_methods=["GET", "POST"], allow_headers=["Content-Type"])

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, error: ServiceError):
        return JSONResponse(status_code=error.status_code, content={"detail": error.detail})

    @app.get("/api/health")
    def health():
        # instance_id 每次后端启动都不同：前端发现它变化即判定后端已重启，
        # 进程内保存的登录令牌随之全部失效，已打开的页面自动回到登录页。
        return {**service.health(), "instance_id": app_instance_id}

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
        if not field.DATA_VALVES.get("vision", True):
            raise ServiceError(503, "相机数据接入已关闭")
        # 视觉接口自身已经把不可达折叠为 available=False，这里不再包错。
        return field.vision()

    @field_routes.get("/vision/frame")
    def field_vision_frame():
        if not field.DATA_VALVES.get("vision", True):
            raise ServiceError(503, "相机数据接入已关闭")
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
        if not field.DATA_VALVES.get("mapping", True):
            raise ServiceError(503, "雷达数据接入已关闭")
        if kind not in ("map", "pose", "path"):
            raise ServiceError(404, "未知的地图数据类型")
        payload = field.map_data(kind)
        if payload is None:
            raise ServiceError(503, "地图数据不可用（建图未运行或 PC3 离线）")
        data, content_type, extra = payload
        return Response(content=data, media_type=content_type,
                        headers={"Cache-Control": "no-store", **extra})

    class ValveRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")
        enabled: bool

    @field_routes.post("/data/{name}")
    def field_data_valve(name: str, payload: ValveRequest):
        # 同步处理器：开阀可能触发程序拉起（建图最长约 25 秒），在线程池中执行。
        try:
            return field.set_data_valve(name, payload.enabled)
        except Exception as error:
            return {"ok": False, "enabled": False, "error": str(error)[:200]}

    @field_routes.get("/asr/text")
    def field_asr_text(since: int = 0):
        # 转写结果增量拉取；阀门关闭时不返回历史，避免绕过关闭语义。
        if not field.DATA_VALVES.get("asr", False):
            raise ServiceError(503, "语音识别已关闭")
        payload = field.asr_text(since)
        if payload is None:
            raise ServiceError(503, "语音服务不可用（未运行或 PC3 离线）")
        return payload

    if settings.hardware_teleop:
        app.include_router(field_routes)

    # 演示编排接口：模拟与真机模式都注册（模拟模式用于编辑器和执行器干跑，
    # 语音编排部分在 DemoService 内部按模式自行跳过）。
    demo_routes = APIRouter(prefix="/api/demo")

    @demo_routes.get("/routes")
    def demo_list_routes():
        return demo.list_routes()

    @demo_routes.post("/routes", status_code=201)
    def demo_create_route(payload: RouteInput):
        data = payload.model_dump()
        for step in data["steps"]:
            # 兼容旧字段：read_aloud=True → task="qr"
            if step.pop("read_aloud", False) and step.get("task") is None:
                step["task"] = "qr"
        return demo.create_route(data)

    @demo_routes.delete("/routes/{route_id}")
    def demo_delete_route(route_id: str):
        return demo.delete_route(route_id)

    @demo_routes.get("/state")
    def demo_state():
        return demo.state()

    @demo_routes.post("/prepare")
    def demo_prepare():
        # 「巡检准备」：一键拉起全部服务并做功能性就绪检测，全绿后播报就绪
        return demo.prepare()

    @demo_routes.post("/start")
    def demo_start(payload: DemoStartRequest):
        return demo.start(payload.route_id)

    @demo_routes.post("/stop")
    def demo_stop():
        return demo.stop()

    @demo_routes.post("/routes/{route_id}/run")
    def demo_run_route(route_id: str):
        return demo.run_route(route_id)

    app.include_router(demo_routes)

    # 真机模式登录门（方案 A）：仅验证/改密/令牌校验三个接口，其余 API 不拦截。
    auth_gate = AuthGate(service)
    app.state.auth_gate = auth_gate
    auth_routes = APIRouter(prefix="/api/auth")

    class LoginRequest(BaseModel):
        password: str = Field(min_length=1)

    class ChangePasswordRequest(BaseModel):
        token: str
        old_password: str = Field(min_length=1)
        new_password: str

    @auth_routes.post("/verify")
    def auth_verify(payload: LoginRequest):
        return {"ok": True, "token": auth_gate.verify(payload.password)}

    # 令牌走请求头而不是查询串，避免出现在访问日志中。
    @auth_routes.get("/check")
    def auth_check(x_auth_token: Annotated[str | None, Header(alias="X-Auth-Token")] = None):
        return {"valid": bool(x_auth_token) and auth_gate.check(x_auth_token)}

    @auth_routes.post("/change")
    def auth_change(payload: ChangePasswordRequest):
        auth_gate.change(payload.token, payload.old_password, payload.new_password)
        return {"ok": True}

    app.include_router(auth_routes)

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
