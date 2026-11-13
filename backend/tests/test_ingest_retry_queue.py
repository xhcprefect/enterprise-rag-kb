"""入库队列的失败退避语义：领取必须尊重 next_attempt_at。

纯 SQL 表达式断言（不需要真实数据库）：确认 ``worker.claim_one`` 生成的
SELECT 里包含退避条件。真实并发/行锁行为由 ``test_ingest_shutdown`` 那组
专用库测试覆盖，这里只锁定"退避不会被静默绕过"这一契约。
"""
import unittest

from sqlalchemy.dialects import postgresql

from app.models import Document, DocumentStatus


class ClaimQueryBackoffTests(unittest.TestCase):
    def _claim_sql(self) -> str:
        """截获 claim_one 内部构造的 SELECT 语句并渲染成 PostgreSQL SQL。"""
        captured = {}

        class _Stop(Exception):
            pass

        class _FakeSession:
            def scalar(self, statement):
                captured["sql"] = str(
                    statement.compile(dialect=postgresql.dialect(),
                                      compile_kwargs={"literal_binds": True})
                )
                raise _Stop

            def rollback(self):
                pass

        import worker

        with self.assertRaises(_Stop):
            worker.claim_one(_FakeSession())  # type: ignore[arg-type]
        return captured["sql"]

    def test_claim_skips_documents_still_in_backoff(self):
        sql = self._claim_sql()
        flat = " ".join(sql.split())
        # 退避条件必须同时覆盖 NULL（从未失败过）和"已到解禁时刻"，
        # 且两者是 OR 关系——只写其中一个会让另一半任务永久卡死。
        self.assertRegex(
            flat,
            r"AND \(documents\.cancel_requested IS true OR documents\.next_attempt_at IS NULL OR documents\.next_attempt_at <= '",
            msg=f"退避条件缺失或不是 OR 关系: {flat}",
        )
        self.assertIn("documents.status = 'pending'", flat)
        # 仍然必须用 SKIP LOCKED，多 worker 才能并发消费。
        self.assertIn("FOR UPDATE SKIP LOCKED", flat.upper())

    def test_claim_orders_by_created_at_for_fairness(self):
        sql = self._claim_sql()
        self.assertRegex(sql, r"ORDER BY\s+documents\.created_at")


class RetryStateTransitionsTests(unittest.TestCase):
    """不连库，直接验证状态机的取值规则（与 ingest 分支一一对应）。"""

    def _delay(self, attempts, base=30, maximum=600):
        from app.services.ingest_failures import retry_delay_seconds
        return retry_delay_seconds(attempts, base=base, maximum=maximum)

    def test_retry_schedule_matches_configured_bounds(self):
        # 第 1/2 次失败进入退避，第 3 次达到上限后终结。
        self.assertEqual([self._delay(n) for n in (1, 2)], [30, 60])
        self.assertGreater(self._delay(3), self._delay(2))

    def test_permanent_failure_is_not_scheduled_for_retry(self):
        from app.services import ingest_failures as failures
        # 永久失败即使 attempts 仍小于上限，也不应进入 pending 退避。
        self.assertFalse(failures.is_retryable(failures.FAILURE_UNSUPPORTED_FORMAT))
        self.assertFalse(failures.is_retryable(failures.FAILURE_EMPTY_CONTENT))


if __name__ == "__main__":
    unittest.main()
