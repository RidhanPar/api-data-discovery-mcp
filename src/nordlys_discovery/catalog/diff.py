"""Structural diff between two versions of an API spec.

Answers the developer question "what do I have to change to move from v1 to v2?".
Endpoints are matched on (method, path with parameter names erased), so
/policies/{policyNumber} and /policies/{policyId} count as the same endpoint whose
path parameter changed; remaining unmatched endpoints are paired by operationId.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from .metadata import iter_operations

_PARAM = re.compile(r"\{[^}]+\}")


class PropertyChange(BaseModel):
    schema_name: str
    property: str
    change: str  # added | removed | type_changed | became_required | no_longer_required
    detail: str = ""


class EndpointChange(BaseModel):
    method: str
    old_path: str
    new_path: str
    changes: list[str]


class VersionDiff(BaseModel):
    endpoints_added: list[str]
    endpoints_removed: list[str]
    endpoints_changed: list[EndpointChange]
    schemas_added: list[str]
    schemas_removed: list[str]
    property_changes: list[PropertyChange]
    breaking_changes: list[str]

    @property
    def is_breaking(self) -> bool:
        return bool(self.breaking_changes)


def _norm(path: str) -> str:
    return _PARAM.sub("{}", path)


def _type(schema: dict[str, Any]) -> str:
    if "$ref" in schema:
        return "ref:" + str(schema["$ref"]).rsplit("/", 1)[-1]
    t = schema.get("type", "any")
    return "|".join(map(str, t)) if isinstance(t, list) else str(t)


def _params(op: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(p.get("in", "?"), p.get("name", "?")): p for p in op.get("parameters", []) if "name" in p}


def _scopes(op: dict[str, Any], doc: dict[str, Any]) -> set[str]:
    return {s for req in op.get("security", doc.get("security", [])) for v in req.values() for s in v}


def _compare_ops(
    old: dict[str, Any], new: dict[str, Any], old_doc: dict[str, Any], new_doc: dict[str, Any]
) -> tuple[list[str], list[str]]:
    changes: list[str] = []
    breaking: list[str] = []
    po, pn = _params(old), _params(new)
    old_path_params = sorted(n for (loc, n) in po if loc == "path")
    new_path_params = sorted(n for (loc, n) in pn if loc == "path")
    if old_path_params != new_path_params:
        msg = f"path parameters {old_path_params} -> {new_path_params}"
        changes.append(msg)
        breaking.append(msg)
    for key in pn.keys() - po.keys():
        if key[0] == "path":
            continue
        req = pn[key].get("required", False)
        changes.append(f"{'required' if req else 'optional'} {key[0]} parameter '{key[1]}' added")
        if req:
            breaking.append(f"new required {key[0]} parameter '{key[1]}'")
    for key in po.keys() - pn.keys():
        if key[0] == "path":
            continue
        changes.append(f"{key[0]} parameter '{key[1]}' removed")
        breaking.append(f"{key[0]} parameter '{key[1]}' removed")
    for key in po.keys() & pn.keys():
        if _type(po[key].get("schema", {})) != _type(pn[key].get("schema", {})):
            changes.append(f"parameter '{key[1]}' type changed")
        if not po[key].get("required") and pn[key].get("required"):
            changes.append(f"parameter '{key[1]}' became required")
            breaking.append(f"parameter '{key[1]}' became required")
    old_ok = sorted(c for c in old.get("responses", {}) if str(c).startswith("2"))
    new_ok = sorted(c for c in new.get("responses", {}) if str(c).startswith("2"))
    if old_ok != new_ok:
        changes.append(f"success status {old_ok} -> {new_ok}")
        breaking.append(f"success status {old_ok} -> {new_ok}")
    so, sn = _scopes(old, old_doc), _scopes(new, new_doc)
    if so != sn:
        changes.append(f"required scopes {sorted(so)} -> {sorted(sn)}")
    if old.get("deprecated") and not new.get("deprecated"):
        changes.append("no longer deprecated")
    return changes, breaking


def diff_specs(old_doc: dict[str, Any], new_doc: dict[str, Any]) -> VersionDiff:
    old_ops = {(m, _norm(p)): (p, o) for p, m, o in iter_operations(old_doc)}
    new_ops = {(m, _norm(p)): (p, o) for p, m, o in iter_operations(new_doc)}

    pairs: list[tuple[str, str, str, dict[str, Any], dict[str, Any]]] = []
    unmatched_old = dict(old_ops)
    unmatched_new = dict(new_ops)
    for key in old_ops.keys() & new_ops.keys():
        pairs.append((key[0], old_ops[key][0], new_ops[key][0], old_ops[key][1], new_ops[key][1]))
        unmatched_old.pop(key)
        unmatched_new.pop(key)
    # Second pass: same operationId on a renamed path.
    by_opid = {o.get("operationId"): (k, p, o) for k, (p, o) in unmatched_new.items() if o.get("operationId")}
    for key, (path, op) in list(unmatched_old.items()):
        match = by_opid.get(op.get("operationId"))
        if match and match[0][0] == key[0]:
            nkey, npath, nop = match
            pairs.append((key[0], path, npath, op, nop))
            unmatched_old.pop(key)
            unmatched_new.pop(nkey, None)

    breaking: list[str] = []
    changed: list[EndpointChange] = []
    for method, opath, npath, oop, nop in sorted(pairs, key=lambda x: (x[1], x[0])):
        changes, brk = _compare_ops(oop, nop, old_doc, new_doc)
        if opath != npath:
            changes.insert(0, f"path renamed {opath} -> {npath}")
            if _norm(opath) != _norm(npath):
                brk.insert(0, f"{method} {opath} moved to {npath}")
        if changes:
            changed.append(EndpointChange(method=method, old_path=opath, new_path=npath, changes=changes))
            breaking.extend(f"{method} {opath}: {b}" for b in brk)

    removed = sorted(f"{m} {p}" for (m, _), (p, _) in unmatched_old.items())
    added = sorted(f"{m} {p}" for (m, _), (p, _) in unmatched_new.items())
    breaking.extend(f"endpoint removed: {r}" for r in removed)

    old_schemas = old_doc.get("components", {}).get("schemas", {})
    new_schemas = new_doc.get("components", {}).get("schemas", {})
    prop_changes: list[PropertyChange] = []
    for name in sorted(old_schemas.keys() & new_schemas.keys()):
        o, n = old_schemas[name], new_schemas[name]
        op_, np_ = o.get("properties", {}), n.get("properties", {})
        oreq, nreq = set(o.get("required", [])), set(n.get("required", []))
        for prop in sorted(np_.keys() - op_.keys()):
            prop_changes.append(PropertyChange(schema_name=name, property=prop, change="added"))
        for prop in sorted(op_.keys() - np_.keys()):
            prop_changes.append(PropertyChange(schema_name=name, property=prop, change="removed"))
            breaking.append(f"schema {name}: property '{prop}' removed")
        for prop in sorted(op_.keys() & np_.keys()):
            if _type(op_[prop]) != _type(np_[prop]):
                detail = f"{_type(op_[prop])} -> {_type(np_[prop])}"
                prop_changes.append(
                    PropertyChange(schema_name=name, property=prop, change="type_changed", detail=detail)
                )
                breaking.append(f"schema {name}: '{prop}' type {detail}")
            elif op_[prop].get("enum") != np_[prop].get("enum") and op_[prop].get("enum"):
                detail = f"enum {op_[prop].get('enum')} -> {np_[prop].get('enum')}"
                prop_changes.append(
                    PropertyChange(schema_name=name, property=prop, change="type_changed", detail=detail)
                )
                breaking.append(f"schema {name}: '{prop}' {detail}")
        for prop in sorted((nreq - oreq) & np_.keys()):
            prop_changes.append(PropertyChange(schema_name=name, property=prop, change="became_required"))
        for prop in sorted((oreq - nreq) & np_.keys()):
            prop_changes.append(PropertyChange(schema_name=name, property=prop, change="no_longer_required"))

    return VersionDiff(
        endpoints_added=added,
        endpoints_removed=removed,
        endpoints_changed=changed,
        schemas_added=sorted(new_schemas.keys() - old_schemas.keys()),
        schemas_removed=sorted(old_schemas.keys() - new_schemas.keys()),
        property_changes=prop_changes,
        breaking_changes=breaking,
    )
