"""Documents domain.

dk-document-archive-api is the deliberately *undocumented* spec: no descriptions,
no summaries, cryptic operation ids, API-key auth. Typical of a legacy system that
was wrapped in a hurry. Discovery has to work from paths and schema names alone.
"""

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
    param,
    path_p,
    query_p,
    s,
    x_nordlys,
)

ALL_MARKETS = ["SE", "NO", "FI", "DK", "EE", "LV", "LT"]


def document_api_v1() -> JSON:
    meta = obj(
        {
            "documentId": s("string", format="uuid"),
            "kind": enum(
                [
                    "policy_schedule",
                    "terms_and_conditions",
                    "invoice",
                    "claim_attachment",
                    "claim_decision_letter",
                    "green_card",
                    "other",
                ]
            ),
            "relatedTo": obj(
                {"type": enum(["policy", "claim", "customer", "invoice"]), "id": s("string")}, ["type", "id"]
            ),
            "fileName": s("string"),
            "mediaType": s("string", "e.g. application/pdf, image/jpeg"),
            "sizeBytes": s("integer"),
            "language": s("string"),
            "createdAt": s("string", format="date-time"),
            "retentionUntil": s("string", "Deleted automatically after this date (retention policy).", format="date"),
        },
        ["documentId", "kind", "relatedTo", "mediaType"],
        "Document metadata. Content is fetched separately.",
    )
    return document(
        title="Document API",
        version="1.9.0",
        description=(
            "Store and retrieve customer-facing documents and claim attachments (photos, receipts, "
            "medical certificates). Uploads are virus-scanned; files are available after status "
            "becomes `clean`. Use `/content` to download - links are pre-signed and valid 5 minutes."
        ),
        server_path="/documents/v1",
        scopes={"documents.read": "Read documents", "documents.write": "Upload documents"},
        x_nordlys=x_nordlys("document-api", "documents", "document-services", ALL_MARKETS),
        paths={
            "/documents": {
                "get": op(
                    "listDocuments",
                    "List documents related to a policy, claim or customer",
                    params=[
                        query_p("relatedType", enum(["policy", "claim", "customer", "invoice"]), required=True),
                        query_p("relatedId", s("string"), required=True),
                        query_p("kind", s("string")),
                        CURSOR,
                        LIMIT,
                    ],
                    ok=("200", "Documents", page_of("DocumentMeta")),
                    scopes=["documents.read"],
                ),
                "post": op(
                    "uploadDocument",
                    "Upload a document",
                    description="multipart/form-data upload is also accepted; JSON variant takes base64 content (max 20 MB).",
                    body=obj(
                        {
                            "kind": meta["properties"]["kind"],
                            "relatedTo": meta["properties"]["relatedTo"],
                            "fileName": s("string"),
                            "contentBase64": s("string", contentEncoding="base64"),
                        },
                        ["kind", "relatedTo", "fileName", "contentBase64"],
                    ),
                    ok=("201", "Stored, scan pending", "DocumentMeta"),
                    scopes=["documents.write"],
                ),
            },
            "/documents/{documentId}": {
                "get": op(
                    "getDocument",
                    "Get document metadata",
                    params=[path_p("documentId", schema=s("string", format="uuid"))],
                    ok=("200", "Metadata", "DocumentMeta"),
                    scopes=["documents.read"],
                )
            },
            "/documents/{documentId}/content": {
                "get": op(
                    "getDocumentContent",
                    "Get a short-lived download link",
                    params=[path_p("documentId", schema=s("string", format="uuid"))],
                    ok=(
                        "200",
                        "Pre-signed URL",
                        obj({"url": s("string", format="uri"), "expiresAt": s("string", format="date-time")}),
                    ),
                    scopes=["documents.read"],
                )
            },
        },
        schemas={"DocumentMeta": meta},
    )


def dk_document_archive_api_v1() -> JSON:
    """The spec with missing descriptions. Intentionally sparse."""
    doc_schema = {
        "type": "object",
        "properties": {
            "docno": {"type": "string"},
            "polno": {"type": "string"},
            "cpr": {"type": "string"},
            "doctype": {"type": "string"},
            "arkdato": {"type": "string"},
            "sti": {"type": "string"},
        },
    }

    def bare(op_id: str, params: list[JSON], schema: JSON) -> JSON:
        return {
            "operationId": op_id,
            "parameters": params,
            "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": schema}}}},
        }

    return document(
        title="DKARKIV",
        version="1.0",
        description=None,
        server_path="/dk/arkiv/v1",
        security_schemes={"key": {"type": "apiKey", "in": "header", "name": "X-ARKIV-KEY"}},
        default_security=[{"key": []}],
        legacy_ext={"x-team": "dk-legacy-it"},
        include_standard_schemas=False,
        paths={
            "/doc": {
                "get": bare(
                    "getDoc1",
                    [param("polno", "query", s("string")), param("cpr", "query", s("string"))],
                    arr({"$ref": "#/components/schemas/ArkDok"}),
                )
            },
            "/doc/{docno}": {"get": bare("getDoc2", [path_p("docno")], {"$ref": "#/components/schemas/ArkDok"})},
            "/doc/{docno}/fil": {"get": bare("getDoc3", [path_p("docno")], s("string"))},
        },
        schemas={"ArkDok": doc_schema},
    )


SPECS = [
    ("documents/document-api.v1.yaml", document_api_v1),
    ("documents/dk-document-archive-api.v1.yaml", dk_document_archive_api_v1),
]
