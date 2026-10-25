"""流式问答的进程内准入限制，保留线程给登录、列表和探活。"""
import threading
from contextlib import contextmanager

from fastapi import HTTPException

from app.core.config import settings


class ChatAdmission:
    def __init__(self):
        self._lock = threading.Lock()
        self._users: dict[str, int] = {}
        self._total = 0

    @contextmanager
    def slot(self, user_id: str):
        with self._lock:
            current = self._users.get(user_id, 0)
            if current >= settings.CHAT_MAX_PER_USER or self._total >= settings.CHAT_MAX_CONCURRENT:
                raise HTTPException(status_code=429, detail="当前问答请求较多，请等待已有回答结束", headers={"Retry-After": "3"})
            self._users[user_id] = current + 1
            self._total += 1
        try:
            yield
        finally:
            with self._lock:
                remaining = self._users[user_id] - 1
                if remaining:
                    self._users[user_id] = remaining
                else:
                    del self._users[user_id]
                self._total -= 1


chat_admission = ChatAdmission()
