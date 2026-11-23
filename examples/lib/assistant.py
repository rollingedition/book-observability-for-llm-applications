"""The Ambrewick assistant: five steps for one user message (plan sections 2.4 and 3).

    1. route     one model call; the reply is JSON {"route": ...}
    2. retrieve  BM25 over the knowledge base, on the three retrieving routes only
    3. tools     at most two tool calls. The route's tools travel in the request's `tools` (OpenAI's function format),
                 so the GGUF's own chat template renders them. A call is read from the runtime's tool_calls when it
                 sends one, else from <tool_call> text (PL-22). Results go back as role "tool" messages (PL-46).
    4. answer    one model call that writes the reply; up to four model turns per message in all
    5. fallback  the fixed reply from policies.md, for the fallback route; no model call

The model is a backend with one method, reply(request) -> RecordedCall: the replay for the examples, the live llama
backend for the recording scripts. Each model call advances the virtual clock by its recorded duration, and each step
becomes a span with explicit times (rule 1). A reply that fails to parse is a malformed tool call; a reply's <think>
block is dropped (PL-22, PL-23). Wrong routes and malformed calls are recorded, never corrected.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Protocol

from opentelemetry import trace as otel_trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from lib import inject
from lib.case import CASE_DIR, Article, Message, load_articles, load_policies, load_routes, scenario_to_route
from lib.metrics import OperationMetrics, TokenMetrics
from lib.replay import Replay, RecordedCall
from lib.tools import ATTEMPTS, MCP_TOOLS, RETRY_DELAY_NS, AttemptFailed, Tools, ToolResult, route_tools

NS_PER_S = 1_000_000_000

MAX_MODEL_TURNS = 4
MAX_TOOL_CALLS = 2
ROUTE_MAX_TOKENS = 32
ANSWER_MAX_TOKENS = 160
# The client's waits after a 429, on the virtual clock (0.5 s, 1 s, 2 s), then it gives up: four attempts in all.
RETRY_BACKOFF_NS = (500_000_000, 1_000_000_000, 2_000_000_000)
# The model's endpoint, the address and port every inference span names (PL-19, PL-48). llama-cpp-python 0.3.37's
# server defaults, from llama_cpp/server/settings.py in the recording venv: host "localhost" (line 210) and port 8000
# (line 211). The recorder's server binds a random port, so this constant, not the server's, is what a span carries.
MODEL_SERVER_ADDRESS = "localhost"
MODEL_SERVER_PORT = 8000


class ModelUnavailable(Exception):
    """The model still answered 429 after the last retry: the message ends without a reply."""
AGENT = "ambrewick-assistant"

ROUTE_NAMES = tuple(sorted(r.name for r in load_routes()))
FALLBACK = "fallback"
# The prompt each model step sends, by its step name: the route prompt and the answer template (PL-62). Each inference
# span names its prompt here; `gen_ai.prompt.version` is then conditionally required with it.
PROMPT_NAMES = {"route": "ambrewick.route", "answer": "ambrewick.answer"}

# The route prompt names the routes, never the 18 scenario labels (plan section 4.7). The check in the data build
# searches these texts against the traffic.
ROUTE_PROMPT_P1 = """You are the router of a personal assistant. Read the user's message and choose the one route that can answer it.
Reply with JSON only, in this form: {"route": "<route>"}

Routes:
- calendar: the user's events on a day.
- alarms: the user's alarms.
- lists: the user's shopping, packing and to do lists.
- messages: finding a contact the user wants to message.
- devices: the user's smart devices and speakers.
- media: songs, films, podcasts and radio.
- fallback: anything else, including questions that need information from outside the app."""

ANSWER_PROMPT = """You are the assistant of a personal assistant app. Answer the user's message in English, in plain words.
{rules}

If a tool can answer the message, call it before you answer. Do not refuse a request a tool can serve.

