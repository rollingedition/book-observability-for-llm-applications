"""Token counts for chapter 6 (plan section 6, PL-21, PL-29). `LocaleCounter` counts text with the model's own tokenizer
file, the counter behind every per-locale number. `recorded_calls` and `derived_cached` read the recorder's own counts from
a recording's log, the counts the cost and cache examples use; recorded usage is never counted again from text.

`LocaleCounter()` loads the 4B model's `tokenizer.json` at the revision `examples/models.toml` pins, from the runtime's
Hugging Face cache, offline, and checks its SHA-256 against the pin. The file carries an NFC normalizer, so `count`
applies NFC by itself. The GGUF file's vocabulary applies no normalisation, so its count can differ on text that is not
in NFC (B-87). `is_nfc` says which texts that is.
"""
from lib import SEED  # noqa: F401  (importing lib fixes threads and seeds before the tokenizer library loads)

import hashlib
import json
import tomllib
import unicodedata
from pathlib import Path

from huggingface_hub import hf_hub_download
from tokenizers import Tokenizer

REPO = "Qwen/Qwen3-4B-Instruct-2507"
EXAMPLES = Path(__file__).resolve().parents[1]
MODELS_TOML = EXAMPLES / "models.toml"
RECORDED = EXAMPLES / "data" / "recorded"


def _pinned(repo: str = REPO) -> dict:
    """The `examples/models.toml` row for one repo: its commit and its files' hashes. A repo with no row, or two, raises."""
    rows = [m for m in tomllib.loads(MODELS_TOML.read_text(encoding="utf-8"))["model"] if m["repo"] == repo]
    if len(rows) != 1:
        raise KeyError(f"{repo} has {len(rows)} rows in models.toml; it needs exactly one")
    return rows[0]


def pinned_revision(repo: str = REPO) -> str:
    """The commit the book pins for the repo, as models.toml writes it."""
    return _pinned(repo)["revision"]


class LocaleCounter:
    """The tokenizer file's counter. `count(text)` is the number of tokens the file's tokenizer gives the text alone:
    no chat template, no system prompt, no special tokens. That is why it is not a whole prompt's count."""

    def __init__(self):
        pin = _pinned()
        path = Path(hf_hub_download(REPO, "tokenizer.json", revision=pin["revision"], local_files_only=True))
        expected = pin["files"]["tokenizer.json"]["sha256"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("the cached tokenizer.json is not the file models.toml pins")
        self._tokenizer = Tokenizer.from_file(str(path))

    def count(self, text: str) -> int:
        """Tokens in `text` by the file's tokenizer, with its NFC normalizer applied."""
        return len(self._tokenizer.encode(text, add_special_tokens=False).ids)


def is_nfc(text: str) -> bool:
    """True when the text is already in Unicode Normalization Form C. A text that is not may count one token differently
    under a counter that applies no normalisation."""
    return unicodedata.is_normalized("NFC", text)


def recorded_calls(recording: str, only=None) -> list[dict]:
    """The model calls in one recording's log (traces.jsonl), in order. `only` keeps the calls of the given message IDs.
    Each call is the recorder's own row: prompt tokens, tokens evaluated, completion tokens, and the step it served."""
    path = RECORDED / recording / "traces.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [row for row in rows if row["kind"] == "model" and (only is None or row["message_id"] in only)]


def derived_cached(calls) -> tuple[int, int, int]:
    """Input, evaluated and cached input tokens over some recorded calls. Cached input is not reported by the runtime
    (PL-21): it is the prompt tokens minus the tokens llama.cpp evaluated. Summed over calls, that equals the sum of each
    call's own difference, which the example asserts call by call."""
    prompt = sum(c["prompt_tokens"] for c in calls)
    evaluated = sum(c["tokens_evaluated"] for c in calls)
    return prompt, evaluated, prompt - evaluated
