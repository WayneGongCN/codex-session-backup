"""Build both homepage languages and copy their illustrations into Static Assets."""

import shutil
from pathlib import Path

site = Path(__file__).resolve().parent
dist = site / "dist"
if dist.exists():
    shutil.rmtree(dist)
shutil.copytree(site / "public", dist)
for language in ("en", "zh-cn"):
    image = f"search-demo-{language}.png"
    shutil.copyfile(site.parent / "docs" / image, dist / image)
