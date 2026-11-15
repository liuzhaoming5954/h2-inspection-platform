from io import BytesIO
import json
import time

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from h2_backend.models import Outbox, ReportJob, Review, Sample, Task


def test_health_reports_real_database_and_simulator(client):
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["database"] in {"postgresql", "sqlite"}
    assert health.json()["gateway"] == "connected"
    assert health.json()["simulated"] is True


def test_request_id_is_idempotent_and_conflicting_payload_rejected(client, gateway, payload):
    first = client.post("/api/tasks", json=payload)
    second = client.post("/api/tasks", json=payload)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert gateway.starts == 1
    changed = {**payload, "name": "不同任务"}
    assert client.post("/api/tasks", json=changed).status_code == 409


def test_samples_files_review_and_three_report_formats(client, app, gateway, payload):
    task = client.post("/api/tasks", json=payload).json()
    gateway.complete(task["id"], payload["points"])
    result = client.get(f"/api/tasks/{task['id']}").json()
    assert result["status"] == "completed"
    assert result["progress"] == 100
    assert len(result["samples"]) == 2
    assert all(s["simulated"] for s in result["samples"])
    image = client.get(result["samples"][0]["image_url"])
    assert image.content.startswith(b"\x89PNG")
    assert client.post(f"/api/tasks/{task['id']}/review", json={"note": "人工复核：合成数据链路正确"}).status_code == 200
    reports = client.post(f"/api/tasks/{task['id']}/reports")
    assert reports.status_code == 200, reports.text
    exported = {r["format"]: client.get(r["url"]) for r in reports.json()["reports"]}
    assert set(exported) == {"pdf", "xlsx", "json"}
    assert exported["pdf"].content.startswith(b"%PDF-")
    workbook = load_workbook(BytesIO(exported["xlsx"].content))
    assert workbook.active["B4"].value == 24.25
    data = exported["json"].json()
    assert data["review_note"] == "人工复核：合成数据链路正确"
    assert len(data["samples"]) == 2
    assert data["data_source"] == "simulated"
    # 重复轮询时应避免重复保存样本。
    client.get(f"/api/tasks/{task['id']}")
    with app.state.service.sessions() as db:
        assert len(list(db.scalars(select(Sample)))) == 2
        assert len(list(db.scalars(select(Review)))) == 1
        assert db.scalar(select(ReportJob)).status == "completed"


def test_recovery_reconciles_after_gateway_accepted_without_restarting(client, app, gateway, payload):
    task = client.post("/api/tasks", json=payload).json()
    with app.state.service.sessions.begin() as db:
        box = db.scalar(select(Outbox).where(Outbox.task_id == task["id"]))
        box.status = "pending"  # 模拟网关接受指令后丢失数据库回执。
    app.state.service.recover_outbox()
    assert gateway.starts == 1
    with app.state.service.sessions() as db:
        assert db.scalar(select(Outbox)).status == "sent"


def test_expired_pending_start_is_never_replayed(client, app, gateway, payload):
    gateway.available = False
    result = client.post("/api/tasks", json=payload).json()
    assert result["status"] == "pending_dispatch"
    with app.state.service.sessions.begin() as db:
        db.scalar(select(Outbox)).expires_at_ms = int(time.time() * 1000) - 1000
    gateway.available = True
    app.state.service.recover_outbox()
    assert gateway.starts == 0
    assert client.get(f"/api/tasks/{result['id']}").json()["status"] == "dispatch_expired"


def test_stop_is_confirmed_and_gateway_unavailability_is_explicit(client, gateway, payload):
    task = client.post("/api/tasks", json=payload).json()
    assert client.post(f"/api/tasks/{task['id']}/stop").json()["status"] == "stopped"
    gateway.available = False
    assert client.post(f"/api/tasks/{task['id']}/stop").status_code == 503


def test_duplicate_point_ids_and_unknown_files_rejected(client, payload):
    payload["points"].append(payload["points"][0])
    assert client.post("/api/tasks", json=payload).status_code == 422
    assert client.get("/api/files/unknown").status_code == 404


def test_gateway_outage_keeps_persisted_history_queryable(client, gateway, payload):
    task = client.post("/api/tasks", json=payload).json()
    gateway.complete(task["id"], payload["points"])
    assert client.get(f"/api/tasks/{task['id']}").json()["status"] == "completed"
    gateway.available = False
    response = client.get("/api/tasks")
    assert response.status_code == 200
    assert response.json()[0]["status"] == "completed"
    assert len(response.json()[0]["samples"]) == 2
    assert client.get("/api/robot").status_code == 503


def test_stop_reaches_gateway_when_business_database_is_unavailable(client, app, gateway, payload, monkeypatch):
    task = client.post("/api/tasks", json=payload).json()

    def unavailable(*args, **kwargs):
        raise SQLAlchemyError("database temporarily unavailable")

    monkeypatch.setattr(app.state.service, "sessions", unavailable)
    response = client.post(f"/api/tasks/{task['id']}/stop")
    assert response.status_code == 200
    assert gateway.tasks[task["id"]]["status"] == "stopped"
    assert response.json()["database_status"] == "unavailable"
    assert response.json()["status"] == "stopped"
