"""Turn specs and data contracts into searchable chunks.

Chunk granularity is the unit a developer actually needs to find:
  * one overview chunk per API version and per data product, and
  * one chunk per endpoint (path + method) and per data product field.

Every chunk repeats a little parent context (API title, domain, markets,
lifecycle) so that it is still meaningful when retrieved on its own - an
endpoint chunk that only said "GET /claims" would match every claims API.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..catalog.metadata import ApiMetadata, iter_operations
from ..catalog.models import DataProduct

# Bump when chunk text layout changes: forces re-chunking + re-embedding of everything.
CHUNKER_VERSION = "3"
MAX_SCHEMA_DEPTH = 3
MAX_SCHEMA_LINES = 40
DESCRIPTION_EXCERPT = 300

# Spelled out because the bare code "NO" is an English stop word and vanishes from the
# full-text index, and because people search for "Norway", not "NO".
COUNTRY_NAMES = {
    "SE": "Sweden",
    "NO": "Norway",
    "FI": "Finland",
    "DK": "Denmark",
    "EE": "Estonia",
    "LV": "Latvia",
    "LT": "Lithuania",
}


def markets_label(countries: Sequence[str]) -> str:
    return ", ".join(f"{c} ({COUNTRY_NAMES.get(c, c)})" for c in countries) or "unknown"


def sha256_json(obj: Any) -> str:
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ChunkDraft:
    key: str
    kind: str  # api | endpoint | data_product | field
    title: str
    body: str
    method: str | None = None
    path: str | None = None
    field_name: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return sha256_json([CHUNKER_VERSION, self.title, self.body])


# --------------------------------------------------------------------------- schema rendering


def resolve_ref(doc: dict[str, Any], ref: str) -> dict[str, Any]:
    """Resolve a local JSON pointer like '#/components/schemas/Claim'. Remote refs are not followed."""
    if not ref.startswith("#/"):
        return {}
    node: Any = doc
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return {}
        node = node[part]
    return node if isinstance(node, dict) else {}


def _type_label(schema: dict[str, Any]) -> str:
    t = schema.get("type", "object" if "properties" in schema else "any")
    label = "|".join(t) if isinstance(t, list) else str(t)
    if schema.get("format"):
        label += f":{schema['format']}"
    if schema.get("enum"):
        label += " enum[" + ", ".join(map(str, schema["enum"][:8])) + "]"
    return label


def render_schema(
    doc: dict[str, Any], schema: dict[str, Any], *, prefix: str = "", depth: int = 0, seen: frozenset[str] = frozenset()
) -> list[str]:
    """Flatten a JSON Schema into 'name (type): description' lines, following local $refs."""
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref in seen or depth > MAX_SCHEMA_DEPTH:
            return [f"{prefix or 'value'} -> {ref.rsplit('/', 1)[-1]}"]
        return render_schema(doc, resolve_ref(doc, ref), prefix=prefix, depth=depth, seen=seen | {ref})

    if schema.get("type") == "array" and "items" in schema:
        return render_schema(doc, schema["items"], prefix=f"{prefix}[]", depth=depth, seen=seen)

    props = schema.get("properties")
    if not props:
        desc = schema.get("description", "")
        return [f"{prefix or 'value'} ({_type_label(schema)})" + (f": {desc}" if desc else "")]

    lines: list[str] = []
    required = set(schema.get("required", []))
    for name, sub in props.items():
        path = f"{prefix}.{name}" if prefix else name
        sub_resolved = resolve_ref(doc, sub["$ref"]) if "$ref" in sub else sub
        nested = "properties" in sub_resolved or (
            sub_resolved.get("type") == "array" and "properties" in _items(doc, sub_resolved)
        )
        if nested and depth < MAX_SCHEMA_DEPTH:
            lines.append(f"{path} ({_type_label(sub_resolved)})" + (" required" if name in required else ""))
            lines.extend(render_schema(doc, sub, prefix=path, depth=depth + 1, seen=seen))
        else:
            desc = sub_resolved.get("description", "")
            req = " required" if name in required else ""
            lines.append(f"{path} ({_type_label(sub_resolved)}){req}" + (f": {desc}" if desc else ""))
    return lines


def _items(doc: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    items = schema.get("items", {})
    return resolve_ref(doc, items["$ref"]) if "$ref" in items else items


def _content_schema(doc: dict[str, Any], node: dict[str, Any]) -> dict[str, Any] | None:
    if "$ref" in node:
        node = resolve_ref(doc, node["$ref"])
    content = node.get("content", {})
    for media in ("application/json", "application/problem+json", *content.keys()):
        if media in content and "schema" in content[media]:
            schema: dict[str, Any] = content[media]["schema"]
            return schema
    return None


def _cap(lines: list[str]) -> list[str]:
    if len(lines) <= MAX_SCHEMA_LINES:
        return lines
    return [*lines[:MAX_SCHEMA_LINES], f"... {len(lines) - MAX_SCHEMA_LINES} more fields"]


# --------------------------------------------------------------------------- APIs


def _lifecycle_line(meta: ApiMetadata) -> str:
    if meta.deprecated:
        return f"Lifecycle: DEPRECATED, sunset {meta.sunset}, replaced by {meta.replacement}."
    return "Lifecycle: active."


def _context_line(meta: ApiMetadata) -> str:
    return (
        f"Domain: {meta.domain}. Markets: {markets_label(meta.countries)}. "
        f"Owner team: {meta.owner_team}. Audience: {meta.audience}."
    )


def _excerpt(text: str | None, n: int = DESCRIPTION_EXCERPT) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


def api_key_prefix(meta: ApiMetadata) -> str:
    return f"api:{meta.api_id}:{meta.major_version}"


def api_chunks(meta: ApiMetadata, doc: dict[str, Any]) -> list[ChunkDraft]:
    info = doc.get("info", {})
    ops = iter_operations(doc)
    header = f"{meta.title} v{meta.major_version} ({meta.api_id}, version {meta.version})"
    op_lines = [f"- {m} {p}" + (f": {o.get('summary')}" if o.get("summary") else "") for p, m, o in ops]
    overview = "\n".join(
        filter(
            None,
            [
                header,
                info.get("description", "").strip() or "No description provided by the owning team.",
                _context_line(meta),
                _lifecycle_line(meta),
                "Operations:",
                *op_lines,
            ],
        )
    )
    chunks = [ChunkDraft(key=api_key_prefix(meta), kind="api", title=header, body=overview)]

    api_excerpt = _excerpt(info.get("description"))
    for path, method, op in ops:
        params = [*doc["paths"][path].get("parameters", []), *op.get("parameters", [])]
        param_lines = []
        for p in params:
            if "$ref" in p:
                p = resolve_ref(doc, p["$ref"])
            label = _type_label(p.get("schema", {}))
            req = ", required" if p.get("required") else ""
            param_lines.append(
                f"- {p.get('name')} ({p.get('in')}, {label}{req})"
                + (f": {p['description']}" if p.get("description") else "")
            )

        lines = [f"{method} {path} - {header}"]
        if op.get("summary"):
            lines.append(f"Summary: {op['summary']}")
        if op.get("description"):
            lines.append(f"Description: {' '.join(op['description'].split())}")
        if op.get("operationId"):
            lines.append(f"Operation id: {op['operationId']}")
        if param_lines:
            lines += ["Parameters:", *param_lines]
        if "requestBody" in op and (body_schema := _content_schema(doc, op["requestBody"])):
            lines += ["Request body:", *_cap(render_schema(doc, body_schema))]
        for code, resp in op.get("responses", {}).items():
            if str(code).startswith("2"):
                resp_schema = _content_schema(doc, resp)
                desc = resp.get("description", "")
                lines.append(f"Response {code}: {desc}".rstrip(": "))
                if resp_schema:
                    lines += _cap(render_schema(doc, resp_schema))
        scopes = sorted({s for req in op.get("security", doc.get("security", [])) for v in req.values() for s in v})
        if scopes:
            lines.append(f"Required scopes: {', '.join(scopes)}")
        lines.append(_context_line(meta))
        lines.append(_lifecycle_line(meta))
        if api_excerpt:
            lines.append(f"About the API: {api_excerpt}")

        title = f"{method} {path} - {meta.title} v{meta.major_version}"
        chunks.append(
            ChunkDraft(
                key=f"{api_key_prefix(meta)}:{method} {path}",
                kind="endpoint",
                title=title,
                body="\n".join(lines),
                method=method,
                path=path,
            )
        )
    return chunks


# --------------------------------------------------------------------------- data products


def data_product_chunks(dp: DataProduct) -> list[ChunkDraft]:
    header = f"{dp.name} ({dp.id}, data product v{dp.version})"
    context = (
        f"Domain: {dp.domain}. Markets: {markets_label(dp.countries)}. Owner team: {dp.owner.team}. "
        f"PII classification: {dp.pii_classification.value}."
    )
    access = (
        f"Access: scope {dp.access.scope}, approval {dp.access.approval}. "
        f"Allowed purposes: {', '.join(dp.access.allowed_purposes)}."
        + (f" Prohibited: {', '.join(dp.access.prohibited_purposes)}." if dp.access.prohibited_purposes else "")
    )
    overview = "\n".join(
        [
            header,
            " ".join(dp.description.split()),
            context,
            f"Freshness: {dp.freshness.update_frequency}, {dp.freshness.sla}",
            access,
            "Output ports: " + "; ".join(f"{p.type} {p.location}" for p in dp.output_ports),
            "Tags: " + ", ".join(dp.tags),
            "Fields: " + ", ".join(f.name for f in dp.schema_),
            *([f"Related APIs: {', '.join(dp.related_apis)}"] if dp.related_apis else []),
        ]
    )
    chunks = [ChunkDraft(key=f"dp:{dp.id}", kind="data_product", title=header, body=overview)]
    excerpt = _excerpt(dp.description, 200)
    for f in dp.schema_:
        body = "\n".join(
            [
                f"Field {f.name} ({f.type}) in {dp.name} ({dp.id})",
                f"Description: {f.description}",
                f"Field PII: {f.pii.value}."
                + (" Primary key." if f.primary_key else "")
                + (" Nullable." if f.nullable else ""),
                context,
                f"About the data product: {excerpt}",
            ]
        )
        chunks.append(
            ChunkDraft(
                key=f"dp:{dp.id}:field:{f.name}",
                kind="field",
                title=f"{f.name} - {dp.name}",
                body=body,
                field_name=f.name,
            )
        )
    return chunks
