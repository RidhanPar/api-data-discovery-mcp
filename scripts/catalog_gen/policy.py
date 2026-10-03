"""Policy domain: policy-api v1 (deprecated) and v2, endorsements, Baltic motor policies."""

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


def policy_api_v1() -> JSON:
    policy = obj(
        {
            "policyNumber": s("string", "Policy number, e.g. SE-HOM-0012345."),
            "customerId": s("string", "Customer id."),
            "product": s("string", "Product code (HOME, MOTOR, TRAVEL, PET, ACCIDENT)."),
            "status": enum(["ACTIVE", "CANCELLED", "EXPIRED", "PENDING"], "Policy status."),
            "country": s("string", "Country."),
            "startDate": s("string", "Start date.", format="date"),
            "endDate": s("string", "End date.", format="date"),
            "premium": s("number", "Yearly premium in local currency."),
        },
        ["policyNumber", "customerId", "product", "status"],
    )
    doc = document(
        title="Policy API",
        version="1.4.2",
        description=(
            "**DEPRECATED - use Policy API v2.** This API will be switched off on 2027-03-31.\n\n"
            "Read and cancel policies. Premium is returned as a plain number in local currency, "
            "which caused rounding issues - v2 fixes this with a Money object."
        ),
        server_path="/policy/v1",
        scopes={"policy.read": "Read policies", "policy.write": "Cancel policies"},
        x_nordlys=x_nordlys(
            "policy-api",
            "policy",
            "policy-core",
            NORDICS,
            sunset="2027-03-31",
            deprecated_since="2025-09-01",
            replacement="policy-api v2",
        ),
        tags=[{"name": "Policies"}],
        paths={
            "/policies": {
                "get": op(
                    "listPolicies",
                    "List policies for a customer",
                    tags=["Policies"],
                    params=[query_p("customerId", s("string"), "Customer id.", required=True)],
                    ok=("200", "Policies", arr(ref("Policy"))),
                    scopes=["policy.read"],
                )
            },
            "/policies/{policyNumber}": {
                "get": op(
                    "getPolicy",
                    "Get a policy",
                    tags=["Policies"],
                    params=[path_p("policyNumber", "Policy number.")],
                    ok=("200", "The policy", "Policy"),
                    scopes=["policy.read"],
                )
            },
            "/policies/{policyNumber}/cancel": {
                "post": op(
                    "cancelPolicy",
                    "Cancel a policy",
                    description="Cancels immediately. Not idempotent - calling twice returns 409.",
                    tags=["Policies"],
                    params=[path_p("policyNumber", "Policy number.")],
                    body=obj({"reason": s("string", "Free text reason.")}),
                    ok=("200", "Cancelled policy", "Policy"),
                    scopes=["policy.write"],
                )
            },
        },
        schemas={"Policy": policy},
    )
    return mark_deprecated(doc, "Wed, 31 Mar 2027 00:00:00 GMT")


