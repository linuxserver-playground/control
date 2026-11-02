"""
Logging setup. httpx logs every request URL at INFO, and a Discord webhook URL carries its token in the path;
uvicorn's access log records query strings, and the setup page takes its token in one. Both would put a
credential in `docker logs`, so every record passes through ``Redact`` before it is written.
"""

import logging
import re

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

PATTERNS = (
    (re.compile(r"(/api/webhooks/\d+/)[\w-]+"), r"\1***"),
    (re.compile(r"([?&](?:token|code|state|client_secret)=)[^&\s\"']+"), r"\1***"),
)


def redact(text: str) -> str:
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class Redact(logging.Filter):
    """Render the message, redact credentials from it, and keep the rendered text."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        return True


def configure() -> None:
    logging.basicConfig(level=logging.INFO, format=FORMAT)
    for handler in logging.getLogger().handlers:
        handler.addFilter(Redact())
