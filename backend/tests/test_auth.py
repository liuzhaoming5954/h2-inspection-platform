"""真机模式登录门接口测试：默认密码播种、令牌签发、改密与错误锁定。"""
from fastapi.testclient import TestClient


def login(client: TestClient, password: str):
    return client.post("/api/auth/verify", json={"password": password})


def test_default_password_login_and_check(client):
    """首次验证自动播种默认密码 123，签发的令牌可通过 check 校验。"""
    response = login(client, "123")
    assert response.status_code == 200
    token = response.json()["token"]
    assert client.get("/api/auth/check", headers={"X-Auth-Token": token}).json()["valid"] is True
    assert client.get("/api/auth/check", headers={"X-Auth-Token": "bogus"}).json()["valid"] is False


def test_wrong_password_rejected(client):
    assert login(client, "wrong").status_code == 401
    assert client.get("/api/auth/check", headers={"X-Auth-Token": "x"}).json()["valid"] is False


def test_lockout_after_consecutive_failures(client):
    """前 4 次错误返回 401，第 5 次触发 30 秒锁定；锁定期间正确密码也暂时拒绝。"""
    for _ in range(4):
        assert login(client, "wrong").status_code == 401
    locked = login(client, "wrong")
    assert locked.status_code == 429
    assert "锁定" in locked.json()["detail"]
    assert login(client, "123").status_code == 429
    assert "重试" in login(client, "123").json()["detail"]


def test_successful_login_resets_failure_count(client):
    """登录成功会清零连续错误计数，不会跨次累积到锁定。"""
    for _ in range(4):
        assert login(client, "wrong").status_code == 401
    assert login(client, "123").status_code == 200
    assert login(client, "wrong").status_code == 401


def test_change_password_flow(client):
    """改密后旧密码失效、新密码可用；当前会话保留，改密前签发的其他令牌被作废。"""
    other_token = login(client, "123").json()["token"]
    current_token = login(client, "123").json()["token"]
    response = client.post("/api/auth/change", json={
        "token": current_token, "old_password": "123", "new_password": "abc456"})
    assert response.status_code == 200
    assert login(client, "123").status_code == 401
    assert login(client, "abc456").status_code == 200
    assert client.get("/api/auth/check", headers={"X-Auth-Token": current_token}).json()["valid"] is True
    assert client.get("/api/auth/check", headers={"X-Auth-Token": other_token}).json()["valid"] is False


def test_change_requires_valid_token(client):
    assert client.post("/api/auth/change", json={
        "token": "bogus", "old_password": "123", "new_password": "x"}).status_code == 401


def test_change_rejects_empty_new_password(client):
    token = login(client, "123").json()["token"]
    assert client.post("/api/auth/change", json={
        "token": token, "old_password": "123", "new_password": ""}).status_code == 422


def test_change_wrong_old_password_counts_toward_lockout(client):
    """旧密码错误计入连续失败：改密接口上乱试同样会触发锁定。"""
    token = login(client, "123").json()["token"]
    for _ in range(4):
        response = client.post("/api/auth/change", json={
            "token": token, "old_password": "wrong", "new_password": "x"})
        assert response.status_code == 401
    assert client.post("/api/auth/change", json={
        "token": token, "old_password": "wrong", "new_password": "x"}).status_code == 429
    response = client.post("/api/auth/change", json={
        "token": token, "old_password": "123", "new_password": "x"})
    assert response.status_code == 429


def test_health_exposes_per_process_instance_id(client, settings, gateway):
    """健康检查带每次后端启动都不同的实例标识：前端据此判定后端重启、登录令牌已全部失效。"""
    from h2_backend.app import create_app
    first = client.get("/api/health").json()["instance_id"]
    with TestClient(create_app(settings, gateway)) as restarted:
        second = restarted.get("/api/health").json()["instance_id"]
        token = login(restarted, "123").json()["token"]
        assert restarted.get("/api/auth/check", headers={"X-Auth-Token": token}).json()["valid"] is True
    assert isinstance(first, str) and first
    assert first != second
