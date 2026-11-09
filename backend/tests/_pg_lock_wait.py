"""真实 PostgreSQL 并发用例的精确阻塞证据；不连接默认或业务数据库。"""
import time

from sqlalchemy import text


def wait_for_blocking(engine, holder_pid: int, waiter_pid: int, *, timeout: float = 8.0) -> bool:
    """只接受本次两个连接之间的锁等待，独立自动提交连接避免统计快照缓存。"""
    deadline = time.monotonic() + timeout
    statement = text(
        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid = :waiter "
        "AND state = 'active' AND wait_event_type = 'Lock' "
        "AND :holder = ANY(pg_blocking_pids(pid)))"
    )
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
        while time.monotonic() < deadline:
            if observer.execute(statement, {"holder": holder_pid, "waiter": waiter_pid}).scalar_one():
                return True
            time.sleep(0.02)
    return False
