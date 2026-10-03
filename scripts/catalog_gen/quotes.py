"""Quotes domain: quote-api v1 (deprecated, sunset soon) and v2, travel quotes."""

from __future__ import annotations

from .builders import (
    IDEMPOTENCY_KEY,
    JSON,
    arr,
    document,
    enum,
    mark_deprecated,
    obj,
    op,
    path_p,
    ref,
    s,
    x_nordlys,
)

NORDICS = ["SE", "NO", "FI", "DK"]
ALL_MARKETS = ["SE", "NO", "FI", "DK", "EE", "LV", "LT"]


def quote_api_v1() -> JSON:
    quote = obj(
        {
            "quoteId": s("string"),
            "product": enum(["HOME", "MOTOR"]),
            "price": s("number", "Yearly price"),
            "validTo": s("string", format="date"),
        },
        ["quoteId", "product", "price"],
    )
    doc = document(
        title="Quote API",
        version="1.12.0",
        description="Price home and motor insurance. Deprecated, replaced by Quote API v2. Sunset 2026-11-15.",
        server_path="/quote/v1",
        scopes={"quote.create": "Create quotes"},
        x_nordlys=x_nordlys(
            "quote-api",
            "quotes",
            "pricing-and-quotes",
            NORDICS,
            sunset="2026-11-15",
            deprecated_since="2025-11-15",
            replacement="quote-api v2",
            data_classification="personal",
        ),
        paths={
            "/quote": {
                "post": op(
                    "createQuote",
                    "Get price",
                    description="Send risk data as free-form key/values; validation errors come back as 200 with `error` field.",
                    body=obj(
                        {
                            "product": enum(["HOME", "MOTOR"]),
                            "country": s("string"),
                            "risk": s("object", "key/value risk data"),
                        },
                        ["product", "risk"],
                    ),
                    ok=("200", "Price", "Quote"),
                    scopes=["quote.create"],
                )
            },
            "/quote/{quoteId}": {
                "get": op(
                    "getQuote",
                    "Get quote",
                    params=[path_p("quoteId")],
                    ok=("200", "Quote", "Quote"),
                    scopes=["quote.create"],
                )
            },
        },
        schemas={"Quote": quote},
    )
    return mark_deprecated(doc, "Sun, 15 Nov 2026 00:00:00 GMT")


