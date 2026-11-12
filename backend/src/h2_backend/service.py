from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import threading
import time
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from .config import Settings
from .models import Base, Outbox, ReportJob, Review, Sample, StoredFile, Task, utcnow


class ServiceError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class InspectionService:
    def __init__(self, settings: Settings, gateway: Any):
        self.settings = settings
        kwargs: dict[str, Any] = {"pool_pre_ping": True}
        if settings.database_url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        self.engine = create_engine(settings.database_url, **kwargs)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.gateway = gateway
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._report_lock = threading.Lock()

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)
        self.recover_outbox()

    def close(self) -> None:
        self.engine.dispose()
        if hasattr(self.gateway, "close"):
            self.gateway.close()

    def health(self) -> dict[str, Any]:
        with self.engine.connect() as connection:
            from sqlalchemy import text
            connection.execute(text("SELECT 1"))
        try:
            self.gateway.get_state()
            gateway_status = "connected"
        except Exception:
            gateway_status = "unavailable"
        return {"status": "ok" if gateway_status == "connected" else "degraded", "database": self.engine.dialect.name,
                "gateway": gateway_status, "simulated": True, "mode": "local-technical-validation"}

    def robot(self) -> dict[str, Any]:
        try:
            state = self.gateway.get_state()
            return {**state, "simulated": state.get("simulated") is True,
                    "connection_status": "connected", "received_at": utcnow().isoformat()}
        except Exception as error:
            raise ServiceError(503, "Robot simulator gateway is unavailable") from error

    def _task(self, session: Any, task_id: str, lock: bool = False) -> Task:
        query = select(Task).where(Task.id == task_id)
        if lock:
            query = query.with_for_update()
        task = session.scalar(query)
        if task is None:
            raise ServiceError(404, "Task not found")
        return task

    def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = payload["request_id"]
        digest = hashlib.sha256(json.dumps({k: v for k, v in payload.items() if k != "request_id"}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        try:
            with self.sessions.begin() as session:
                existing = session.scalar(select(Task).where(Task.request_id == request_id))
                if existing:
                    if existing.request_hash != digest:
                        raise ServiceError(409, "request_id was already used with a different task payload")
                    task_id = existing.id
                else:
                    task_id = str(uuid4())
                    task = Task(id=task_id, request_id=request_id, request_hash=digest, name=payload["name"], points=payload["points"])
                    session.add(task)
                    session.flush()
                    session.add(Outbox(id=str(uuid4()), task_id=task_id, expires_at_ms=int(time.time() * 1000) + self.settings.command_ttl_ms))
        except IntegrityError:
            # Another request may have inserted the same request_id after our
            # initial SELECT. Resolve to its committed task rather than 500.
            with self.sessions() as session:
                existing = session.scalar(select(Task).where(Task.request_id == request_id))
                if existing is None:
                    raise
                if existing.request_hash != digest:
                    raise ServiceError(409, "request_id was already used with a different task payload")
                task_id = existing.id
        try:
            self.dispatch(task_id)
        except IntegrityError:
            # A concurrent synchronization has already persisted the same result.
            pass
        return self.get_task(task_id)

    def recover_outbox(self) -> None:
        with self.sessions() as session:
            ids = list(session.scalars(select(Outbox.task_id).where(Outbox.status == "pending")))
        for task_id in ids:
            self.dispatch(task_id)

    def dispatch(self, task_id: str) -> None:
        # The row lock serializes API/recovery retries. The gateway supplies the
        # second idempotency boundary and persists command_id across restarts.
        with self.sessions.begin() as session:
            outbox = session.scalar(select(Outbox).where(Outbox.task_id == task_id).with_for_update())
            if outbox is None or outbox.status != "pending":
                return
            task = self._task(session, task_id)
            if task.status in ("stopped", "dispatch_expired", "rejected"):
                outbox.status = "cancelled"
                return
            outbox.attempts += 1
            try:
                snapshot = self.gateway.get_task(task_id)
                if snapshot.get("status") != "not_found":
                    outbox.status = "sent"
                    outbox.result = {"code": "already_present", "accepted": True}
                    return
                if int(time.time() * 1000) > outbox.expires_at_ms:
                    outbox.status = "expired"
                    task.status = "dispatch_expired"
                    task.reason = "Start command expired; it was not replayed."
                    task.updated_at = utcnow()
                    return
                result = self.gateway.start_task(task_id, outbox.id, outbox.expires_at_ms, task.points)
                outbox.result = result
                outbox.status = "sent" if result.get("accepted") else "rejected"
                task.status = "running" if result.get("accepted") else "rejected"
                task.reason = "" if result.get("accepted") else result.get("message", result.get("code", "Rejected"))
                task.updated_at = utcnow()
            except Exception:
                task.reason = "Gateway unavailable; pending command will be retried only within its validity period."
                task.updated_at = utcnow()

    def synchronize(self, task_id: str) -> None:
        try:
            snapshot = self.gateway.get_task(task_id)
        except Exception:
            return
        if snapshot.get("status") == "not_found":
            return
        with self.sessions.begin() as session:
            task = self._task(session, task_id, lock=True)
            task.status = snapshot["status"]
            task.progress = max(0, min(100, float(snapshot.get("progress", 0))))
            task.reason = snapshot.get("reason", "")
            task.updated_at = utcnow()
            task.last_synced_at = utcnow()
            points = {p["id"]: p for p in task.points}
            for item in snapshot.get("samples", []):
                point_id = item["point_id"]
                if session.scalar(select(Sample.id).where(Sample.task_id == task_id, Sample.point_id == point_id)):
                    continue
                image = item.get("image_png", b"")
                file_id = None
                if image:
                    record = self.store_file(session, task_id, bytes(image), f"sample-{point_id}.png", "image/png", "sample")
                    file_id = record.id
                    session.flush()
                session.add(Sample(id=str(uuid4()), task_id=task_id, point_id=point_id,
                                   label=item.get("label") or points.get(point_id, {}).get("label", point_id),
                                   device_id=item.get("device_id", ""), temperature_c=item["temperature_c"],
                                   humidity_percent=item["humidity_percent"], captured_at=str(item.get("captured_at", "")),
                                   image_file_id=file_id, simulated=True))

    def store_file(self, session: Any, task_id: str, data: bytes, name: str, media_type: str, kind: str) -> StoredFile:
        file_id = str(uuid4())
        folder = self.settings.data_dir / task_id
        folder.mkdir(exist_ok=True)
        suffix = Path(name).suffix
        path = folder / (file_id + suffix)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_bytes(data)
        temp.replace(path)
        record = StoredFile(id=file_id, task_id=task_id, path=str(path), name=name, media_type=media_type,
                            kind=kind, sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
        session.add(record)
        return record

    def task_dict(self, session: Any, task: Task) -> dict[str, Any]:
        samples = list(session.scalars(select(Sample).where(Sample.task_id == task.id).order_by(Sample.captured_at)))
        files = list(session.scalars(select(StoredFile).where(StoredFile.task_id == task.id, StoredFile.kind != "sample").order_by(StoredFile.created_at)))
        return {"id": task.id, "name": task.name, "request_id": task.request_id, "status": task.status,
                "progress": task.progress, "points": task.points, "review_note": task.review_note, "reason": task.reason,
                "simulated": True, "created_at": iso(task.created_at), "updated_at": iso(task.updated_at),
                "last_synced_at": iso(task.last_synced_at),
                "samples": [{"point_id": s.point_id, "label": s.label, "device_id": s.device_id, "temperature_c": s.temperature_c,
                             "humidity_percent": s.humidity_percent, "captured_at": s.captured_at,
                             "image_url": f"/api/files/{s.image_file_id}" if s.image_file_id else None, "simulated": True} for s in samples],
                "reports": [{"id": f.id, "format": f.kind, "name": f.name, "url": f"/api/files/{f.id}",
                             "sha256": f.sha256, "size_bytes": f.size_bytes} for f in files]}

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self.sessions() as session:
            self._task(session, task_id)
        self.synchronize(task_id)
        with self.sessions() as session:
            return self.task_dict(session, self._task(session, task_id))

    def list_tasks(self) -> list[dict[str, Any]]:
        with self.sessions() as session:
            rows = list(session.scalars(select(Task).order_by(Task.created_at.desc()).limit(100)))
            active_ids = [task.id for task in rows if task.status in ("pending_dispatch", "running")]
        if active_ids:
            try:
                self.gateway.get_state()
            except Exception:
                active_ids = []
        for task_id in active_ids:
            self.synchronize(task_id)
        with self.sessions() as session:
            rows = list(session.scalars(select(Task).order_by(Task.created_at.desc()).limit(100)))
            return [self.task_dict(session, task) for task in rows]

    def stop_task(self, task_id: str) -> dict[str, Any]:
        # Send the control request before consulting the business database.
        # A database failure must not prevent an already-running simulated
        # robot from receiving a stop request.
        try:
            reply = self.gateway.stop_task(task_id, str(uuid4()), int(time.time() * 1000) + 5000)
        except Exception as error:
            raise ServiceError(503, "Stop was not confirmed: gateway unavailable") from error
        if reply.get("accepted"):
            return self.confirmed_stop(task_id)
        with self.sessions() as session:
            task = self._task(session, task_id)
            old_status = task.status
        if old_status != "pending_dispatch":
            raise ServiceError(409, reply.get("message", "Stop rejected"))
        retry_stop = False
        with self.sessions.begin() as session:
            outbox = session.scalar(select(Outbox).where(Outbox.task_id == task_id).with_for_update())
            task = self._task(session, task_id)
            if outbox and outbox.status == "pending":
                outbox.status = "cancelled"
                task.status = "stopped"
                task.reason = "Cancelled before dispatch."
            elif not reply.get("accepted"):
                # Dispatch could have completed between the initial read and
                # cancellation. Confirm a stop against that newly sent task.
                retry_stop = True
        if retry_stop:
            try:
                reply = self.gateway.stop_task(task_id, str(uuid4()), int(time.time() * 1000) + 5000)
            except Exception as error:
                raise ServiceError(503, "Stop was not confirmed: gateway unavailable") from error
            if not reply.get("accepted"):
                raise ServiceError(409, reply.get("message", "Stop rejected"))
        return self.confirmed_stop(task_id) if retry_stop else self.get_task(task_id)

    def confirmed_stop(self, task_id: str) -> dict[str, Any]:
        try:
            return self.get_task(task_id)
        except SQLAlchemyError:
            snapshot = self.gateway.get_task(task_id)
            return {"id": task_id, "name": "Stop confirmed by gateway", "request_id": "",
                    "status": snapshot.get("status", "stop_confirmed"), "progress": snapshot.get("progress", 0),
                    "points": [], "samples": [], "reports": [], "review_note": "", "simulated": True,
                    "reason": "Gateway confirmed stop; database unavailable. Persistent state will be reconciled after recovery.",
                    "database_status": "unavailable", "last_synced_at": None, "created_at": None,
                    "updated_at": utcnow().isoformat()}

    def review(self, task_id: str, note: str) -> dict[str, Any]:
        with self.sessions.begin() as session:
            task = self._task(session, task_id, lock=True)
            task.review_note = note
            task.updated_at = utcnow()
            session.add(Review(id=str(uuid4()), task_id=task_id, note=note))
        return self.get_task(task_id)

    def reports(self, task_id: str) -> dict[str, Any]:
        return {"task_id": task_id, "reports": self.get_task(task_id)["reports"]}

    def generate_reports(self, task_id: str) -> dict[str, Any]:
        from .reports import render_reports
        with self._report_lock:
            task_data = self.get_task(task_id)
            if task_data["status"] not in ("completed", "stopped", "interrupted"):
                raise ServiceError(409, "Wait for the task to complete or stop before generating a report")
            job_id = str(uuid4())
            with self.sessions.begin() as session:
                session.add(ReportJob(id=job_id, task_id=task_id, status="running"))
                image_records = list(session.scalars(select(StoredFile).where(StoredFile.task_id == task_id, StoredFile.kind == "sample")))
                images = [{"label": f.name, "bytes": Path(f.path).read_bytes()} for f in image_records]
            try:
                outputs = render_reports(task_data, images)
                with self.sessions.begin() as session:
                    for format_name, (data, media_type) in outputs.items():
                        self.store_file(session, task_id, data, f"inspection-{task_id}.{format_name}", media_type, format_name)
                    session.get(ReportJob, job_id).status = "completed"
            except Exception as error:
                with self.sessions.begin() as session:
                    job = session.get(ReportJob, job_id)
                    job.status = "failed"
                    job.error = str(error)
                raise ServiceError(500, f"Report generation failed: {type(error).__name__}") from error
            return {"task_id": task_id, "status": "completed", "reports": self.get_task(task_id)["reports"]}

    def file(self, file_id: str) -> dict[str, Any]:
        with self.sessions() as session:
            record = session.get(StoredFile, file_id)
            if record is None:
                raise ServiceError(404, "File not found")
            path = Path(record.path).resolve()
            if not path.is_relative_to(self.settings.data_dir) or not path.is_file():
                raise ServiceError(404, "File content unavailable")
            return {"path": path, "media_type": record.media_type, "name": record.name}
