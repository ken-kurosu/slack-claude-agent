"""ログに秘密情報（トークン・鍵）が混ざっても伏せ字にするフィルタ。app.py で全ハンドラに付ける"""
import logging
import re

_SECRET_RX = re.compile(
    r"xox[abposr]-[0-9A-Za-z-]{10,}|xapp-\d-[0-9A-Za-z-]{10,}|sk-ant-[a-z0-9]{2,}-[0-9A-Za-z_-]{20,}|"
    r"sk-(proj-)?[0-9A-Za-z]{32,}|(ghp|gho|ghs)_[0-9A-Za-z]{30,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|"
    r"(secret|ntn)_[0-9A-Za-z]{40,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


def redact(text: str) -> str:
    return _SECRET_RX.sub(lambda m: m.group(0)[:5] + "…[REDACTED]", text)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        red = redact(msg)
        if red != msg:
            record.msg, record.args = red, ()
        return True


def install() -> None:
    """ルートロガーの全ハンドラに伏せ字フィルタを付ける"""
    f = RedactFilter()
    for h in logging.getLogger().handlers:
        h.addFilter(f)
