"""测试包初始化：在导入任何应用模块之前固定测试环境。

`app.core.config` 在模块导入时就实例化 Settings，`app.db.session` 随即按
当时的 DATABASE_URL 建好 engine。只要有一个测试模块先于 `test_database`
导入应用代码，后者的 os.environ 赋值就来不及生效。

以 `python -m unittest discover -s tests -t .` 运行时本文件先于所有子模块
执行。若仍以目录方式运行（`-s tests` 不带 `-t`），本文件不会被加载，
`tests/test_database.py` 里的 `_bind_test_engine()` 会兜底重绑。
"""
from tests._env import prepare

prepare()
