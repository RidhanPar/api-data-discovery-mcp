"""Fraud signals domain. Both APIs are classified sensitive and restricted."""

from __future__ import annotations

from .builders import (
    IDEMPOTENCY_KEY,
    JSON,
    arr,
    document,
    enum,
    obj,
    op,
    path_p,
    s,
    x_nordlys,
)

ALL_MARKETS = ["SE", "NO", "FI", "DK", "EE", "LV", "LT"]


def fraud_signals_api_v1() -> JSON:
    score = obj(
        {
            "claimId": s("string", format="uuid"),
            "score": s("number", "0.0 (no indication) to 1.0 (strong indication).", minimum=0, maximum=1),
            "band": enum(["low", "medium", "high"]),
            "reasonCodes": arr(
                s("string"),
                "Explainable reason codes, e.g. `EARLY_CLAIM_AFTER_INCEPTION`, "
                "`SHARED_BANK_ACCOUNT`, `DOCUMENT_METADATA_MISMATCH`.",
            ),
            "modelVersion": s("string"),
            "scoredAt": s("string", format="date-time"),
        },
        ["claimId", "score", "band", "modelVersion"],
        "A fraud risk score. A score is a signal for a human investigator, never an automated decision.",
    )
    return document(
        title="Fraud Signals API",
        version="1.3.0",
        description=(
            "Real-time fraud risk scoring for claims. Returns a score, a band and explainable reason "
            "codes. **Restricted**: only the Special Investigations Unit (SIU) and claims handling "
            "systems may call it. Scores must never be shown to customers or used as the sole basis "
            "for rejecting a claim (GDPR Art. 22)."
        ),
        server_path="/fraud/signals/v1",
        scopes={"fraud.score.read": "Read fraud scores (SIU / claims systems only)"},
        x_nordlys=x_nordlys(
            "fraud-signals-api",
            "fraud",
            "fraud-analytics",
            ALL_MARKETS,
            data_classification="sensitive",
            audience="restricted",
        ),
        paths={
            "/claims/{claimId}/score": {
                "get": op(
                    "getFraudScore",
                    "Get the latest fraud score for a claim",
                    params=[path_p("claimId", "Claim id from the Claims API v2.", s("string", format="uuid"))],
                    ok=("200", "Score", "FraudScore"),
                    scopes=["fraud.score.read"],
                )
            },
            "/claims/{claimId}/score:recalculate": {
                "post": op(
                    "recalculateFraudScore",
                    "Force re-scoring after new information",
                    params=[path_p("claimId", schema=s("string", format="uuid"))],
                    ok=("202", "Re-scoring queued", None),
                    scopes=["fraud.score.read"],
                )
            },
        },
        schemas={"FraudScore": score},
    )


def fraud_case_referral_api_v1() -> JSON:
    referral = obj(
        {
            "referralId": s("string", format="uuid"),
            "claimId": s("string", format="uuid"),
            "referredBy": s("string", "Handler user id."),
            "reason": s("string", maxLength=2000),
            "priority": enum(["normal", "urgent"]),
            "status": enum(
                ["new", "triaged", "investigating", "closed_no_fraud", "closed_confirmed", "closed_inconclusive"]
            ),
            "assignedInvestigator": s(["string", "null"]),
        },
        ["referralId", "claimId", "status"],
    )
    return document(
        title="Fraud Case Referral API",
        version="1.0.2",
        description=(
            "claims handlers refer suspicious claims to SIU here. creates an investigation case. "
            "SIU updates status. handlers only see status, never investigation notes."
        ),
        server_path="/fraud/referrals/v1",
        scopes={"fraud.referral.create": "Refer a claim to SIU", "fraud.referral.read": "Read referral status"},
        x_nordlys=x_nordlys(
            "fraud-case-referral-api",
            "fraud",
            "special-investigations",
            ALL_MARKETS,
            data_classification="sensitive",
            audience="restricted",
        ),
        paths={
            "/referrals": {
                "post": op(
                    "createReferral",
                    "refer claim to SIU",
                    params=[IDEMPOTENCY_KEY],
                    body=obj(
                        {
                            "claimId": s("string", format="uuid"),
                            "reason": s("string"),
                            "priority": referral["properties"]["priority"],
                        },
                        ["claimId", "reason"],
                    ),
                    ok=("201", "created", "Referral"),
                    scopes=["fraud.referral.create"],
                )
            },
            "/referrals/{referralId}": {
                "get": op(
                    "getReferral",
                    "referral status",
                    params=[path_p("referralId", schema=s("string", format="uuid"))],
                    ok=("200", "referral", "Referral"),
                    scopes=["fraud.referral.read"],
                )
            },
        },
        schemas={"Referral": referral},
    )


SPECS = [
    ("fraud/fraud-signals-api.v1.yaml", fraud_signals_api_v1),
    ("fraud/fraud-case-referral-api.v1.yaml", fraud_case_referral_api_v1),
]
