"""Request/response example validation for OpenAPI 3.x documents.

``POST /examples/validate`` walks every operation of a document and checks
the examples carried by JSON media types (``application/json`` and any
``*+json`` structured media type), both for ``requestBody`` content and for
response content:

* a media type may carry a single ``example`` (a raw value) and/or an
  ``examples`` map of named Example Objects whose ``value`` member supplies
  the raw value;
* an Example Object may instead live in ``components/examples`` and be
  reached through a local ``$ref`` -- such references are resolved and
  validated in place;
* ``externalValue`` URIs and non-local references cannot be fetched (the
  service never performs network access) and are rejected up front with
  HTTP 422, together with dangling references reached while the example
  value is validated against its schema.

Each example is validated as a JSON instance against the media type's
schema with the OpenAPI direction rules applied:

* ``readOnly`` properties may appear in response examples only -- sending
  one in a request example is an error;
* ``writeOnly`` properties may appear in request examples only -- a
  response example containing one is an error;
* ``required`` is direction-specific: a read-only required property is not
  required of requests, and a write-only required property is not required
  of responses;
* OpenAPI 3.0 ``nullable: true`` and the OpenAPI 3.1 ``"null"`` member of a
  type union both admit ``null`` (``nullable`` is ignored under 3.1, where
  it is not a JSON Schema 2020-12 keyword).

Schema-validation failures never make the request itself fail: the answer
stays HTTP 200 with ``valid: false`` and a flat, stably sorted list of
errors. Structural problems with the document's references (dangling or
external pointers, ``externalValue``) are a contract-level defect and are
reported with HTTP 422.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from .audit import (
    HTTP_METHODS,
    MAX_SCHEMA_DEPTH,
    _domain_contains,
    _is_openapi_31,
    ptr,
)
from .refresolver import (
    CircularReferenceError,
    RefResolver,
    ReferenceError,
)


@dataclass(frozen=True)
class ExampleError:
    """One schema-validation failure for one example instance.

    *example_pointer* locates the example declaration inside the document
    (the media-type ``example`` or one named member of ``examples``);
    *instance_pointer* locates the offending value inside the example;
    *schema_pointer* locates the responsible schema node (following
    ``$ref`` chains to the component definition, as elsewhere in the API).
    """

    example_pointer: str
    instance_pointer: str
    schema_pointer: str
    keyword: str
    reason: str

    def as_dict(self) -> Dict[str, str]:
        return {
            "example_pointer": self.example_pointer,
            "instance_pointer": self.instance_pointer,
            "schema_pointer": self.schema_pointer,
            "keyword": self.keyword,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ExampleIssue:
    """A structural problem that prevents example validation (HTTP 422)."""

    pointer: str
    kind: str
    ref: Optional[str]
    error: str

    def as_dict(self) -> Dict[str, str]:
        data: Dict[str, str] = {
            "pointer": self.pointer,
            "kind": self.kind,
            "error": self.error,
        }
        if self.ref is not None:
            data["ref"] = self.ref
        return data


class _ReferenceFailure(Exception):
    """Internal signal: a traversed reference is external, dangling or cyclic.

    Carries the :class:`ExampleIssue` describing the failing source so the
    collector can keep scanning and report every problem at once.
    """

    def __init__(self, issue: ExampleIssue) -> None:
        self.issue = issue
        super().__init__(issue.error)


def _follow_ref(
    resolver: RefResolver,
    node: Any,
    source_pointer: str,
    chain: Tuple[str, ...],
) -> Tuple[Any, str]:
    """Resolve *node* through ``$ref`` or return it unchanged.

    Mirrors :meth:`RefResolver.resolve` but translates every
    :class:`ReferenceError` into a :class:`_ReferenceFailure` annotated with
    the pointer of the node that carried the bad reference.
    """
    if not (isinstance(node, dict) and isinstance(node.get("$ref"), str)):
        return node, ""
    ref = node["$ref"]
    try:
        return resolver.resolve(node, chain)
    except CircularReferenceError as exc:
        raise _ReferenceFailure(
            ExampleIssue(
                pointer=source_pointer,
                kind="circular-reference",
                ref=exc.ref,
                error=str(exc),
            )
        ) from exc
    except ReferenceError as exc:
        kind = (
            "external-reference"
            if "non-local" in str(exc)
            else "unresolvable-reference"
        )
        raise _ReferenceFailure(
            ExampleIssue(
                pointer=source_pointer,
                kind=kind,
                ref=exc.ref,
                error=str(exc),
            )
        ) from exc


# --------------------------------------------------------------------------- #
# Example discovery
# --------------------------------------------------------------------------- #


def _effective_path_item(
    resolver: RefResolver,
    raw_item: Any,
    source_pointer: str,
    chain: Tuple[str, ...],
) -> Dict[str, Any]:
    """Resolve a path item, honoring OpenAPI 3.1 ``$ref`` siblings.

    Siblings on the referencing node win over referenced fields, exactly as
    in ``/slice`` and ``/compose``. A broken reference raises
    :class:`_ReferenceFailure`.
    """
    if not isinstance(raw_item, dict):
        return {}
    ref = raw_item.get("$ref")
    if not isinstance(ref, str):
        return raw_item
    target, _ = _follow_ref(resolver, raw_item, source_pointer + ptr("$ref"), chain)
    if not isinstance(target, dict):
        raise _ReferenceFailure(
            ExampleIssue(
                pointer=source_pointer,
                kind="unresolvable-reference",
                ref=ref,
                error=f"path item reference '{ref}' does not target an object",
            )
        )
    merged = dict(target)
    for key, value in raw_item.items():
        if key != "$ref":
            merged[key] = value
    return merged


def _materialize_example(
    resolver: RefResolver,
    example: Any,
    source_pointer: str,
) -> Optional[Tuple[Any, str]]:
    """Return ``(declaration_pointer, value)`` for an Example Object entry.

    A plain inline value (an ``examples`` map entry that is not an Example
    Object) is returned verbatim. An Example Object with an
    ``externalValue`` member cannot be fetched and raises a 422 issue, as
    does an external/dangling ``$ref``. ``None`` is returned for an empty
    Example Object (``summary``/``description`` only, no value to validate).
    """
    if not isinstance(example, dict):
        return source_pointer, example

    current = example
    resolved_pointer: Optional[str] = None
    if isinstance(example.get("$ref"), str):
        ref = example["$ref"]
        target, target_pointer = _follow_ref(
            resolver, example, source_pointer + ptr("$ref"), ()
        )
        if not isinstance(target, dict):
            # The reference target is not an Example Object; treat its raw
            # target as the example value (a dangling/external reference
            # would already have been rejected by _follow_ref).
            return source_pointer, target
        current = target
        resolved_pointer = target_pointer
        # A referenced Example Object may itself carry another $ref (rare);
        # carrying the first ref in the chain makes such a loop detectable.
        if isinstance(current.get("$ref"), str):
            further_target, further_pointer = _follow_ref(
                resolver, current, target_pointer or source_pointer, (ref,)
            )
            current = further_target
            resolved_pointer = further_pointer or resolved_pointer
            if not isinstance(current, dict):
                return source_pointer, current

    external = current.get("externalValue") if isinstance(current, dict) else None
    if isinstance(external, str) and external:
        # The fix lives where the URI is declared: inside the referenced
        # component when one was followed, otherwise at the usage site.
        location = resolved_pointer or source_pointer
        raise _ReferenceFailure(
            ExampleIssue(
                pointer=location,
                kind="external-value",
                ref=external,
                error=(
                    f"externalValue '{external}' cannot be validated: only "
                    "inline example values and document-local $ref targets "
                    "are supported (no external requests are made)"
                ),
            )
        )
    if isinstance(current, dict) and "value" in current:
        # Keep the *usage* declaration as the example pointer so a shared
        # component example used by several media types reports each usage;
        # the instance pointer then navigates the value from there.
        return source_pointer, current["value"]
    return None


def _json_media_types(content: Any) -> List[str]:
    """Sorted media-type keys that carry JSON-ish payloads."""
    if not isinstance(content, dict):
        return []
    media = ["application/json"] if "application/json" in content else []
    media += sorted(
        key
        for key in content
        if isinstance(key, str)
        and key != "application/json"
        and key.endswith("+json")
    )
    return media


def _collect_for_media(
    resolver: RefResolver,
    media_item: Any,
    media_pointer: str,
    issues: List[ExampleIssue],
) -> List[Tuple[str, Any]]:
    """Return ``(example_pointer, value)`` pairs for one media type."""
    found: List[Tuple[str, Any]] = []
    if not isinstance(media_item, dict):
        return found

    if "example" in media_item:
        # The media-type shorthand: the raw value sits directly under
        # "example" and is never an Example Object.
        found.append((media_pointer + ptr("example"), media_item["example"]))

    examples = media_item.get("examples")
    if isinstance(examples, dict):
        for name in sorted(examples):
            entry_pointer = media_pointer + ptr("examples", name)
            try:
                materialized = _materialize_example(
                    resolver, examples[name], entry_pointer
                )
            except _ReferenceFailure as failure:
                issues.append(failure.issue)
                continue
            if materialized is not None:
                found.append(materialized)
    return found


def _validate_content(
    resolver: RefResolver,
    doc: Dict[str, Any],
    content: Any,
    content_pointer: str,
    side: str,
    errors: List[ExampleError],
    issues: List[ExampleIssue],
) -> int:
    """Validate every example of every JSON media type in a content map."""
    count = 0
    for media in _json_media_types(content):
        media_item = content[media]
        media_pointer = content_pointer + ptr(media)
        schema = media_item.get("schema") if isinstance(media_item, dict) else None
        pairs = _collect_for_media(resolver, media_item, media_pointer, issues)
        count += len(pairs)
        if schema is None:
            continue
        for example_pointer, value in pairs:
            validator = _SchemaValidator(
                resolver, doc, side, example_pointer=example_pointer
            )
            try:
                validator.validate(
                    value,
                    schema,
                    schema_pointer=media_pointer + ptr("schema"),
                    instance_pointer="",
                )
            except _ReferenceFailure as failure:
                issues.append(failure.issue)
                continue
            errors.extend(validator.errors)
    return count


def validate_document_examples(
    doc: Dict[str, Any],
) -> Tuple[List[ExampleError], List[ExampleIssue], int]:
    """Validate every request/response JSON example in *doc*.

    Returns ``(errors, issues, example_count)``: schema failures, structural
    422 problems, and the number of examples examined.
    """
    resolver = RefResolver(doc, label="document")
    errors: List[ExampleError] = []
    issues: List[ExampleIssue] = []
    count = 0

    paths = doc.get("paths")
    if not isinstance(paths, dict):
        return _finalize(errors, issues, count)

    for url_path in sorted(paths):
        raw_item = paths.get(url_path)
        path_pointer = ptr("paths", url_path)
        try:
            path_item = _effective_path_item(resolver, raw_item, path_pointer, ())
        except _ReferenceFailure as failure:
            issues.append(failure.issue)
            continue

        methods = sorted(
            (key for key in path_item if key.lower() in HTTP_METHODS),
            key=str.lower,
        )
        for method in methods:
            operation = path_item[method]
            if not isinstance(operation, dict):
                continue
            op_pointer = path_pointer + ptr(method.lower())

            body = operation.get("requestBody")
            if body is not None:
                body_pointer = op_pointer + ptr("requestBody")
                try:
                    body_node, _ = _follow_ref(
                        resolver, body, body_pointer, ()
                    )
                except _ReferenceFailure as failure:
                    issues.append(failure.issue)
                else:
                    content = (
                        body_node.get("content")
                        if isinstance(body_node, dict)
                        else None
                    )
                    count += _validate_content(
                        resolver,
                        doc,
                        content,
                        body_pointer + ptr("content"),
                        "request",
                        errors,
                        issues,
                    )

            responses = operation.get("responses")
            if isinstance(responses, dict):
                for code in sorted(responses):
                    resp_pointer = op_pointer + ptr("responses", code)
                    try:
                        resp_node, _ = _follow_ref(
                            resolver, responses[code], resp_pointer, ()
                        )
                    except _ReferenceFailure as failure:
                        issues.append(failure.issue)
                        continue
                    content = (
                        resp_node.get("content")
                        if isinstance(resp_node, dict)
                        else None
                    )
                    count += _validate_content(
                        resolver,
                        doc,
                        content,
                        resp_pointer + ptr("content"),
                        "response",
                        errors,
                        issues,
                    )

    return _finalize(errors, issues, count)


def _finalize(
    errors: List[ExampleError], issues: List[ExampleIssue], count: int
) -> Tuple[List[ExampleError], List[ExampleIssue], int]:
    """Stable de-duplication and ordering of the collected results."""
    unique_errors = {
        (
            e.example_pointer,
            e.instance_pointer,
            e.schema_pointer,
            e.keyword,
            e.reason,
        ): e
        for e in errors
    }
    sorted_errors = sorted(
        unique_errors.values(),
        key=lambda e: (
            e.example_pointer,
            e.instance_pointer,
            e.schema_pointer,
            e.keyword,
            e.reason,
        ),
    )
    unique_issues = {
        (i.pointer, i.kind, i.ref or "", i.error): i for i in issues
    }
    sorted_issues = sorted(
        unique_issues.values(),
        key=lambda i: (i.pointer, i.kind, i.ref or "", i.error),
    )
    return sorted_errors, sorted_issues, count


# --------------------------------------------------------------------------- #
# Schema instance validation
# --------------------------------------------------------------------------- #


def _value_kind(value: Any) -> str:
    """JSON Schema primitive kind, with booleans distinct from numbers."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return "unknown"
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _type_gate(schema: Dict[str, Any], doc: Dict[str, Any]) -> Optional[frozenset]:
    """Accepted primitive kinds for the ``type`` gate of one schema.

    Unlike :func:`app.audit._type_domain` this ignores the enum interaction:
    an enum that omits ``null`` is reported by the enum check itself, so the
    type gate keeps admitting ``null`` via 3.0 ``nullable`` / 3.1 unions.
    """
    raw = schema.get("type")
    if raw is None:
        return None
    if isinstance(raw, str):
        tags = {raw}
    elif isinstance(raw, list):
        tags = {str(t) for t in raw}
    else:
        tags = {str(raw)}
    if not _is_openapi_31(doc) and schema.get("nullable") is True:
        tags.add("null")
    return frozenset(tags)


