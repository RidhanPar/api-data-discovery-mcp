"""Claims domain.

Deliberate realism here:
* claims-api v1 is deprecated (sunset 2026-12-31).
* claims-search-api and claim-lookup-api are NEAR-DUPLICATES: both list claims by
  status and market. claims-search-api is the internal system-of-record search;
  claim-lookup-api is a channel BFF that only returns the caller's own claims.
"""

from __future__ import annotations

from .builders import (
    COUNTRY,
    CURSOR,
    IDEMPOTENCY_KEY,
    JSON,
    LIMIT,
    arr,
    document,
    enum,
    mark_deprecated,
    obj,
    op,
    page_of,
    path_p,
    query_p,
    ref,
    s,
    x_nordlys,
)

NORDICS = ["SE", "NO", "FI", "DK"]
ALL_MARKETS = ["SE", "NO", "FI", "DK", "EE", "LV", "LT"]

CLAIM_STATUS_V2 = [
    "registered",
    "open",
    "in_assessment",
    "awaiting_documents",
    "approved",
    "rejected",
    "paid",
    "closed",
    "reopened",
]


def claims_api_v1() -> JSON:
    claim = obj(
        {
            "claimNumber": s("string", "Claim number."),
            "policyNumber": s("string", "Policy number."),
            "status": enum(["OPEN", "CLOSED", "REJECTED"], "Status."),
            "lossDate": s("string", "Date of loss.", format="date"),
            "description": s("string", "What happened."),
            "estimatedAmount": s("number", "Estimate in local currency."),
            "country": s("string"),
        },
        ["claimNumber", "policyNumber", "status"],
    )
    doc = document(
        title="Claims API",
        version="1.9.0",
        description=(
            "DEPRECATED. Sunset 2026-12-31. Migrate to Claims API v2 (see migration guide on the "
            "developer portal). v1 only knows three statuses and has no event history."
        ),
        server_path="/claims/v1",
        scopes={"claims.read": "Read claims", "claims.write": "Update claims"},
        x_nordlys=x_nordlys(
            "claims-api",
            "claims",
            "claims-platform",
            NORDICS,
            sunset="2026-12-31",
            deprecated_since="2025-06-15",
            replacement="claims-api v2",
        ),
        paths={
            "/claims": {
                "get": op(
                    "listClaims",
                    "List claims",
                    description="Returns claims for a policy. Max 500 results, no pagination.",
                    params=[
                        query_p("policyNumber", s("string"), "Policy number.", required=True),
                        query_p("status", enum(["OPEN", "CLOSED", "REJECTED"])),
                    ],
                    ok=("200", "Claims", arr(ref("Claim"))),
                    scopes=["claims.read"],
                )
            },
            "/claims/{claimNumber}": {
                "get": op(
                    "getClaim",
                    "Get claim",
                    params=[path_p("claimNumber", "Claim number.")],
                    ok=("200", "Claim", "Claim"),
                    scopes=["claims.read"],
                ),
                "put": op(
                    "updateClaim",
                    "Update claim",
                    params=[path_p("claimNumber", "Claim number.")],
                    body="Claim",
                    ok=("200", "Updated", "Claim"),
                    scopes=["claims.write"],
                ),
            },
        },
        schemas={"Claim": claim},
    )
    return mark_deprecated(doc, "Thu, 31 Dec 2026 23:59:59 GMT")


def _claim_v2() -> JSON:
    return obj(
        {
            "claimId": s("string", "Technical claim id.", format="uuid"),
            "claimNumber": s("string", "Claim reference shown to the customer, e.g. NO-CLM-2026-004211."),
            "policyId": s("string", "Policy the claim is registered against.", format="uuid"),
            "customerId": s("string", format="uuid"),
            "status": enum(CLAIM_STATUS_V2, "Claim handling status."),
            "productLine": enum(["home", "motor", "travel", "pet", "accident", "commercial_property"]),
            "country": ref("Country"),
            "lossDate": s("string", "Date the loss occurred.", format="date"),
            "reportedAt": s("string", "When the claim was first reported.", format="date-time"),
            "lossCause": s("string", "Cause code, e.g. `water_leak`, `theft`, `collision`, `storm`."),
            "reserve": ref("Money"),
            "paidToDate": ref("Money"),
            "handlerTeam": s("string", "Claims handling unit currently responsible."),
        },
        ["claimId", "claimNumber", "policyId", "status", "country", "lossDate"],
        "A claim as held in the claims handling system.",
    )


