"""Context that crosses threads and tasks, and the baggage this book allows (plan section 6, context.py; DE-15).

The current span lives in a ContextVar. A thread started from a pool does not copy it, so a span made on that thread
has no parent and starts a trace of its own: a detached root (chapter 4's lost-context). carry() and start_thread() run
the work in a copy of the caller's context. asyncio tasks copy it already; new_task() makes that explicit.

Baggage is sent with every MCP call (W3C Baggage, chapter 4's what-goes-in-baggage), so only the release and the
prompt version may go in it, each in its own form. A user identifier is refused here, not left to review, under an
unknown key or under an allowed one.
"""
from __future__ import annotations

import asyncio
import contextvars
import re
import threading
from collections.abc import Callable, Coroutine, Mapping
from typing import Any

from opentelemetry import baggage
from opentelemetry import context as otel_context
from opentelemetry.context import Context

ALLOWED_BAGGAGE_KEYS = ("ambrewick.prompt.version", "ambrewick.release")
# The form each allowed key's value must have: a release is MAJOR.MINOR.PATCH, a prompt version is p then digits.
VALUE_FORMS = {"ambrewick.release": re.compile(r"\d+\.\d+\.\d+"), "ambrewick.prompt.version": re.compile(r"p\d+")}


def baggage_context(values: Mapping[str, str], context: Context | None = None) -> Context:
    """Return `context` (the current one by default) with `values` set as its baggage.

    Raises ValueError for any key outside ALLOWED_BAGGAGE_KEYS, and for a value that is not in its key's form, so a user
    identifier cannot reach the wire under an allowed key either.
    """
    for key, value in values.items():
        if key not in ALLOWED_BAGGAGE_KEYS:
            raise ValueError(f"baggage key {key!r} is not allowed")
        if not VALUE_FORMS[key].fullmatch(value):
            raise ValueError(f"baggage key {key!r} has value {value!r}, which is not in its form")
    ctx = otel_context.get_current() if context is None else context
    for key, value in sorted(values.items()):
        ctx = baggage.set_baggage(key, value, context=ctx)
    return ctx


def carry(function: Callable[..., Any]) -> Callable[..., Any]:
    """Return `function` wrapped to run in a copy of the context that is current now.

    Why a copy: each call gets its own snapshot, so the same wrapper can run on several threads at once. Pass the
    result to threading.Thread, or to loop.run_in_executor or executor.submit.
    """
    snapshot = contextvars.copy_context()

    def run(*args: Any, **kwargs: Any) -> Any:
        return snapshot.copy().run(function, *args, **kwargs)

    return run


def start_thread(target: Callable[..., Any], *args: Any) -> threading.Thread:
    """Start a daemon thread that runs `target(*args)` with the caller's current context, and return it."""
    thread = threading.Thread(target=carry(target), args=args, daemon=True)
    thread.start()
    return thread


def new_task(coroutine: Coroutine[Any, Any, Any], name: str | None = None) -> asyncio.Task:
    """Schedule `coroutine` as a task that carries the caller's current context.

    asyncio copies the context when a task is created, so the explicit copy only states what happens.
    """
    return asyncio.create_task(coroutine, name=name, context=contextvars.copy_context())