def _json_equal(left: Any, right: Any) -> bool:
    """JSON equality for ``enum`` / ``const`` / ``uniqueItems``."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            _json_equal(left[k], right[k]) for k in left
        )
    if isinstance(left, list):
        return (
            len(left) == len(right)
            and all(_json_equal(a, b) for a, b in zip(left, right))
        )
    return left == right


@dataclass
class _ObjectParts:
    """Effective object-level schema gathered from a node and its allOf."""

    properties: Dict[str, Any]
    property_pointers: Dict[str, str]
    required: Set[str]
    extras: Dict[str, Any]
    extra_pointers: Dict[str, str]


class _SchemaValidator:
    """Validate one example instance against one media-type schema."""

    def __init__(
        self,
        resolver: RefResolver,
        doc: Dict[str, Any],
        side: str,
        *,
        example_pointer: str,
    ) -> None:
        self.resolver = resolver
        self.doc = doc
        self.side = side
        self.example_pointer = example_pointer
        self.errors: List[ExampleError] = []
        self._emitted: Set[Tuple[str, str, str, str, str]] = set()
        # (resolved schema pointer, value identity) pairs cut the traversal
        # at recursive schemas such as Pet.parent -> Pet.
        self._visited: Set[Tuple[str, int]] = set()
        # When set, emit() records errors into the trial list instead.
        self._trial: Optional[List[ExampleError]] = None

    # -- infrastructure ---------------------------------------------------- #

    def emit(
        self,
        instance_pointer: str,
        schema_pointer: str,
        keyword: str,
        reason: str,
    ) -> None:
        """Record one error (de-duplicated)."""
        error = ExampleError(
            example_pointer=self.example_pointer,
            instance_pointer=instance_pointer,
            schema_pointer=schema_pointer,
            keyword=keyword,
            reason=reason,
        )
        key = (
            error.example_pointer,
            error.instance_pointer,
            error.schema_pointer,
            error.keyword,
            error.reason,
        )
        if key in self._emitted:
            return
        self._emitted.add(key)
        if self._trial is not None:
            self._trial.append(error)
        else:
            self.errors.append(error)

    def validate(
        self,
        value: Any,
        schema: Any,
        *,
        schema_pointer: str,
        instance_pointer: str,
        chain: Tuple[str, ...] = (),
        depth: int = 0,
        collect_object: bool = True,
    ) -> None:
        if depth > MAX_SCHEMA_DEPTH:
            self.emit(
                instance_pointer,
                schema_pointer,
                "max-depth",
                f"schema nesting exceeds {MAX_SCHEMA_DEPTH} levels; deeper "
                "constraints were not checked",
            )
            return

        target, target_pointer = _follow_ref(
            self.resolver, schema, schema_pointer, chain
        )
        eff_pointer = target_pointer or schema_pointer
        if target_pointer and isinstance(schema, dict):
            chain = chain + (schema["$ref"],)

        if target is True:
            return
        if target is False:
            self.emit(
                instance_pointer,
                eff_pointer,
                "not",
                "schema is boolean false; no value is accepted",
            )
            return
        if not isinstance(target, dict):
            return

        identity = (eff_pointer, id(value))
        if identity in self._visited:
            return
        self._visited.add(identity)

        type_ok = self._validate_scalars(value, target, eff_pointer, ip=instance_pointer)

        if value is not None and type_ok:
            kind = _value_kind(value)
            if kind == "array":
                self._validate_array(
                    value,
                    target,
                    eff_pointer,
                    instance_pointer,
                    chain,
                    depth,
                )
            elif kind == "object" and collect_object:
                parts = self._collect_object_parts(
                    target, eff_pointer, chain, depth, set()
                )
                self._validate_object(
                    value,
                    parts,
                    eff_pointer,
                    instance_pointer,
                    chain,
                    depth,
                )

            self._validate_combiners(
                value,
                target,
                eff_pointer,
                instance_pointer,
                chain,
                depth,
            )
        self._visited.discard(identity)

    # -- scalar / numeric / string constraints ----------------------------- #

    def _validate_scalars(
        self,
        value: Any,
        schema: Dict[str, Any],
        sp: str,
        ip: str,
    ) -> bool:
        """Return False when a primitive type mismatch stops further checks."""
        kind = _value_kind(value)

        domain = _type_gate(schema, self.doc)
        if domain is not None and not _domain_contains(domain, frozenset({kind})):
            expected = "|".join(sorted(domain))
            self.emit(
                ip,
                sp + ptr("type"),
                "type",
                f"expected type {expected}, got {kind}",
            )
            # A value of the wrong primitive kind cannot meaningfully fail
            # any other assertion; stop here to keep the report focused.
            return False

        enum = schema.get("enum")
        if isinstance(enum, list) and not any(_json_equal(value, v) for v in enum):
            allowed = ", ".join(sorted(repr(v) for v in enum))
            self.emit(
                ip,
                sp + ptr("enum"),
                "enum",
                f"value {value!r} is not one of the allowed values: {allowed}",
            )

        if "const" in schema and not _json_equal(value, schema["const"]):
            self.emit(
                ip,
                sp + ptr("const"),
                "const",
                f"value must equal const {schema['const']!r}",
            )

        if kind in ("number", "integer"):
            self._validate_number(value, schema, sp, ip)
        elif kind == "string":
            self._validate_string(value, schema, sp, ip)
        return True

    def _validate_number(
        self, value: Any, schema: Dict[str, Any], sp: str, ip: str
    ) -> None:
        multiple = schema.get("multipleOf")
        if isinstance(multiple, (int, float)) and not isinstance(multiple, bool) and multiple > 0:
            quotient = float(value) / float(multiple)
            if abs(quotient - round(quotient)) > 1e-9:
                self.emit(
                    ip,
                    sp + ptr("multipleOf"),
                    "multipleOf",
                    f"value {value!r} must be a multiple of {multiple}",
                )

        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and value < minimum:
            self.emit(
                ip,
                sp + ptr("minimum"),
                "minimum",
                f"value {value!r} must be greater than or equal to {minimum}",
            )
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and not isinstance(maximum, bool) and value > maximum:
            self.emit(
                ip,
                sp + ptr("maximum"),
                "maximum",
                f"value {value!r} must be less than or equal to {maximum}",
            )

        # OpenAPI 3.0 reuses "exclusiveMinimum" as a boolean modifier of
        # "minimum"; 3.1 adopts the JSON Schema numeric form.
        exclusive_min = schema.get("exclusiveMinimum")
        if isinstance(exclusive_min, bool) and exclusive_min:
            bound = schema.get("minimum")
            if isinstance(bound, (int, float)) and value <= bound:
                self.emit(
                    ip,
                    sp + ptr("minimum"),
                    "minimum",
                    f"value {value!r} must be strictly greater than {bound}",
                )
        elif (
            isinstance(exclusive_min, (int, float))
            and not isinstance(exclusive_min, bool)
            and value <= exclusive_min
        ):
            self.emit(
                ip,
                sp + ptr("exclusiveMinimum"),
                "exclusiveMinimum",
                f"value {value!r} must be strictly greater than "
                f"{exclusive_min}",
            )

        exclusive_max = schema.get("exclusiveMaximum")
        if isinstance(exclusive_max, bool) and exclusive_max:
            bound = schema.get("maximum")
            if isinstance(bound, (int, float)) and value >= bound:
                self.emit(
                    ip,
                    sp + ptr("maximum"),
                    "maximum",
                    f"value {value!r} must be strictly less than {bound}",
                )
        elif (
            isinstance(exclusive_max, (int, float))
            and not isinstance(exclusive_max, bool)
            and value >= exclusive_max
        ):
            self.emit(
                ip,
                sp + ptr("exclusiveMaximum"),
                "exclusiveMaximum",
                f"value {value!r} must be strictly less than {exclusive_max}",
            )

    def _validate_string(
        self, value: str, schema: Dict[str, Any], sp: str, ip: str
    ) -> None:
        length = len(value)
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and length < min_length:
            self.emit(
                ip,
                sp + ptr("minLength"),
                "minLength",
                f"string length {length} is shorter than minLength {min_length}",
            )
        max_length = schema.get("maxLength")
        if isinstance(max_length, int) and length > max_length:
            self.emit(
                ip,
                sp + ptr("maxLength"),
                "maxLength",
                f"string length {length} exceeds maxLength {max_length}",
            )
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            try:
                matched = re.search(pattern, value) is not None
            except re.error:
                # An invalid ECMA 262 pattern cannot be evaluated; treat it
                # as unconstrained rather than failing the request.
                matched = True
            if not matched:
                self.emit(
                    ip,
                    sp + ptr("pattern"),
                    "pattern",
                    f"string {value!r} does not match pattern {pattern!r}",
                )

    # -- arrays ------------------------------------------------------------- #

    def _validate_array(
        self,
        value: List[Any],
        schema: Dict[str, Any],
        sp: str,
        ip: str,
        chain: Tuple[str, ...],
        depth: int,
    ) -> None:
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            self.emit(
                ip,
                sp + ptr("minItems"),
                "minItems",
                f"array length {len(value)} is smaller than minItems {min_items}",
            )
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(value) > max_items:
            self.emit(
                ip,
                sp + ptr("maxItems"),
                "maxItems",
                f"array length {len(value)} exceeds maxItems {max_items}",
            )
        if schema.get("uniqueItems") is True:
            seen: List[Any] = []
            for index, item in enumerate(value):
                if any(_json_equal(item, other) for other in seen):
                    self.emit(
                        ip + ptr(index),
                        sp + ptr("uniqueItems"),
                        "uniqueItems",
                        f"array items must be unique; item {index} duplicates "
                        "an earlier item",
                    )
                    break
                seen.append(item)

        # JSON Schema 2020-12 (OpenAPI 3.1) uses "prefixItems" for the
        # positional tuple form and "items" for the remainder; 3.0 uses a
        # single "items" schema for every element.
        prefix = schema.get("prefixItems")
        if isinstance(prefix, list):
            for index, item_schema in enumerate(prefix):
                if index >= len(value):
                    break
                self.validate(
                    value[index],
                    item_schema,
                    schema_pointer=sp + ptr("prefixItems", index),
                    instance_pointer=ip + ptr(index),
                    chain=chain,
                    depth=depth + 1,
                )
        items = schema.get("items")
        if items is False:
            remainder_start = (
                len(prefix) if isinstance(prefix, list) else 0
            )
            for index in range(remainder_start, len(value)):
                self.emit(
                    ip + ptr(index),
                    sp + ptr("items"),
                    "items",
                    "no array items are allowed beyond the schema's prefix",
                )
        elif isinstance(items, dict):
            start = len(prefix) if isinstance(prefix, list) else 0
            for index in range(start, len(value)):
                self.validate(
                    value[index],
                    items,
                    schema_pointer=sp + ptr("items"),
                    instance_pointer=ip + ptr(index),
                    chain=chain,
                    depth=depth + 1,
                )

    # -- objects ------------------------------------------------------------ #

    def _collect_object_parts(
        self,
        schema: Dict[str, Any],
        sp: str,
        chain: Tuple[str, ...],
        depth: int,
        seen_pointers: Set[str],
    ) -> _ObjectParts:
        """Gather effective object constraints incl. every allOf branch.

        The gathered view powers direction-specific ``required`` filtering
        and readOnly/writeOnly detection without having to special-case
        those rules inside the allOf branches themselves.
        """
        parts = _ObjectParts({}, {}, set(), {}, {})
        self._merge_object_parts(schema, sp, chain, depth, parts, seen_pointers)
        return parts

    def _merge_object_parts(
        self,
        schema: Any,
        sp: str,
        chain: Tuple[str, ...],
        depth: int,
        parts: _ObjectParts,
        seen_pointers: Set[str],
    ) -> None:
        target, target_pointer = _follow_ref(self.resolver, schema, sp, chain)
        eff = target_pointer or sp
        if target_pointer and isinstance(schema, dict):
            chain = chain + (schema["$ref"],)
        if not isinstance(target, dict):
            return
        if eff and eff in seen_pointers:
            return
        if eff:
            seen_pointers.add(eff)

        properties = target.get("properties")
        if isinstance(properties, dict):
            for name, sub in properties.items():
                if name not in parts.properties:
                    parts.properties[name] = sub
                    parts.property_pointers[name] = eff + ptr("properties", name)
        required = target.get("required")
        if isinstance(required, list):
            parts.required.update(str(name) for name in required)
        for key in ("additionalProperties", "minProperties", "maxProperties"):
            if key in target and key not in parts.extras:
                parts.extras[key] = target[key]
                parts.extra_pointers[key] = eff + ptr(key)

        all_of = target.get("allOf")
        if isinstance(all_of, list):
            for index, branch in enumerate(all_of):
                if depth + 1 > MAX_SCHEMA_DEPTH:
                    break
                self._merge_object_parts(
                    branch,
                    eff + ptr("allOf", index),
                    chain,
                    depth + 1,
                    parts,
                    seen_pointers,
                )

    def _property_effective(
        self, raw_schema: Any, source_pointer: str, chain: Tuple[str, ...]
    ) -> Tuple[Any, str]:
        """Resolve a property schema for its readOnly/writeOnly flags."""
        return _follow_ref(self.resolver, raw_schema, source_pointer, chain)

    @staticmethod
    def _access_flag(schema: Any) -> Optional[str]:
        if isinstance(schema, dict):
            if schema.get("readOnly") is True:
                return "readOnly"
            if schema.get("writeOnly") is True:
                return "writeOnly"
        return None

    def _validate_object(
        self,
        value: Dict[str, Any],
        parts: _ObjectParts,
        sp: str,
        ip: str,
        chain: Tuple[str, ...],
        depth: int,
    ) -> None:
        # Direction-specific required: read-only members are output-only,
        # write-only members are input-only, so their absence must not be
        # flagged on the opposite side.
        for name in sorted(parts.required):
            if name in value:
                continue
            flag = None
            prop_schema = parts.properties.get(name)
            required_pointer = sp + ptr("required", name)
            if prop_schema is not None:
                prop_pointer = parts.property_pointers.get(
                    name, sp + ptr("properties", name)
                )
                effective, _ = self._property_effective(prop_schema, prop_pointer, chain)
                flag = self._access_flag(effective)
                # Point at the property declaration when one exists (its
                # readOnly/writeOnly flag and type live there); otherwise
                # point into the required list.
                required_pointer = prop_pointer
            if self.side == "request" and flag == "readOnly":
                continue
            if self.side == "response" and flag == "writeOnly":
                continue
            self.emit(
                ip,
                required_pointer,
                "required",
                f"missing required {self.side} property '{name}'",
            )

        for name in sorted(value):
            member_pointer = ip + ptr(name)
            if name in parts.properties:
                prop_schema = parts.properties[name]
                prop_pointer = parts.property_pointers[name]
                effective, eff_pointer = self._property_effective(
                    prop_schema, prop_pointer, chain
                )
                flag = self._access_flag(effective)
                if self.side == "request" and flag == "readOnly":
                    self.emit(
                        member_pointer,
                        (eff_pointer or prop_pointer) + ptr("readOnly"),
                        "readOnly",
                        "request example must not include readOnly property "
                        f"'{name}'; it is produced only in responses",
                    )
                    continue
                if self.side == "response" and flag == "writeOnly":
                    self.emit(
                        member_pointer,
                        (eff_pointer or prop_pointer) + ptr("writeOnly"),
                        "writeOnly",
                        "response example must not include writeOnly property "
                        f"'{name}'; it is accepted only in requests",
                    )
                    continue
                self.validate(
                    value[name],
                    prop_schema,
                    schema_pointer=prop_pointer,
                    instance_pointer=member_pointer,
                    chain=chain,
                    depth=depth + 1,
                )
            else:
                self._validate_additional_property(
                    value[name], name, parts, member_pointer, chain, depth
                )

        self._validate_property_counts(value, parts, ip)

    def _validate_additional_property(
        self,
        member_value: Any,
        name: str,
        parts: _ObjectParts,
        member_pointer: str,
        chain: Tuple[str, ...],
        depth: int,
    ) -> None:
        if "additionalProperties" not in parts.extras:
            return
        rule = parts.extras["additionalProperties"]
        rule_pointer = parts.extra_pointers["additionalProperties"]
        if rule is False:
            self.emit(
                member_pointer,
                rule_pointer,
                "additionalProperties",
                f"additional property '{name}' is not allowed",
            )
            return
        if isinstance(rule, dict):
            self.validate(
                member_value,
                rule,
                schema_pointer=rule_pointer,
                instance_pointer=member_pointer,
                chain=chain,
                depth=depth + 1,
            )

    def _validate_property_counts(
        self, value: Dict[str, Any], parts: _ObjectParts, ip: str
    ) -> None:
        min_props = parts.extras.get("minProperties")
        if isinstance(min_props, int) and len(value) < min_props:
            self.emit(
                ip,
                parts.extra_pointers["minProperties"],
                "minProperties",
                f"object has {len(value)} properties; minProperties requires "
                f"{min_props}",
            )
        max_props = parts.extras.get("maxProperties")
        if isinstance(max_props, int) and len(value) > max_props:
            self.emit(
                ip,
                parts.extra_pointers["maxProperties"],
                "maxProperties",
                f"object has {len(value)} properties; maxProperties allows "
                f"{max_props}",
            )

    # -- allOf / anyOf / oneOf ---------------------------------------------- #

    def _validate_combiners(
        self,
        value: Any,
        schema: Dict[str, Any],
        sp: str,
        ip: str,
        chain: Tuple[str, ...],
        depth: int,
    ) -> None:
        all_of = schema.get("allOf")
        if isinstance(all_of, list):
            for index, branch in enumerate(all_of):
                # The object-level view (properties, required,
                # additionalProperties, property counts) is enforced once
                # via the gathered parts on the parent; branches here only
                # need their scalar/array/combiner assertions.
                self.validate(
                    value,
                    branch,
                    schema_pointer=sp + ptr("allOf", index),
                    instance_pointer=ip,
                    chain=chain,
                    depth=depth + 1,
                    collect_object=False,
                )

        any_of = schema.get("anyOf")
        if isinstance(any_of, list):
            self._validate_any_of(
                value, any_of, sp + ptr("anyOf"), ip, chain, depth
            )

        one_of = schema.get("oneOf")
        if isinstance(one_of, list):
            self._validate_one_of(
                value, one_of, sp + ptr("oneOf"), ip, chain, depth
            )

    def _validate_any_of(
        self,
        value: Any,
        branches: List[Any],
        base_pointer: str,
        ip: str,
        chain: Tuple[str, ...],
        depth: int,
    ) -> None:
        for index, branch in enumerate(branches):
            if self._branch_valid(
                value, branch, base_pointer + ptr(index), ip, chain, depth
            ):
                return
        self.emit(
            ip,
            base_pointer,
            "anyOf",
            f"value does not satisfy any of the {len(branches)} anyOf schemas",
        )

    def _validate_one_of(
        self,
        value: Any,
        branches: List[Any],
        base_pointer: str,
        ip: str,
        chain: Tuple[str, ...],
        depth: int,
    ) -> None:
        matches = 0
        for index, branch in enumerate(branches):
            if self._branch_valid(
                value, branch, base_pointer + ptr(index), ip, chain, depth
            ):
                matches += 1
        if matches == 0:
            self.emit(
                ip,
                base_pointer,
                "oneOf",
                f"value does not satisfy any of the {len(branches)} oneOf schemas",
            )
        elif matches > 1:
            self.emit(
                ip,
                base_pointer,
                "oneOf",
                f"value satisfies {matches} of the {len(branches)} oneOf "
                "schemas; exactly one must match",
            )

    def _branch_valid(
        self,
        value: Any,
        branch: Any,
        branch_pointer: str,
        ip: str,
        chain: Tuple[str, ...],
        depth: int,
    ) -> bool:
        """Run one branch in trial mode: True when it produced no errors."""
        trial: List[ExampleError] = []
        previous_trial = self._trial
        saved_emitted = self._emitted
        self._trial = trial
        self._emitted = set(saved_emitted)
        try:
            self.validate(
                value,
                branch,
                schema_pointer=branch_pointer,
                instance_pointer=ip,
                chain=chain,
                depth=depth + 1,
            )
        finally:
            self._emitted = saved_emitted
            self._trial = previous_trial
        return not trial
