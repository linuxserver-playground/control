import logging

from control.logs import Redact, redact


def test_redacts_discord_webhook_token() -> None:
    line = 'HTTP Request: POST https://discord.com/api/webhooks/123456/AbC-d_9?wait=true "HTTP/1.1 200 OK"'
    assert redact(line) == 'HTTP Request: POST https://discord.com/api/webhooks/123456/***?wait=true "HTTP/1.1 200 OK"'


def test_redacts_token_query_values() -> None:
    assert redact('"GET /setup?token=deadbeef HTTP/1.1" 200') == '"GET /setup?token=*** HTTP/1.1" 200'
    assert redact("/login/callback?code=abc&state=xyz") == "/login/callback?code=***&state=***"


def test_leaves_other_text_alone() -> None:
    line = "HTTP Request: GET https://api.github.com/repos/o/r?per_page=100 200"
    assert redact(line) == line


def test_filter_renders_args_before_redacting() -> None:
    record = logging.LogRecord(
        "httpx", logging.INFO, __file__, 1, "HTTP Request: %s %s", ("POST", "https://x/api/webhooks/1/secret"), None
    )
    assert Redact().filter(record)
    assert record.getMessage() == "HTTP Request: POST https://x/api/webhooks/1/***"
