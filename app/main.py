"""FastAPI application exposing the OpenAPI contract compatibility audit."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .audit import compare_documents, validate_references
from .composer import (
    ComposeReferenceError,
    InvalidComposeRequestError,
    MAX_SERVICES,
    MIN_SERVICES,
    OperationConflictError,
    compose_gateway,
)
from .slicer import (
    InvalidDocumentError,
    InvalidSelectorError,
    SliceReferenceError,
    slice_document,
)

app = FastAPI(
    title="OpenAPI Contract Compatibility Audit API",
    version="1.0.0",
    description=(
        "Submit a baseline and a candidate OpenAPI 3.x document. The service "
        "resolves local $refs (cycle-safe), checks operation deletions, new "
        "required request parameters, and type/enum/required changes, then "
        "returns a report sorted stably by JSON Pointer. Request constraints "
        "must not be tightened; response constraints must not be loosened. "
        "POST /slice instead extracts the minimal sub-contract for a set of "
        "'METHOD /path' selectors, including inherited security and the "
        "reachable local $ref closure, and returns it with a SHA-256 digest. "
        "POST /compose merges 2..20 OpenAPI services (each with a service_id "
        "and path_prefix) into one gateway contract: paths are mounted under "
        "their normalized prefixes, root security is materialized onto "
        "operations, component names / security keys / operationIds / tags "
        "are namespaced with the service_id prefix, local components $refs "
        "are rewritten, and a provenance map traces every output path, "
        "operation and component back to its source."
    ),
)


class AuditRequest(BaseModel):
    baseline: Dict[str, Any] = Field(
        ..., description="The baseline OpenAPI 3.x JSON document."
    )
    candidate: Dict[str, Any] = Field(
        ..., description="The candidate OpenAPI 3.x JSON document."
    )


class SliceRequest(BaseModel):
    document: Dict[str, Any] = Field(
        ..., description="The OpenAPI 3.x JSON document to slice."
    )
    selectors: List[str] = Field(
        ...,
        description=(
            "Unique operation selectors of the form 'METHOD /path', e.g. "
            "'GET /pets/{id}'. Selectors are case-insensitive for the "
            "method and must match declared paths exactly."
        ),
    )


class SliceResponse(BaseModel):
    contract: Dict[str, Any] = Field(
        ...,
        description=(
            "The minimal sub-contract: openapi/info, the selected "
            "operations with their path-level fields, inherited security, "
            "used security schemes, and the reachable local $ref closure, "
            "all rebuilt at their original JSON Pointer locations."
        ),
    )
    sha256: str = Field(
        ...,
        description=(
            "SHA-256 hex digest of the canonical (sorted-key, compact) JSON "
            "encoding of 'contract'. Independent of input key or selector "
            "order."
        ),
    )


class ComposeServiceModel(BaseModel):
    service_id: str = Field(
        ...,
        description=(
            "Unique identifier of the source service; used as the "
            "'<service_id>_' namespace prefix for component names, security "
            "requirement keys, operationIds and tags."
        ),
    )
    path_prefix: str = Field(
        ...,
        description=(
            "Mount prefix for the service's paths. Normalized before "
            "merging: a leading '/' is enforced, trailing slashes are "
            "stripped, and '' or '/' mounts the service at the root."
        ),
    )
    document: Dict[str, Any] = Field(
        ..., description="The service's OpenAPI 3.x JSON document."
    )


class ComposeRequest(BaseModel):
    info: Dict[str, Any] = Field(
        ..., description="The info object of the composed gateway contract."
    )
    services: List[ComposeServiceModel] = Field(
        ...,
        description=(
            f"{MIN_SERVICES}..{MAX_SERVICES} source services to compose. "
            "service_id values must be unique."
        ),
    )


class ComposeResponse(BaseModel):
    contract: Dict[str, Any] = Field(
        ...,
        description=(
            "The composed gateway contract: the request's info, the highest "
            "source openapi version, merged tags, all paths mounted under "
            "their normalized prefixes, and all components namespaced with "
            "the '<service_id>_' prefix. Operations carry materialized "
            "security and every '#/components/...' $ref is rewritten."
        ),
    )
    provenance: Dict[str, Any] = Field(
        ...,
        description=(
            "Source mapping covering every output path, operation and "
            "component: 'paths' maps each composed path to its contributing "
            "services, 'operations' maps each composed path + method to its "
            "source operation, and 'components' maps each section + "
            "prefixed name to its source component. Every entry carries "
            "service_id and a JSON Pointer into the source document."
        ),
    )


class FindingModel(BaseModel):
    pointer: str = Field(..., description="JSON Pointer into the document")
    category: str = Field(..., description="Machine-readable change category")
    severity: Literal["breaking", "info"] = Field(
        ...,
        description="'breaking' violates the compatibility policy; "
        "'info' is a compatible change worth noting",
    )
    reason: str = Field(..., description="Human-readable explanation")


class ReferenceErrorModel(BaseModel):
    document: Literal["baseline", "candidate"]
    ref: str
    error: str


class AuditResponse(BaseModel):
    compatible: bool = Field(
        ...,
        description="True when there are no breaking findings and no "
        "unresolvable references.",
    )
    breaking_count: int
    info_count: int
    findings: List[FindingModel]


def _validate_openapi_shape(doc: Dict[str, Any], label: str) -> Optional[str]:
    """Lightweight structural validation of an OpenAPI 3.x document."""
    if not isinstance(doc, dict):
        return f"{label} must be a JSON object"
    version = doc.get("openapi")
    if not isinstance(version, str) or not version.startswith("3."):
        return (
            f"{label}.openapi must be a version string starting with '3.' "
            f"(OpenAPI 3.x), got {version!r}"
        )
    if "paths" not in doc or not isinstance(doc.get("paths"), dict):
        return f"{label}.paths must be an object"
    return None


@app.get("/", include_in_schema=False)
def root() -> Dict[str, str]:
    return {
        "service": "openapi-contract-audit",
        "docs": "/docs",
        "health": "/health",
        "audit": "POST /audit",
        "slice": "POST /slice",
        "compose": "POST /compose",
    }


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/audit",
    response_model=AuditResponse,
    responses={
        400: {"description": "The submitted document is not OpenAPI 3.x"},
        422: {"description": "A $ref cannot be resolved"},
    },
    summary="Audit candidate compatibility against baseline",
)
def audit_contract(payload: AuditRequest) -> JSONResponse:
    shape_errors = [
        message
        for message in (
            _validate_openapi_shape(payload.baseline, "baseline"),
            _validate_openapi_shape(payload.candidate, "candidate"),
        )
        if message
    ]
    if shape_errors:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_openapi_document",
                "messages": shape_errors,
            },
        )

    reference_errors = [
        *validate_references(payload.baseline, "baseline"),
        *validate_references(payload.candidate, "candidate"),
    ]
    if reference_errors:
        return JSONResponse(
            status_code=422,
            content={
                "error": "unresolvable_reference",
                "message": (
                    "one or more $ref entries could not be resolved; fix the "
                    "references before the contract can be audited"
                ),
                "references": reference_errors,
            },
        )

    findings = compare_documents(payload.baseline, payload.candidate)
    breaking = [f for f in findings if f.severity == "breaking"]
    info = [f for f in findings if f.severity == "info"]

    body = AuditResponse(
        compatible=not breaking,
        breaking_count=len(breaking),
        info_count=len(info),
        findings=[FindingModel(**f.as_dict()) for f in findings],
    )
    return JSONResponse(status_code=200, content=body.model_dump())


@app.post(
    "/slice",
    response_model=SliceResponse,
    responses={
        400: {"description": "Bad document shape, duplicate or unknown selector"},
        422: {"description": "A retained node references an unresolvable $ref"},
    },
    summary="Extract a minimal sub-contract for selected operations",
)
def slice_contract(payload: SliceRequest) -> JSONResponse:
    try:
        contract, digest = slice_document(
            payload.document, payload.selectors
        )
    except InvalidDocumentError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_openapi_document",
                "messages": exc.messages,
            },
        )
    except InvalidSelectorError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_selector", "messages": exc.messages},
        )
    except SliceReferenceError as exc:
        return JSONResponse(
            status_code=422,
            content={
                "error": "unresolvable_reference",
                "message": (
                    "a $ref reachable from the selected operations could not "
                    "be resolved; fix the reference or remove the operation "
                    "that uses it"
                ),
                "references": exc.references,
            },
        )
    return JSONResponse(
        status_code=200, content={"contract": contract, "sha256": digest}
    )


@app.post(
    "/compose",
    response_model=ComposeResponse,
    responses={
        400: {
            "description": "Malformed request: bad service count, missing/"
            "duplicate/invalid service_id, invalid path_prefix, or a "
            "document that is not OpenAPI 3.x"
        },
        409: {
            "description": "Two services declare the same composed path "
            "and HTTP method"
        },
        422: {
            "description": "A retained $ref is external, dangling, or "
            "points outside components"
        },
    },
    summary="Compose a gateway contract from multiple OpenAPI services",
)
def compose_contract(payload: ComposeRequest) -> JSONResponse:
    try:
        contract, provenance = compose_gateway(
            payload.info,
            [service.model_dump() for service in payload.services],
        )
    except InvalidComposeRequestError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_compose_request",
                "messages": exc.messages,
            },
        )
    except ComposeReferenceError as exc:
        return JSONResponse(
            status_code=422,
            content={
                "error": "unsupported_reference",
                "message": (
                    "one or more retained $ref entries are external, "
                    "unresolvable, or point outside components; only "
                    "'#/components/...' references can be rewritten into "
                    "the composed contract"
                ),
                "references": exc.references,
            },
        )
    except OperationConflictError as exc:
        return JSONResponse(
            status_code=409,
            content={
                "error": "operation_conflict",
                "message": (
                    "multiple services declare the same composed path and "
                    "HTTP method; adjust the path prefixes or drop one of "
                    "the conflicting operations"
                ),
                "conflicts": exc.conflicts,
            },
        )
    return JSONResponse(
        status_code=200,
        content={"contract": contract, "provenance": provenance},
    )
