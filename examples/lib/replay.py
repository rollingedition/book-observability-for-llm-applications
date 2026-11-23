"""Replay of recorded calls (plan section 4.6): answers a request with the recorded reply whose input key matches.

A recording keeps one file per call under calls/, named by the input key: the SHA-256 of the request's canonical JSON.
The live recorder and the replay both compute the key from the same request dict, so a recorded call is found exactly
when the assistant asks the same question again. A miss raises NotRecorded; nothing falls back to a live model.

The cached input tokens are derived, never read (PL-21): prompt tokens minus the tokens the runtime evaluated.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from lib.tools import ContactReply


class NotRecorded(KeyError):
    """The request has no recorded reply: the recording does not cover this call."""


def input_key(request: dict) -> str:
    """The key of a request: SHA-256 of its canonical JSON (sorted keys, no spaces, UTF-8 kept)."""
    text = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RecordedCall:
    """One recorded call. Times are nanoseconds on the recording machine; `chunk_ns` holds the time of each streamed
    chunk, counted from the start of the call. `tool_calls` is the runtime's own structured calls, empty when the
    runtime sent none (R-6); each is {"name": ..., "arguments": ...} as sent."""

    text: str
    finish_reason: str
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    ttfc_ns: int
    chunk_ns: tuple[int, ...]
    total_ns: int
    prefill_ns: int
    gen_ns: int
    tool_calls: tuple[dict, ...] = ()


class Replay:
    """The recorded calls of one recording folder, answered by input key. The recorded model was served behind an
    endpoint, so its calls are CLIENT spans that name the endpoint (`in_process` is False; PL-49)."""

    in_process = False

    def __init__(self, recording_dir: Path | str):
        self._calls = Path(recording_dir) / "calls"

    def reply(self, request: dict) -> RecordedCall:
        key = input_key(request)
        path = self._calls / f"{key}.json"
        if not path.is_file():
            raise NotRecorded(key)
        row = json.loads(path.read_text(encoding="utf-8"))
        if input_key(row["request"]) != key:
            raise ValueError(f"{path.name}: the stored request does not hash to its file name")
        evaluated = int(row["tokens_evaluated"])
        prompt = int(row["prompt_tokens"])
        return RecordedCall(
            text=row["text"],
            finish_reason=row["finish_reason"],
            prompt_tokens=prompt,
            cached_tokens=prompt - evaluated,
            completion_tokens=int(row["completion_tokens"]),
            ttfc_ns=int(row["ttfc_ns"]),
            chunk_ns=tuple(int(x) for x in row["chunk_ns"]),
            total_ns=int(row["total_ns"]),
            prefill_ns=int(row["prefill_ns"]),
            gen_ns=int(row["gen_ns"]),
            tool_calls=tuple(row.get("tool_calls", [])),
        )


class ReplayContacts:
    """The contacts server's recorded answers, for the messages route. The call is recorded where it ran (plan
    section 4.4): its arguments, its result and its duration."""

    def __init__(self, replay: Replay):
        self._replay = replay

    def find(self, user_id: str, name: str) -> ContactReply:
        request = {"kind": "mcp", "tool": "find_contact", "arguments": {"user_id": user_id, "name": name}}
        call = self._replay.reply(request)
        body = json.loads(call.text)
        return ContactReply(output=body["output"], is_error=bool(body["is_error"]), duration_ns=call.total_ns)
