"""Logging that keeps the token and the pairing code out of Kodi's log."""

import re

PREFIX = "[service.movieparadise] "

_PATTERNS = (
    re.compile(r"(Bearer\s+)[^\s\"',;]+", re.IGNORECASE),
    re.compile(r"((?:\"|')(?:token|deviceCode|device_code|authorization)(?:\"|')\s*:\s*(?:\"|'))[^\"']*", re.IGNORECASE),
    re.compile(r"((?:token|deviceCode|device_code)=)[^\s&]+", re.IGNORECASE),
)

_sink = None
_secrets = set()


def set_sink(sink):
    """sink(message, level); level is DEBUG, INFO, WARNING or ERROR."""
    global _sink
    _sink = sink


def remember_secret(value):
    """Also redact this value (the token, the device code)."""
    if value and len(value) >= 8:
        _secrets.add(value)


def forget_secret(value):
    _secrets.discard(value)


def redact(text):
    text = str(text)
    for secret in _secrets:
        text = text.replace(secret, "***")
    for pattern in _PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + "***", text)
    return text


def _write(level, message, args):
    if _sink is None:
        return
    if args:
        try:
            message = message % args
        except (TypeError, ValueError):
            message = "%s %r" % (message, args)
    _sink(PREFIX + redact(message), level)


def debug(message, *args):
    _write("DEBUG", message, args)


def info(message, *args):
    _write("INFO", message, args)


def warning(message, *args):
    _write("WARNING", message, args)


def error(message, *args):
    _write("ERROR", message, args)
