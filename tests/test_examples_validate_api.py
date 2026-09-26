"""End-to-end tests for the ``POST /examples/validate`` endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def make_doc(
    request_content=None,
    response_content=None,
    *,
    version="3.0.3",
    components=None,
    path_item_extra=None,
) -> dict:
    """One POST /pets operation carrying the given request/response content."""
    operation = {
        "responses": {
            "200": {
                "description": "ok",
                **({"content": response_content} if response_content else {}),
            }
        }
    }
    if request_content is not None:
        operation["requestBody"] = {"content": request_content}
    path_item = {"post": operation}
    if path_item_extra:
        path_item.update(path_item_extra)
    return {
        "openapi": version,
        "info": {"title": "t", "version": "1"},
        "paths": {"/pets": path_item},
        **({"components": components} if components else {}),
    }


def validate(doc: dict):
    return client.post("/examples/validate", json={"document": doc})


def json_media(schema=None, **media_kwargs) -> dict:
    media = {}
    if schema is not None:
        media["schema"] = schema
    media.update(media_kwargs)
    return {"application/json": media}


# --------------------------------------------------------------------------- #
# General behavior
# --------------------------------------------------------------------------- #

def test_no_examples_is_valid():
    resp = validate(make_doc())
    assert resp.status_code == 200
    assert resp.json() == {"valid": True, "examples_checked": 0, "errors": []}


def test_invalid_document_shape_returns_400():
    resp = client.post("/examples/validate", json={"document": {"swagger": "2.0"}})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_openapi_document"


def test_valid_example_passes():
    doc = make_doc(
        request_content=json_media(
            {"type": "object", "required": ["name"],
             "properties": {"name": {"type": "string"}}},
            example={"name": "fido"},
        )
    )
    resp = validate(doc)
    assert resp.status_code == 200
    assert resp.json()["valid"] is True
    assert resp.json()["examples_checked"] == 1


def test_invalid_example_returns_200_with_valid_false():
    doc = make_doc(
        request_content=json_media({"type": "integer"}, example="not-an-int")
    )
    resp = validate(doc)
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert body["examples_checked"] == 1
    assert len(body["errors"]) == 1
    error = body["errors"][0]
    assert set(error) == {
        "example_pointer",
        "instance_pointer",
        "schema_pointer",
        "keyword",
        "reason",
    }
    assert error["keyword"] == "type"
    assert error["instance_pointer"] == ""
    assert error["example_pointer"].endswith("/requestBody/content/application~1json/example")
    assert error["schema_pointer"].endswith("/schema/type")


def test_errors_sorted_by_example_then_instance_pointer():
    doc = make_doc(
        request_content=json_media(
            {
                "type": "object",
                "properties": {
                    "b": {"type": "integer"},
                    "a": {"type": "integer"},
                },
            },
            examples={
                "second": {"value": {"b": "x", "a": "y"}},
                "first": {"value": {"b": "x"}},
            },
        )
    )
    body = validate(doc).json()
    keys = [(e["example_pointer"], e["instance_pointer"]) for e in body["errors"]]
    assert keys == sorted(keys)
    assert keys[0][0].endswith("/examples/first")
    assert keys[1:] == sorted(keys[1:])


def test_example_without_schema_is_counted_but_unconstrained():
    doc = make_doc(request_content=json_media(example={"anything": [1, 2]}))
    body = validate(doc).json()
    assert body["valid"] is True
    assert body["examples_checked"] == 1


def test_plus_json_media_type_is_checked_and_others_ignored():
    doc = make_doc(
        response_content={
            "application/vnd.pets+json": {"schema": {"type": "integer"}, "example": "x"},
            "text/plain": {"example": {"not": "checked"}},
        }
    )
    body = validate(doc).json()
    assert body["examples_checked"] == 1
    assert body["valid"] is False
    assert body["errors"][0]["keyword"] == "type"


# --------------------------------------------------------------------------- #
# readOnly / writeOnly / required direction rules
# --------------------------------------------------------------------------- #

PET_SCHEMA = {
    "type": "object",
    "required": ["id", "name", "secret"],
    "properties": {
        "id": {"type": "integer", "readOnly": True},
        "name": {"type": "string"},
        "secret": {"type": "string", "writeOnly": True},
    },
}


def test_readonly_property_in_request_example_is_an_error():
    doc = make_doc(
        request_content=json_media(PET_SCHEMA, example={"id": 1, "name": "f", "secret": "s"})
    )
    body = validate(doc).json()
    assert body["valid"] is False
    assert len(body["errors"]) == 1
    error = body["errors"][0]
    assert error["keyword"] == "readOnly"
    assert error["instance_pointer"] == "/id"
    assert error["schema_pointer"].endswith("/properties/id/readOnly")


def test_readonly_property_in_response_example_is_fine():
    doc = make_doc(
        response_content=json_media(PET_SCHEMA, example={"id": 1, "name": "f"})
    )
    body = validate(doc).json()
    # 'secret' is writeOnly: not required of responses, and absent anyway.
    assert body["valid"] is True, body


def test_writeonly_property_in_response_example_is_an_error():
    doc = make_doc(
        response_content=json_media(
            PET_SCHEMA, example={"id": 1, "name": "f", "secret": "s"}
        )
    )
    body = validate(doc).json()
    assert [e["keyword"] for e in body["errors"]] == ["writeOnly"]
    assert body["errors"][0]["instance_pointer"] == "/secret"


def test_writeonly_property_in_request_example_is_fine():
    doc = make_doc(
        request_content=json_media(PET_SCHEMA, example={"name": "f", "secret": "s"})
    )
    body = validate(doc).json()
    # 'id' is readOnly: not required of requests, and absent anyway.
    assert body["valid"] is True, body


def test_required_is_direction_specific():
    # Response example missing a writeOnly required property is fine.
    doc = make_doc(
        response_content=json_media(PET_SCHEMA, example={"id": 1, "name": "f"})
    )
    assert validate(doc).json()["valid"] is True
    # Request example missing a readOnly required property is fine.
    doc = make_doc(
        request_content=json_media(PET_SCHEMA, example={"name": "f", "secret": "s"})
    )
    assert validate(doc).json()["valid"] is True
    # Missing an ordinary required property is an error on both sides.
    doc = make_doc(request_content=json_media(PET_SCHEMA, example={"secret": "s"}))
    errors = validate(doc).json()["errors"]
    assert [e["keyword"] for e in errors] == ["required"]
    assert "'name'" in errors[0]["reason"]


def test_readonly_flag_seen_through_property_ref():
    doc = make_doc(
        request_content=json_media(
            {
                "type": "object",
                "properties": {"id": {"$ref": "#/components/schemas/Id"}},
            },
            example={"id": 1},
        ),
        components={
            "schemas": {"Id": {"type": "integer", "readOnly": True}}
        },
    )
    body = validate(doc).json()
    assert [e["keyword"] for e in body["errors"]] == ["readOnly"]
    assert body["errors"][0]["schema_pointer"] == "/components/schemas/Id/readOnly"


# --------------------------------------------------------------------------- #
# 3.0 nullable vs 3.1 type unions
# --------------------------------------------------------------------------- #

def test_nullable_30_accepts_null():
    doc = make_doc(
        request_content=json_media({"type": "string", "nullable": True}, example=None),
        version="3.0.3",
    )
    assert validate(doc).json()["valid"] is True


def test_type_union_31_accepts_null():
    doc = make_doc(
        request_content=json_media({"type": ["string", "null"]}, example=None),
        version="3.1.0",
    )
    assert validate(doc).json()["valid"] is True


def test_nullable_is_ignored_in_31():
    doc = make_doc(
        request_content=json_media({"type": "string", "nullable": True}, example=None),
        version="3.1.0",
    )
    body = validate(doc).json()
    assert body["valid"] is False
    assert body["errors"][0]["keyword"] == "type"


def test_enum_without_null_rejects_nullable_null():
    doc = make_doc(
        request_content=json_media(
            {"type": "string", "nullable": True, "enum": ["a", "b"]}, example=None
        ),
        version="3.0.3",
    )
    body = validate(doc).json()
    assert body["valid"] is False
    assert {e["keyword"] for e in body["errors"]} == {"enum"}


def test_integer_accepted_for_number_and_integer():
    doc = make_doc(request_content=json_media({"type": "number"}, example=5))
    assert validate(doc).json()["valid"] is True
    doc = make_doc(request_content=json_media({"type": "integer"}, example=5))
    assert validate(doc).json()["valid"] is True
    doc = make_doc(request_content=json_media({"type": "integer"}, example=5.5))
    assert validate(doc).json()["valid"] is False


def test_boolean_is_not_an_integer():
    doc = make_doc(request_content=json_media({"type": "integer"}, example=True))
    body = validate(doc).json()
    assert body["valid"] is False
    assert body["errors"][0]["keyword"] == "type"


# --------------------------------------------------------------------------- #
# externalValue / external and dangling references -> 422
# --------------------------------------------------------------------------- #

def test_external_value_returns_422():
    doc = make_doc(
        response_content=json_media(
            {"type": "object"},
            examples={"ext": {"externalValue": "https://example.com/pet.json"}},
        )
    )
    resp = validate(doc)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "unvalidatable_example"
    issue = body["issues"][0]
    assert issue["kind"] == "external-value"
    assert issue["ref"] == "https://example.com/pet.json"
    assert issue["pointer"].endswith("/examples/ext")


def test_external_example_ref_returns_422():
    doc = make_doc(
        response_content=json_media(
            {"type": "object"},
            examples={"ext": {"$ref": "https://example.com/examples.json#/Pet"}},
        )
    )
    resp = validate(doc)
    assert resp.status_code == 422
    issue = resp.json()["issues"][0]
    assert issue["kind"] == "external-reference"
    assert issue["ref"] == "https://example.com/examples.json#/Pet"


def test_dangling_example_ref_returns_422_with_source_pointer():
    doc = make_doc(
        response_content=json_media(
            {"type": "object"},
            examples={"missing": {"$ref": "#/components/examples/Gone"}},
        )
    )
    resp = validate(doc)
    assert resp.status_code == 422
    issue = resp.json()["issues"][0]
    assert issue["kind"] == "unresolvable-reference"
    assert issue["ref"] == "#/components/examples/Gone"
    assert issue["pointer"].endswith("/examples/missing/$ref")


def test_dangling_schema_ref_returns_422():
    doc = make_doc(
        response_content=json_media(
            {"$ref": "#/components/schemas/Gone"}, example={"a": 1}
        )
    )
    resp = validate(doc)
    assert resp.status_code == 422
    assert resp.json()["issues"][0]["kind"] == "unresolvable-reference"


def test_dangling_request_body_ref_returns_422():
    doc = make_doc()
    doc["paths"]["/pets"]["post"]["requestBody"] = {
        "$ref": "#/components/requestBodies/Gone"
    }
    resp = validate(doc)
    assert resp.status_code == 422
    issue = resp.json()["issues"][0]
    assert issue["kind"] == "unresolvable-reference"
    assert issue["pointer"] == "/paths/~1pets/post/requestBody"


def test_external_value_inside_referenced_example_returns_422():
    doc = make_doc(
        response_content=json_media(
            {"type": "object"},
            examples={"pet": {"$ref": "#/components/examples/Pet"}},
        ),
        components={
            "examples": {"Pet": {"externalValue": "https://example.com/pet.json"}}
        },
    )
    resp = validate(doc)
    assert resp.status_code == 422
    issue = resp.json()["issues"][0]
    assert issue["kind"] == "external-value"
    # The fix belongs at the component declaration.
    assert issue["pointer"] == "/components/examples/Pet"


# --------------------------------------------------------------------------- #
# Local $ref Example Objects
# --------------------------------------------------------------------------- #

def test_local_ref_example_object_is_resolved_and_validated():
    doc = make_doc(
        response_content=json_media(
            {
                "type": "object",
                "required": ["id"],
                "properties": {"id": {"type": "integer"}},
            },
            examples={
                "good": {"$ref": "#/components/examples/Good"},
                "bad": {"$ref": "#/components/examples/Bad"},
            },
        ),
        components={
            "examples": {
                "Good": {"summary": "a good pet", "value": {"id": 1}},
                "Bad": {"value": {"id": "nope"}},
            }
        },
    )
    body = validate(doc).json()
    assert body["examples_checked"] == 2
    assert body["valid"] is False
    assert len(body["errors"]) == 1
    error = body["errors"][0]
    assert error["example_pointer"].endswith("/examples/bad")
    assert error["instance_pointer"] == "/id"
    assert error["keyword"] == "type"


def test_schema_ref_is_followed_to_component_pointer():
    doc = make_doc(
        response_content=json_media(
            {"$ref": "#/components/schemas/Pet"}, example={"id": "nope"}
        ),
        components={
            "schemas": {
                "Pet": {"type": "object", "properties": {"id": {"type": "integer"}}}
            }
        },
    )
    body = validate(doc).json()
    error = body["errors"][0]
    assert error["schema_pointer"] == "/components/schemas/Pet/properties/id/type"


def test_referenced_request_body_and_response_are_validated():
    doc = make_doc(
        components={
            "requestBodies": {
                "PetBody": {
                    "content": {
                        "application/json": {
                            "schema": {"type": "integer"},
                            "example": "not-an-int",
                        }
                    }
                }
            },
            "responses": {
                "PetOut": {
                    "description": "ok",
                    "content": {
                        "application/json": {
                            "schema": {"type": "string"},
                            "example": 42,
                        }
                    },
                }
            },
        }
    )
    doc["paths"]["/pets"]["post"]["requestBody"] = {
        "$ref": "#/components/requestBodies/PetBody"
    }
    doc["paths"]["/pets"]["post"]["responses"]["200"] = {
        "$ref": "#/components/responses/PetOut"
    }
    body = validate(doc).json()
    assert body["valid"] is False
    assert body["examples_checked"] == 2
    keywords = {e["keyword"] for e in body["errors"]}
    assert keywords == {"type"}


def test_recursive_schema_example_does_not_explode():
    doc = make_doc(
        response_content=json_media(
            {"$ref": "#/components/schemas/Node"},
            example={"v": 1, "child": {"v": 2, "child": {"v": "bad"}}},
        ),
        components={
            "schemas": {
                "Node": {
                    "type": "object",
                    "properties": {
                        "v": {"type": "integer"},
                        "child": {"$ref": "#/components/schemas/Node"},
                    },
                }
            }
        },
    )
    body = validate(doc).json()
    assert body["valid"] is False
    assert len(body["errors"]) == 1
    assert body["errors"][0]["instance_pointer"] == "/child/child/v"


# --------------------------------------------------------------------------- #
# Schema keyword coverage
# --------------------------------------------------------------------------- #

def test_numeric_string_and_array_constraints():
    doc = make_doc(
        request_content=json_media(
            {
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "minimum": 0, "maximum": 10,
                          "multipleOf": 2},
                    "s": {"type": "string", "minLength": 2, "maxLength": 4,
                          "pattern": "^a"},
                    "arr": {"type": "array", "minItems": 1, "maxItems": 2,
                            "uniqueItems": True, "items": {"type": "integer"}},
                },
            },
            example={"n": 5, "s": "z", "arr": [1, 1, 3]},
        )
    )
    keywords = {(e["instance_pointer"], e["keyword"]) for e in validate(doc).json()["errors"]}
    assert ("/n", "multipleOf") in keywords
    assert ("/s", "minLength") in keywords
    assert ("/s", "pattern") in keywords
    assert ("/arr", "maxItems") in keywords
    assert ("/arr/1", "uniqueItems") in keywords
    assert not any(pointer == "/n" and kw in ("minimum", "maximum")
                   for pointer, kw in keywords)


def test_exclusive_bounds_30_boolean_and_31_numeric_forms():
    doc = make_doc(
        request_content=json_media(
            {"type": "number", "minimum": 5, "exclusiveMinimum": True},
            example=5,
        ),
        version="3.0.3",
    )
    errors = validate(doc).json()["errors"]
    assert [e["keyword"] for e in errors] == ["minimum"]

    doc = make_doc(
        request_content=json_media(
            {"type": "number", "exclusiveMinimum": 5}, example=5
        ),
        version="3.1.0",
    )
    errors = validate(doc).json()["errors"]
    assert [e["keyword"] for e in errors] == ["exclusiveMinimum"]


def test_enum_and_const():
    doc = make_doc(
        request_content=json_media({"enum": ["a", "b"]}, example="c")
    )
    assert validate(doc).json()["errors"][0]["keyword"] == "enum"
    doc = make_doc(
        request_content=json_media({"const": {"x": 1}}, example={"x": 2}),
        version="3.1.0",
    )
    assert validate(doc).json()["errors"][0]["keyword"] == "const"


def test_additional_properties_false():
    doc = make_doc(
        request_content=json_media(
            {"type": "object", "additionalProperties": False},
            example={"surprise": 1},
        )
    )
    errors = validate(doc).json()["errors"]
    assert [e["keyword"] for e in errors] == ["additionalProperties"]
    assert errors[0]["instance_pointer"] == "/surprise"


def test_allof_merges_object_constraints():
    doc = make_doc(
        request_content=json_media(
            {
                "allOf": [
                    {"type": "object",
                     "properties": {"id": {"type": "integer", "readOnly": True}}},
                    {"type": "object", "required": ["name"],
                     "properties": {"name": {"type": "string"}}},
                ]
            },
            example={"id": 1},
        )
    )
    keywords = {e["keyword"] for e in validate(doc).json()["errors"]}
    assert keywords == {"readOnly", "required"}


def test_anyof_and_oneof():
    doc = make_doc(
        request_content=json_media(
            {"anyOf": [{"type": "string"}, {"type": "integer"}]}, example=True
        )
    )
    assert [e["keyword"] for e in validate(doc).json()["errors"]] == ["anyOf"]

    doc = make_doc(
        request_content=json_media(
            {"oneOf": [{"type": "string"}, {"type": "integer"}]}, example=True
        )
    )
    assert [e["keyword"] for e in validate(doc).json()["errors"]] == ["oneOf"]

    # Matching more than one oneOf branch is also an error.
    doc = make_doc(
        request_content=json_media(
            {"oneOf": [{"type": "integer", "minimum": 0}, {"type": "integer"}]},
            example=5,
        )
    )
    errors = validate(doc).json()["errors"]
    assert [e["keyword"] for e in errors] == ["oneOf"]
    assert "exactly one" in errors[0]["reason"]


def test_prefix_items_31():
    doc = make_doc(
        request_content=json_media(
            {"prefixItems": [{"type": "integer"}, {"type": "string"}],
             "items": False},
            example=[1, "ok", "extra"],
        ),
        version="3.1.0",
    )
    errors = validate(doc).json()["errors"]
    assert [e["keyword"] for e in errors] == ["items"]
    assert errors[0]["instance_pointer"] == "/2"


def test_empty_example_object_is_skipped():
    doc = make_doc(
        response_content=json_media(
            {"type": "object"},
            examples={"empty": {"summary": "no value here"}},
        )
    )
    body = validate(doc).json()
    assert body["valid"] is True
    assert body["examples_checked"] == 0


def test_null_value_example_is_distinguished_from_missing():
    doc = make_doc(
        response_content=json_media({"type": "null"}, example=None),
        version="3.1.0",
    )
    body = validate(doc).json()
    assert body["valid"] is True
    assert body["examples_checked"] == 1


def test_circular_example_ref_returns_422():
    doc = make_doc(
        response_content=json_media(
            {"type": "object"},
            examples={"pet": {"$ref": "#/components/examples/A"}},
        ),
        components={
            "examples": {
                "A": {"$ref": "#/components/examples/B"},
                "B": {"$ref": "#/components/examples/A"},
            }
        },
    )
    resp = validate(doc)
    assert resp.status_code == 422
    issue = resp.json()["issues"][0]
    assert issue["kind"] == "circular-reference"
    assert issue["pointer"].endswith("/examples/pet/$ref")


def test_root_and_health_advertise_examples_endpoint():
    assert client.get("/").json()["examples"] == "POST /examples/validate"
    assert client.get("/health").json() == {"status": "ok"}
