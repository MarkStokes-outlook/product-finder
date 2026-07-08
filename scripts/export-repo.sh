#!/bin/bash
#
# Product Finder Repository Export Script
#
# Exports documentation, source code, and tests only — no working folders (data/,
# reports/, .venv/), no compiled/generated artifacts (__pycache__,
# *.egg-info, .pytest_cache), and no local secrets (config.yaml — only the
# committed config.example.yaml template is included).
#
# Output: code-export/<timestamp>/product-finder-export.zip at the repo root.
#

set -e

# Get script directory and repo root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if ! command -v zip &> /dev/null; then
    echo "Error: 'zip' is required but not found on PATH." >&2
    exit 1
fi

# Generate ISO timestamp for the run folder
TIMESTAMP=$(date +"%Y-%m-%dT%H-%M-%S")

EXPORT_BASE="$REPO_ROOT/code-export"
EXPORT_DIR="$EXPORT_BASE/$TIMESTAMP"
OUT_ZIP="product-finder-export.zip"

mkdir -p "$EXPORT_DIR"

echo "=== Product Finder Repository Export ==="
echo "Repo root: $REPO_ROOT"
echo "Export to: $EXPORT_DIR"
echo ""

cd "$REPO_ROOT"

# ---------------------------------------------------------
# What gets exported — documentation, source, and tests only.
#
# Edit these arrays to change scope. Everything else in the repo (data/,
# reports/, .venv/, prompts history aside, build artifacts, the
# real config.yaml, git history, editor/tool state) is deliberately left
# out — this is an allow-list, not an "everything except" exclude list,
# so anything new that shows up in the repo root in future is left out by
# default rather than accidentally swept in.
# ---------------------------------------------------------
INCLUDE_PATHS=(
    "docs"                  # ADRs, design notes, implementation notes, strategy, examples
    "src/product_finder"    # application source (excludes egg-info/__pycache__ below)
    "tests"                 # pytest suite and fixtures
    "prompts"                # original build prompts — project intent history
    "README.md"
    "ARCHITECTURE.md"
    "VISION.md"
    "AGENTS.md"
    "CLAUDE.md"
    "gemini-boot.md"
    "config.example.yaml"   # documented config template, not the real (gitignored) config.yaml
    "pyproject.toml"        # package metadata/dependencies — needed to make sense of src/
)

EXCLUDE_PATTERNS=(
    "*/__pycache__/*"
    "*.pyc"
    "*.pyo"
    "*/.pytest_cache/*"
    "*/*.egg-info/*"
    "*/.DS_Store"
    ".DS_Store"
)

# Only pass paths that actually exist — keeps the script safe to run even
# if an optional file (e.g. gemini-boot.md) isn't present in a given
# checkout.
EXISTING_PATHS=()
for p in "${INCLUDE_PATHS[@]}"; do
    if [ -e "$p" ]; then
        EXISTING_PATHS+=("$p")
    else
        echo "  (skipping missing: $p)"
    fi
done

if [ "${#EXISTING_PATHS[@]}" -eq 0 ]; then
    echo "Error: none of the configured INCLUDE_PATHS exist in $REPO_ROOT" >&2
    exit 1
fi

echo "Creating export: $OUT_ZIP"

ZIP_EXCLUDES=()
for pattern in "${EXCLUDE_PATTERNS[@]}"; do
    ZIP_EXCLUDES+=(-x "$pattern")
done

zip -r "$EXPORT_DIR/$OUT_ZIP" "${EXISTING_PATHS[@]}" "${ZIP_EXCLUDES[@]}" > /dev/null

echo "  Created: $EXPORT_DIR/$OUT_ZIP"

# ---------------------------------------------------------
# Summary
# ---------------------------------------------------------
echo ""
echo "=== Export Complete ==="
echo ""
ls -lh "$EXPORT_DIR/$OUT_ZIP" | awk '{print "  " $NF " (" $5 ")"}'
echo ""
echo "Files exported to: $EXPORT_DIR"
