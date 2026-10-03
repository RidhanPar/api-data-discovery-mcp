"""Payments domain: premium invoices, claim payouts, payment methods."""

from __future__ import annotations

from .builders import (
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

ALL_MARKETS = ["SE", "NO", "FI", "DK", "EE", "LV", "LT"]


def premium_invoice_api_v1() -> JSON:
    invoice = obj(
        {
            "invoiceId": s("string", format="uuid"),
            "customerId": s("string", format="uuid"),
            "policyIds": arr(s("string", format="uuid")),
            "amount": ref("Money"),
            "dueDate": s("string", format="date"),
            "status": enum(["open", "paid", "overdue", "reminded", "sent_to_collection", "credited"]),
            "paymentReference": s(
                "string", "Structured reference: OCR (SE), KID (NO), viitenumero (FI), FIK/+71 (DK), viitenumber (EE)."
            ),
            "distribution": enum(["e_invoice", "email", "paper", "direct_debit"]),
        },
        ["invoiceId", "customerId", "amount", "dueDate", "status"],
    )
    return document(
        title="Premium Invoice API",
        version="1.7.0",
        description=(
            "Premium invoicing and accounts receivable. Read invoices for a customer, see payment "
            "status, and calculate refunds of unearned premium on cancellation."
        ),
        server_path="/billing/invoices/v1",
        scopes={"billing.read": "Read invoices", "billing.write": "Create credit notes"},
        x_nordlys=x_nordlys("premium-invoice-api", "payments", "billing", ALL_MARKETS),
        paths={
            "/invoices": {
                "get": op(
                    "listInvoices",
                    "List invoices",
                    params=[
                        query_p("customerId", s("string", format="uuid"), required=True),
                        query_p("status", s("string")),
                        CURSOR,
                        LIMIT,
                    ],
                    ok=("200", "Invoices", page_of("Invoice")),
                    scopes=["billing.read"],
                )
            },
            "/invoices/{invoiceId}": {
                "get": op(
                    "getInvoice",
                    "Get invoice",
                    params=[path_p("invoiceId", schema=s("string", format="uuid"))],
                    ok=("200", "Invoice", "Invoice"),
                    scopes=["billing.read"],
                )
            },
            "/refund-calculations": {
                "post": op(
                    "calculateRefund",
                    "Calculate unearned-premium refund",
                    description="Pro-rata by day. Does not create a credit note - it only calculates.",
                    body=obj(
                        {"policyId": s("string", format="uuid"), "cancellationDate": s("string", format="date")},
                        ["policyId", "cancellationDate"],
                    ),
                    ok=("200", "Refund", obj({"refund": ref("Money"), "days": s("integer")})),
                    scopes=["billing.read"],
                )
            },
        },
        schemas={"Invoice": invoice},
    )


def claims_payout_api_v1() -> JSON:
    payout = obj(
        {
            "payoutId": s("string", format="uuid"),
            "claimId": s("string", format="uuid"),
            "payee": obj(
                {
                    "type": enum(["policyholder", "repair_shop", "third_party", "healthcare_provider"]),
                    "name": s("string"),
                    "iban": s("string", "IBAN, or Swedish bankgiro/clearing+account in `localAccount`."),
                    "localAccount": s(["string", "null"]),
                }
            ),
            "amount": ref("Money"),
            "status": enum(["requested", "four_eyes_pending", "approved", "sent", "settled", "failed"]),
            "requestedBy": s("string"),
            "approvedBy": s(["string", "null"]),
        },
        ["payoutId", "claimId", "amount", "status"],
    )
    return document(
        title="Claims Payout API",
        version="1.1.0",
        description=(
            "Pay out claim compensation. Amounts above the handler's authority limit require "
            "four-eyes approval by a second handler before the payment is released. Uses instant "
            "payments where the market supports it (Swish, Vipps, MobilePay, SEPA Instant)."
        ),
        server_path="/claims-payout/v1",
        scopes={
            "payout.request": "Request a payout",
            "payout.approve": "Approve payouts (four-eyes)",
            "payout.read": "Read payouts",
        },
        x_nordlys=x_nordlys(
            "claims-payout-api", "payments", "claims-finance", ALL_MARKETS, data_classification="sensitive"
        ),
        paths={
            "/payouts": {
                "post": op(
                    "requestPayout",
                    "Request a claim payout",
                    params=[IDEMPOTENCY_KEY],
                    body=obj(
                        {
                            "claimId": s("string", format="uuid"),
                            "payee": payout["properties"]["payee"],
                            "amount": ref("Money"),
                        },
                        ["claimId", "payee", "amount"],
                    ),
                    ok=("201", "Payout requested", "Payout"),
                    scopes=["payout.request"],
                ),
                "get": op(
                    "listPayouts",
                    "List payouts for a claim",
                    params=[query_p("claimId", s("string", format="uuid"), required=True)],
                    ok=("200", "Payouts", arr(ref("Payout"))),
                    scopes=["payout.read"],
                ),
            },
            "/payouts/{payoutId}/approval": {
                "post": op(
                    "approvePayout",
                    "Approve a payout (second handler)",
                    description="The approver must be a different user than the requester; otherwise 403.",
                    params=[path_p("payoutId", schema=s("string", format="uuid"))],
                    ok=("200", "Approved", "Payout"),
                    scopes=["payout.approve"],
                )
            },
        },
        schemas={"Payout": payout},
    )


def payment_methods_api_v1() -> JSON:
    mandate = obj(
        {
            "mandateId": s("string", format="uuid"),
            "customerId": s("string", format="uuid"),
            "scheme": enum(
                ["autogiro_se", "avtalegiro_no", "betalingsservice_dk", "e_invoice_fi", "sepa_direct_debit", "card"]
            ),
            "status": enum(["pending", "active", "rejected", "cancelled"]),
            "maskedAccount": s("string", "e.g. ****1234"),
            "createdAt": s("string", format="date-time"),
        },
        ["mandateId", "customerId", "scheme", "status"],
    )
    return document(
        title="Payment Methods API",
        version="1.4.0",
        description=(
            "Manage how customers pay their premiums: direct-debit mandates (Autogiro, AvtaleGiro, "
            "Betalingsservice, SEPA DD), e-invoice agreements (Finland) and stored cards."
        ),
        server_path="/payment-methods/v1",
        scopes={"payment-methods.read": "Read mandates", "payment-methods.write": "Create/cancel mandates"},
        x_nordlys=x_nordlys("payment-methods-api", "payments", "billing", ALL_MARKETS),
        paths={
            "/customers/{customerId}/mandates": {
                "get": op(
                    "listMandates",
                    "List mandates",
                    params=[path_p("customerId", schema=s("string", format="uuid"))],
                    ok=("200", "Mandates", arr(ref("Mandate"))),
                    scopes=["payment-methods.read"],
                ),
                "post": op(
                    "createMandate",
                    "Create mandate",
                    description="Mandate becomes `active` when the bank confirms (1-3 banking days).",
                    params=[path_p("customerId", schema=s("string", format="uuid")), IDEMPOTENCY_KEY],
                    body=obj(
                        {"scheme": mandate["properties"]["scheme"], "accountNumber": s("string")},
                        ["scheme", "accountNumber"],
                    ),
                    ok=("201", "Pending mandate", "Mandate"),
                    scopes=["payment-methods.write"],
                ),
            },
            "/mandates/{mandateId}": {
                "delete": op(
                    "cancelMandate",
                    "Cancel mandate",
                    params=[path_p("mandateId", schema=s("string", format="uuid"))],
                    ok=("204", "Cancelled", None),
                    scopes=["payment-methods.write"],
                )
            },
        },
        schemas={"Mandate": mandate},
    )


SPECS = [
    ("payments/premium-invoice-api.v1.yaml", premium_invoice_api_v1),
    ("payments/claims-payout-api.v1.yaml", claims_payout_api_v1),
    ("payments/payment-methods-api.v1.yaml", payment_methods_api_v1),
]
