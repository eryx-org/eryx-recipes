# Runs in eryx with the built wheel: ujson is only an extension module (no
# pure-Python fallback), so every call here runs its C, and the float
# formatting and parsing run the bundled double-conversion (C++).
import math

import ujson

assert ujson.__file__.endswith(".so"), ujson.__file__

doc = {"a": [1, 2.5, None, True, False], "b": "héllo \U0001f600", "c": {"d": -(2**63)}}
text = ujson.dumps(doc, sort_keys=True)
assert text == '{"a":[1,2.5,null,true,false],"b":"h\\u00e9llo \\ud83d\\ude00","c":{"d":-9223372036854775808}}', text
assert ujson.loads(text) == doc
assert ujson.dumps(doc, ensure_ascii=False, sort_keys=True).count("\U0001f600") == 1
assert ujson.dumps([1, {"x": 2}], indent=2) == '[\n  1,\n  {\n    "x": 2\n  }\n]'
assert ujson.dumps("</script>", escape_forward_slashes=False) == '"</script>"'
assert ujson.dumps("</script>") == '"<\\/script>"'

# double-conversion: shortest round-trip output and correct parsing
for x in (0.1, 1 / 3, 1e300, 5e-324, 123456789.125, -0.0):
    s = ujson.dumps(x)
    assert ujson.loads(s) == x and math.copysign(1, ujson.loads(s)) == math.copysign(1, x), s
assert ujson.dumps(0.1) == "0.1"
assert ujson.loads("1.7976931348623157e308") == 1.7976931348623157e308
assert ujson.loads("2" * 40) == int("2" * 40)

for bad in ('{"a": ', "[1,]", "nul", '"\\x"'):
    try:
        ujson.loads(bad)
    except ujson.JSONDecodeError:
        pass
    else:
        raise AssertionError(f"accepted {bad!r}")
try:
    ujson.dumps(float("nan"), allow_nan=False)
except OverflowError:
    pass
else:
    raise AssertionError("encoded nan")

class Custom:
    def __json__(self):
        return '{"custom":1}'

assert ujson.dumps([Custom()]) == '[{"custom":1}]'
assert ujson.dumps(object(), default=lambda o: "obj") == '"obj"'

print("ujson", ujson.__version__, "from", ujson.__file__)
