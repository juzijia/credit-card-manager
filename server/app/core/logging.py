import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.core.redaction import redact_sensitive


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_sensitive(record.getMessage(), limit=4000)
        record.args = ()
        if record.exc_info:
            _exc_type, exc, traceback = record.exc_info
            safe_exc = RuntimeError(redact_sensitive(exc, limit=1000))
            record.exc_info = (RuntimeError, safe_exc, traceback)
        return True


def configure_error_log(path_value: str | None) -> None:
    if not path_value:
        return
    path = Path(path_value).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    handler_key = str(path)
    for handler in root.handlers:
        if getattr(handler, "ccm_error_log", None) == handler_key:
            return
    handler = RotatingFileHandler(
        path,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setLevel(logging.ERROR)
    handler.addFilter(SensitiveDataFilter())
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handler.ccm_error_log = handler_key  # type: ignore[attr-defined]
    root.addHandler(handler)
