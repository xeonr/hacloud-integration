"""Build dist/xeonr_cloud.zip: the integration folder, ready to unzip into
config/custom_components/. Fails if the tag (if given) and manifest.json's
version disagree.

    python3 scripts/build_release.py [v0.1.0]
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "custom_components" / "xeonr_cloud"
DIST = ROOT / "dist"


def main() -> int:
    version = json.loads((SRC / "manifest.json").read_text())["version"]
    if len(sys.argv) > 1 and sys.argv[1].removeprefix("v") != version:
        print(f"tag {sys.argv[1]} does not match manifest.json version {version}")
        return 1
    DIST.mkdir(exist_ok=True)
    out = DIST / "xeonr_cloud.zip"
    files = sorted(
        p
        for p in SRC.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    )
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            # Fixed timestamps so the same tree always builds the same zip.
            info = zipfile.ZipInfo(
                str(Path("xeonr_cloud") / path.relative_to(SRC)), (2026, 1, 1, 0, 0, 0)
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    (DIST / "xeonr_cloud.zip.sha256").write_text(f"{digest}  xeonr_cloud.zip\n")
    print(f"{out.relative_to(ROOT)} {version} {len(files)} files sha256={digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
