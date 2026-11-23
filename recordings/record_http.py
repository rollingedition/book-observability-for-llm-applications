"""Record the HTTP layer for chapter 3: the first five dev-day route calls, sent through the openai client to
llama-cpp-python's server (plan sections 4.4 and 4.6, the `http-replay` recording).

    python recordings/record_http.py --model-dir DIR

The server is the `server` extra's, started on loopback with port 0; its port is read from its log line and is
never printed. The calls are non-streamed, because only non-streamed replies carry usage (R-8). Each request body is
stored under calls/ by the hash of the body as the client sent it (lib.replay.input_key), with the reply as the
server returned it, so the book's HTTP replay answers the same body with the same bytes. The messages are the first
five of dev-day, and their route requests are the assistant's own (lib.assistant), sent with `model` added as the
client sends it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

BOOK = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(BOOK / "examples"), str(BOOK / "recordings")]

import lib  # noqa: E402,F401  (lib first, plan 1.4 rule 10)

import httpx  # noqa: E402
import openai  # noqa: E402

from lib.assistant import RELEASES, ROUTE_MAX_TOKENS, prompt_sha256, route_prompt  # noqa: E402
from lib.case import load_messages  # noqa: E402
from lib.replay import input_key  # noqa: E402
from recorded import Recording  # noqa: E402
from record_schedule import MODELS, machine, verify_model  # noqa: E402

DAY = "2026-12-07"
COUNT = 5  # the first five dev-day messages
PORT_LINE = re.compile(r"Uvicorn running on http://127\.0\.0\.1:(\d+)")


def start_server(model: Path, log: Path) -> tuple[subprocess.Popen, int]:
    """Start the server on port 0 and read the port from its log. The port is returned, never printed."""
    handle = log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "llama_cpp.server", "--model", str(model), "--host", "127.0.0.1", "--port", "0",
         "--n_ctx", "8192", "--n_threads", "4"],
        stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        found = PORT_LINE.search(log.read_text(encoding="utf-8", errors="replace"))
        if found:
            return process, int(found.group(1))
        if process.poll() is not None:
            raise SystemExit(f"the server stopped; its log is at {log}")
        time.sleep(0.5)
    process.kill()
    raise SystemExit("the server did not report its port in five minutes")


def stop_server(process: subprocess.Popen) -> None:
    """Stop the server's whole process group (K-2, K-5)."""
    os.killpg(process.pid, 15)
    process.wait(timeout=60)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-dir", default=os.environ.get("RE_MODEL_DIR"))
    parser.add_argument("--name", default="http-replay")
    parser.add_argument("--count", type=int, default=COUNT, help="the first N messages (smoke runs)")
    args = parser.parse_args()
    if not args.model_dir:
        parser.error("give --model-dir, or set RE_MODEL_DIR")
    release = RELEASES["r0"]
    pin = MODELS[release.model]
    model_path = Path(args.model_dir) / pin["file"]
    model_sha = verify_model(model_path, pin)
    messages = load_messages(DAY)[: args.count]
    manifest = {"day": DAY, "release": release.name, "messages": len(messages), "server": "llama-cpp-python server extra",
                "model_file": {"name": pin["file"], "size": pin["size"], "sha256": model_sha},
                "runtime": "llama-cpp-python server extra", "cached_input_tokens_derived": False,
                "machine": machine(), "sampling": {"temperature": 0, "seed": 0, "threads": 4, "stream": False}}
    with Recording(args.name, model=pin["repo"], model_version=pin["revision"],
                   prompt_sha256=prompt_sha256(release), script=__file__, **manifest) as out:
        calls = out / "calls"
        calls.mkdir(parents=True, exist_ok=True)
        log = out / "server.log"
        process, port = start_server(model_path, log)
        try:
            sent: list[dict] = []
            received: list[dict] = []

            def on_request(request: httpx.Request) -> None:
                sent.append(json.loads(request.content.decode("utf-8")))

            def on_response(response: httpx.Response) -> None:
                response.read()
                received.append({"status": response.status_code, "body": response.json()})

            http = httpx.Client(event_hooks={"request": [on_request], "response": [on_response]}, timeout=600)
            client = openai.OpenAI(base_url=f"http://127.0.0.1:{port}/v1", api_key="local", max_retries=0,
                                   http_client=http)
            for index, message in enumerate(messages, 1):
                body_messages = [{"role": "system", "content": route_prompt(release.prompt_version)},
                                 {"role": "user", "content": message.text}]
                started = time.perf_counter_ns()
                client.chat.completions.create(model=release.model, messages=body_messages,
                                               max_tokens=ROUTE_MAX_TOKENS, temperature=0, seed=0)
                elapsed = time.perf_counter_ns() - started
                body, reply = sent[-1], received[-1]
                row = {"request": body, "response": reply["body"], "status": reply["status"],
                       "message_id": message.message_id, "elapsed_ns": elapsed}
                (calls / f"{input_key(body)}.json").write_text(json.dumps(row, indent=1, sort_keys=True) + "\n",
                                                               encoding="utf-8")
                print(f"{index}/{len(messages)} recorded", flush=True)
            http.close()
        finally:
            stop_server(process)
        log.unlink(missing_ok=True)  # the port is in the log: the copy in the book keeps no port


if __name__ == "__main__":
    main()
