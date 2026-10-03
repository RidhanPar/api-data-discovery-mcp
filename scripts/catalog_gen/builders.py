"""Small builder helpers for writing OpenAPI 3.1 documents as Python data.

The synthetic catalog is *generated* so it is reproducible, but every
description string is written by hand per API so that team-to-team
inconsistency (tone, language, completeness) is deliberate, not random.
"""

from __future__ import annotations

from typing import Any

JSON = dict[str, Any]

AUTH_BASE = "https://auth.nordlys.example/realms/nordlys/protocol/openid-connect"
API_BASE = "https://api.nordlys.example"

# --------------------------------------------------------------------------- schemas


def s(type_: str | list[str], description: str | None = None, **kw: Any) -> JSON:
    out: JSON = {"type": type_}
    if description:
        out["description"] = description
    out.update(kw)
    return out


def ref(name: str) -> JSON:
    return {"$ref": f"#/components/schemas/{name}"}


def arr(items: JSON, description: str | None = None) -> JSON:
    return s("array", description, items=items)


def enum(values: list[str], description: str | None = None) -> JSON:
    return s("string", description, enum=values)


def obj(props: dict[str, JSON], required: list[str] | None = None, description: str | None = None) -> JSON:
    out: JSON = {"type": "object"}
    if description:
        out["description"] = description
    if required:
        out["required"] = required
    out["properties"] = props
    return out


def page_of(item_schema: str, description: str | None = None) -> JSON:
    return obj(
        {
            "items": arr(ref(item_schema)),
            "nextCursor": s(["string", "null"], "Opaque cursor for the next page, null on the last page."),
        },
        ["items"],
        description,
    )


PROBLEM = obj(
    {
        "type": s("string", "URI reference identifying the problem type.", format="uri-reference"),
        "title": s("string", "Short human-readable summary."),
        "status": s("integer", "HTTP status code."),
        "detail": s("string", "Explanation specific to this occurrence."),
        "instance": s("string", "URI reference for this occurrence.", format="uri-reference"),
        "correlationId": s("string", "Trace id to quote when contacting the owning team."),
    },
    ["type", "title", "status"],
    "Error body following RFC 9457 (Problem Details for HTTP APIs).",
)

MONEY = obj(
    {
        "amount": s("string", "Decimal amount as a string to avoid float rounding.", pattern=r"^-?\d+(\.\d{1,2})?$"),
        "currency": enum(["SEK", "NOK", "DKK", "EUR"], "ISO 4217 currency code."),
    },
    ["amount", "currency"],
    "A monetary amount.",
)

COUNTRY = enum(["SE", "NO", "FI", "DK", "EE", "LV", "LT"], "ISO 3166-1 alpha-2 market code.")

# --------------------------------------------------------------------------- parameters


def param(
    name: str,
    where: str,
    schema: JSON,
    description: str | None = None,
    required: bool = False,
    **kw: Any,
) -> JSON:
    out: JSON = {"name": name, "in": where}
    if description:
        out["description"] = description
    if required or where == "path":
        out["required"] = True
    out["schema"] = schema
    out.update(kw)
    return out


def path_p(name: str, description: str | None = None, schema: JSON | None = None) -> JSON:
    return param(name, "path", schema or s("string"), description, True)


def query_p(name: str, schema: JSON, description: str | None = None, required: bool = False) -> JSON:
    return param(name, "query", schema, description, required)


def header_p(name: str, schema: JSON, description: str | None = None, required: bool = False) -> JSON:
    return param(name, "header", schema, description, required)


CURSOR = query_p("cursor", s("string"), "Cursor returned as `nextCursor` by the previous page.")
LIMIT = query_p("limit", s("integer", minimum=1, maximum=200, default=50), "Page size (1-200).")
IDEMPOTENCY_KEY = header_p(
    "Idempotency-Key",
    s("string", format="uuid"),
    "Client-generated UUID; retries with the same key return the original result.",
    required=True,
)

# --------------------------------------------------------------------------- operations

STANDARD_ERRORS = {
    "400": "Request failed validation.",
    "401": "Missing or invalid access token.",
    "403": "Token lacks the required scope.",
    "404": "Resource not found.",
    "429": "Rate limit exceeded; honour the Retry-After header.",
}


def op(
    operation_id: str | None,
    summary: str | None,
    *,
    description: str | None = None,
    tags: list[str] | None = None,
    params: list[JSON] | None = None,
    body: str | JSON | None = None,
    body_description: str | None = None,
    ok: tuple[str, str | None, str | JSON | None] = ("200", "OK", None),
    errors: list[str] | None = None,
    scopes: list[str] | None = None,
    security: list[JSON] | None = None,
    deprecated: bool = False,
    extra_responses: dict[str, JSON] | None = None,
) -> JSON:
    """Build one operation.

    ``ok`` is (status, description, schema-name-or-inline-schema-or-None).
    ``errors`` lists standard error codes to attach (default 400/401/403/404/429).
    """
    out: JSON = {}
    if tags:
        out["tags"] = tags
    if summary:
        out["summary"] = summary
    if description:
        out["description"] = description
    if operation_id:
        out["operationId"] = operation_id
    if deprecated:
        out["deprecated"] = True
    if params:
        out["parameters"] = params
    if body is not None:
        body_schema = ref(body) if isinstance(body, str) else body
        rb: JSON = {"required": True, "content": {"application/json": {"schema": body_schema}}}
        if body_description:
            rb = {"description": body_description, **rb}
        out["requestBody"] = rb

    status, ok_desc, ok_schema = ok
    resp: JSON = {"description": ok_desc or "Success"}
    if ok_schema is not None:
        schema = ref(ok_schema) if isinstance(ok_schema, str) else ok_schema
        resp["content"] = {"application/json": {"schema": schema}}
    responses: JSON = {status: resp}
    for code in errors if errors is not None else list(STANDARD_ERRORS):
        responses[code] = {"$ref": f"#/components/responses/E{code}"}
    if extra_responses:
        responses.update(extra_responses)
    out["responses"] = responses

    if security is not None:
        out["security"] = security
    elif scopes is not None:
        out["security"] = [{"oauth2": scopes}]
    return out


