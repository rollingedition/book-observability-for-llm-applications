"""Redaction before export, as defence in depth (chapter 12). The primary defence is capture off; this module removes
what the process can recognise: the synthetic users' names and addresses, their user IDs, the folder of a `file://`
content reference, a span status's text, and a stack trace (dropped, since its paths name the process's folders).

It cannot tell a person's name in free text from any other word, so MASSIVE's personal-looking values pass through it
unchanged; `scan.py` counts them so the chapter can report the residue.

`RedactingProcessor(exporter)` is a span processor that exports a redacted copy of each span as the span ends. The SDK
freezes a span's attributes when it ends, so the copy is the only place a change can go. `redact_row` does the same
to a row a receiver stored, which is where a Collector would redact spans from another process (chapter 5).
"""
import hashlib
import hmac
import re
from collections.abc import Mapping
from functools import lru_cache

from opentelemetry.sdk.trace import Event, ReadableSpan, SpanProcessor
from opentelemetry.trace import Status

from lib.case import load_records, load_users

# A demonstration key, written in the code. It is not a secret: anyone who reads this file has it. A pseudonym made with
# it shows the construction, not a protected identifier; a real deployment keeps its key in a secret store.
DEMO_KEY = b"demonstration key in code, not a secret"
USER_ID_ATTRIBUTE = "ambrewick.user.id"
STACKTRACE = "exception.stacktrace"
EMAIL = re.compile(r"\b[A-Za-z0-9._+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.example\b")
USER_ID = re.compile(r"\bU\d{4}\b")
FOLDER = re.compile(r"file://[^\s\"',\]]*/")  # a content reference's folder, up to its last slash
PLACEHOLDER_FOLDER = "file://<folder>/"
# A path left in a value: a file:// address whose folder is not the placeholder, or a POSIX absolute path of two or more
# parts. The lookbehind keeps URLs and fractions such as 1/2 out of it.
ABSOLUTE_PATH = re.compile(r"file://(?!<folder>)|(?<![\w./:<>])/(?:[\w.-]+/)+[\w.-]+")


def hash_user(user_id: str) -> str:
    """The keyed pseudonym of a user ID: HMAC-SHA256 under DEMO_KEY, as 64 hex digits.

    Why a keyed hash, not a plain SHA-256: the case's IDs are U0001 to U0200, so anyone can hash all 200 and match them.
    With the key, the same enumeration recovers nothing unless the key is known as well.
    """
    return hmac.new(DEMO_KEY, user_id.encode("utf-8"), hashlib.sha256).hexdigest()


def pseudonym(user_id: str) -> str:
    """The value that replaces a user ID: "user-" and the first twelve hex digits of its keyed hash."""
    return "user-" + hash_user(user_id)[:12]


@lru_cache(maxsize=1)
def name_pattern() -> re.Pattern:
    """Whole words that are the synthetic users' given names and surnames, or the words of the contacts' names.

    Why case-sensitive: the case's names are capitalised; MASSIVE's values are lower case, so a capitalised word that
    matches is a name from the lists and not a common word in a request.
    """
    users = load_users()
    words = {u.given_name for u in users} | {u.surname for u in users}
    for record in load_records("contacts"):
        words |= set(record["name"].split())
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)) + r")\b")


def redact_text(text: str) -> str:
    """The text with each synthetic email, name and user ID removed, and each file:// folder replaced by a placeholder
    (the scheme and the file name stay). An email and a name become placeholders; a user ID becomes its pseudonym, so
    two spans about one user still match."""
    text = EMAIL.sub("[email]", text)
    text = name_pattern().sub("[name]", text)
    text = USER_ID.sub(lambda match: pseudonym(match.group(0)), text)
    return FOLDER.sub(PLACEHOLDER_FOLDER, text)


def has_absolute_path(text: str) -> bool:
    """Whether a value still names a folder of the machine that wrote it."""
    return ABSOLUTE_PATH.search(text) is not None


def redact_status(status: Status) -> Status:
    """The span's status with its description redacted: an exception's message can hold a name or a path."""
    if not status.description:
        return status
    return Status(status.status_code, redact_text(status.description))


def redact_attributes(attributes: Mapping) -> dict:
    """A new dict of one span's (or one event's) attributes, redacted. The user ID attribute is replaced by its
    pseudonym; a stack trace is dropped, since it names the folders of the process that ran the code; other text is
    redacted with `redact_text`; other values pass unchanged."""
    result = {}
    for key, value in attributes.items():
        if key == STACKTRACE:
            continue
        if key == USER_ID_ATTRIBUTE:
            result[key] = pseudonym(value)
        elif isinstance(value, str):
            result[key] = redact_text(value)
        else:
            result[key] = value
    return result


def redact_span(span: ReadableSpan) -> ReadableSpan:
    """A copy of an ended span, with its attributes, its events' attributes and its status description redacted. The
    span itself is unchanged, and its identity (IDs, parent, times, status code, scope) is copied as it is."""
    events = [Event(e.name, attributes=redact_attributes(e.attributes or {}), timestamp=e.timestamp)
              for e in span.events]
    return ReadableSpan(
        name=span.name, context=span.context, parent=span.parent, resource=span.resource,
        attributes=redact_attributes(span.attributes or {}), events=events, links=span.links, kind=span.kind,
        status=redact_status(span.status), start_time=span.start_time, end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


def redact_row(row: dict) -> dict:
    """A copy of one stored row (lib/export.py's shape), redacted as `redact_span` redacts a span."""
    events = [dict(e, attributes=redact_attributes(e["attributes"])) for e in row["events"]]
    return dict(row, attributes=redact_attributes(row["attributes"]), events=events)


class RedactingProcessor(SpanProcessor):
    """Exports each span, redacted, as it ends. Put it where the exporter would go: it owns the export.

    Why on end: the SDK freezes a span's attributes when the span ends, so the copy that is exported must be made then,
    holding everything the span holds. A start hook sees only what was set at the start.
    """

    def __init__(self, exporter):
        self._exporter = exporter

    def on_start(self, span, parent_context=None):
        return None

    def on_end(self, span):
        self._exporter.export((redact_span(span),))

    def shutdown(self):
        self._exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30_000):
        return self._exporter.force_flush(timeout_millis)
