"""Classify failures without exposing request bodies, headers, or credentials."""

import json
import random
import re
import time


class UserError(RuntimeError):
    """An actionable local error whose message is safe for terminal output."""


class InputError(ValueError):
    """A validation error with a message controlled by this application."""


class APIError(RuntimeError):
    def __init__(self, reason: str, status: int = 0):
        self.reason, self.status = reason, status
        super().__init__(f"API failure: {reason} (HTTP {status})")


class QuotaError(APIError):
    pass


class AmbiguousError(APIError):
    pass


def classify(exc: Exception) -> APIError:
    if isinstance(exc, APIError):
        return exc
    status = int(getattr(getattr(exc, "resp", None), "status", 0) or 0)
    response = getattr(exc, "response", None)
    if response is not None:
        status = response.status_code
    reason = type(exc).__name__
    if reason == "YTMusicServerError":
        match = re.match(r"Server returned HTTP (\d{3}):", str(exc))
        if match:
            status = int(match.group(1))
    try:
        body = json.loads(getattr(exc, "content", b"{}"))
        reason = body["error"]["errors"][0]["reason"]
    except (ValueError, KeyError, IndexError, TypeError):
        pass
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", reason):
        reason = "unrecognizedReason"
    if reason in {"quotaExceeded", "dailyLimitExceeded", "dailyLimitExceededUnreg"}:
        return QuotaError(reason, status)
    return APIError(reason, status)


class Caller:
    def __init__(self, interval: float = 0.2):
        self.interval = interval
        self.units = 0

    def call(self, fn, *, write: bool = False, units: int = 0):
        for attempt in range(5):
            time.sleep(self.interval)
            self.units += units
            try:
                return fn()
            except Exception as exc:
                error = classify(exc)
                if isinstance(error, QuotaError):
                    raise error from None
                transient = (
                    error.status in {429, 500, 502, 503, 504}
                    or (error.status == 0 and isinstance(exc, (OSError, TimeoutError)))
                    or error.reason in {"rateLimitExceeded", "userRateLimitExceeded"}
                )
                # Never blindly retry a write whose response could have been lost.
                if transient and write:
                    raise AmbiguousError(error.reason, error.status) from None
                if not transient or attempt == 4:
                    raise error from None
                time.sleep(min(2**attempt + random.random(), 30))
