from __future__ import annotations

from contextvars import ContextVar, Token

_correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def set_correlation_id(
    value: str | None, token: Token[str | None] | None = None
) -> Token[str | None] | None:
    if token is not None:
        _correlation_id_var.reset(token)
        return None
    return _correlation_id_var.set(value)


def get_correlation_id() -> str | None:
    return _correlation_id_var.get()
