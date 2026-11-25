# Runs in eryx with the built wheel: validates and serializes through the
# Rust core (pydantic_core has no pure-Python fallback)
from pydantic_core import SchemaValidator, SchemaSerializer, ValidationError, core_schema, from_json, to_json

schema = core_schema.typed_dict_schema({
    "name": core_schema.typed_dict_field(core_schema.str_schema(min_length=1)),
    "age": core_schema.typed_dict_field(core_schema.int_schema(ge=0)),
    "tags": core_schema.typed_dict_field(core_schema.list_schema(core_schema.str_schema())),
})
validator = SchemaValidator(schema)

assert validator.validate_python({"name": "Ada", "age": "36", "tags": ["x"]}) == {"name": "Ada", "age": 36, "tags": ["x"]}
assert validator.validate_json(b'{"name": "Bo", "age": 7, "tags": []}') == {"name": "Bo", "age": 7, "tags": []}
try:
    validator.validate_python({"name": "", "age": -1, "tags": "no"})
except ValidationError as e:
    assert sorted(error["loc"][0] for error in e.errors()) == ["age", "name", "tags"], e.errors()
else:
    raise AssertionError("invalid data validated")

serializer = SchemaSerializer(schema)
assert serializer.to_json({"name": "Ada", "age": 36, "tags": []}) == b'{"name":"Ada","age":36,"tags":[]}'
assert from_json(to_json({"a": [1, 2.5, None]})) == {"a": [1, 2.5, None]}
print("pydantic_core", __import__("pydantic_core").__version__)