Today's date (UTC) is {day}.
{articles}"""


@dataclass(frozen=True)
class Release:
    """One release of the assistant: its versions and its model. The recording scripts pin the files."""

    name: str
    service_version: str
    prompt_version: str
    model: str
    top_k: int
    cache_prompt: bool


RELEASES = {
    "r0": Release("r0", "1.0.0", "p1", "qwen3-4b-instruct-2507", 3, True),
    "r1": Release("r1", "1.1.0", "p2", "qwen3-4b-instruct-2507", 3, True),
    "r2": Release("r2", "1.2.0", "p1", "qwen3-0.6b", 3, True),
    "r3": Release("r3", "1.3.0", "p1", "qwen3-4b-instruct-2507", 8, True),
    "r4": Release("r4", "1.4.0", "p1", "qwen3-4b-instruct-2507", 3, False),
}


class Backend(Protocol):
    """The model. `in_process` says whether it runs in the same process as the assistant: then its inference spans are
    INTERNAL and name no server; otherwise they are CLIENT and name the endpoint (PL-49)."""

    in_process: bool

    def reply(self, request: dict) -> RecordedCall: ...


class Clock(Protocol):
    now_ns: int

    def advance(self, ns: int) -> None: ...


@dataclass(frozen=True)
class Step:
    """One step of a message, on the virtual clock. Model steps carry their token counts."""

    step: str
    name: str
    start_ns: int
    end_ns: int
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class Outcome:
    message_id: str
    route: str
    route_error: bool
    expected_route: str
    source: str
    reply: str
    article_ids: tuple[str, ...]
    tool_names: tuple[str, ...]
    malformed_tool_calls: int
    model_turns: int
    turn_cap_reached: bool
    steps: list[Step] = field(default_factory=list)


THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
TOOL_BLOCK = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)
ROUTE_JSON = re.compile(r"\{.*\}", re.DOTALL)


def strip_think(text: str) -> str:
    """Drop a <think> block, closed or not: an unclosed one runs to the end of the reply (PL-23)."""
    return THINK_BLOCK.sub("", text).split("<think>", 1)[0]


def parse_route(text: str) -> str | None:
    """The route in a route reply, or None when the reply is not JSON naming one of the seven routes."""
    found = ROUTE_JSON.search(strip_think(text))
    if found is None:
        return None
    try:
        route = json.loads(found.group(0)).get("route")
    except (ValueError, AttributeError):
        return None
    return route if route in ROUTE_NAMES else None


class MalformedToolCall(ValueError):
    """A <tool_call> block that is not JSON with a name and arguments."""


def parse_tool_call(text: str) -> tuple[str, dict] | None:
    """The tool call in an answer reply: (name, arguments), or None when the reply asks for no tool. Raises
    MalformedToolCall when the reply opens a <tool_call> that does not parse."""
    text = strip_think(text)
    if "<tool_call>" not in text:
        return None
    block = TOOL_BLOCK.search(text)
    if block is None:
        raise MalformedToolCall("the <tool_call> block is not closed")
    try:
        body = json.loads(block.group(1))
    except ValueError as exc:
        raise MalformedToolCall(str(exc)) from exc
    if not isinstance(body, dict) or not isinstance(body.get("name"), str):
        raise MalformedToolCall("the tool call has no name")
    arguments = body.get("arguments", {})
    if not isinstance(arguments, dict):
        raise MalformedToolCall("the arguments are not an object")
    return body["name"], arguments


def tool_request(call) -> tuple[str, dict] | None:
    """The tool call a reply asks for: the runtime's own tool_calls when it sends one (this build never does, R-6),
    else the <tool_call> text. Raises MalformedToolCall when the call does not parse."""
    if not call.tool_calls:
        return parse_tool_call(call.text)
    if len(call.tool_calls) > 1:
        raise MalformedToolCall("the reply asks for more than one tool call")
    entry = call.tool_calls[0]
    name = entry.get("name")
    arguments = entry.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except ValueError as exc:
            raise MalformedToolCall(str(exc)) from exc
    if not isinstance(name, str) or not name:
        raise MalformedToolCall("the tool call has no name")
    if not isinstance(arguments, dict):
        raise MalformedToolCall("the arguments are not an object")
    return name, arguments


@lru_cache(maxsize=1)
def _articles_by_id() -> dict[str, Article]:
    return {a.article_id: a for a in load_articles()}


@lru_cache(maxsize=1)
def _routing_examples() -> tuple[tuple[str, str], ...]:
    table = tomllib.loads((CASE_DIR / "routing_examples.toml").read_text(encoding="utf-8"))
    return tuple(sorted((row["route"], row["text"]) for row in table["example"]))


def route_prompt(prompt_version: str) -> str:
    """Prompt p1 lists the routes; p2 adds the six routing examples (plan section 2.7)."""
    if prompt_version == "p1":
        return ROUTE_PROMPT_P1
    if prompt_version == "p2":
        lines = [f'- "{text}" -> {route}' for route, text in _routing_examples()]
        return ROUTE_PROMPT_P1 + "\n\nExamples:\n" + "\n".join(lines)
    raise KeyError(f"unknown prompt version {prompt_version!r}")


def prompt_sha256(release: Release) -> str:
    """SHA-256 of the prompt texts one release sends: its route prompt and the answer template. Recorded in each
    manifest (plan section 4.4)."""
    text = route_prompt(release.prompt_version) + "\n\x1f\n" + ANSWER_PROMPT
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _day_of(now_ns: int) -> str:
    """The UTC date of a virtual time, as YYYY-MM-DD. Read from the clock's own count: no system clock is read."""
    moment = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(microseconds=now_ns // 1000)
    return moment.date().isoformat()


def answer_system(day: str, article_ids: tuple[str, ...]) -> str:
    """The answer prompt: the policy rules, the date and the articles. The tools are not in the text; they travel in
    the request (answer_tools), so the chat template renders them (PL-46)."""
    rules = load_policies()["Answer rules"]
    if article_ids:
        by_id = _articles_by_id()
        block = "\n\n".join(f"[{i}] {by_id[i].title}\n{by_id[i].text}" for i in article_ids)
        articles = f"\nKnowledge base articles:\n\n{block}\n"
    else:
        articles = "\nKnowledge base articles: none apply.\n"
    return ANSWER_PROMPT.format(rules=rules, day=day, articles=articles)


def answer_tools(tool_names: tuple[str, ...], tools: Tools) -> list[dict]:
    """The route's tools in OpenAI's function format: name, description, and the parameters schema from the pydantic
    model with user_id left out (Tools.args_schema). An empty list offers none."""
    return [{"type": "function",
             "function": {"name": name, "description": tools.description(name),
                          "parameters": tools.args_schema(name)}}
            for name in tool_names]


def _retrieves(route: str) -> bool:
    return any(r.name == route and r.retrieves for r in load_routes())


class Assistant:
    """Runs the five steps for one message. One instance serves one recording or one replay, in order."""

    def __init__(self, backend: Backend, retriever, tools: Tools, clock: Clock, tracer, release: Release,
                 meter_provider=None):
        """`meter_provider`, when given, is the assistant's live meter: it records the token metrics of each model call,
        and each operation's duration, as the operation ends (PL-70). Without it, nothing is recorded as the spans are."""
        self._backend = backend
        self._retriever = retriever
        self._tools = tools
        self._clock = clock
        self._tracer = tracer
        self._release = release
        self._in_process = bool(getattr(backend, "in_process", False))
        self._tokens = TokenMetrics(meter_provider) if meter_provider is not None else None
        self._durations = OperationMetrics(meter_provider) if meter_provider is not None else None
        self._touched: list[inject.Incident] = []  # the injected incidents this message's spans carry, as they were met

    # --- spans and steps ---------------------------------------------------------------------------------

    def _span(self, parent, name: str, start_ns: int, kind: SpanKind, attributes: dict):
        """A started span under `parent`, at `start_ns`. The caller ends it with the end time it computed."""
        ctx = otel_trace.set_span_in_context(parent) if parent is not None else None
        span = self._tracer.start_span(name, context=ctx, kind=kind, attributes=attributes, start_time=start_ns)
        return span

    def _model_kind(self) -> SpanKind:
        """An inference span's kind: INTERNAL for a model in the same process, CLIENT for one behind an endpoint (the
        registry's gen_ai.client.inference note, plan section 8.2)."""
        return SpanKind.INTERNAL if self._in_process else SpanKind.CLIENT

    def _server_attributes(self) -> dict:
        """The server an inference span names: the configured endpoint, or none for a model in the same process."""
        if self._in_process:
            return {}
        return {"server.address": MODEL_SERVER_ADDRESS, "server.port": MODEL_SERVER_PORT}

    def _observe(self, operation: str, seconds: float, attributes: dict) -> None:
        """Record one operation's duration on the live meter, when there is one, with the attributes its span carries."""
        if self._durations is not None:
            self._durations.observe(operation, seconds, attributes)

    def _mark(self, attributes: dict, incident) -> dict:
        """The attributes with the injected incident an operation touched (`ambrewick.incident.id` and
        `ambrewick.incident.injected`), and the incident noted for the message's root. An operation no fault touches
        gets its attributes back unchanged, so the marker is never false."""
        if incident is None:
            return attributes
        if incident.incident_id not in [i.incident_id for i in self._touched]:
            self._touched.append(incident)
        return {**attributes, "ambrewick.incident.id": incident.incident_id, "ambrewick.incident.injected": True}

    def _usage(self) -> dict:
        """The message's token usage, summed over its model calls (the chat spans' own counts). The invoke_agent span
        carries it as the registry's agent-span usage attributes, input and output (the registry's invoke_agent span names
        no cache attribute, so the cached part stays on the chat spans, PL-69). A sum over every span would count each
        token twice."""
        return {"gen_ai.usage.input_tokens": sum(s.prompt_tokens for s in self._steps),
                "gen_ai.usage.output_tokens": sum(s.completion_tokens for s in self._steps)}

    def _model(self, root, request: dict, step: str, turn: int):
        """One model call: advances the clock by its recorded duration. Returns the call, its span (not yet ended)
        and the span's end time, so the caller can add events before it ends the span."""
        attempt = 0
        while True:
            attempt += 1
            start = self._clock.now_ns
            try:
                self._refuse_if_rate_limited(start)
                call = self._backend.reply(request)
                break
            except inject.RateLimitError as err:
                self._refused_attempt(root, step, turn, attempt, start, err)
                if attempt > len(RETRY_BACKOFF_NS):
                    raise ModelUnavailable(step) from err
                self._clock.advance(RETRY_BACKOFF_NS[attempt - 1])  # the client's backoff, not slept
        self._clock.advance(call.total_ns)
        end = start + call.total_ns
        attrs = {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": "openai",
            "gen_ai.request.model": self._release.model,
            "gen_ai.response.model": self._release.model,
            "gen_ai.response.finish_reasons": (call.finish_reason,),
            "gen_ai.usage.input_tokens": call.prompt_tokens,
            "gen_ai.usage.output_tokens": call.completion_tokens,
            "gen_ai.usage.cache_read.input_tokens": call.cached_tokens,
            "gen_ai.prompt.name": PROMPT_NAMES[step],
            "gen_ai.prompt.version": self._release.prompt_version,
            **self._server_attributes(),
            "ambrewick.step": step,
            "ambrewick.model_turn": turn,
            "ambrewick.attempt": attempt,
        }
        span = self._span(root, f"chat {self._release.model}", start, self._model_kind(), attrs)
        self._steps.append(Step(step, "chat", start, end, call.prompt_tokens, call.cached_tokens,
                                call.completion_tokens))
        self._observe("chat", call.total_ns / NS_PER_S, attrs)  # the live meter, as the call ends
        if self._tokens is not None:
            self._tokens.record_call(model=self._release.model, prompt_tokens=call.prompt_tokens,
                                     cached_tokens=call.cached_tokens, completion_tokens=call.completion_tokens)
        return call, span, end

    def _refuse_if_rate_limited(self, now_ns: int) -> None:
        """On replay only, refuse a request whose virtual time falls in the rate-limit window (plan section 2.9). The
        recorder's live model is never refused: its calls are the ones the window's retries replay."""
        if not isinstance(self._backend, Replay):
            return
        window = inject.rate_limited(now_ns)
        if window is not None:
            raise inject.RateLimitError(window)

    def _refused_attempt(self, root, step: str, turn: int, attempt: int, start_ns: int, err) -> None:
        """A span for one refused attempt: zero length, labelled injected, with its attempt number and error.type."""
        attrs = self._mark({"gen_ai.operation.name": "chat", "gen_ai.request.model": self._release.model,
                            **self._server_attributes(),
                            "http.response.status_code": 429, "error.type": type(err).__name__,
                            "ambrewick.step": step, "ambrewick.model_turn": turn, "ambrewick.attempt": attempt},
                           err.incident)
        span = self._span(root, f"chat {self._release.model}", start_ns, self._model_kind(), attrs)
        span.set_status(Status(StatusCode.ERROR, "429 Too Many Requests (injected)"))
        span.end(end_time=start_ns)
        self._observe("chat", 0.0, attrs)  # a refused attempt is zero length, and its error.type is on the point

    def _run_tool(self, root, name: str, arguments: dict, user_id: str):
        """One tool call, as one `execute_tool` span per attempt under the message's root, as `_model` does for a chat
        call. An attempt that raises is recorded by `_fail_attempt`, and the call is retried after RETRY_DELAY_NS, up to
        ATTEMPTS. The give-up reply is the call's result. The clock moves for every attempt and wait."""
        call_start = self._clock.now_ns
        for attempt in range(1, ATTEMPTS + 1):
            start = self._clock.now_ns
            # A contacts call inside an incident window is one the fault touches: its attempt carries the marker.
            incident = inject.contact_fault(start) if name in MCP_TOOLS else None
            attrs = self._mark({
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": name,
                "ambrewick.step": "tool",
                "ambrewick.attempt": attempt,
            }, incident)
            span = self._span(root, f"execute_tool {name}", start, SpanKind.INTERNAL, attrs)
            # Current while the attempt runs, so the MCP client's span and the server's span join under this attempt.
            try:  # the attempt's error is recorded by _fail_attempt, so use_span does not record the exception too
                with otel_trace.use_span(span, end_on_exit=False, record_exception=False,
                                         set_status_on_exception=False):
                    result = self._tools.call(name, dict(arguments, user_id=user_id))
            except AttemptFailed as failed:
                self._clock.advance(failed.duration_ns)
                self._fail_attempt(span, failed, start + failed.duration_ns)
                if attempt < ATTEMPTS:
                    self._clock.advance(RETRY_DELAY_NS)  # the wrapper's wait, not slept
                    continue
                self._steps.append(Step("tool", name, call_start, self._clock.now_ns))
                return ToolResult(output="timed out" if failed.timed_out else "error", is_error=True,
                                  malformed=False, duration_ns=self._clock.now_ns - call_start, modelled=False)
            self._clock.advance(result.duration_ns)
            error_type = None
            if result.malformed:
                # Arguments the schema rejects: an error, with the exception's class name (PL-68).
                error_type = result.error_type
                span.set_status(Status(StatusCode.ERROR, "the arguments do not match the tool's schema"))
            elif result.is_error and name in MCP_TOOLS:
                # An isError result: the MCP conventions (v0.1.0, docs/gen-ai/mcp.md) set error.type to tool_error (PL-78).
                error_type = "tool_error"
                span.set_status(Status(StatusCode.ERROR, f"{name} returned an error result"))
            elif result.is_error and not result.not_found:
                error_type = "tool_error"  # a not-found reply is a normal result: status UNSET, no error.type (PL-68)
            if error_type is not None:
                span.set_attribute("error.type", error_type)
            span.end(end_time=start + result.duration_ns)
            self._observe("execute_tool", result.duration_ns / NS_PER_S,
                          {"gen_ai.operation.name": "execute_tool", **({"error.type": error_type} if error_type else {})})
            self._steps.append(Step("tool", name, call_start, self._clock.now_ns))
            return result

    def _fail_attempt(self, span, failed: AttemptFailed, end_ns: int) -> None:
        """Ends one tool attempt that raised: ERROR, the exception's class in `error.type`, the MCP error code, and an
        exception event with the class and message. The stack trace is left out: it would hold this machine's paths."""
        error = failed.error
        span.set_attribute("error.type", failed.error_type)
        if failed.code is not None:
            span.set_attribute("ambrewick.mcp.error_code", failed.code)
        span.set_status(Status(StatusCode.ERROR, str(error)))
        span.add_event("exception", {"exception.type": f"{type(error).__module__}.{failed.error_type}",
                                     "exception.message": str(error)})
        span.end(end_time=end_ns)
        self._observe("execute_tool", failed.duration_ns / NS_PER_S,
                      {"gen_ai.operation.name": "execute_tool", "error.type": failed.error_type})

    # --- the five steps -----------------------------------------------------------------------------------

    def handle(self, message: Message) -> Outcome:
        """Run the five steps for one message, from the clock's current time."""
        self._steps: list[Step] = []
        self._tools.begin_message()  # this message's records start from the seed (plan section 2.6)
        self._touched = []
        expected = scenario_to_route(message.scenario)
        started = self._clock.now_ns
        root = self._tracer.start_span(
            f"invoke_agent {AGENT}", kind=SpanKind.INTERNAL, start_time=started,
            attributes={"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": AGENT,
                        "ambrewick.user.id": message.user_id, "gen_ai.prompt.version":
                        self._release.prompt_version})

        error_type = None
        try:
            route, route_error, route_call = self._route(root, message)
            if route == FALLBACK:
                outcome = self._fallback(root, message, expected, route, route_error)
            else:
                outcome = self._answer(root, message, expected, route, route_error, route_call)
        except ModelUnavailable as err:
            # The model gave up after its retries: the message ends with no reply, and the root span says why.
            error_type = type(err).__name__
            root.set_attribute("error.type", error_type)
            root.set_status(Status(StatusCode.ERROR, "model unavailable (injected 429)"))
            outcome = Outcome(message.message_id, "", True, expected, "unavailable", "", (), (), 0, 0, False)
            route = ""

        # The root carries the message's usage (the registry's agent-span usage) and any incident its spans touched.
        root.set_attributes({"ambrewick.route": route, "ambrewick.reply.source": outcome.source, **self._usage()})
        if self._touched:  # the first incident the message met labels the root; a message rarely meets two
            root.set_attributes({"ambrewick.incident.id": self._touched[0].incident_id,
                                 "ambrewick.incident.injected": True})
        root.end(end_time=self._clock.now_ns)
        self._observe("invoke_agent", (self._clock.now_ns - started) / NS_PER_S,
                      {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": AGENT,
                       "gen_ai.prompt.version": self._release.prompt_version,
                       **({"error.type": error_type} if error_type else {})})
        outcome.steps = self._steps
        return outcome

    def _route(self, root, message: Message):
        request = {"kind": "model", "max_tokens": ROUTE_MAX_TOKENS, "temperature": 0, "seed": 0,
                   "messages": [{"role": "system", "content": route_prompt(self._release.prompt_version)},
                                {"role": "user", "content": message.text}]}
        call, span, end = self._model(root, request, "route", 1)
        route = parse_route(call.text)
        route_error = route is None
        chosen = route if route is not None else FALLBACK
        expected = scenario_to_route(message.scenario)
        # The check event (plan section 3): the route the scenario's table gives, and the label it was graded on.
        # A reply that is not a route is recorded as the fallback route, with route_error set.
        span.set_attribute("ambrewick.route", chosen)
        span.add_event("ambrewick.check", {"ambrewick.route.correct": chosen == expected,
                                           "ambrewick.route.expected": expected,
                                           "ambrewick.scenario": message.scenario}, timestamp=end)
        span.end(end_time=end)
        return chosen, route_error, call

    def _fallback(self, root, message, expected, route, route_error) -> Outcome:
        now = self._clock.now_ns
        span = self._span(root, "ambrewick.fallback", now, SpanKind.INTERNAL,
                          {"ambrewick.step": "fallback", "ambrewick.reply.source": "fallback"})
        span.end(end_time=now)
        self._steps.append(Step("fallback", "fallback", now, now))
        return Outcome(message.message_id, route, route_error, expected, "fallback",
                       load_policies()["Fallback reply"], (), (), 0, 1, False)

    def _answer(self, root, message, expected, route, route_error, route_call) -> Outcome:
        article_ids: tuple[str, ...] = ()
        if _retrieves(route):
            article_ids = self._retrieve(root, message.text)
        offered = route_tools(route)
        system = answer_system(_day_of(self._clock.now_ns), article_ids)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": message.text}]
        tools = answer_tools(offered, self._tools)
        tool_names: list[str] = []
        malformed = 0
        reply = ""
        turn = 1
        cap = False
        while True:
            if turn >= MAX_MODEL_TURNS:
                cap = True
                break
            turn += 1
            request = {"kind": "model", "max_tokens": ANSWER_MAX_TOKENS, "temperature": 0, "seed": 0,
                       "messages": messages, "tools": tools}
            call, span, end = self._model(root, request, "answer", turn)
            span.end(end_time=end)
            try:
                asked = tool_request(call)
            except MalformedToolCall:
                malformed += 1
                messages += [{"role": "assistant", "content": call.text},
                             {"role": "user", "content": "Your tool call could not be read. Answer without a tool."}]
                continue
            if asked is None:
                reply = strip_think(call.text).strip()
                break
            name, arguments = asked
            if name not in offered or len(tool_names) >= MAX_TOOL_CALLS:
                messages += [{"role": "assistant", "content": call.text},
                             {"role": "user", "content": "No such tool is available now. Answer the user."}]
                continue
            tool_names.append(name)
            result = self._run_tool(root, name, arguments, message.user_id)
            if result.malformed:
                malformed += 1
            body = json.dumps(result.output, sort_keys=True, ensure_ascii=False)
            messages += [{"role": "assistant", "content": call.text},
                         {"role": "tool", "name": name, "content": body}]
        if cap:
            reply = ""
        return Outcome(message.message_id, route, route_error, expected, "model", reply,
                       article_ids, tuple(tool_names), malformed, turn, cap)

    def _retrieve(self, root, text: str) -> tuple[str, ...]:
        start = self._clock.now_ns
        ids = tuple(self._retriever.search(text))
        span = self._span(root, "retrieval", start, SpanKind.INTERNAL,
                          {"gen_ai.operation.name": "retrieval", "ambrewick.step": "retrieve",
                           "ambrewick.help.article_ids": ids})
        span.end(end_time=start)
        self._steps.append(Step("retrieve", "retrieval", start, start))
        return ids
