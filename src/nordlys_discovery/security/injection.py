"""Prompt-injection screening for catalog text.

Spec descriptions, examples and data-contract text are written by many teams and, for
partner-submitted specs, by outsiders. They end up in an LLM's context, so they are an
injection vector. The defence has layers:

1. Treat catalog text as data: server instructions and the agent system prompt say so,
   and tool results keep catalog text in clearly named data fields.
2. Screen it (this module): deterministic patterns flag suspicious text at ingestion. A
   flagged asset carries a `content_warning` everywhere it is returned.
3. Make injection pointless: no tool can grant access, return restricted sample rows or
   change the catalog, whatever the model is persuaded to do. Authorisation lives in
   code and the database, not in the prompt.

Pattern screening is a tripwire, not a guarantee; layer 3 is what actually holds.
"""

from __future__ import annotations

import re

# Bump when patterns change: ingestion then re-screens every file (without re-embedding).
SCANNER_VERSION = "1"

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "instruction_override",
        re.compile(
            r"\b(ignore|disregard|forget)\b.{0,40}\b(previous|prior|above|all|earlier)\b.{0,20}\b(instructions?|rules?|prompts?)",
            re.I,
        ),
    ),
    ("role_hijack", re.compile(r"\b(you are now|act as|pretend to be|new instructions?|system prompt)\b", re.I)),
    (
        "tool_steering",
        re.compile(r"\b(call|invoke|use|run)\b.{0,30}\b(request_access|check_access|get_data_product|tool)\b", re.I),
    ),
    (
        "secrecy",
        re.compile(
            r"\b(do not|don't|never)\b.{0,20}\b(tell|mention|reveal|inform)\b.{0,20}\b(user|human|anyone)\b", re.I
        ),
    ),
    (
        "exfiltration",
        re.compile(
            r"\b(send|post|upload|exfiltrate)\b.{0,40}\b(token|secret|password|credentials?|api[ _-]?key)\b", re.I
        ),
    ),
    ("fake_markup", re.compile(r"</?(system|assistant|instructions?|tool_call)>|\[\s*(SYSTEM|INST)\s*\]", re.I)),
    (
        "approval_claim",
        re.compile(r"\b(pre-?approved|auto(matically)?[ -]?(approve|grant)|access (is|has been) granted)\b", re.I),
    ),
]


def scan(text: str | None) -> list[str]:
    """Names of the injection patterns found in `text` (empty list = nothing suspicious)."""
    if not text:
        return []
    return [name for name, pattern in _PATTERNS if pattern.search(text)]


def scan_document(node: object, _found: set[str] | None = None) -> list[str]:
    """Scan every string in a nested JSON-like document (descriptions, examples, titles...)."""
    found = _found if _found is not None else set()
    if isinstance(node, str):
        found.update(scan(node))
    elif isinstance(node, dict):
        for k, v in node.items():
            found.update(scan(str(k)))
            scan_document(v, found)
    elif isinstance(node, list):
        for v in node:
            scan_document(v, found)
    return sorted(found)
