"""真机模式登录门：密码哈希校验、会话令牌与错误次数锁定。

方案 A（轻量操作闸门）：仅登录/改密/令牌校验由后端处理，其余 API 不做
拦截——真实运动的安全边界仍由遥控租约独占与现场规程保障。
令牌保存在进程内存：后端重启后所有会话失效，前端需重新登录。
"""
import hashlib
import secrets
import threading
import time

from fastapi import HTTPException
from sqlalchemy import select

from .models import AppCredential

DEFAULT_PASSWORD = "123"
MAX_FAILS = 5
LOCK_SECONDS = 30


def _hash(salt: str, password: str) -> str:
    return hashlib.sha256((salt + password).encode()).hexdigest()


class AuthGate:
    """密码校验与令牌签发。错误计数/锁定为进程内存状态。"""

    def __init__(self, service):
        # service 提供 sessions（SQLAlchemy sessionmaker），随宿主应用生命周期存活。
        self.service = service
        self.tokens: set[str] = set()
        self._fails = 0
        self._locked_until = 0.0
        self._lock = threading.Lock()

    def _locked_remaining(self) -> float:
        return max(0.0, self._locked_until - time.monotonic())

    def ensure_default(self) -> None:
        """首次验证时播种默认密码，幂等。"""
        with self.service.sessions() as session:
            if session.get(AppCredential, 1) is None:
                salt = secrets.token_hex(16)
                session.add(AppCredential(id=1, salt=salt, password_hash=_hash(salt, DEFAULT_PASSWORD)))
                session.commit()

    def _fail(self) -> None:
        """记一次失败；达到上限则锁定并抛 429。调用方需持锁。"""
        self._fails += 1
        if self._fails >= MAX_FAILS:
            self._locked_until = time.monotonic() + LOCK_SECONDS
            self._fails = 0
            raise HTTPException(status_code=429, detail=f"连续错误 {MAX_FAILS} 次，锁定 {LOCK_SECONDS} 秒")
        raise HTTPException(status_code=401, detail=f"密码错误（连续错误 {self._fails}/{MAX_FAILS} 次）")

    def verify(self, password: str) -> str:
        with self._lock:
            remaining = self._locked_remaining()
            if remaining > 0:
                raise HTTPException(status_code=429, detail=f"错误次数过多，{int(remaining) + 1} 秒后重试")
            self.ensure_default()
            with self.service.sessions() as session:
                record = session.scalars(select(AppCredential).where(AppCredential.id == 1)).first()
                ok = record is not None and _hash(record.salt, password) == record.password_hash
            if ok:
                self._fails = 0
                token = secrets.token_urlsafe(32)
                self.tokens.add(token)
                return token
            self._fail()

    def check(self, token: str) -> bool:
        return token in self.tokens

    def change(self, token: str, old_password: str, new_password: str) -> None:
        if token not in self.tokens:
            raise HTTPException(status_code=401, detail="登录会话已失效，请重新登录")
        if not new_password:
            raise HTTPException(status_code=422, detail="新密码不能为空")
        with self._lock:
            remaining = self._locked_remaining()
            if remaining > 0:
                raise HTTPException(status_code=429, detail=f"错误次数过多，{int(remaining) + 1} 秒后重试")
            with self.service.sessions() as session:
                record = session.scalars(select(AppCredential).where(AppCredential.id == 1)).first()
                if record is None or _hash(record.salt, old_password) != record.password_hash:
                    # 旧密码错误同样计入连续失败，防止绕过登录页在改密接口上乱试。
                    self._fail()
                new_salt = secrets.token_hex(16)
                record.salt = new_salt
                record.password_hash = _hash(new_salt, new_password)
                session.commit()
            # 改密成功：保留当前会话，作废其余全部旧令牌。
            self.tokens &= {token}
