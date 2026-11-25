# Runs in eryx with the built wheel (and our numpy): pandas' Cython, C and C++
# extensions do all of this
import io

import numpy as np
import pandas as pd
from pandas._libs import hashtable, lib
from pandas._libs.window import aggregations

assert lib.__file__.endswith(".so") and aggregations.__file__.endswith(".so"), lib.__file__

# groupby/agg, hashing (hashtable), sorting
df = pd.DataFrame({"k": np.arange(100_000) % 7, "v": np.arange(100_000, dtype=np.float64)})
g = df.groupby("k").v.agg(["sum", "mean", "count"])
assert g.loc[0, "count"] == 14286 and g["sum"].sum() == df.v.sum()
assert pd.unique(pd.Series([3, 1, 3, 2])).tolist() == [3, 1, 2]
assert pd.util.hash_pandas_object(pd.Series(["a", "b"]), index=False).nunique() == 2

# Joins, including merge on a C int key (pandas picks its factorizer by the
# key's size, and wasm32's C long is 4 bytes) and merge_asof
left = pd.DataFrame({"key": np.array([1, 2, 3], dtype=np.intc), "a": [1, 2, 3]})
right = pd.DataFrame({"key": np.array([2, 3, 4], dtype=np.intc), "b": [20, 30, 40]})
assert pd.merge(left, right, on="key").b.tolist() == [20, 30]
quotes = pd.DataFrame({"t": [1, 5, 10], "q": [1.0, 2.0, 3.0]})
trades = pd.DataFrame({"t": [0, 6, 11]})
assert pd.merge_asof(trades, quotes, on="t").q.tolist()[1:] == [2.0, 3.0]

# Time series: parsing, time zones, resampling, time-based rolling windows,
# and the C++ skiplist behind rolling median/quantile
ts = pd.Series(np.arange(48.0), index=pd.date_range("2026-01-01", periods=48, freq="h", tz="UTC"))
assert ts.resample("6h").mean().iloc[0] == 2.5
assert ts.rolling("3h").sum().iloc[-1] == 45 + 46 + 47
assert ts.rolling(5).median().iloc[-1] == 45
assert ts.rolling(5).quantile(0.25).iloc[-1] == 44
assert ts.tz_convert("Europe/London").index[0].hour == 0
assert pd.to_datetime("2026-11-20 13:45").minute == 45
assert (pd.Timestamp("2026-03-01") - pd.Timestamp("2026-02-01")).days == 28

# The C CSV parser, without mmap or threads (patched for WASI)
parsed = pd.read_csv(io.StringIO("a,b\n1,x\n2,y\n"), engine="c")
assert parsed.a.sum() == 3 and parsed.b.tolist() == ["x", "y"]

# Pivoting, categoricals and missing values
pivot = pd.DataFrame({"r": ["x", "x", "y"], "c": ["p", "q", "p"], "v": [1, 2, 3]}).pivot_table(
    index="r", columns="c", values="v", aggfunc="sum"
)
assert pivot.loc["y", "p"] == 3 and pd.isna(pivot.loc["y", "q"])
assert pd.Categorical(["b", "a", "b"]).codes.tolist() == [1, 0, 1]

print("pandas", pd.__version__, "with numpy", np.__version__)