def quote_api_v2() -> JSON:
    home_risk = obj(
        {
            "postalCode": s("string"),
            "livingAreaSqm": s("integer", minimum=10, maximum=1000),
            "buildingType": enum(["apartment", "detached_house", "terraced_house", "holiday_home"]),
            "constructionYear": s("integer"),
            "residents": s("integer", minimum=1),
        },
        ["postalCode", "livingAreaSqm", "buildingType"],
        "Risk data for home insurance.",
    )
    motor_risk = obj(
        {
            "registrationNumber": s(
                "string", "Vehicle registration plate; vehicle data is looked up in the national register."
            ),
            "annualMileageKm": s("integer"),
            "driverBirthYear": s("integer"),
            "bonusClass": s("integer", "No-claims bonus class (market-specific scale)."),
        },
        ["registrationNumber", "annualMileageKm"],
        "Risk data for motor insurance.",
    )
    quote = obj(
        {
            "quoteId": s("string", format="uuid"),
            "productLine": enum(["home", "motor"]),
            "country": ref("Country"),
            "options": arr(
                obj(
                    {
                        "packageCode": s("string", "e.g. BASIC, STANDARD, PLUS"),
                        "annualPremium": ref("Money"),
                        "monthlyPremium": ref("Money"),
                        "coverages": arr(s("string")),
                    }
                ),
                "Priced package alternatives.",
            ),
            "validUntil": s("string", "Price guaranteed until.", format="date-time"),
            "status": enum(["priced", "declined", "referred", "bound", "expired"]),
            "declineReason": s(["string", "null"]),
        },
        ["quoteId", "productLine", "country", "status"],
    )
    return document(
        title="Quote API",
        version="2.4.0",
        description=(
            "Price home and motor insurance for private customers in all markets and turn an "
            "accepted quote into a policy (bind).\n\n"
            "Changes from v1: typed risk models per product line, several priced packages per "
            "quote, proper 4xx validation errors instead of `200 + error`, Baltic markets added."
        ),
        server_path="/quote/v2",
        scopes={"quote.create": "Create and read quotes", "quote.bind": "Bind a quote into a policy"},
        x_nordlys=x_nordlys("quote-api", "quotes", "pricing-and-quotes", ALL_MARKETS),
        paths={
            "/quotes/home": {
                "post": op(
                    "quoteHome",
                    "Price home insurance",
                    params=[IDEMPOTENCY_KEY],
                    body=obj(
                        {"country": ref("Country"), "risk": ref("HomeRisk"), "customerId": s("string", format="uuid")},
                        ["country", "risk"],
                    ),
                    ok=("201", "Quote created", "Quote"),
                    errors=["400", "401", "403", "429"],
                    scopes=["quote.create"],
                )
            },
            "/quotes/motor": {
                "post": op(
                    "quoteMotor",
                    "Price motor insurance",
                    params=[IDEMPOTENCY_KEY],
                    body=obj(
                        {"country": ref("Country"), "risk": ref("MotorRisk"), "customerId": s("string", format="uuid")},
                        ["country", "risk"],
                    ),
                    ok=("201", "Quote created", "Quote"),
                    errors=["400", "401", "403", "429"],
                    scopes=["quote.create"],
                )
            },
            "/quotes/{quoteId}": {
                "get": op(
                    "getQuote",
                    "Get a quote",
                    params=[path_p("quoteId", "Quote id.", s("string", format="uuid"))],
                    ok=("200", "The quote", "Quote"),
                    scopes=["quote.create"],
                )
            },
            "/quotes/{quoteId}/bind": {
                "post": op(
                    "bindQuote",
                    "Bind a quote into a policy",
                    description="Creates the policy in the Policy API. The chosen package must still be within `validUntil`.",
                    params=[path_p("quoteId", "Quote id.", s("string", format="uuid")), IDEMPOTENCY_KEY],
                    body=obj(
                        {
                            "packageCode": s("string"),
                            "inceptionDate": s("string", format="date"),
                            "paymentFrequency": enum(["annual", "monthly"]),
                        },
                        ["packageCode", "inceptionDate"],
                    ),
                    ok=(
                        "201",
                        "Policy created",
                        obj({"policyId": s("string", format="uuid"), "policyNumber": s("string")}),
                    ),
                    extra_responses={"409": {"description": "Quote expired or already bound."}},
                    scopes=["quote.bind"],
                )
            },
        },
        schemas={"Quote": quote, "HomeRisk": home_risk, "MotorRisk": motor_risk},
    )


def travel_quote_api_v1() -> JSON:
    return document(
        title="Travel Insurance Quote API",
        version="1.0.0",
        description=(
            "Single-trip and annual travel insurance pricing for Finland and Sweden. "
            "Bought mostly via airline and travel-agency partners. Returns price in EUR or SEK."
        ),
        server_path="/travel-quote/v1",
        scopes={"quote.create": "Create travel quotes"},
        x_nordlys=x_nordlys("travel-quote-api", "quotes", "travel-and-affinity", ["FI", "SE"], audience="partner"),
        paths={
            "/travel-quotes": {
                "post": op(
                    "quoteTravel",
                    "Quote travel insurance",
                    description="Price is per traveller group. Destinations outside Europe add a surcharge.",
                    body=obj(
                        {
                            "country": enum(["FI", "SE"]),
                            "tripType": enum(["single_trip", "annual"]),
                            "departureDate": s("string", format="date"),
                            "returnDate": s("string", format="date"),
                            "destinationRegion": enum(["nordics", "europe", "worldwide_ex_us", "worldwide"]),
                            "travellers": arr(obj({"age": s("integer", minimum=0, maximum=110)})),
                        },
                        ["country", "tripType", "destinationRegion", "travellers"],
                    ),
                    ok=(
                        "201",
                        "Priced",
                        obj(
                            {
                                "quoteId": s("string", format="uuid"),
                                "premium": ref("Money"),
                                "validUntil": s("string", format="date-time"),
                            }
                        ),
                    ),
                    errors=["400", "401", "403", "429"],
                    scopes=["quote.create"],
                )
            },
        },
        schemas={},
    )


SPECS = [
    ("quotes/quote-api.v1.yaml", quote_api_v1),
    ("quotes/quote-api.v2.yaml", quote_api_v2),
    ("quotes/travel-quote-api.v1.yaml", travel_quote_api_v1),
]
