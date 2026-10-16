"""Offline stand-in for HACS's repository validation.

The HACS action (hacs/action) needs the GitHub API: topics, description,
releases, brands. This checks the parts that live in the repository itself,
so a broken layout is caught here rather than on publication.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
errors: list[str] = []

hacs = json.loads((ROOT / "hacs.json").read_text())
for key in ("name", "homeassistant"):
    if key not in hacs:
        errors.append(f"hacs.json: missing {key}")
allowed = {
    "name",
    "content_in_root",
    "zip_release",
    "filename",
    "hide_default_branch",
    "country",
    "homeassistant",
    "hacs",
    "persistent_directory",
    "render_readme",
}
for key in set(hacs) - allowed:
    errors.append(f"hacs.json: unknown key {key}")
if hacs.get("content_in_root"):
    errors.append(
        "hacs.json: content_in_root should be false for custom_components/ layout"
    )

integrations = [
    p
    for p in (ROOT / "custom_components").iterdir()
    if p.is_dir() and not p.name.startswith("__")
]
if len(integrations) != 1:
    errors.append(
        f"custom_components/: expected exactly one integration, found {[p.name for p in integrations]}"
    )
for integration in integrations:
    manifest = json.loads((integration / "manifest.json").read_text())
    for key in (
        "domain",
        "name",
        "version",
        "documentation",
        "issue_tracker",
        "codeowners",
    ):
        if not manifest.get(key):
            errors.append(f"{integration.name}/manifest.json: missing {key}")
    if manifest.get("domain") != integration.name:
        errors.append(f"{integration.name}/manifest.json: domain does not match folder")
    if not (integration / "translations" / "en.json").exists():
        errors.append(f"{integration.name}: no translations/en.json")
    elif json.loads((integration / "strings.json").read_text()) != json.loads(
        (integration / "translations" / "en.json").read_text()
    ):
        errors.append(
            f"{integration.name}: translations/en.json differs from strings.json"
        )

if not (ROOT / "README.md").exists():
    errors.append("README.md missing (render_readme)")

for error in errors:
    print(f"ERROR {error}")
print("HACS layout OK" if not errors else f"{len(errors)} problem(s)")
sys.exit(1 if errors else 0)
