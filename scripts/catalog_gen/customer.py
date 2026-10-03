"""Customer domain: customer master data, consents, identity verification."""

from __future__ import annotations

from .builders import (
    CURSOR,
    JSON,
    LIMIT,
    arr,
    document,
    enum,
    obj,
    op,
    page_of,
    path_p,
    query_p,
    ref,
    s,
    x_nordlys,
)

ALL_MARKETS = ["SE", "NO", "FI", "DK", "EE", "LV", "LT"]


def customer_api_v2() -> JSON:
    customer = obj(
        {
            "customerId": s("string", format="uuid"),
            "type": enum(["private", "business"]),
            "nationalId": s(
                "string",
                "Personnummer / fødselsnummer / henkilötunnus / CPR / personal code. "
                "Only returned with scope `customer.read.pii`.",
            ),
            "firstName": s("string"),
            "lastName": s("string"),
            "companyName": s(["string", "null"]),
            "email": s("string", format="email"),
            "phone": s("string"),
            "address": obj(
                {"street": s("string"), "postalCode": s("string"), "city": s("string"), "country": ref("Country")}
            ),
            "preferredLanguage": enum(["sv", "nb", "fi", "da", "et", "lv", "lt", "en"]),
            "segment": s("string", "Marketing segment, e.g. `young_urban`."),
            "createdAt": s("string", format="date-time"),
        },
        ["customerId", "type"],
        "A private or business customer.",
    )
    return document(
        title="Customer API",
        version="2.8.0",
        description=(
            "Golden record for customers across all markets (v1 was retired in 2025).\n\n"
            "National identity numbers are masked unless the token carries `customer.read.pii`, "
            "which requires a DPO-approved purpose."
        ),
        server_path="/customer/v2",
        scopes={
            "customer.read": "Read customer profile (national id masked)",
            "customer.read.pii": "Read unmasked national identity number",
            "customer.write": "Update contact details",
        },
        x_nordlys=x_nordlys("customer-api", "customer", "customer-data", ALL_MARKETS),
        paths={
            "/customers": {
                "get": op(
                    "searchCustomers",
                    "Search customers",
                    description="Exact-match search. At least one filter is required.",
                    params=[
                        query_p("email", s("string")),
                        query_p("phone", s("string")),
                        query_p("nationalId", s("string"), "Requires `customer.read.pii`."),
                        CURSOR,
                        LIMIT,
                    ],
                    ok=("200", "Matches", page_of("Customer")),
                    scopes=["customer.read"],
                )
            },
            "/customers/{customerId}": {
                "get": op(
                    "getCustomer",
                    "Get customer",
                    params=[path_p("customerId", "Customer id.", s("string", format="uuid"))],
                    ok=("200", "Customer", "Customer"),
                    scopes=["customer.read"],
                ),
                "patch": op(
                    "updateCustomerContact",
                    "Update contact details",
                    description="Only email, phone, address and preferredLanguage can be changed here.",
                    params=[path_p("customerId", "Customer id.", s("string", format="uuid"))],
                    body=obj(
                        {
                            "email": s("string", format="email"),
                            "phone": s("string"),
                            "address": customer["properties"]["address"],
                            "preferredLanguage": customer["properties"]["preferredLanguage"],
                        }
                    ),
                    ok=("200", "Updated", "Customer"),
                    scopes=["customer.write"],
                ),
            },
            "/customers/{customerId}/policies": {
                "get": op(
                    "listCustomerPolicies",
                    "Policy ids held by the customer",
                    description="Convenience lookup; returns ids only. Fetch details from the Policy API v2.",
                    params=[path_p("customerId", "Customer id.", s("string", format="uuid"))],
                    ok=("200", "Policy ids", arr(s("string", format="uuid"))),
                    scopes=["customer.read"],
                )
            },
        },
        schemas={"Customer": customer},
    )


