# Runs in eryx with the built wheel: rapidfuzz must use its C++ scorers
from rapidfuzz import distance, fuzz, process, process_cpp_impl, utils
from rapidfuzz.distance import Levenshtein, metrics_cpp

assert fuzz.ratio.__module__ == "rapidfuzz.fuzz_cpp", fuzz.ratio.__module__
assert Levenshtein.distance is metrics_cpp.levenshtein_distance
assert process.extractOne is process_cpp_impl.extractOne
assert utils.default_process.__module__ == "rapidfuzz.utils_cpp"

assert round(fuzz.ratio("this is a test", "this is a test!"), 2) == 96.55
assert fuzz.partial_ratio("this is a test", "this is a test!") == 100
assert round(fuzz.token_sort_ratio("fuzzy wuzzy was a bear", "wuzzy fuzzy was a bear")) == 100
assert Levenshtein.distance("kitten", "sitting") == 3
assert distance.Indel.distance("lewenstein", "levenshtein") == 3

choices = ["Atlanta Falcons", "New York Jets", "New York Giants", "Dallas Cowboys"]
best = process.extractOne("cowboys", choices, scorer=fuzz.WRatio)
assert best[0] == "Dallas Cowboys" and best[2] == 3, best
top = process.extract("new york", choices, limit=2)
assert [match[0] for match in top] == ["New York Jets", "New York Giants"], top
assert utils.default_process(" Hello, World! ") == "hello  world"
print("rapidfuzz C++ scorers, e.g.", best)
