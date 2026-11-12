"""Build the homepage and copy its synthetic screenshot into Static Assets."""

import shutil
from pathlib import Path

site = Path(__file__).resolve().parent
dist = site / "dist"
if dist.exists():
    shutil.rmtree(dist)
shutil.copytree(site / "public", dist)
shutil.copyfile(site.parent / "docs/search-demo.png", dist / "search-demo.png")
