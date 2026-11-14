# Runs in eryx with the built wheel: numpy's C extensions do all of this
import numpy as np
from numpy._core import _multiarray_umath

assert _multiarray_umath.__file__.endswith(".so"), _multiarray_umath.__file__

# Arrays, broadcasting and ufuncs
a = np.arange(12, dtype=np.float64).reshape(3, 4)
assert a.sum() == 66 and a.shape == (3, 4)
assert (a.T @ a).shape == (4, 4)
assert np.allclose(np.sin(a) ** 2 + np.cos(a) ** 2, 1)
assert (a + np.array([1, 2, 3, 4])).tolist()[0] == [1, 3, 5, 7]
assert np.array_equal(np.sort(np.array([3, 1, 2])), [1, 2, 3])
assert np.unique(np.array([3, 1, 3, 2, 1])).tolist() == [1, 2, 3]
assert np.array(["b", "a"]).astype("U").tolist() == ["b", "a"]
assert np.float16(1.5) * 2 == 3
assert np.longdouble(1) / 3 != np.float64(1) / 3  # binary128 long double

# Linear algebra (numpy's bundled lapack_lite)
m = np.array([[4.0, 1.0], [2.0, 3.0]])
assert np.allclose(np.linalg.inv(m) @ m, np.eye(2))
assert np.isclose(np.linalg.det(m), 10)
assert np.allclose(sorted(np.linalg.eigvals(m)), [2, 5])
assert np.allclose(m @ np.linalg.solve(m, [1, 2]), [1, 2])

# FFT (pocketfft, C++)
x = np.random.default_rng(0).random(64)
assert np.allclose(np.fft.ifft(np.fft.fft(x)).real, x)

# Random
rng = np.random.default_rng(42)
assert rng.integers(0, 10, size=5).tolist() == np.random.default_rng(42).integers(0, 10, size=5).tolist()
assert abs(rng.normal(size=10_000).mean()) < 0.05
np.random.seed(1)
assert 0 <= np.random.rand() < 1

print("numpy", np.__version__, "from", np.__file__)