def policy_api_v2() -> JSON:
    coverage = obj(
        {
            "coverageCode": s("string", "Coverage code from the product catalogue, e.g. `HOME_CONTENTS`."),
            "name": s("string", "Display name in the customer's language."),
            "sumInsured": ref("Money"),
            "deductible": ref("Money"),
        },
        ["coverageCode", "sumInsured"],
        "A single cover included in the policy.",
    )
    policy = obj(
        {
            "policyId": s("string", "Stable technical id (UUID). Prefer this over policyNumber.", format="uuid"),
            "policyNumber": s("string", "Human-readable policy number printed on documents."),
            "customerId": s("string", "Id of the policyholder in the Customer API.", format="uuid"),
            "productLine": enum(
                ["home", "motor", "travel", "pet", "accident", "commercial_property"], "Line of business."
            ),
            "status": enum(
                ["quoted", "in_force", "lapsed", "cancelled", "expired"],
                "Lifecycle state. `in_force` replaces v1 `ACTIVE`.",
            ),
            "country": ref("Country"),
            "inceptionDate": s("string", "Date cover starts.", format="date"),
            "expiryDate": s("string", "Date cover ends (exclusive).", format="date"),
            "annualPremium": ref("Money"),
            "paymentFrequency": enum(["annual", "semi_annual", "quarterly", "monthly"]),
            "brokerId": s(["string", "null"], "Set when the policy was sold through a broker."),
            "updatedAt": s("string", "Last change timestamp (UTC).", format="date-time"),
        },
        ["policyId", "policyNumber", "customerId", "productLine", "status", "country", "annualPremium"],
        "An insurance policy as held in the policy administration system.",
    )
    cancellation = obj(
        {
            "effectiveDate": s("string", "Date the cancellation takes effect.", format="date"),
            "reason": enum(["customer_request", "non_payment", "moved_abroad", "risk_sold", "other"]),
            "comment": s("string", "Optional free text, max 500 chars.", maxLength=500),
        },
        ["effectiveDate", "reason"],
    )
    return document(
        title="Policy API",
        version="2.3.0",
        description=(
            "The system-of-record API for insurance policies across all Nordlys markets.\n\n"
            "Use it to read policies, their coverages and their version history, and to cancel "
            "policies. Mid-term changes (endorsements) live in the separate Policy Endorsement API.\n\n"
            "Breaking changes from v1: `policyNumber` path key replaced by `policyId`, premium is a "
            "`Money` object, status values are lower_snake_case, cancellation is idempotent."
        ),
        server_path="/policy/v2",
        scopes={
            "policy.read": "Read policies and coverages",
            "policy.write": "Cancel policies",
        },
        auth_code=True,
        x_nordlys=x_nordlys("policy-api", "policy", "policy-core", ALL_MARKETS),
        tags=[{"name": "Policies", "description": "Read policies"}, {"name": "Cancellations"}],
        paths={
            "/policies": {
                "get": op(
                    "searchPolicies",
                    "Search policies",
                    description="Filter by customer, status, market and line of business. Cursor paginated.",
                    tags=["Policies"],
                    params=[
                        query_p("customerId", s("string", format="uuid"), "Policyholder id."),
                        query_p("status", enum(["quoted", "in_force", "lapsed", "cancelled", "expired"])),
                        query_p("country", COUNTRY, "Market."),
                        query_p("productLine", s("string"), "Line of business."),
                        CURSOR,
                        LIMIT,
                    ],
                    ok=("200", "A page of policies", page_of("Policy")),
                    scopes=["policy.read"],
                )
            },
            "/policies/{policyId}": {
                "get": op(
                    "getPolicy",
                    "Get a policy by id",
                    tags=["Policies"],
                    params=[path_p("policyId", "Policy id (UUID).", s("string", format="uuid"))],
                    ok=("200", "The policy", "Policy"),
                    scopes=["policy.read"],
                )
            },
            "/policies/{policyId}/coverages": {
                "get": op(
                    "listCoverages",
                    "List coverages on a policy",
                    tags=["Policies"],
                    params=[path_p("policyId", "Policy id.", s("string", format="uuid"))],
                    ok=("200", "Coverages", arr(ref("Coverage"))),
                    scopes=["policy.read"],
                )
            },
            "/policies/{policyId}/versions": {
                "get": op(
                    "listPolicyVersions",
                    "Policy version history",
                    description="Every endorsement or renewal creates a new version. Newest first.",
                    tags=["Policies"],
                    params=[path_p("policyId", "Policy id.", s("string", format="uuid"))],
                    ok=(
                        "200",
                        "Versions",
                        arr(
                            obj(
                                {
                                    "version": s("integer"),
                                    "validFrom": s("string", format="date"),
                                    "changeType": enum(["new_business", "endorsement", "renewal", "cancellation"]),
                                }
                            )
                        ),
                    ),
                    scopes=["policy.read"],
                )
            },
            "/policies/{policyId}/cancellations": {
                "post": op(
                    "cancelPolicy",
                    "Cancel a policy",
                    description=(
                        "Schedules a cancellation. Idempotent via the `Idempotency-Key` header. "
                        "Refund of unearned premium is calculated by the Premium Invoice API."
                    ),
                    tags=["Cancellations"],
                    params=[path_p("policyId", "Policy id.", s("string", format="uuid")), IDEMPOTENCY_KEY],
                    body="CancellationRequest",
                    ok=("202", "Cancellation accepted", "Policy"),
                    errors=["400", "401", "403", "404", "429"],
                    extra_responses={"409": {"description": "Policy already cancelled or expired."}},
                    scopes=["policy.write"],
                )
            },
        },
        schemas={"Policy": policy, "Coverage": coverage, "CancellationRequest": cancellation},
    )


