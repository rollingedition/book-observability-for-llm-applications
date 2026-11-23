"""Record one schedule's model and tool calls for one release (plan sections 4.4 to 4.6).

    python recordings/record_schedule.py --recording dev-day --model-dir DIR
    python recordings/record_schedule.py --day 2026-12-07 --release r0 --name smoke-dev-2 --limit 2 --model-dir DIR
    python recordings/record_schedule.py --recording dev-day --model-dir DIR --resume
    python recordings/record_schedule.py --day 2026-12-07 --release r0 --name wire-check --only 15965 --model-dir DIR
    python recordings/record_schedule.py --day 2026-12-08-paired --release r1 --name check --dry-run   (no model)

The model files come from --model-dir (or the RE_MODEL_DIR variable), under the names pinned below. Each file's size
and SHA-256 are checked before anything runs. One llama process runs one call at a time. Every call is stored under
calls/ by the hash of its request (lib.replay.input_key), so a call already recorded is skipped with --resume. The
run writes into scratch and copies into examples/data/recorded/NAME/ only when it finishes (recordings/recorded.py).
--resume refuses when the run's settings differ from the ones the scratch folder was started with (the prompt hash,
the output and turn caps, the tools per route, the tool schemas): a changed cap changes every answer's key, and
the old and new rows would sit side by side. After a run, traces.jsonl and calls/ must hold the same keys.

The assistant runs as it does in replay: the same requests, the same parsing. The live model's replies are kept as
sent (<think> blocks and <tool_call> text included, PL-22, PL-23). The contacts server runs as a separate process and
its answers are recorded where it runs (plan section 4.4). Run it from the recording venv, in the background.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
import tomllib
from pathlib import Path

BOOK = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(BOOK / "examples"), str(BOOK / "recordings")]

import lib  # noqa: E402,F401  (lib first: threads and seeds are fixed before any library loads, plan 1.4)

import llama_cpp  # noqa: E402
from llama_cpp import Llama, LlamaRAMCache  # noqa: E402
from llama_cpp.llama_chat_format import Jinja2ChatFormatter  # noqa: E402
from opentelemetry import trace as otel_trace  # noqa: E402

from lib import assistant as assistant_module  # noqa: E402  (the caps are read when a run starts, see run_limits)
from lib.assistant import (RELEASES, ROUTE_MAX_TOKENS, Assistant, answer_system, answer_tools,  # noqa: E402
                           prompt_sha256, route_prompt)
from lib.bm25 import Retriever  # noqa: E402
from lib.case import CASE_DIR, load_articles, load_messages, load_tool_durations, scenario_to_route  # noqa: E402
from lib.clock import VirtualClock  # noqa: E402
from lib.replay import RecordedCall, Replay, ReplayContacts, input_key  # noqa: E402
from lib.tools import ROUTE_TOOLS, TOOL_NAMES, ContactReply, LiveContacts, Tools, route_tools  # noqa: E402
from recorded import Recording  # noqa: E402

N_CTX = 8192
THREADS = 4
SEED = 0
TEMPERATURE = 0
# The prompt cache keeps one saved state per cached prompt. A state is about 140 KiB per token of context (the 4B
# model's KV cache), so a full 8192-token context is about 1.2 GiB, and each save makes two more transient copies of it.
# 1 GiB holds the shared system prompts and the recent turns and keeps the resident cache bounded.
CACHE_BYTES = 1 << 30
NO_THINK_MODELS = {"qwen3-0.6b"}  # PL-23: the hybrid-thinking model's prompts carry /no_think
CONTACT_TIMEOUT_S = 10.0

# The pinned model files (plan section 4.2). The 4B's full SHA-256 is not pinned, only its prefix and suffix (B-24).
MODELS = {
    "qwen3-4b-instruct-2507": {
        "repo": "unsloth/Qwen3-4B-Instruct-2507-GGUF",
        "revision": "a06e946bb6b655725eafa393f4a9745d460374c9",
        "file": "Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
        "size": 2497281120,
        "sha256": None,
        "sha256_prefix": "3605803b",
        "sha256_suffix": "e597",
    },
    "qwen3-0.6b": {
        "repo": "Qwen/Qwen3-0.6B-GGUF",
        "revision": "23749fefcc72300e3a2ad315e1317431b06b590a",
        "file": "Qwen3-0.6B-Q8_0.gguf",
        "size": 639446688,
        "sha256": "9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031",
    },
}

# The named recordings of plan section 4.6: the traffic day or paired subset, and the release.
RECORDINGS = {
    "dev-day": ("2026-12-07", "r0"),
    "baseline-day-1": ("2026-12-08", "r0"),
    "release-prompt-v2": ("2026-12-08-paired", "r1"),
    "release-top-8": ("2026-12-08-paired", "r3"),
    "release-cache-off": ("2026-12-08-paired", "r4"),
    "release-model-swap": ("2026-12-08", "r2"),
    "incident-day": ("2026-12-09", "r0"),
    "baseline-day-2": ("2026-12-10", "r0"),
}

# A paired schedule is a subset of one baseline day's messages, with that day's arrival times in its file. Its
# messages belong to the day, so the clock, the answer prompt's date and the manifest's date use the day; the
# schedule's own name is kept in the manifest beside it. A day's file maps to itself.
PAIRED_SCHEDULES = {"2026-12-08-paired": "2026-12-08"}


def schedule_date(schedule: str) -> str:
    """The calendar day a traffic schedule's messages belong to. Raises ValueError when that is not a date."""
    date = PAIRED_SCHEDULES.get(schedule, schedule)
    dt.date.fromisoformat(date)
    return date


