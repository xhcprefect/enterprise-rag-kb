"""统一日志配置：控制台 + 滚动文件。

排查问题时最常用的是文件日志：``backend/logs/app.log``（API 进程）与
``backend/logs/worker.log``（入库进程）。按 10MB 滚动、保留 5 份，避免无限增长。
"""
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.core.config import settings

_CONFIGURED = False

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def _console_handler() -> logging.Handler:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    # Windows 控制台默认 GBK，中文/emoji 可能编码失败；只影响显示，不影响文件日志
    stream = getattr(handler, "stream", None)
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass
    return handler


def _file_handler(component: str) -> logging.Handler:
    log_dir = Path(settings.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / f"{component}.log",
        maxBytes=settings.LOG_MAX_BYTES,
        backupCount=settings.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    return handler


def setup_logging(component: str = "app") -> None:
    """配置根 logger；重复调用（如 uvicorn reload）不会叠加 handler。"""
    global _CONFIGURED

    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    if not _CONFIGURED:
        _CONFIGURED = True
        root.addHandler(_console_handler())
        try:
            root.addHandler(_file_handler(component))
        except OSError as exc:  # 目录不可写时退化为仅控制台
            logging.getLogger(__name__).warning(
                "无法创建日志文件（%s），仅输出到控制台: %s", settings.LOG_DIR, exc
            )

    # uvicorn 自带 handler，且它配置时未必设置 propagate=False：
    # 若不显式关掉传播，同一条日志会被"uvicorn 自己的 handler + 根 handler"写两遍。
    # 这里保留 uvicorn 自己的控制台输出，只额外挂文件 handler，并关闭向上传播。
    file_handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.propagate = False
        for handler in file_handlers:
            if handler not in lg.handlers:
                lg.addHandler(handler)