def policy_endorsement_api_v1() -> JSON:
    endorsement = obj(
        {
            "endorsementId": s("string", format="uuid"),
            "policyId": s("string", format="uuid"),
            "state": enum(["draft", "priced", "accepted", "rejected", "expired"]),
            "changes": arr(
                obj(
                    {
                        "field": s("string", "Dotted path of the changed attribute, e.g. `vehicle.annualMileage`."),
                        "newValue": s(["string", "number", "boolean"]),
                    }
                )
            ),
            "premiumDelta": ref("Money"),
            "effectiveDate": s("string", format="date"),
            "validUntil": s("string", "Price is guaranteed until this time.", format="date-time"),
        },
        ["endorsementId", "policyId", "state"],
    )
    return document(
        title="Policy Endorsement API",
        version="1.1.0",
        description=(
            "Mid-term adjustments (MTA) to an in-force policy: change address, add a driver, "
            "raise sum insured... Flow is create draft -> get price -> accept. "
            "Only SE and NO are migrated to the new platform so far, FI/DK still use the old "
            "back-office screens."
        ),
        server_path="/policy-endorsements/v1",
        scopes={"policy.endorse": "Create and accept endorsements", "policy.read": "Read endorsements"},
        x_nordlys=x_nordlys("policy-endorsement-api", "policy", "policy-core", ["SE", "NO"]),
        paths={
            "/policies/{policyId}/endorsements": {
                "post": op(
                    "createEndorsement",
                    "create endorsement draft",
                    params=[path_p("policyId", schema=s("string", format="uuid")), IDEMPOTENCY_KEY],
                    body=obj(
                        {"changes": endorsement["properties"]["changes"], "effectiveDate": s("string", format="date")},
                        ["changes"],
                    ),
                    ok=("201", "draft created + priced", "Endorsement"),
                    scopes=["policy.endorse"],
                ),
                "get": op(
                    "listEndorsements",
                    "list endorsements for policy",
                    params=[path_p("policyId", schema=s("string", format="uuid"))],
                    ok=("200", "endorsements", arr(ref("Endorsement"))),
                    scopes=["policy.read"],
                ),
            },
            "/endorsements/{endorsementId}": {
                "get": op(
                    "getEndorsement",
                    "get endorsement",
                    params=[path_p("endorsementId", schema=s("string", format="uuid"))],
                    ok=("200", "endorsement", "Endorsement"),
                    scopes=["policy.read"],
                )
            },
            "/endorsements/{endorsementId}/accept": {
                "post": op(
                    "acceptEndorsement",
                    "accept priced endorsement",
                    description="Fails with 409 if price guarantee (`validUntil`) has passed.",
                    params=[path_p("endorsementId", schema=s("string", format="uuid"))],
                    ok=("200", "accepted, new policy version created", "Endorsement"),
                    extra_responses={"409": {"description": "price expired"}},
                    scopes=["policy.endorse"],
                )
            },
        },
        schemas={"Endorsement": endorsement},
    )


def motor_policy_api_v1() -> JSON:
    """Inherited from the acquired Baltic insurer; uses its own older metadata conventions."""
    motor = obj(
        {
            "id": s("integer", "internal number"),
            "policy_no": s("string", "Policy no. (format LT-MTPL-xxxxxxx)"),
            "vehicle_reg_no": s("string", "Vehicle registration number (plate)"),
            "vin": s("string", "VIN code"),
            "holder_personal_code": s("string", "Personal code (isikukood / personas kods / asmens kodas) of holder"),
            "type": enum(["MTPL", "CASCO"], "MTPL = compulsory motor third party, CASCO = own damage"),
            "valid_from": s("string", format="date"),
            "valid_to": s("string", format="date"),
            "country": s("string", "EE / LV / LT"),
        },
        ["id", "policy_no", "vehicle_reg_no", "type"],
    )
    return document(
        title="Motor policy service (Baltics)",
        version="1.0.7",
        description=(
            "Service for motor policies in Baltic countries. Give MTPL and CASCO policies by vehicle. "
            "Also issue Green Card certificate for travel outside EU.\n\n"
            "Contact Baltic IT team for access."
        ),
        server_path="/baltics/motor/v1",
        scopes={"motor.read": "read", "motor.greencard": "issue green card"},
        legacy_ext={"x-owner": "baltic-motor-it", "x-countries": "EE,LV,LT", "x-domain": "Policy"},
        paths={
            "/motor-policies": {
                "get": op(
                    "findMotorPolicies",
                    "Find motor policies by vehicle",
                    params=[
                        query_p("vehicle_reg_no", s("string"), "plate number"),
                        query_p("vin", s("string"), "VIN"),
                    ],
                    ok=("200", "list", arr(ref("MotorPolicy"))),
                    scopes=["motor.read"],
                )
            },
            "/motor-policies/{id}": {
                "get": op(
                    "getMotorPolicy",
                    "Get motor policy",
                    params=[path_p("id", "internal number", s("integer"))],
                    ok=("200", "policy", "MotorPolicy"),
                    scopes=["motor.read"],
                )
            },
            "/green-cards": {
                "post": op(
                    "issueGreenCard",
                    "Issue Green card",
                    description="Generate Green Card (international motor insurance certificate). Returns PDF link.",
                    body=obj({"policy_no": s("string"), "countries": arr(s("string"))}, ["policy_no"]),
                    ok=("201", "issued", obj({"certificate_no": s("string"), "pdf_url": s("string", format="uri")})),
                    scopes=["motor.greencard"],
                )
            },
        },
        schemas={"MotorPolicy": motor},
    )


SPECS = [
    ("policy/policy-api.v1.yaml", policy_api_v1),
    ("policy/policy-api.v2.yaml", policy_api_v2),
    ("policy/policy-endorsement-api.v1.yaml", policy_endorsement_api_v1),
    ("policy/motor-policy-api.v1.yaml", motor_policy_api_v1),
]
