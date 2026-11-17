"""Assemble the Developer skin demo: base.html + parts/*.html -> ../developer-theme.html.

Each part may contain <style> and <script> blocks (moved into the head / before the router)
and any number of <section class="view"> blocks (inserted into <main>). Parts are read in name order.
"""

import pathlib
import re

here = pathlib.Path(__file__).parent
base = (here / "base.html").read_text(encoding="utf-8")
styles, scripts, views = [], [], []
for part in sorted((here / "parts").glob("*.html")):
    text = part.read_text(encoding="utf-8")
    styles += re.findall(r"<style[^>]*>.*?</style>", text, flags=re.S)
    scripts += re.findall(r"<script[^>]*>.*?</script>", text, flags=re.S)
    text = re.sub(r"<style[^>]*>.*?</style>|<script[^>]*>.*?</script>", "", text, flags=re.S)
    views.append(f"<!-- ===== {part.name} ===== -->\n{text.strip()}\n")
out = (
    base.replace("<!--PART-STYLES-->", "\n".join(styles))
    .replace("<!--PART-SCRIPTS-->", "\n".join(scripts))
    .replace("<!--VIEWS-->", "\n".join(views))
)
(here.parent / "developer-theme.html").write_text(out, encoding="utf-8")
print(f"built {len(views)} parts, {out.count('class=\"view')} views, {len(out) // 1024} KB")