def customer_consent_api_v1() -> JSON:
    consent = obj(
        {
            "purpose": enum(
                ["marketing_email", "marketing_sms", "profiling", "partner_sharing", "health_data_processing"],
                "GDPR processing purpose.",
            ),
            "granted": s("boolean"),
            "channel": s("string", "Where the consent was captured (web, app, call_centre, broker)."),
            "capturedAt": s("string", format="date-time"),
            "withdrawnAt": s(["string", "null"], format="date-time"),
            "textVersion": s("string", "Version of the consent text shown to the customer."),
        },
        ["purpose", "granted", "capturedAt"],
    )
    return document(
        title="Customer Consent API",
        version="1.3.0",
        description=(
            "Read and record GDPR consents per customer and purpose. **Always check consent "
            "before using customer data for marketing or profiling.** Withdrawals take effect "
            "immediately and are propagated to the CRM within 15 minutes."
        ),
        server_path="/consent/v1",
        scopes={"consent.read": "Read consents", "consent.write": "Record consent changes"},
        x_nordlys=x_nordlys("customer-consent-api", "customer", "privacy-engineering", ALL_MARKETS),
        paths={
            "/customers/{customerId}/consents": {
                "get": op(
                    "getConsents",
                    "Current consents for a customer",
                    params=[path_p("customerId", schema=s("string", format="uuid"))],
                    ok=("200", "Consents", arr(ref("Consent"))),
                    scopes=["consent.read"],
                ),
                "post": op(
                    "recordConsent",
                    "Grant or withdraw a consent",
                    params=[path_p("customerId", schema=s("string", format="uuid"))],
                    body="Consent",
                    ok=("201", "Recorded", "Consent"),
                    scopes=["consent.write"],
                ),
            },
        },
        schemas={"Consent": consent},
    )


def identity_verification_api_v1() -> JSON:
    session = obj(
        {
            "sessionId": s("string", format="uuid"),
            "method": enum(
                ["bankid_se", "bankid_no", "ftn_fi", "mitid_dk", "smart_id", "mobile_id"],
                "eID method. Smart-ID / Mobile-ID are used in the Baltics.",
            ),
            "status": enum(["pending", "complete", "failed", "expired"]),
            "autoStartToken": s(["string", "null"], "For launching the eID app on the same device."),
            "verifiedNationalId": s(["string", "null"], "Only present when status = complete."),
        },
        ["sessionId", "method", "status"],
    )
    return document(
        title="Identity Verification API",
        version="1.2.0",
        description=(
            "Strong customer authentication with the national eID schemes: BankID (SE, NO), "
            "FTN (FI), MitID (DK), Smart-ID and Mobile-ID (EE, LV, LT). Wraps the eID broker so "
            "product teams never integrate with each scheme directly."
        ),
        server_path="/identity-verification/v1",
        scopes={"idv.session": "Start and poll verification sessions"},
        x_nordlys=x_nordlys(
            "identity-verification-api", "customer", "identity-and-access", ALL_MARKETS, data_classification="sensitive"
        ),
        paths={
            "/sessions": {
                "post": op(
                    "startVerification",
                    "Start an eID verification",
                    body=obj({"method": session["properties"]["method"], "endUserIp": s("string")}, ["method"]),
                    ok=("201", "Session started", "VerificationSession"),
                    errors=["400", "401", "403", "429"],
                    scopes=["idv.session"],
                )
            },
            "/sessions/{sessionId}": {
                "get": op(
                    "pollVerification",
                    "Poll session status",
                    description="Poll every 2 seconds. Sessions expire after 3 minutes.",
                    params=[path_p("sessionId", schema=s("string", format="uuid"))],
                    ok=("200", "Session", "VerificationSession"),
                    scopes=["idv.session"],
                )
            },
        },
        schemas={"VerificationSession": session},
    )


SPECS = [
    ("customer/customer-api.v2.yaml", customer_api_v2),
    ("customer/customer-consent-api.v1.yaml", customer_consent_api_v1),
    ("customer/identity-verification-api.v1.yaml", identity_verification_api_v1),
]
