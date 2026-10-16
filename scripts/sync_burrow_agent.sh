#!/usr/bin/env bash
# Vendor burrow-agent-py into custom_components/xeonr_cloud/burrow_agent.
#
# HACS installs a custom integration by copying its folder; it cannot pull a
# private package from our registry, so the agent library ships inside the
# integration. tunnel.py is the only module that imports it.
#
#   scripts/sync_burrow_agent.sh [ref]          # default: main
#   BURROW_AGENT_REPO=/path/to/checkout scripts/sync_burrow_agent.sh
#
# Writes burrow_agent/VENDORED with the source commit. Absolute imports of
# `burrow_agent` inside the library are rewritten to relative ones so the
# vendored copy never resolves to some other installed package.
set -euo pipefail

ref="${1:-main}"
repo="${BURROW_AGENT_REPO:-git@git.prod.wtf:xeonr/burrow-agent-py.git}"
root="$(cd "$(dirname "$0")/.." && pwd)"
dest="$root/custom_components/xeonr_cloud/burrow_agent"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

if [[ -d "$repo/.git" ]]; then
  git -C "$repo" archive --format=tar "$ref" | tar -x -C "$work"
  commit="$(git -C "$repo" rev-parse "$ref")"
else
  git clone --quiet --depth 1 --branch "$ref" "$repo" "$work/src" 2>/dev/null \
    || { git clone --quiet "$repo" "$work/src" && git -C "$work/src" checkout --quiet "$ref"; }
  commit="$(git -C "$work/src" rev-parse HEAD)"
  work="$work/src"
fi

src=""
for candidate in "$work/src/burrow_agent" "$work/burrow_agent"; do
  [[ -f "$candidate/__init__.py" ]] && src="$candidate" && break
done
[[ -n "$src" ]] || { echo "no burrow_agent package in $repo@$ref" >&2; exit 1; }

rm -rf "$dest"
mkdir -p "$dest"
(cd "$src" && find . -name '*.py' -o -name 'py.typed' | grep -v '/tests\?/' | cpio -pdm --quiet "$dest")

# from burrow_agent.x import y  ->  from .x import y (depth-aware)
find "$dest" -name '*.py' | while read -r file; do
  rel="${file#"$dest"/}"
  depth="$(awk -F/ '{print NF-1}' <<<"$rel")"
  dots="$(printf '.%.0s' $(seq 0 "$depth"))"
  sed -E -i.bak \
    -e "s/^([[:space:]]*)from burrow_agent\./\1from ${dots}/" \
    -e "s/^([[:space:]]*)from burrow_agent import/\1from ${dots} import/" \
    -e "s/^([[:space:]]*)import burrow_agent(\.[a-z_]+)?$/\1# vendored: import burrow_agent\2/" \
    "$file"
  rm -f "$file.bak"
done

cat > "$dest/VENDORED" <<META
source: $repo
ref: $ref
commit: $commit
synced: $(date -u +%Y-%m-%dT%H:%M:%SZ)
META

echo "Vendored burrow_agent @ $commit into $dest"
if [[ -f "$work/pyproject.toml" ]]; then
  echo "Its declared dependencies (each must be in manifest.json requirements or already in Home Assistant):"
  py=""
  for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
    "$candidate" -c 'import tomllib' 2>/dev/null && py="$candidate" && break
  done
  [[ -n "$py" ]] || { echo "  (need Python 3.11+ to read $work/pyproject.toml)"; exit 0; }
  "$py" - "$work/pyproject.toml" <<'PY'
import sys, tomllib
deps = tomllib.load(open(sys.argv[1], "rb")).get("project", {}).get("dependencies", [])
print("\n".join(f"  {d}" for d in deps) or "  (none)")
PY
fi
grep -rn "^\s*import burrow_agent\|# vendored: import" "$dest" && \
  echo "WARNING: bare 'import burrow_agent' lines were commented out; fix them by hand" >&2 || true
