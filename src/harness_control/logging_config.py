"""JSON logs, and a filter that makes it impossible to log the API key.

SPEC §5.7: "The key is never logged. A redaction filter on the logging config
asserts this." The filter runs on every record from every logger — including
uvicorn's and httpx's, which is where an accidental leak would actually come from,
since a bearer token can ride along in a URL or a header dump.
"""

import json
import logging
import logging.config
import re
from typing import Any

REDACTED = "[redacted]"

#: Anything shaped like a bearer token, whether or not it is *our* key. A key that
#: has been rotated is still a key, and a leaked upstream token is just as bad.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9\-._~+/]{8,}=*)"),
    re.compile(r"(?i)\b(api[_-]?key|token|authorization)\b(\s*[=:]\s*)(\"?)([^\s\"',}]{8,})"),
)


class RedactingFilter(logging.Filter):
    """Scrubs the configured key, and anything token-shaped, out of every record.

    A `Filter` rather than a `Formatter` because it must apply to `record.args` and
    `record.msg` before any handler sees them — including handlers added later by
    uvicorn.
    """

    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        # Short strings are not secrets worth matching; scrubbing "" would blank
        # out every message.
        self._secrets = [s for s in (secrets or []) if len(s) >= 8]

    def add_secret(self, secret: str) -> None:
        if len(secret) >= 8 and secret not in self._secrets:
            self._secrets.append(secret)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self.redact(record.msg)
        if record.args:
            record.args = self._redact_args(record.args)
        for attr in ("upstream", "url", "path"):
            value = getattr(record, attr, None)
            if isinstance(value, str):
                setattr(record, attr, self.redact(value))
        return True

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        text = _PATTERNS[0].sub(rf"\1 {REDACTED}", text)
        return _PATTERNS[1].sub(rf"\1\2\3{REDACTED}", text)

    def _redact_args(self, args: Any) -> Any:
        if isinstance(args, tuple):
            return tuple(self._redact_value(a) for a in args)
        if isinstance(args, dict):
            return {k: self._redact_value(v) for k, v in args.items()}
        return self._redact_value(args)

    def _redact_value(self, value: Any) -> Any:
        return self.redact(value) if isinstance(value, str) else value


class JsonFormatter(logging.Formatter):
    """One JSON object per line — what the journal and logrotate expect."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


#: The one filter instance, so `configure_logging` and later `add_secret` calls
#: act on the same object every handler holds.
_filter = RedactingFilter()


def redacting_filter() -> RedactingFilter:
    return _filter


def configure_logging(level: str = "INFO", api_key: str = "") -> RedactingFilter:
    """Install JSON logging with redaction. Safe to call more than once."""
    if api_key:
        _filter.add_secret(api_key)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(_filter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn and httpx install their own handlers; the filter has to reach those
    # too or a request line with a token in it would go out unscrubbed.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "httpx", "httpcore"):
        logger = logging.getLogger(name)
        logger.addFilter(_filter)
        for existing_handler in logger.handlers:
            existing_handler.addFilter(_filter)

    return _filter