def error_responses(codes: list[str] | None = None) -> JSON:
    out: JSON = {}
    for code in codes or list(STANDARD_ERRORS):
        out[f"E{code}"] = {
            "description": STANDARD_ERRORS[code],
            "content": {"application/problem+json": {"schema": ref("Problem")}},
        }
    return out


def oauth2_scheme(scopes: dict[str, str], *, auth_code: bool = False) -> JSON:
    flows: JSON = {"clientCredentials": {"tokenUrl": f"{AUTH_BASE}/token", "scopes": scopes}}
    if auth_code:
        flows["authorizationCode"] = {
            "authorizationUrl": f"{AUTH_BASE}/auth",
            "tokenUrl": f"{AUTH_BASE}/token",
            "scopes": scopes,
        }
    return {
        "type": "oauth2",
        "description": "OAuth 2.1 via the Nordlys identity provider.",
        "flows": flows,
    }


# --------------------------------------------------------------------------- documents


def document(
    *,
    title: str,
    version: str,
    description: str | None,
    server_path: str,
    paths: JSON,
    schemas: JSON,
    tags: list[JSON] | None = None,
    scopes: dict[str, str] | None = None,
    auth_code: bool = False,
    security_schemes: JSON | None = None,
    default_security: list[JSON] | None = None,
    x_nordlys: JSON | None = None,
    legacy_ext: JSON | None = None,
    contact: JSON | None = None,
    include_standard_schemas: bool = True,
    error_codes: list[str] | None = None,
) -> JSON:
    info: JSON = {"title": title, "version": version}
    if description:
        info["description"] = description
    if contact:
        info["contact"] = contact
    info["license"] = {"name": "Proprietary - Nordlys Insurance internal use", "identifier": "LicenseRef-Nordlys"}
    if x_nordlys:
        info["x-nordlys"] = x_nordlys
    if legacy_ext:
        info.update(legacy_ext)

    comps: JSON = {}
    all_schemas: JSON = {}
    if include_standard_schemas:
        all_schemas.update({"Problem": PROBLEM, "Money": MONEY, "Country": COUNTRY})
    all_schemas.update(schemas)
    comps["schemas"] = all_schemas
    if include_standard_schemas:
        comps["responses"] = error_responses(error_codes)
    if security_schemes is not None:
        comps["securitySchemes"] = security_schemes
    elif scopes is not None:
        comps["securitySchemes"] = {"oauth2": oauth2_scheme(scopes, auth_code=auth_code)}

    doc: JSON = {
        "openapi": "3.1.0",
        "info": info,
        "servers": [
            {"url": f"{API_BASE}{server_path}", "description": "Production"},
            {"url": f"https://api.test.nordlys.example{server_path}", "description": "Test"},
        ],
    }
    if default_security is not None:
        doc["security"] = default_security
    elif scopes is not None:
        doc["security"] = [{"oauth2": [next(iter(scopes))]}]
    if tags:
        doc["tags"] = tags
    doc["paths"] = paths
    doc["components"] = comps
    return doc


def deprecation_headers(sunset_http_date: str) -> JSON:
    """Response headers advertised by deprecated APIs (RFC 8594 Sunset, RFC 9745 Deprecation)."""
    return {
        "Deprecation": {
            "description": "Present on every response: this API is deprecated.",
            "schema": s("string"),
        },
        "Sunset": {
            "description": f"Date after which the API stops responding ({sunset_http_date}).",
            "schema": s("string"),
        },
    }


def mark_deprecated(doc: JSON, sunset_http_date: str) -> JSON:
    """Flag every operation deprecated and add Sunset/Deprecation headers to 2xx responses."""
    for item in doc["paths"].values():
        for method, operation in item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            operation["deprecated"] = True
            for code, resp in operation["responses"].items():
                if code.startswith("2") and "$ref" not in resp:
                    resp["headers"] = deprecation_headers(sunset_http_date)
    return doc


def x_nordlys(
    api_id: str,
    domain: str,
    owner_team: str,
    countries: list[str],
    *,
    audience: str = "internal",
    support: str | None = None,
    data_classification: str = "personal",
    sunset: str | None = None,
    deprecated_since: str | None = None,
    replacement: str | None = None,
) -> JSON:
    """The standard catalog metadata block most (not all) teams put in ``info``."""
    lifecycle: JSON = {"status": "deprecated" if sunset else "active"}
    if sunset:
        lifecycle["deprecated-since"] = deprecated_since
        lifecycle["sunset"] = sunset
        lifecycle["replacement"] = replacement
    return {
        "api-id": api_id,
        "domain": domain,
        "owner-team": owner_team,
        "countries": countries,
        "audience": audience,
        "data-classification": data_classification,
        "support": support or f"#team-{owner_team}",
        "lifecycle": lifecycle,
    }