class PromptCache(LlamaRAMCache):
    """LlamaRAMCache whose size counts each saved state's logits copy as well as its KV bytes. The library counts only
    the KV bytes, but save_state also copies the logits buffer (up to 512 rows of vocabulary floats, about 300 MiB),
    so a byte cap on the library's count let the entries grow past it and the process grew with every call."""

    @property
    def cache_size(self) -> int:
        return sum(state.llama_state_size + state.scores.nbytes for state in self.cache_state.values())


def verify_model(path: Path, pin: dict) -> str:
    """Check size and SHA-256 of a model file against its pin, and return its SHA-256 for the manifest."""
    if path.stat().st_size != pin["size"]:
        raise SystemExit(f"{path.name}: size {path.stat().st_size} is not the pinned {pin['size']}")
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 24), b""):
            digest.update(block)
    value = digest.hexdigest()
    if pin["sha256"] is not None and value != pin["sha256"]:
        raise SystemExit(f"{path.name}: SHA-256 does not match the pin")
    if pin["sha256"] is None and not (value.startswith(pin["sha256_prefix"]) and value.endswith(pin["sha256_suffix"])):
        raise SystemExit(f"{path.name}: SHA-256 does not match the pinned prefix and suffix")
    return value


def machine() -> dict:
    """The CPU model and vCPU count of the recording machine (the manifest's `machine`)."""
    model = "unknown"
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return {"cpu_model": model, "vcpus": os.cpu_count(), "threads": THREADS}


def run_limits() -> dict:
    """The limits every request of a run depends on: the two output caps, the model-turn and tool-call caps, the tools
    each route offers, and a SHA-256 of the tool schemas as the answer requests send them. Read when the run starts,
    so a change to any of them is in the manifest and --resume refuses it (the input keys change with the caps, so a
    resumed run would otherwise keep the old rows and add new ones beside them)."""
    tools = Tools(load_tool_durations(), None)
    schemas = json.dumps(answer_tools(TOOL_NAMES, tools), sort_keys=True, ensure_ascii=False)
    return {"route_max_tokens": assistant_module.ROUTE_MAX_TOKENS,
            "answer_max_tokens": assistant_module.ANSWER_MAX_TOKENS,
            "max_model_turns": assistant_module.MAX_MODEL_TURNS,
            "max_tool_calls": assistant_module.MAX_TOOL_CALLS,
            "route_tools": {route: list(names) for route, names in sorted(ROUTE_TOOLS.items())},
            "tool_schema_sha256": hashlib.sha256(schemas.encode("utf-8")).hexdigest()}


