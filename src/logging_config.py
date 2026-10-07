from __future__ import annotations

import logging
import os
import re


def redact(value: object) -> str:
    text = str(value)
    for name, secret in os.environ.items():
        if secret and (name.endswith("API_KEY") or name.endswith("TOKEN")):
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(https?://[^\s?'\"]+)\?[^\s'\"]+", r"\1?[REDACTED]", text)
    return text


class SecretFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            # Exception strings can include auth headers / signed URLs.
            record.msg += f" [exception={record.exc_info[0].__name__}]"
            record.exc_info = None
            record.exc_text = None
        return True


def configure_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, SecretFilter) for f in handler.filters):
            handler.addFilter(SecretFilter())
