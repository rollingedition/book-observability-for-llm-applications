"""Build the GenAI registry subset and examples/lib/genai.py from the pinned semantic conventions (plan section 8.1).

Run from the book's root, with a clone of the registry at the pinned tag as the source:

    python examples/data/genai/build_genai.py --source PATH_TO_CLONE

The script refuses any clone that is not at the pinned tag and commit. It reads the YAML under model/gen-ai and
model/mcp, keeps the span types, metrics and attributes the book names (plan section 8.2 and 8.3), and writes:

- examples/data/genai/registry-subset.json, the subset, with the version and commit read from the clone, the schema URL
  the clone's manifest names, and for each span type every attribute's requirement level and its condition, and for
  each histogram the bucket boundaries the clone's docs recommend, with the file and line they are on;
- examples/data/genai/LICENSE, the clone's Apache-2.0 licence;
- examples/lib/genai.py, generated from the subset. Nothing in either output is typed by hand.

The clone's path is an argument and is never written into a file, so the outputs carry no local paths.
"""
import argparse
import json
import re
import subprocess
from pathlib import Path
from pprint import pformat

import yaml

PINNED_TAG = "v0.1.0"
PINNED_COMMIT = "d0e74ba6c48bcc4ded473b1828a8212f06b23753"

# Span types the book uses (plan section 8.2). A name the registry lacks stops the build.
SPAN_TYPES = (
    "gen_ai.client.inference",
    "gen_ai.retrieval",
    "gen_ai.execute_tool",
    "gen_ai.invoke_agent.internal",
    "mcp.client",
    "mcp.server",
)

# The operation values of the span types the book names that are typed here (checked against the enum of
# gen_ai.operation.name in the clone). The inference span's operations are not typed: they are read from the enum (T-4).
TYPED_OPERATIONS = {
    "gen_ai.retrieval": ("retrieval",),
    "gen_ai.execute_tool": ("execute_tool",),
    "gen_ai.invoke_agent.internal": ("invoke_agent",),
}
# The enum has no flag for a model call, so an inference operation is a member whose brief names a model completion or
# generation. Checked: the registry's enum must give chat, text_completion and generate_content this way.
INFERENCE_BRIEF = re.compile(r"completion|generation")
INFERENCE_CHECKED = ("chat", "text_completion", "generate_content")

# Metrics and attributes the book prints (plan section 8.3).
METRICS = (
    "gen_ai.client.inference.duration",
    "gen_ai.client.inference.time_to_first_chunk",
    "gen_ai.client.inference.time_per_output_chunk",
    "gen_ai.client.inference.usage.input_tokens",
    "gen_ai.client.inference.usage.output_tokens",
    "gen_ai.client.inference.usage.cache_read.input_tokens",
    "gen_ai.client.inference.usage.cache_write.input_tokens",
    "gen_ai.client.inference.operation.input_tokens",
    "gen_ai.client.inference.operation.output_tokens",
    "gen_ai.execute_tool.duration",
    "gen_ai.retrieval.duration",
    "gen_ai.client.invoke_agent.duration",
    "gen_ai.invoke_agent.duration",
    "gen_ai.invoke_agent.inference_calls",
    "gen_ai.invoke_agent.tool_calls",
    "mcp.client.operation.duration",
    "mcp.server.operation.duration",
)
# The bucket advice. Each metric's section in the registry's docs gives its recommended ExplicitBucketBoundaries as a
# SHOULD, in the section its "Metric:" heading opens. The advice is prose, so it is read from the docs, not the YAML.
METRIC_HEADING = re.compile(r"^#+ .*?Metric: `([a-z0-9_.]+)`", re.M)
ANY_HEADING = re.compile(r"^#+ ", re.M)
BUCKET_ADVICE = re.compile(r"ExplicitBucketBoundaries[^\[]{0,200}\[\s*([^\]]+?)\s*\]")
BOOK_ATTRIBUTES = (
    "gen_ai.operation.name",
    "gen_ai.provider.name",
    "gen_ai.request.model",
    "gen_ai.response.model",
    "gen_ai.response.finish_reasons",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.cache_read.input_tokens",
    "gen_ai.usage.cache_write.input_tokens",
    "gen_ai.tool.name",
    "gen_ai.tool.call.id",
    "gen_ai.agent.name",
    "gen_ai.prompt.version",
    "gen_ai.retrieval.query.text",
    "gen_ai.token.modality",
    "error.type",
)

