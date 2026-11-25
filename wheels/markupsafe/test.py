# Runs in eryx with the built wheel: markupsafe must use its C speedups
import markupsafe
from markupsafe import _speedups

assert markupsafe._escape_inner is _speedups._escape_inner
assert str(markupsafe.escape("<script>&'\"")) == "&lt;script&gt;&amp;&#39;&#34;"
assert markupsafe.Markup("<em>%s</em>") % "<x>" == "<em>&lt;x&gt;</em>"
print("markupsafe speedups from", _speedups.__file__)
