"""Safe error types whose messages never contain secrets or source paths."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class HistoryError(Exception):
    code: str
    safe_message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.safe_message}"

    def payload(self) -> dict:
        return {
            "ok": False,
            "error": {"code": self.code, "message": self.safe_message},
        }


def fail(code: str, message: str) -> HistoryError:
    return HistoryError(code=code, safe_message=message)

