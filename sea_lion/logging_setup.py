"""Logging to stdout + runtime/logs, with credential redaction."""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

SECRET_ENV = ["ALPACA_PAPER_API_KEY", "ALPACA_PAPER_SECRET_KEY", "ALPACA_LIVE_API_KEY", "ALPACA_LIVE_SECRET_KEY",
              "ANTHROPIC_API_KEY", "SEA_LION_LIVE_CONFIRM_TOKEN"]


class Redact(logging.Filter):
    def __init__(self):
        super().__init__()
        self._pat = re.compile(r"(sk-ant-[A-Za-z0-9_\-]{8,}|PK[A-Z0-9]{16,}|APCA-API-SECRET-KEY\S*)")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for k in SECRET_ENV:
            v = os.environ.get(k)
            if v and len(v) >= 6 and v in msg:
                msg = msg.replace(v, "***REDACTED***")
        msg = self._pat.sub("***REDACTED***", msg)
        record.msg, record.args = msg, ()
        return True


def setup(log_dir: Path, level: str = "INFO") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in (logging.StreamHandler(), logging.FileHandler(log_dir / "sea_lion.log")):
        h.setFormatter(fmt)
        h.addFilter(Redact())
        root.addHandler(h)
    logging.getLogger("httpx").setLevel("WARNING")
    logging.getLogger("yfinance").setLevel("WARNING")
    logging.getLogger("peewee").setLevel("WARNING")
