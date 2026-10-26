"""测试环境变量准备。

必须在导入任何 `app.*` 模块之前执行：`app.core.config` 在导入时实例化
Settings，`app.db.session` 紧接着按当时的 DATABASE_URL 建好 engine。
一旦 engine 建好，之后再改环境变量也不会重连。
"""
import os

TEST_URL = os.environ.get("TEST_DATABASE_URL", "")


def prepare() -> None:
    if not TEST_URL:
        return
    from sqlalchemy.engine import make_url

    if not (make_url(TEST_URL).database or "").endswith("_test"):
        raise RuntimeError("TEST_DATABASE_URL 必须指向以 _test 结尾的专用数据库")
    os.environ["DATABASE_URL"] = TEST_URL
    os.environ["JWT_SECRET"] = "regression-tests-only-not-for-production"