# The registry's note on an inference span says which kind an in-process model call may take: "MAY be set to `INTERNAL`
# on spans representing call to models running in the same process" (model/gen-ai/spans.yaml, gen_ai.client.inference).
IN_PROCESS_NOTE = re.compile(r"MAY be set to `(\w+)` on spans representing call to models running in the same process")

HERE = Path(__file__).resolve().parent
SUBSET = HERE / "registry-subset.json"
LIB_GENAI = HERE.parents[1] / "lib" / "genai.py"


def git(source: Path, *args: str) -> str:
    """Run git on the clone and return its output, without the trailing newline."""
    return subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True, text=True).stdout.strip()


def load_model(model: Path):
    """Read every YAML file in the model folders, and return the groups, attributes, spans and metrics they hold."""
    groups, attrs, spans, metrics = {}, {}, {}, {}
    for folder in ("gen-ai", "mcp"):
        for path in sorted((model / folder).glob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            groups.update({g["id"]: g for g in data.get("attribute_groups", [])})
            attrs.update({a["key"]: a for a in data.get("attributes", [])})
            spans.update({s["type"]: s for s in data.get("spans", [])})
            metrics.update({m["name"]: m for m in data.get("metrics", [])})
    return groups, attrs, spans, metrics


def walk(entries, groups, depth=0):
    """Yield every attribute reference in a list, following ref_group links into their groups."""
    if depth > 10:
        raise ValueError("ref_group nesting is too deep")
    for entry in entries:
        if "ref_group" in entry:
            yield from walk(groups[entry["ref_group"]]["attributes"], groups, depth + 1)
        elif "ref" in entry:
            yield entry


def attr_type(defn):
    """Name an attribute's type: its scalar name, or the member values of an enum."""
    if defn is None:
        return {"type": "external"}
    kind = defn.get("type")
    if isinstance(kind, dict) and "members" in kind:
        return {"type": "enum", "members": sorted(m["value"] for m in kind["members"])}
    return {"type": kind if isinstance(kind, str) else "other"}


def bucket_advice(docs: Path, source: Path) -> dict:
    """Each metric's recommended bucket boundaries, with the file and line the advice is on. A metric's advice counts only
    in the section its heading opens, up to the next heading, so a link definition at the foot of a page does not count.
    Two sections that give one metric different advice stop the build."""
    found = {}
    for path in sorted(docs.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        headings = [m.start() for m in ANY_HEADING.finditer(text)] + [len(text)]
        for heading in METRIC_HEADING.finditer(text):
            end = min(h for h in headings if h > heading.start())
            for match in BUCKET_ADVICE.finditer(text, heading.start(), end):
                line = text.count("\n", 0, match.start()) + 1
                advice = {"boundaries": json.loads("[" + match.group(1) + "]"),
                          "source": f"{path.relative_to(source).as_posix()}:{line}"}
                if found.setdefault(heading.group(1), advice) != advice:
                    raise SystemExit(f"two bucket advice sections disagree for {heading.group(1)}")
    return found


def read_schema_url(manifest: Path) -> str:
    """The semantic-conventions schema URL the registry's manifest names as its dependency (plan section 8.1, P4-2)."""
    deps = yaml.safe_load(manifest.read_text(encoding="utf-8"))["dependencies"]
    urls = [d["schema_url"] for d in deps if d.get("schema_url", "").startswith("https://opentelemetry.io/schemas/1.")]
    if len(urls) != 1:
        raise SystemExit("the manifest does not name exactly one semantic-conventions schema URL")
    return urls[0]


def level_of(requirement):
    """A requirement level as the registry writes it: a bare word ("required", "opt_in"), or one key whose value is the
    condition ({"conditionally_required": "If available."}, {"recommended": "When available."})."""
    if isinstance(requirement, str):
        return {"level": requirement}
    ((level, condition),) = requirement.items()
    return {"level": level, "condition": " ".join(condition.split())}


def span_levels(span, groups) -> dict:
    """Each attribute the span names, with its requirement level. A level the span gives on its own reference wins over
    the levels its groups give (the registry's invoke_agent span does this for gen_ai.request.model). A reference with
    no level takes its group's. Where the registry gives no level at all, the level is "unstated": the registry says
    nothing, so the book does not supply one. Two groups that disagree, with no span-level reference between them, stop
    the build."""
    entries = span.get("attributes", [])
    direct = {e["ref"]: level_of(e["requirement_level"]) for e in entries
              if "ref" in e and "requirement_level" in e}
    grouped = {}
    for ref in walk([e for e in entries if "ref_group" in e], groups):
        if "requirement_level" in ref and ref["ref"] not in direct:
            found = level_of(ref["requirement_level"])
            if grouped.setdefault(ref["ref"], found) != found:
                raise SystemExit(f"two requirement levels for {ref['ref']} in {span['type']}")
    levels = {**grouped, **direct}
    for ref in walk(entries, groups):
        levels.setdefault(ref["ref"], {"level": "unstated"})
    return dict(sorted(levels.items()))


def in_process_kind(span):
    """The kind the registry allows an in-process call of this span type, from its note; None if it allows none."""
    match = IN_PROCESS_NOTE.search(" ".join(span.get("note", "").split()))
    return match.group(1).lower() if match else None


def build(source: Path) -> dict:
    """Check the clone's pin, then return the subset as a plain dict."""
    if git(source, "rev-parse", "HEAD") != PINNED_COMMIT:
        raise SystemExit("the clone is not at the pinned commit")
    if git(source, "describe", "--tags", "--exact-match", "HEAD") != PINNED_TAG:
        raise SystemExit("the clone is not at the pinned tag")
    groups, attrs, spans, metrics = load_model(source / "model")
    subset = {
        "source": {
            "name": "semantic-conventions-genai",
            "version": PINNED_TAG.removeprefix("v"),
            "commit": PINNED_COMMIT,
            "licence": "Apache-2.0",
            "schema_url": read_schema_url(source / "model" / "manifest.yaml"),
        },
        "spans": {},
        "metrics": {},
        "attributes": {},
        "operations": {},
    }
    needed = set(BOOK_ATTRIBUTES)
    for name in SPAN_TYPES:
        if name not in spans:
            raise SystemExit(f"span type missing from the registry: {name}")
        span = spans[name]
        levels = span_levels(span, groups)
        required = [attr for attr, level in levels.items() if level["level"] == "required"]
        subset["spans"][name] = {"kind": span["kind"], "required": required, "levels": levels}
        if name == "gen_ai.client.inference":  # the only span type the book uses whose note allows an in-process kind
            kind_in_process = in_process_kind(span)
            if kind_in_process is None:
                raise SystemExit("the inference span's note allows no in-process kind")
            subset["spans"][name]["in_process_kind"] = kind_in_process
        needed.update(required)
    advice = bucket_advice(source / "docs", source)
    for name in METRICS:
        if name not in metrics:
            raise SystemExit(f"metric missing from the registry: {name}")
        subset["metrics"][name] = {"instrument": metrics[name]["instrument"], "unit": metrics[name].get("unit", "")}
        if metrics[name]["instrument"] == "histogram":
            if name not in advice:
                raise SystemExit(f"the registry's docs give no bucket advice for {name}")
            subset["metrics"][name]["buckets"] = advice[name]
        elif name in advice:
            raise SystemExit(f"the registry's docs give bucket advice for {name}, which is not a histogram")
    for key in sorted(needed):
        subset["attributes"][key] = attr_type(attrs.get(key))
    operation = attrs["gen_ai.operation.name"]["type"]["members"]
    members = {m["value"] for m in operation}
    inference = sorted(m["value"] for m in operation
                       if INFERENCE_BRIEF.search(" ".join(str(m.get("brief", "")).split())))
    if not set(INFERENCE_CHECKED) <= set(inference):
        raise SystemExit(f"the registry enum's inference operations are not {INFERENCE_CHECKED}: {inference}")
    subset["operations"]["gen_ai.client.inference"] = inference
    for span_type, values in TYPED_OPERATIONS.items():
        for value in values:
            if value not in members:
                raise SystemExit(f"operation missing from the registry enum: {value}")
        subset["operations"][span_type] = sorted(values)
    return subset


def write_lib(subset: dict) -> None:
    """Write examples/lib/genai.py from the subset: data literals, plus the validator's fixed code."""
    src = subset["source"]
    lines = [
        '"""GenAI registry names, generated from the pinned semantic conventions (plan section 8.1). Do not edit.',
        "",
        "Generated by examples/data/genai/build_genai.py from examples/data/genai/registry-subset.json. To regenerate:",
        "    python examples/data/genai/build_genai.py --source PATH_TO_CLONE",
        "where PATH_TO_CLONE is a clone of the registry at the tag below.",
        "",
        "The registry is Apache-2.0 (examples/data/genai/LICENSE). Its development stability applies to every name here.",
        '"""',
        f'REGISTRY_VERSION = {src["version"]!r}',
        f'REGISTRY_COMMIT = {src["commit"]!r}',
        f'SCHEMA_URL = {src["schema_url"]!r}  # the semantic-conventions schema the registry\'s manifest names',
        "",
        "# span type -> kind, the attributes it requires, each attribute's requirement level (and the registry's",
        "# condition where it has one), and the kind an in-process call may take (plan section 8.2)",
        f"SPAN_TYPES = {pformat(subset['spans'], sort_dicts=True)}",
        "",
        "# span type -> the gen_ai.operation.name values that identify it",
        f"SPAN_OPERATIONS = {pformat(subset['operations'], sort_dicts=True)}",
        "",
        "# attribute name -> its registry type; enums list their members",
        f"ATTR = {pformat(subset['attributes'], sort_dicts=True)}",
        "",
        "# metric name -> instrument and unit (seconds where the unit is s)",
        f"METRICS = {pformat({n: {k: v for k, v in m.items() if k != 'buckets'} for n, m in subset['metrics'].items()}, sort_dicts=True)}",
        "",
        "# histogram name -> the bucket boundaries the registry's docs recommend (a SHOULD), in the metric's unit; where each",
        "# is written is in registry-subset.json, under the metric's buckets.source",
        f"BUCKETS = {pformat({n: tuple(m['buckets']['boundaries']) for n, m in subset['metrics'].items() if 'buckets' in m}, sort_dicts=True)}",
        "",
        "",
        VALIDATE,
    ]
    LIB_GENAI.write_text("\n".join(lines), encoding="utf-8")


VALIDATE = '''def validate(span_row):
    """Return the findings for one exported span row (export.spans_to_rows): one string per problem, empty if none.

    The span type is found from gen_ai.operation.name, or for MCP spans from mcp.method.name and the span kind. A row
    with no known type is reported, since a span the book names must be one the registry defines.
    """
    attributes = span_row["attributes"]
    operation = attributes.get("gen_ai.operation.name")
    if operation is not None:
        found = [t for t, values in SPAN_OPERATIONS.items() if operation in values]
    elif "mcp.method.name" in attributes:
        found = ["mcp.client" if span_row["kind"] == "CLIENT" else "mcp.server"]
    else:
        found = []
    if len(found) != 1:
        return [f"{span_row['name']}: no known span type for this row"]
    span_type = found[0]
    spec = SPAN_TYPES[span_type]
    findings = []
    # The registry says a model call is CLIENT, and MAY be INTERNAL for a model in the same process. A call with a
    # server address went to an endpoint, so only CLIENT passes it; a call without one ran in process, so INTERNAL too.
    allowed = {spec["kind"]} if spec["kind"] else set()
    if spec.get("in_process_kind") and "server.address" not in attributes:
        allowed.add(spec["in_process_kind"])
    if allowed and span_row["kind"].lower() not in allowed:
        findings.append(f"{span_type}: kind is {span_row['kind']}, the registry says {spec['kind']}")
    for key in spec["required"]:
        if key not in attributes:
            findings.append(f"{span_type}: missing required attribute {key}")
    return findings
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, type=Path, help="a clone of the registry at the pinned tag")
    args = parser.parse_args()
    subset = build(args.source)
    SUBSET.write_text(json.dumps(subset, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (HERE / "LICENSE").write_bytes((args.source / "LICENSE").read_bytes())
    write_lib(subset)


if __name__ == "__main__":
    main()
