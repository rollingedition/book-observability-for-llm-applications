"""The `openai` client, answered from the recording with no socket (plan section 6, chapter 3).

The client is the real one, unchanged. Its HTTP transport is an httpx.MockTransport that answers each request from
the recording `recordings/record_http.py` made: the stored reply, with the status it was sent with. A request is
found by the same key the recorder stored it under, the SHA-256 of the parsed JSON body the client sent
(lib.replay.input_key), so a body the recording lacks raises NotRecorded, as the assistant's replay does.
Only non-streamed exchanges are recorded, because only they carry `usage` (R-8).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import openai

from lib.assistant import MODEL_SERVER_ADDRESS, MODEL_SERVER_PORT, RELEASES, ROUTE_MAX_TOKENS, route_prompt
from lib.replay import NotRecorded, input_key

# httpx logs every request at INFO. A replay has no request to report, and the line would be printed on every call.
logging.getLogger("httpx").setLevel(logging.WARNING)
RECORDING = Path(__file__).resolve().parents[1] / "data" / "recorded" / "http-replay"
# The book's configured model endpoint, so the spans name the server the assistant calls. The mock transport answers
# every request, so no socket opens even though the URL names the model server.
BASE_URL = f"http://{MODEL_SERVER_ADDRESS}:{MODEL_SERVER_PORT}/v1"


def route_body(message) -> dict:
    """The body of one message's route call, as recorder/record_http.py sent it: the route prompt and the message, with
    the release's model and the route call's limits. Its key is what the recording stores the reply under."""
    release = RELEASES["r0"]
    return dict(model=release.model, max_tokens=ROUTE_MAX_TOKENS, temperature=0, seed=0,
                messages=[{"role": "system", "content": route_prompt(release.prompt_version)},
                          {"role": "user", "content": message.text}])


def transport(recording_dir=RECORDING) -> httpx.MockTransport:
    """Return a transport that answers each request with its recorded reply.

    Why the key is computed from the parsed body, not the raw bytes: the recorder hashed the parsed body too, so
    the client's own JSON layout (spacing, key order) cannot change which reply a request gets.
    """
    calls = Path(recording_dir) / "calls"

    def answer(request: httpx.Request) -> httpx.Response:
        key = input_key(json.loads(request.content.decode("utf-8")))
        path = calls / f"{key}.json"
        if not path.is_file():
            raise NotRecorded(key)
        row = json.loads(path.read_text(encoding="utf-8"))
        if input_key(row["request"]) != key:
            raise ValueError(f"{path.name}: the stored request does not hash to its file name")
        # The stored reply is the server's JSON body, returned as the server sent it.
        return httpx.Response(row["status"], json=row["response"])

    return httpx.MockTransport(answer)


def client(recording_dir=RECORDING) -> openai.OpenAI:
    """Return an openai client whose requests are answered by transport(recording_dir).

    Why max_retries is 0: a request the recording lacks should fail at once, not be retried into the same miss.
    The api_key is a placeholder; the transport never checks it.
    """
    return openai.OpenAI(base_url=BASE_URL, api_key="local", max_retries=0,
                         http_client=httpx.Client(transport=transport(recording_dir)))
