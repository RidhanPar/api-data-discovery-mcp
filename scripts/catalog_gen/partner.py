"""Partner / broker domain."""

from __future__ import annotations

from .builders import (
    AUTH_BASE,
    CURSOR,
    IDEMPOTENCY_KEY,
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

NORDICS = ["SE", "NO", "FI", "DK"]


def broker_portfolio_api_v1() -> JSON:
    item = obj(
        {
            "policyId": s("string", format="uuid"),
            "policyNumber": s("string"),
            "clientName": s("string", "Policyholder (company or person) name."),
            "productLine": s("string"),
            "status": s("string"),
            "renewalDate": s("string", format="date"),
            "annualPremium": ref("Money"),
            "openClaimsCount": s("integer"),
        },
        ["policyId", "policyNumber", "clientName"],
    )
    return document(
        title="Broker Portfolio API",
        version="1.6.0",
        description=(
            "Lets authorised brokers (försäkringsförmedlare / forsikringsmeglere) see their book of "
            "business with Nordlys: policies they placed, upcoming renewals and open-claim counts. "
            "A broker only ever sees policies where they are broker of record."
        ),
        server_path="/partner/broker-portfolio/v1",
        scopes={"broker.portfolio.read": "Read own broker book"},
        x_nordlys=x_nordlys("broker-portfolio-api", "partner", "partner-integrations", NORDICS, audience="partner"),
        paths={
            "/portfolio": {
                "get": op(
                    "getPortfolio",
                    "List policies in the broker's book",
                    params=[
                        query_p(
                            "renewalBefore", s("string", format="date"), "Only policies renewing before this date."
                        ),
                        query_p("productLine", s("string")),
                        CURSOR,
                        LIMIT,
                    ],
                    ok=("200", "Book of business", page_of("PortfolioItem")),
                    scopes=["broker.portfolio.read"],
                )
            },
            "/portfolio/renewals": {
                "get": op(
                    "getUpcomingRenewals",
                    "Upcoming renewals in the next N days",
                    params=[query_p("days", s("integer", minimum=1, maximum=180, default=60))],
                    ok=("200", "Renewals", arr(ref("PortfolioItem"))),
                    scopes=["broker.portfolio.read"],
                )
            },
        },
        schemas={"PortfolioItem": item},
    )


def partner_quote_submission_api_v1() -> JSON:
    submission = obj(
        {
            "partnerReference": s("string", "Partner's own order/booking reference."),
            "productCode": s("string", "Embedded product agreed in the partner contract, e.g. `CAR_DEALER_MOTOR_3M`."),
            "country": ref("Country"),
            "customer": obj(
                {"firstName": s("string"), "lastName": s("string"), "email": s("string"), "nationalId": s("string")},
                ["firstName", "lastName"],
            ),
            "riskObject": s("object", "Product-specific risk object (vehicle, gadget, trip)."),
            "startDate": s("string", format="date"),
        },
        ["partnerReference", "productCode", "country", "customer", "riskObject"],
    )
    return document(
        title="Partner Quote Submission API",
        version="1.3.2",
        description=(
            "Embedded insurance: partners (car dealers, electronics retailers, travel agencies) "
            "submit quote-and-bind requests for pre-agreed products. Requires **mutual TLS** with a "
            "partner certificate *and* an OAuth client-credentials token.\n\n"
            "Not for brokers - brokers use the Broker Portfolio API and the broker portal."
        ),
        server_path="/partner/submissions/v1",
        security_schemes={
            "partnerMtls": {"type": "mutualTLS", "description": "Partner client certificate issued by Nordlys PKI."},
            "oauth2": {
                "type": "oauth2",
                "flows": {
                    "clientCredentials": {
                        "tokenUrl": f"{AUTH_BASE}/token",
                        "scopes": {"partner.submit": "Submit quote-and-bind requests"},
                    }
                },
            },
        },
        default_security=[{"partnerMtls": [], "oauth2": ["partner.submit"]}],
        x_nordlys=x_nordlys(
            "partner-quote-submission-api",
            "partner",
            "partner-integrations",
            ["SE", "NO", "FI", "DK", "EE"],
            audience="partner",
        ),
        paths={
            "/submissions": {
                "post": op(
                    "submitQuote",
                    "Submit an embedded-insurance quote and bind request",
                    params=[IDEMPOTENCY_KEY],
                    body="Submission",
                    ok=(
                        "202",
                        "Accepted",
                        obj(
                            {
                                "submissionId": s("string", format="uuid"),
                                "status": enum(["accepted", "referred", "declined"]),
                            }
                        ),
                    ),
                )
            },
            "/submissions/{submissionId}": {
                "get": op(
                    "getSubmission",
                    "Get submission status",
                    params=[path_p("submissionId", schema=s("string", format="uuid"))],
                    ok=(
                        "200",
                        "Status",
                        obj(
                            {"submissionId": s("string"), "status": s("string"), "policyNumber": s(["string", "null"])}
                        ),
                    ),
                )
            },
        },
        schemas={"Submission": submission},
    )


def broker_commission_api_v1() -> JSON:
    """Owned by Finance; still uses the old flat extension style."""
    line = obj(
        {
            "statementId": s("string"),
            "brokerId": s("string"),
            "period": s("string", "YYYY-MM"),
            "policyNumber": s("string"),
            "commissionRate": s("number"),
            "commissionAmount": ref("Money"),
            "type": enum(["new_business", "renewal", "clawback"]),
        },
        ["statementId", "brokerId", "period"],
    )
    return document(
        title="broker-commission-api",
        version="1.0.0",
        description="Commission statements for brokers. Monthly. Clawbacks shown as negative amounts.",
        server_path="/finance/broker-commission/v1",
        scopes={"broker.commission.read": "read commission statements"},
        legacy_ext={"x-owner": "finance-systems", "x-countries": "SE,NO,DK,FI", "x-domain": "Partner"},
        paths={
            "/statements": {
                "get": op(
                    "listStatements",
                    "list statements",
                    params=[query_p("brokerId", s("string"), required=True), query_p("period", s("string"))],
                    ok=("200", "ok", arr(ref("CommissionLine"))),
                    scopes=["broker.commission.read"],
                )
            },
        },
        schemas={"CommissionLine": line},
    )


SPECS = [
    ("partner/broker-portfolio-api.v1.yaml", broker_portfolio_api_v1),
    ("partner/partner-quote-submission-api.v1.yaml", partner_quote_submission_api_v1),
    ("partner/broker-commission-api.v1.yaml", broker_commission_api_v1),
]
