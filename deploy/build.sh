#!/usr/bin/env bash
# Build the Lambda deployment zip.
#
# uv cross-compiles the Linux aarch64 wheels directly on macOS, so no Docker and
# no ECR are needed -- this stays a zip deploy rather than a container image.
set -euo pipefail

cd "$(dirname "$0")/.."
BUILD=deploy/build
ZIP=deploy/app.zip

rm -rf "$BUILD" "$ZIP"
mkdir -p "$BUILD"

echo "==> installing dependencies for linux/aarch64"
uv pip install \
  --target "$BUILD" \
  --python-platform aarch64-manylinux2014 \
  --python-version 3.13 \
  --only-binary=:all: \
  --quiet \
  -r requirements.txt

echo "==> adding application code"
cp app.py ingest.py populate_database.py get_embedding_function.py "$BUILD/"
cp -r templates static "$BUILD/"
cp deploy/run.sh "$BUILD/"
chmod +x "$BUILD/run.sh"

# pytest and its tree are test-only; they cost package size and cold start.
rm -rf "$BUILD"/{pytest,_pytest,pluggy,iniconfig}* 2>/dev/null || true
find "$BUILD" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUILD" -name "*.dist-info" -type d -prune -exec rm -rf {} + 2>/dev/null || true

echo "==> zipping"
(cd "$BUILD" && zip -qr "../../$ZIP" .)

echo "==> built $ZIP ($(du -h "$ZIP" | cut -f1) zipped, $(du -sh "$BUILD" | cut -f1) unzipped)"
echo "    Lambda limits: 50 MB zipped (direct upload) / 250 MB unzipped"