def claims_api_v2() -> JSON:
    event = obj(
        {
            "eventId": s("string", format="uuid"),
            "type": enum(["status_changed", "document_received", "payment_made", "note_added", "reserve_changed"]),
            "occurredAt": s("string", format="date-time"),
            "data": s("object", "Event-specific payload."),
        },
        ["eventId", "type", "occurredAt"],
    )
    return document(
        title="Claims API",
        version="2.6.1",
        description=(
            "System-of-record API for individual claims. Read a claim, follow its event history, "
            "move it through the handling workflow and add notes.\n\n"
            "Looking for *lists* of claims across customers (e.g. all open claims in a market)? "
            "Use the Claims Search API. Registering a brand new claim? Use the FNOL API."
        ),
        server_path="/claims/v2",
        scopes={
            "claims.read": "Read claims and events",
            "claims.write": "Change claim status, add notes",
        },
        x_nordlys=x_nordlys("claims-api", "claims", "claims-platform", ALL_MARKETS),
        tags=[{"name": "Claims"}, {"name": "Events"}, {"name": "Notes"}],
        paths={
            "/claims/{claimId}": {
                "get": op(
                    "getClaim",
                    "Get a claim",
                    tags=["Claims"],
                    params=[path_p("claimId", "Claim id (UUID).", s("string", format="uuid"))],
                    ok=("200", "The claim", "Claim"),
                    scopes=["claims.read"],
                )
            },
            "/claims/by-number/{claimNumber}": {
                "get": op(
                    "getClaimByNumber",
                    "Get a claim by its customer-facing number",
                    tags=["Claims"],
                    params=[path_p("claimNumber", "Claim number, e.g. SE-CLM-2026-000123.")],
                    ok=("200", "The claim", "Claim"),
                    scopes=["claims.read"],
                )
            },
            "/claims/{claimId}/status": {
                "patch": op(
                    "updateClaimStatus",
                    "Move a claim to a new status",
                    description="Only transitions allowed by the claims workflow are accepted; others return 422.",
                    tags=["Claims"],
                    params=[path_p("claimId", "Claim id.", s("string", format="uuid"))],
                    body=obj({"status": enum(CLAIM_STATUS_V2), "reason": s("string")}, ["status"]),
                    ok=("200", "Updated claim", "Claim"),
                    extra_responses={"422": {"description": "Transition not allowed from the current status."}},
                    scopes=["claims.write"],
                )
            },
            "/claims/{claimId}/events": {
                "get": op(
                    "listClaimEvents",
                    "Claim event history",
                    tags=["Events"],
                    params=[path_p("claimId", "Claim id.", s("string", format="uuid")), CURSOR, LIMIT],
                    ok=("200", "Events, oldest first", page_of("ClaimEvent")),
                    scopes=["claims.read"],
                )
            },
            "/claims/{claimId}/notes": {
                "post": op(
                    "addClaimNote",
                    "Add a handler note",
                    tags=["Notes"],
                    params=[path_p("claimId", "Claim id.", s("string", format="uuid")), IDEMPOTENCY_KEY],
                    body=obj(
                        {"text": s("string", maxLength=4000), "visibleToCustomer": s("boolean", default=False)},
                        ["text"],
                    ),
                    ok=("201", "Note added", None),
                    scopes=["claims.write"],
                )
            },
        },
        schemas={"Claim": _claim_v2(), "ClaimEvent": event},
    )


def claims_search_api_v1() -> JSON:
    summary = obj(
        {
            "claimId": s("string", format="uuid"),
            "claimNumber": s("string"),
            "status": enum(CLAIM_STATUS_V2),
            "country": ref("Country"),
            "productLine": s("string"),
            "lossDate": s("string", format="date"),
            "reportedAt": s("string", format="date-time"),
            "reserve": ref("Money"),
        },
        ["claimId", "claimNumber", "status", "country"],
        "Lightweight claim projection returned by search.",
    )
    return document(
        title="Claims Search API",
        version="1.2.0",
        description=(
            "Search and filter claims across all customers and markets - for example all open "
            "claims in Norway, or motor claims reported last week in Finland. Backed by a search "
            "index that lags the claims system by up to 60 seconds.\n\n"
            "Returns summaries; fetch the full claim from the Claims API v2."
        ),
        server_path="/claims-search/v1",
        scopes={"claims.search": "Search claims across customers"},
        x_nordlys=x_nordlys("claims-search-api", "claims", "claims-platform", ALL_MARKETS),
        paths={
            "/claims": {
                "get": op(
                    "searchClaims",
                    "Search claims",
                    description=(
                        "Filter claims by status, market, line of business and reported date. "
                        "`status=open` matches every non-terminal status (registered, open, "
                        "in_assessment, awaiting_documents, reopened)."
                    ),
                    params=[
                        query_p(
                            "status",
                            enum(["open", "closed", "any", *CLAIM_STATUS_V2]),
                            "Status filter. `open` = all non-terminal statuses.",
                        ),
                        query_p("country", COUNTRY, "Market, e.g. `NO` for Norway."),
                        query_p("productLine", s("string"), "Line of business."),
                        query_p("reportedFrom", s("string", format="date"), "Reported on or after."),
                        query_p("reportedTo", s("string", format="date"), "Reported on or before."),
                        query_p("lossCause", s("string"), "Cause code."),
                        CURSOR,
                        LIMIT,
                    ],
                    ok=("200", "Matching claims", page_of("ClaimSummary")),
                    scopes=["claims.search"],
                )
            },
            "/claims/aggregations": {
                "get": op(
                    "aggregateClaims",
                    "Count claims by dimension",
                    description="Counts grouped by status, country or productLine. No personal data returned.",
                    params=[
                        query_p("groupBy", enum(["status", "country", "productLine", "lossCause"]), required=True),
                        query_p("country", COUNTRY),
                        query_p("status", s("string")),
                    ],
                    ok=("200", "Buckets", obj({"buckets": arr(obj({"key": s("string"), "count": s("integer")}))})),
                    scopes=["claims.search"],
                )
            },
        },
        schemas={"ClaimSummary": summary},
    )


