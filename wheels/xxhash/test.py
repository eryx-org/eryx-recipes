# Runs in eryx with the built wheel: xxhash has no pure-Python fallback
# (xxhash/__init__.py only re-exports xxhash._xxhash), so every hash here
# runs the C extension and the bundled xxHash library. Known digests are from
# the package's README and tests.
import xxhash
import xxhash._xxhash as ext

assert ext.__file__.endswith(".so"), ext.__file__
assert xxhash.VERSION == "4.1.0", xxhash.VERSION
assert xxhash.XXHASH_VERSION == "0.8.4", xxhash.XXHASH_VERSION

# xxh32
x = xxhash.xxh32()
x.update(b"Nobody inspects")
x.update(b" the spammish repetition")
assert x.digest() == b"\xe2);/", x.digest()
assert x.hexdigest() == "e2293b2f"
assert (x.digest_size, x.block_size) == (4, 16)
assert xxhash.xxh32_hexdigest(b"I want an unsigned 32-bit seed!", seed=1) == "d8d4b4ba"
assert xxhash.xxh32_hexdigest(b"I want an unsigned 32-bit seed!", seed=2**32 + 1) == "d8d4b4ba"
assert xxhash.xxh32_hexdigest(b"I want an unsigned 32-bit seed!") == "f7a35af8"

# xxh64
assert xxhash.xxh64(b"xxhash").hexdigest() == "32dd38952c4bc720"
assert xxhash.xxh64_intdigest(b"xxhash", seed=20141025) == 13067679811253438005
assert xxhash.xxh64_digest(b"xxhash", seed=20141025) == b"\xb5Y\xb9\x8d\x84N\x065"
assert xxhash.xxh64().intdigest() == 17241709254077376921
assert xxhash.xxh64_hexdigest(b"I want an unsigned 64-bit seed!", seed=2**64 + 1) == "ce5087f12470d961"

# xxh3 (64 and 128)
assert xxhash.xxh3_64_intdigest(b"a") == 16629034431890738719
assert xxhash.xxh3_64(b"a", 1).intdigest() == 15201566949650179872
assert xxhash.xxh3_64_intdigest(b"a", 2**64 - 1) == 4875116479388997462
assert xxhash.xxh3_128_intdigest(b"a") == 225219434562328483135862406050043285023
assert xxhash.xxh128(b"a", 1).intdigest() == 337425133163118381928709500770786453280
assert xxhash.xxh3_128_intdigest(b"a", 2**64 - 1) == 198297796855923085494266857744987477846

# Streaming (in uneven chunks, past the GIL-release threshold and xxh3's
# 1 KiB stripes), copy() and reset() agree with the one-shot functions
data = bytes(range(256)) * 4099
for name, size in (("xxh32", 4), ("xxh64", 8), ("xxh3_64", 8), ("xxh3_128", 16)):
    cls = getattr(xxhash, name)
    oneshot = getattr(xxhash, name + "_digest")
    for seed in (0, 7, 2**31 + 5):
        h = cls(seed=seed)
        i = 0
        for n in (1, 3, 15, 64, 1000, 5000, 70000):
            h.update(data[i:i + n])
            i += n
        snapshot = h.copy()
        h.update(memoryview(data)[i:])
        want = oneshot(data, seed=seed)
        assert len(want) == size
        assert h.digest() == want, (name, seed)
        assert cls(data, seed).digest() == want, (name, seed)
        assert h.intdigest() == int.from_bytes(want, "big")
        assert snapshot.digest() == oneshot(data[:i], seed=seed), (name, seed)
        h.reset()
        assert h.digest() == cls(seed=seed).digest()
    assert h.name == name.upper(), h.name
    assert h.digest_size == size

try:
    xxhash.xxh64("not bytes")
except TypeError:
    pass
else:
    raise AssertionError("hashed a str")

print("xxhash", xxhash.VERSION, "(xxHash", xxhash.XXHASH_VERSION + ") from", ext.__file__)
