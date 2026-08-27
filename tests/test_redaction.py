"""The API key must never reach a log record.

SPEC §5.7: "The key is never logged. A redaction filter on the logging config
asserts this." A leak here is quiet and permanent — the journal keeps it, logrotate
copies it, and `scripts/tail-logs.sh` prints it to whoever is debugging.
"""

import logging

import httpx
import pytest

from conftest import API_KEY, AUTH, MODEL_ID
from harness_control.logging_config import (
    REDACTED,
    JsonFormatter,
    RedactingFilter,
    configure_logging,
    log_buffer,
)


@pytest.fixture
def redactor() -> RedactingFilter:
    return RedactingFilter([API_KEY])


def record(msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord("test", logging.INFO, __file__, 1, msg, args, None)


def apply(redactor: RedactingFilter, msg: str, *args: object) -> str:
    entry = record(msg, *args)
    assert redactor.filter(entry) is True
    return entry.getMessage()


# ----------------------------------------------------------------- the unit


def test_the_configured_key_is_scrubbed_from_the_message(redactor: RedactingFilter) -> None:
    assert API_KEY not in apply(redactor, f"authenticating with {API_KEY}")


def test_the_configured_key_is_scrubbed_from_the_args(redactor: RedactingFilter) -> None:
    """`log.info("key=%s", key)` is the most likely way it would actually leak."""
    assert API_KEY not in apply(redactor, "key=%s", API_KEY)


def test_a_key_in_a_dict_arg_is_scrubbed(redactor: RedactingFilter) -> None:
    assert API_KEY not in apply(redactor, "%(key)s", {"key": API_KEY})


def test_a_bearer_header_is_scrubbed_even_if_the_key_is_unknown() -> None:
    """A rotated key, or an upstream provider's key, is still a secret."""
    plain = RedactingFilter()
    out = apply(plain, "Authorization: Bearer sk-someone-elses-secret-token")
    assert "sk-someone-elses-secret-token" not in out
    assert REDACTED in out


@pytest.mark.parametrize(
    "template",
    [
        "api_key=%s",
        "api-key: %s",
        'token="%s"',
        "authorization: %s",
        "APIKEY=%s",
    ],
)
def test_key_shaped_assignments_are_scrubbed(template: str) -> None:
    secret = "a-very-secret-value-9876543210"  # noqa: S105 - a fake secret is the point
    out = apply(RedactingFilter(), template.replace("%s", secret))
    assert secret not in out


def test_short_strings_are_not_treated_as_secrets() -> None:
    """Redacting an empty or tiny secret would blank out unrelated messages."""
    redactor = RedactingFilter(["", "ab"])
    assert apply(redactor, "a normal message about a table") == "a normal message about a table"


def test_ordinary_messages_are_untouched(redactor: RedactingFilter) -> None:
    assert apply(redactor, "model %s is ready", MODEL_ID) == f"model {MODEL_ID} is ready"


def test_non_string_args_survive(redactor: RedactingFilter) -> None:
    assert apply(redactor, "loaded in %d s", 25) == "loaded in 25 s"


def test_the_formatter_emits_one_json_object_per_record(redactor: RedactingFilter) -> None:
    import json

    entry = record("key=%s", API_KEY)
    redactor.filter(entry)
    payload = json.loads(JsonFormatter().format(entry))
    assert set(payload) >= {"ts", "level", "logger", "message"}
    assert API_KEY not in json.dumps(payload)


# ------------------------------------------------------------- end to end


async def test_no_handler_ever_sees_the_key(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Drive real traffic — including a rejected key — and read every record back."""
    configure_logging("DEBUG", API_KEY)
    with caplog.at_level(logging.DEBUG):
        await client.get("/v1/models", headers=AUTH)
        await client.get("/admin/state", headers=AUTH)
        await client.get("/v1/models", headers={"Authorization": f"Bearer {API_KEY}"})
        await client.post(
            "/v1/chat/completions",
            json={"model": MODEL_ID, "messages": [{"role": "user", "content": "hi"}]},
            headers=AUTH,
        )

    for entry in caplog.records:
        assert API_KEY not in entry.getMessage(), f"the key leaked into {entry.name}"
        assert API_KEY not in str(entry.args or "")


async def test_the_key_does_not_leak_through_a_failed_auth_attempt(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    configure_logging("DEBUG", API_KEY)
    with caplog.at_level(logging.DEBUG):
        await client.get(
            "/v1/models", headers={"Authorization": "Bearer wrong-but-long-enough-key"}
        )

    for entry in caplog.records:
        assert "wrong-but-long-enough-key" not in entry.getMessage()


def test_configure_logging_installs_the_filter_on_uvicorn_loggers() -> None:
    """uvicorn's access log prints the request line — a token in a URL would leak there."""
    installed = configure_logging("INFO", API_KEY)
    for name in ("uvicorn.access", "uvicorn.error", "httpx"):
        assert installed in logging.getLogger(name).filters, name


def test_configure_logging_is_idempotent() -> None:
    configure_logging("INFO", API_KEY)
    configure_logging("INFO", API_KEY)
    # Two by design: the stream handler and the ring buffer /admin/logs serves.
    # Calling twice must not accumulate more.
    assert len(logging.getLogger().handlers) == 2


# --- the ring buffer -----------------------------------------------------------


def test_the_ring_buffer_is_redacted_too() -> None:
    """`/admin/logs` serves this buffer over HTTP, so it needs the same guarantee.

    The buffer is fed by a logging handler that carries the redaction filter, so
    this holds for anything that logs — not only for call sites that remembered.
    """
    configure_logging("DEBUG", API_KEY)
    log_buffer().clear()

    logging.getLogger("harness_control.test").info("connecting with %s", API_KEY)

    lines = log_buffer().tail(10)
    assert lines, "the buffer received nothing"
    assert API_KEY not in "\n".join(lines)
    assert REDACTED in "\n".join(lines)


def test_the_ring_buffer_redacts_a_key_it_was_never_told_about() -> None:
    configure_logging("DEBUG", API_KEY)
    log_buffer().clear()

    logging.getLogger("harness_control.test").warning(
        "upstream said: Authorization: Bearer sk-someone-elses-token-here"
    )

    assert "sk-someone-elses-token-here" not in "\n".join(log_buffer().tail(10))


def test_the_ring_buffer_keeps_useful_content() -> None:
    """Redaction must not reduce the buffer to noise — it is read to diagnose."""
    configure_logging("DEBUG", API_KEY)
    log_buffer().clear()

    logging.getLogger("harness_control.test").info(
        "activation requested: model=qwen3.8-27b backend=ollama job=act_00000001"
    )

    joined = "\n".join(log_buffer().tail(10))
    assert "qwen3.8-27b" in joined
    assert "act_00000001" in joined