def claim_lookup_api_v1() -> JSON:
    """Near-duplicate of claims-search-api, owned by a different team for the customer app."""
    case = obj(
        {
            "caseRef": s("string", "claim reference"),
            "state": enum(["OPEN", "WAITING_FOR_CUSTOMER", "DECIDED", "PAID", "CLOSED"]),
            "market": s("string", "NO/SE/DK/FI"),
            "title": s("string", "short text shown in app e.g. 'Water damage kitchen'"),
            "reportedDate": s("string", format="date"),
            "nextStep": s("string", "what the customer needs to do next, localised"),
        },
        ["caseRef", "state", "market"],
    )
    return document(
        title="Claim Lookup API",
        version="1.0.3",
        description=(
            "Lookup claim cases. Used by My Pages and the Nordlys app to show the status of a "
            "customer's claims. Supports filtering by state and market."
        ),
        server_path="/digital/claim-lookup/v1",
        scopes={"claims.read.self": "Read the authenticated customer's own claims"},
        auth_code=True,
        x_nordlys=x_nordlys(
            "claim-lookup-api",
            "claims",
            "digital-channels",
            NORDICS,
            audience="channel-bff",
        ),
        paths={
            "/claim-cases": {
                "get": op(
                    "getClaimCases",
                    "Get claim cases",
                    description=(
                        "Returns claim cases filtered by state and market. Only cases belonging to "
                        "the customer in the access token (`sub`) are returned."
                    ),
                    params=[
                        query_p("state", enum(["OPEN", "WAITING_FOR_CUSTOMER", "DECIDED", "PAID", "CLOSED"])),
                        query_p("market", s("string"), "NO, SE, DK or FI"),
                    ],
                    ok=("200", "cases", arr(ref("ClaimCase"))),
                    scopes=["claims.read.self"],
                )
            },
            "/claim-cases/{caseRef}": {
                "get": op(
                    "getClaimCase",
                    "Get one claim case",
                    params=[path_p("caseRef", "claim reference")],
                    ok=("200", "case", "ClaimCase"),
                    scopes=["claims.read.self"],
                )
            },
        },
        schemas={"ClaimCase": case},
    )


def fnol_api_v1() -> JSON:
    fnol = obj(
        {
            "policyId": s("string", format="uuid"),
            "lossDate": s("string", format="date"),
            "lossCause": s("string", "Cause code, see /loss-causes"),
            "description": s("string", "Customer's own description of what happened.", maxLength=5000),
            "location": obj({"postalCode": s("string"), "city": s("string"), "country": ref("Country")}),
            "estimatedAmount": ref("Money"),
            "thirdPartyInvolved": s("boolean"),
            "attachments": arr(s("string", format="uuid"), "Document ids uploaded via the Document API."),
        },
        ["policyId", "lossDate", "lossCause", "description"],
        "First notice of loss (skadeanmälan / skademelding / vahinkoilmoitus / skadeanmeldelse).",
    )
    return document(
        title="FNOL API - First Notice of Loss",
        version="1.5.0",
        description=(
            "Register a new claim (first notice of loss). Used by web, app, call centre and "
            "partners. The claim is created asynchronously: you get a `claimNumber` immediately "
            "and the claim appears in the Claims API within seconds.\n\n"
            "Rapportera en ny skada. Meld en ny skade. Ilmoita uusi vahinko."
        ),
        server_path="/fnol/v1",
        scopes={"claims.fnol": "Register new claims", "claims.read": "Read reference data"},
        x_nordlys=x_nordlys("fnol-api", "claims", "claims-intake", NORDICS),
        paths={
            "/notifications": {
                "post": op(
                    "registerLoss",
                    "Register a first notice of loss",
                    params=[IDEMPOTENCY_KEY],
                    body="LossNotification",
                    ok=(
                        "202",
                        "Accepted; claim will be created",
                        obj({"claimNumber": s("string"), "trackingUrl": s("string", format="uri")}),
                    ),
                    scopes=["claims.fnol"],
                )
            },
            "/loss-causes": {
                "get": op(
                    "listLossCauses",
                    "List valid loss cause codes per product line",
                    params=[query_p("productLine", s("string")), query_p("lang", enum(["en", "sv", "nb", "fi", "da"]))],
                    ok=("200", "Codes", arr(obj({"code": s("string"), "label": s("string")}))),
                    scopes=["claims.read"],
                )
            },
        },
        schemas={"LossNotification": fnol},
    )


SPECS = [
    ("claims/claims-api.v1.yaml", claims_api_v1),
    ("claims/claims-api.v2.yaml", claims_api_v2),
    ("claims/claims-search-api.v1.yaml", claims_search_api_v1),
    ("claims/claim-lookup-api.v1.yaml", claim_lookup_api_v1),
    ("claims/fnol-api.v1.yaml", fnol_api_v1),
]
