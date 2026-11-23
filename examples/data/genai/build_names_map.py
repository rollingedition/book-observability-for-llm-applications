"""Build examples/lib/names_map.py: the names the pinned OpenAI instrumentation emits, each mapped to its GenAI
conventions v0.1.0 name (plan section 6, chapter 3, probes O-4 and O-5).

Run from the book's root, with the book's runtime (examples/requirements.txt) and a clone of the registry at the
pinned tag as the source:

    python examples/data/genai/build_names_map.py --source PATH_TO_CLONE

The script makes three instrumented calls and records the names each one produces, as the package spells them:
the recorded call (examples/data/recorded/http-replay), a streamed call on a reply this script constructs (for the
names a stream adds; nothing from it is kept), and a call with content events on (for the event's name; only
names are kept). It refuses a name it did not see and has no entry for, a target the registry lacks, and a clone at
any other commit. Only the names are written: no content, no clone path and no local path.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

BOOK = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(BOOK / "examples")]

import lib  # noqa: E402,F401  (lib first, plan 1.4 rule 10)

import httpx  # noqa: E402
import openai  # noqa: E402
from importlib.metadata import version  # noqa: E402
from opentelemetry.instrumentation.genai.openai import OpenAIInstrumentor  # noqa: E402
from opentelemetry.sdk._logs import LoggerProvider  # noqa: E402
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor  # noqa: E402
from opentelemetry.sdk.metrics import MeterProvider  # noqa: E402
from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from lib import http_replay  # noqa: E402
from lib.assistant import MODEL_SERVER_ADDRESS, MODEL_SERVER_PORT, RELEASES, ROUTE_MAX_TOKENS, route_prompt  # noqa: E402
from lib.case import load_messages  # noqa: E402

PACKAGE = "opentelemetry-instrumentation-genai-openai"
CAPTURE_VAR = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
DAY = "2026-12-07"
REGISTRY_ROOTS = ("model",)

# The judgement, written here and only here: the v0.1.0 name each emitted name corresponds to. () means v0.1.0 has
# no such name. The package's span is named "{operation} {model}", so its key is written with the placeholder.
MAP = {
    "chat {model}": ("gen_ai.client.inference",),
    "gen_ai.operation.name": ("gen_ai.operation.name",),
    "gen_ai.provider.name": ("gen_ai.provider.name",),
    "gen_ai.request.max_tokens": ("gen_ai.request.max_tokens",),
    "gen_ai.request.model": ("gen_ai.request.model",),
    "gen_ai.request.seed": ("gen_ai.request.seed",),
    "gen_ai.request.stream": ("gen_ai.request.stream",),
    "gen_ai.request.temperature": ("gen_ai.request.temperature",),
    "gen_ai.response.finish_reasons": ("gen_ai.response.finish_reasons",),
    "gen_ai.response.id": ("gen_ai.response.id",),
    "gen_ai.response.model": ("gen_ai.response.model",),
    "gen_ai.response.time_to_first_chunk": ("gen_ai.response.time_to_first_chunk",),
    "gen_ai.usage.cache_read.input_tokens": ("gen_ai.usage.cache_read.input_tokens",),
    "gen_ai.usage.input_tokens": ("gen_ai.usage.input_tokens",),
    "gen_ai.usage.output_tokens": ("gen_ai.usage.output_tokens",),
    "gen_ai.input.messages": ("gen_ai.input.messages",),
    "gen_ai.output.messages": ("gen_ai.output.messages",),
    "gen_ai.client.inference.operation.details": ("gen_ai.client.inference.operation.details",),
    "gen_ai.client.operation.duration": ("gen_ai.client.inference.duration",),
    "gen_ai.client.token.usage": ("gen_ai.client.inference.operation.input_tokens",
                                  "gen_ai.client.inference.operation.output_tokens"),
    "gen_ai.client.operation.time_to_first_chunk": ("gen_ai.client.inference.time_to_first_chunk",),
    "gen_ai.client.operation.time_per_output_chunk": ("gen_ai.client.inference.time_per_output_chunk",),
    "gen_ai.token.type": (),
    "server.address": ("server.address",),
    "server.port": ("server.port",),
}

# Emitted only when the response carries cached prompt tokens, which the recorded replies do not. Listed here, and
# the build checks that the package's source reads the field that carries them.
CONDITIONAL = {"gen_ai.usage.cache_read.input_tokens": "cached_tokens"}

def sse_client() -> openai.OpenAI:
    """A client whose streamed reply is constructed here. It exists to observe the streaming names only."""
    chunks = [{"id": "names", "object": "chat.completion.chunk", "created": 0, "model": "constructed",
               "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
              for text in ('{"route"', ': "devices"}')]
    chunks.append({"id": "names", "object": "chat.completion.chunk", "created": 0, "model": "constructed",
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    chunks.append({"id": "names", "object": "chat.completion.chunk", "created": 0, "model": "constructed",
                   "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"

    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode("utf-8"), headers={"content-type": "text/event-stream"})

    return openai.OpenAI(base_url=f"http://{MODEL_SERVER_ADDRESS}:{MODEL_SERVER_PORT}/v1", api_key="local", max_retries=0,
                         http_client=httpx.Client(transport=httpx.MockTransport(reply)))


def observe(streamed: bool = False, capture: str | None = None) -> dict:
    """Make one instrumented call and return the names it produced, by kind. Capture is read when the instrumentor
    is installed, so the variable is set before that and removed after."""
    if capture:
        os.environ[CAPTURE_VAR] = capture
    spans, reader, logs = InMemorySpanExporter(), InMemoryMetricReader(), InMemoryLogRecordExporter()
    tracer = TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(spans))
    loggers = LoggerProvider()
    loggers.add_log_record_processor(SimpleLogRecordProcessor(logs))
    instrumentor = OpenAIInstrumentor()
    instrumentor.instrument(tracer_provider=tracer, meter_provider=MeterProvider(metric_readers=[reader]),
                            logger_provider=loggers)
    try:
        release = RELEASES["r0"]
        message = load_messages(DAY)[0]
        body = dict(model=release.model, max_tokens=ROUTE_MAX_TOKENS, temperature=0, seed=0,
                    messages=[{"role": "system", "content": route_prompt(release.prompt_version)},
                              {"role": "user", "content": message.text}])
        if streamed:
            stream = sse_client().chat.completions.create(stream=True, **body)
            for _ in stream:
                pass
        else:
            http_replay.client().chat.completions.create(**body)
    finally:
        instrumentor.uninstrument()
        os.environ.pop(CAPTURE_VAR, None)
    names = {"span": set(), "attribute": set(), "metric": set(), "point attribute": set(), "event": set()}
    for span in spans.get_finished_spans():
        operation = span.attributes.get("gen_ai.operation.name")
        names["span"].add("chat {model}" if span.name == f"{operation} {span.attributes['gen_ai.request.model']}"
                          else span.name)
        names["attribute"].update(span.attributes.keys())
    for resource in reader.get_metrics_data().resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                names["metric"].add(metric.name)
                for point in metric.data.data_points:
                    names["point attribute"].update(key for key in point.attributes if key == "gen_ai.token.type")
    for record in logs.get_finished_logs():
        names["event"].add(record.log_record.event_name)
        names["attribute"].update(record.log_record.attributes.keys())
    return names


def registry_tokens(clone: Path) -> set[str]:
    """Every dotted word in the registry's model files: a name the registry defines appears in them."""
    words = set()
    for root in REGISTRY_ROOTS:
        for path in (clone / root).rglob("*"):
            if path.suffix in (".yaml", ".json"):
                words.update(re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", path.read_text(encoding="utf-8")))
    return words


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, help="a clone of the registry at the pinned tag")
    args = parser.parse_args()
    clone = Path(args.source)
    pin = json.loads((BOOK / "examples/data/genai/registry-subset.json").read_text(encoding="utf-8"))["source"]
    commit = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], capture_output=True, text=True,
                            check=True).stdout.strip()
    if commit != pin["commit"]:
        raise SystemExit(f"the clone is at {commit}, not the pinned commit {pin['commit']}")

    seen = {"span": set(), "attribute": set(), "metric": set(), "point attribute": set(), "event": set()}
    for run in (dict(), dict(streamed=True), dict(capture="EVENT_ONLY")):
        for kind, found in observe(**run).items():
            seen[kind] |= found
    source = Path(sys.modules["opentelemetry.instrumentation.genai.openai"].__file__).parent
    source_text = "".join(p.read_text(encoding="utf-8") for p in source.rglob("*.py"))
    for name, field in CONDITIONAL.items():
        if field not in source_text:
            raise SystemExit(f"{name}: the package's source does not read {field}")
        seen["attribute"].add(name)                    # conditional: not in these runs, read from the source
    observed = set().union(*seen.values())
    unknown = sorted(observed - set(MAP))
    if unknown:
        raise SystemExit(f"names the package emits with no entry in MAP: {unknown}")
    tokens = registry_tokens(clone)
    for name, targets in MAP.items():
        for target in targets:
            if target.startswith("gen_ai.") and target not in tokens:
                raise SystemExit(f"{name}: the registry at the pin has no {target}")
    kinds = {}
    for name in observed:
        found = sorted(kind for kind, names in seen.items() if name in names)
        if len(found) != 1:
            raise SystemExit(f"{name}: seen as {found}, one kind expected")
        kinds[name] = found[0]
    lines = [
        '"""Names the pinned OpenAI instrumentation emits, mapped to the GenAI conventions v0.1.0 names (plan section 6,',
        'chapter 3). Generated by examples/data/genai/build_names_map.py. Do not edit.',
        '',
        'To regenerate, from the book\'s root with the book\'s runtime and a clone of the registry at the pinned tag:',
        '    python examples/data/genai/build_names_map.py --source PATH_TO_CLONE',
        '',
        'Each key is a name the package emits, spelled as the package spells it. Each value is the tuple of v0.1.0 names',
        'it corresponds to: one name when the name is the same or renamed, two where one name is split (the token',
        'counts, by gen_ai.token.type), and an empty tuple where v0.1.0 has no such name.',
        '"""',
        f'REGISTRY_VERSION = {"0.1.0"!r}',
        f'REGISTRY_COMMIT = {commit!r}',
        f'PACKAGE_VERSIONS = {{{PACKAGE!r}: {version(PACKAGE)!r}}}',
        '',
        'KIND = {',
    ]
    lines += [f'    {name!r}: {kinds[name]!r},' for name in sorted(observed)]
    lines += ['}', '', 'NAMES = {']
    lines += [f'    {name!r}: {tuple(MAP[name])!r},' for name in sorted(observed)]
    lines += ['}', '', '', 'def name_map() -> dict:',
              '    """Return a copy of NAMES: each emitted name to its v0.1.0 names, as a tuple."""',
              '    return dict(NAMES)', '']
    (BOOK / "examples/lib/names_map.py").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"names written: {len(observed)}")


if __name__ == "__main__":
    main()