def check_settings(folder: Path, settings: dict, resume: bool) -> None:
    """Refuse a --resume whose settings differ from the run that wrote the scratch folder; otherwise record this run's
    settings beside the folder (not in it: Recording copies the whole folder into the book). A fresh run replaces the
    record. A resume with calls but no record is refused too, since nothing can check them."""
    path = folder.with_name(folder.name + ".settings.json")
    if resume and path.is_file():
        stored = json.loads(path.read_text(encoding="utf-8"))
        differ = sorted(k for k in set(stored) | set(settings) if stored.get(k) != settings.get(k))
        if differ:
            raise SystemExit("--resume refused: these settings differ from the run that wrote the scratch folder: "
                             + ", ".join(differ) + ". Run without --resume to start the recording again.")
    elif resume and any((folder / "calls").glob("*.json")):
        raise SystemExit("--resume refused: the scratch folder has calls but no settings record, so they cannot be "
                         "checked. Run without --resume to start the recording again.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def check_keys(folder: Path) -> int:
    """After the run, traces.jsonl and calls/ must hold the same input keys: one trace line per call file, none
    missing, no extra file in calls/, and each file's name is the hash of the request it holds."""
    calls = folder / "calls"
    stray = sorted(p.name for p in calls.iterdir() if p.suffix != ".json")
    if stray:
        raise SystemExit(f"calls/ holds files that are not calls: {', '.join(stray)}")
    in_calls = set()
    for path in calls.glob("*.json"):
        if input_key(json.loads(path.read_text(encoding="utf-8"))["request"]) != path.stem:
            raise SystemExit(f"{path.name}: its request hashes to another key")
        in_calls.add(path.stem)
    lines = (folder / "traces.jsonl").read_text(encoding="utf-8").splitlines()
    keys = [json.loads(line)["input_key"] for line in lines]
    if len(keys) != len(set(keys)) or set(keys) != in_calls:
        raise SystemExit(f"traces.jsonl and calls/ disagree: {len(in_calls - set(keys))} call(s) not in the traces, "
                         f"{len(set(keys) - in_calls)} trace key(s) with no call, {len(keys) - len(set(keys))} repeated")
    return len(keys)


class Runner:
    """The live model: one llama process, one call at a time. It is the assistant's backend (reply(request)), and
    it keeps each call under calls/ by its input key. A key already recorded is answered from the recording."""

    def __init__(self, llm: Llama, release, folder: Path, origin_ns: int):
        self.llm = llm
        self.release = release
        self.folder = folder
        self.calls = folder / "calls"
        self.calls.mkdir(parents=True, exist_ok=True)
        self.replay = Replay(folder)
        self.origin = origin_ns
        self.message_id = ""
        template = llm.metadata["tokenizer.chat_template"]
        bos = llm.detokenize([llm.token_bos()]).decode("utf-8", "ignore") if llm.token_bos() >= 0 else ""
        eos = llm.detokenize([llm.token_eos()]).decode("utf-8", "ignore")
        self.formatter = Jinja2ChatFormatter(template=template, eos_token=eos, bos_token=bos,
                                             add_generation_prompt=True)
        self.next_seq = 1 + max((json.loads(p.read_text(encoding="utf-8"))["seq"] for p in self.calls.glob("*.json")),
                                default=0)
        if release.cache_prompt:
            llm.set_cache(PromptCache(capacity_bytes=CACHE_BYTES))

    def prompt_tokens(self, messages: list[dict], tools: list[dict] | None = None) -> int:
        rendered = self.formatter(messages=messages, tools=tools or None)
        return len(self.llm.tokenize(rendered.prompt.encode("utf-8"), add_bos=not rendered.added_special, special=True))

    def text_tokens(self, text: str) -> int:
        return len(self.llm.tokenize(text.encode("utf-8"), add_bos=False, special=True))

    def _perf(self) -> tuple[int, float, int, float]:
        counters = llama_cpp.llama_perf_context(self.llm._ctx.ctx)
        return counters.n_p_eval, counters.t_p_eval_ms, counters.n_eval, counters.t_eval_ms

    def _store(self, key: str, row: dict) -> None:
        row["seq"] = self.next_seq
        self.next_seq += 1
        tmp = self.calls / f"{key}.tmp"
        tmp.write_text(json.dumps(row, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.calls / f"{key}.json")

    def _messages(self, request: dict) -> list[dict]:
        messages = [dict(m) for m in request["messages"]]
        if self.release.model in NO_THINK_MODELS:
            messages[0]["content"] += " /no_think"
        return messages

    def reply(self, request: dict):
        """Answer one model request: from the recording when its key is there, else from the model."""
        key = input_key(request)
        if (self.calls / f"{key}.json").is_file():
            return self.replay.reply(request)
        messages = self._messages(request)
        tools = request.get("tools") or None  # PL-46: the route's tools, rendered by the chat template
        if not self.release.cache_prompt:
            self.llm.reset()  # PL-21: the cache-off release starts from a fresh state for every call
        prompt = self.prompt_tokens(messages, tools)
        before = self._perf()
        started = time.perf_counter_ns()
        marks, parts, finish = [], [], None
        structured: dict[int, dict] = {}  # the runtime's tool_calls, by index; none in this build (R-6)
        for chunk in self.llm.create_chat_completion(messages=messages, tools=tools, temperature=TEMPERATURE,
                                                     seed=SEED, max_tokens=request["max_tokens"], stream=True):
            marks.append(time.perf_counter_ns() - started)
            choice = chunk["choices"][0]
            parts.append(choice["delta"].get("content") or "")
            for part in choice["delta"].get("tool_calls") or []:
                slot = structured.setdefault(part.get("index", 0), {"name": "", "arguments": ""})
                slot["name"] += (part.get("function") or {}).get("name") or ""
                slot["arguments"] += (part.get("function") or {}).get("arguments") or ""
            finish = choice.get("finish_reason") or finish
        total = time.perf_counter_ns() - started
        after = self._perf()
        text = "".join(parts)
        row = {"kind": "model", "request": request, "message_id": self.message_id,
               "step": "route" if request["max_tokens"] == ROUTE_MAX_TOKENS else "answer",
               "text": text, "finish_reason": finish or "stop", "prompt_tokens": prompt,
               "tool_calls": [structured[i] for i in sorted(structured)],
               "tokens_evaluated": after[0] - before[0], "completion_tokens": self.text_tokens(text),
               "ttfc_ns": marks[0] if marks else total, "chunk_ns": marks, "total_ns": total,
               "prefill_ns": round((after[1] - before[1]) * 1_000_000),
               "gen_ns": round((after[3] - before[3]) * 1_000_000),
               "offset_ns": started - self.origin}
        self._store(key, row)
        return self.replay.reply(request)

    def warm_up(self, day: str) -> dict:
        """One routing and one answer call that are not recorded (R-5, B-49). The runtime's own usage must agree
        with the token counts this recorder takes, so the counts are checked once per process."""
        tools = Tools(load_tool_durations(), None)
        offered = answer_tools(route_tools("calendar"), tools)  # PL-46: the tools travel in the request
        call = {"role": "assistant", "content": '<tool_call>\n{"name": "list_events", "arguments": {}}\n</tool_call>'}
        result = {"role": "tool", "name": "list_events", "content": '{"events": []}'}
        samples = [
            ([{"role": "system", "content": route_prompt(self.release.prompt_version)},
              {"role": "user", "content": "warm-up"}], None),
            ([{"role": "system", "content": answer_system(day, ())},
              {"role": "user", "content": "warm-up"}], None),
            ([{"role": "system", "content": answer_system(day, ())},
              {"role": "user", "content": "warm-up"}], offered),
            ([{"role": "system", "content": answer_system(day, ())},
              {"role": "user", "content": "warm-up"}, call, result], offered),
        ]
        names = ["route", "answer", "answer_with_tools", "answer_tool_round_trip"]
        checks = []
        for name, (messages, tool_list) in zip(names, samples):
            if self.release.model in NO_THINK_MODELS:
                messages[0]["content"] += " /no_think"
            self.llm.reset()
            usage_call = self.llm.create_chat_completion(messages=messages, tools=tool_list or None,
                                                         temperature=TEMPERATURE, seed=SEED, max_tokens=8,
                                                         stream=False)
            reported = usage_call["usage"]
            text = usage_call["choices"][0]["message"]["content"] or ""
            ours = (self.prompt_tokens(messages, tool_list), self.text_tokens(text))
            runtime = (reported["prompt_tokens"], reported["completion_tokens"])
            checks.append({"sample": name, "prompt_tokens": ours[0], "completion_tokens": ours[1],
                           "runtime_prompt_tokens": runtime[0], "runtime_completion_tokens": runtime[1],
                           "matched": ours == runtime})
            if ours != runtime:
                raise SystemExit(f"token counts disagree with the runtime's usage: {ours} against {runtime}")
        self.llm.reset()
        # Written into the manifest by the caller: the run stops above on a mismatch, so a finished run's check
        # has matched set on every sample.
        return {"matched": all(c["matched"] for c in checks), "samples": checks}


class StoredContacts:
    """The contacts server through lib.tools.LiveContacts, with each answer stored in the recording's calls/ (plan
    section 4.4). A call already stored is answered from the store, so --resume does not repeat it."""

    def __init__(self, runner: Runner, live: LiveContacts):
        self._runner = runner
        self._live = live

    def find(self, user_id: str, name: str) -> ContactReply:
        request = {"kind": "mcp", "tool": "find_contact", "arguments": {"user_id": user_id, "name": name}}
        key = input_key(request)
        if (self._runner.calls / f"{key}.json").is_file():
            return ReplayContacts(self._runner.replay).find(user_id, name)
        started = time.perf_counter_ns()
        reply = self._live.find(user_id, name)
        body = {"output": reply.output, "is_error": reply.is_error}
        row = {"kind": "mcp", "request": request, "message_id": self._runner.message_id, "step": "tool",
               "text": json.dumps(body, sort_keys=True), "finish_reason": "", "prompt_tokens": 0,
               "tokens_evaluated": 0, "completion_tokens": 0, "ttfc_ns": reply.duration_ns,
               "chunk_ns": [reply.duration_ns], "total_ns": reply.duration_ns, "prefill_ns": 0, "gen_ns": 0,
               "offset_ns": started - self._runner.origin}
        self._runner._store(key, row)
        return reply

    def close(self) -> None:
        self._live.close()


def incidents_on(day: str) -> list[str]:
    """The injected windows that fall on the day (incidents.toml), for the manifest. Their injection is in
    lib/inject.py and the contacts server, not in this script."""
    if not day.startswith("2026-12-09"):
        return []
    table = tomllib.loads((CASE_DIR / "incidents.toml").read_text(encoding="utf-8"))
    return sorted(row["id"] for row in table.get("incident", []))


def write_traces(folder: Path) -> int:
    """traces.jsonl: one line per call row, in the order the calls were made (their sequence numbers)."""
    rows = sorted((json.loads(p.read_text(encoding="utf-8")) for p in (folder / "calls").glob("*.json")),
                  key=lambda r: r["seq"])
    lines = []
    for row in rows:
        line = {"seq": row["seq"], "kind": row["kind"], "step": row["step"], "message_id": row["message_id"],
                "input_key": input_key(row["request"]), "offset_ns": row["offset_ns"],
                "total_ns": row["total_ns"], "ttfc_ns": row["ttfc_ns"], "prefill_ns": row["prefill_ns"],
                "gen_ns": row["gen_ns"], "prompt_tokens": row["prompt_tokens"],
                "tokens_evaluated": row["tokens_evaluated"],
                "cached_tokens_derived": row["prompt_tokens"] - row["tokens_evaluated"],
                "completion_tokens": row["completion_tokens"], "finish_reason": row["finish_reason"]}
        if row["kind"] == "mcp":
            line["tool"] = row["request"]["tool"]
        lines.append(json.dumps(line, sort_keys=True))
    (folder / "traces.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


class DryRunModel:
    """Stands in for the live model in a dry run. A route call gets the route the scenario's table gives (the one a
    model that routes correctly returns); an answer call gets a fixed reply that ends the turn. It keeps the input key
    of each step's first request, so the keys are those a run would send. It loads no model and writes nothing."""

    def __init__(self):
        self.route = ""
        self.keys: dict[str, str] = {}

    def reply(self, request: dict) -> RecordedCall:
        step = "route" if request["max_tokens"] == ROUTE_MAX_TOKENS else "answer"
        self.keys.setdefault(step, input_key(request))
        text = json.dumps({"route": self.route}) if step == "route" else "dry run"
        return RecordedCall(text=text, finish_reason="stop", prompt_tokens=0, cached_tokens=0, completion_tokens=0,
                            ttfc_ns=0, chunk_ns=(0,), total_ns=0, prefill_ns=0, gen_ns=0)


def dry_run(schedule: str, date: str, release, messages) -> None:
    """Print each message's route and answer input keys under the release (16 hex digits of each), with no model read
    and nothing written. Two runs send the same requests exactly when their keys match."""
    model = DryRunModel()
    retriever = Retriever(load_articles(), release.top_k)
    tools = Tools(load_tool_durations(), None)
    tracer = otel_trace.get_tracer("recorder")
    print(f"{schedule}: {len(messages)} messages on {date}, release {release.name} (prompt {release.prompt_version}, "
          f"top_k {release.top_k}, cache {'on' if release.cache_prompt else 'off'})")
    for message in messages:
        model.route, model.keys = scenario_to_route(message.scenario), {}
        clock = VirtualClock(date, message.arrival_ns)
        Assistant(model, retriever, tools, clock, tracer, release).handle(message)
        arrival = dt.timedelta(microseconds=message.arrival_ns // 1000)
        keys = {step: model.keys.get(step, "")[:16] or "none" for step in ("route", "answer")}  # fallback: no answer
        print(f"{message.message_id} {date} {arrival} route {keys['route']} answer {keys['answer']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--recording", choices=sorted(RECORDINGS), help="a named recording of plan section 4.6")
    parser.add_argument("--day", help="a traffic file's name, for a scratch run (with --release and --name)")
    parser.add_argument("--release", choices=sorted(RELEASES))
    parser.add_argument("--name", help="the recording's name (lowercase words joined by hyphens)")
    parser.add_argument("--model-dir", default=os.environ.get("RE_MODEL_DIR"))
    parser.add_argument("--resume", action="store_true", help="keep the scratch folder and skip recorded calls")
    parser.add_argument("--limit", type=int, default=0, help="the first N messages only (smoke runs)")
    parser.add_argument("--only", help="message IDs only, comma-separated (scratch runs, e.g. 15965 on the dev day)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print each message's input keys under the release, with no model read and nothing written")
    args = parser.parse_args()
    if args.recording:
        day, release_name = RECORDINGS[args.recording]
        name = args.name or args.recording
    else:
        if not (args.day and args.release and args.name):
            parser.error("give --recording, or --day, --release and --name")
        day, release_name, name = args.day, args.release, args.name
    date = schedule_date(day)
    release = RELEASES[release_name]
    messages = load_messages(day)  # the schedule's own file: its arrival times are the day's
    if args.only:
        wanted = [x.strip() for x in args.only.split(",") if x.strip()]
        messages = [m for m in messages if m.message_id in wanted]
        missing = sorted(set(wanted) - {m.message_id for m in messages})
        if missing:
            parser.error(f"no message {', '.join(missing)} on {day}")
    if args.limit:
        messages = messages[: args.limit]
    if args.dry_run:
        dry_run(day, date, release, messages)
        return
    if not args.model_dir:
        parser.error("give --model-dir, or set RE_MODEL_DIR")
    pin = MODELS[release.model]
    model_path = Path(args.model_dir) / pin["file"]
    model_sha = verify_model(model_path, pin)
    manifest = {"schedule": day, "day": date, "release": release.name, "service_version": release.service_version,
                "prompt_version": release.prompt_version, "top_k": release.top_k,
                "cache_prompt": release.cache_prompt, "messages": len(messages),
                "model_file": {"name": pin["file"], "size": pin["size"], "sha256": model_sha},
                "runtime": f"llama-cpp-python {llama_cpp.__version__}", "llama_cpp_source": "unknown",
                "cached_input_tokens_derived": True,
                "token_counts": "prompt tokens from the re-rendered chat template; completion tokens re-tokenized",
                "machine": machine(), "incidents": incidents_on(date), "sampling": {
                    "temperature": TEMPERATURE, "seed": SEED, "threads": THREADS, "n_ctx": N_CTX}}
    limits = run_limits()
    manifest.update(limits)
    settings = {"day": date, "release": release.name, "model_file": pin["file"], "model_sha256": model_sha,
                "prompt_sha256": prompt_sha256(release), **limits}
    recording = Recording(name, model=pin["repo"], model_version=pin["revision"],
                          prompt_sha256=prompt_sha256(release), fresh=not args.resume, script=__file__,
                          **manifest)
    check_settings(recording.folder, settings, args.resume)  # refuses a resume that would mix settings
    with recording as out:
        llm = Llama(model_path=str(model_path), n_ctx=N_CTX, n_threads=THREADS, seed=SEED, verbose=False)
        origin = time.perf_counter_ns()
        runner = Runner(llm, release, out, origin)
        # Raises if the runtime's usage disagrees with the counts; the result goes into the manifest on finish.
        recording.manifest["warm_up_token_check"] = runner.warm_up(date)
        clock = VirtualClock(date)  # one clock for the run: each message starts it at its arrival
        midnight_ns = clock.now_ns
        live = LiveContacts(clock, out / "contacts-spans.jsonl", timer_ns=time.perf_counter_ns)
        contacts = StoredContacts(runner, live)
        tools = Tools(load_tool_durations(), contacts)
        retriever = Retriever(load_articles(), release.top_k)
        tracer = otel_trace.get_tracer("recorder")  # spans are made by replay; the recorder keeps calls only
        try:
            for index, message in enumerate(messages, 1):
                runner.message_id = message.message_id
                clock.now_ns = midnight_ns + message.arrival_ns
                outcome = Assistant(runner, retriever, tools, clock, tracer, release).handle(message)
                if index % 20 == 0 or index == len(messages):
                    print(f"{index}/{len(messages)} messages, last route {outcome.route}", flush=True)
        finally:
            contacts.close()
        count = write_traces(out)
        check_keys(out)  # raises, so the run copies nothing into the book, if the two disagree
        print(f"{count} calls in traces.jsonl, the same keys as calls/", flush=True)


if __name__ == "__main__":
    main()
