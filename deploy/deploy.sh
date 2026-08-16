#!/usr/bin/env bash
# Build, apply, and publish -- in that order, as one command.
#
#   ./deploy/deploy.sh
#
# Why this exists: Terraform tracks the Lambda package by `filebase64sha256` of
# deploy/app.zip. If the zip is stale, the hash is unchanged, Terraform reports
# "no changes", and the old code stays live -- silently. Running the three steps
# separately makes skipping the build easy and the failure invisible: the apply
# succeeds and the site keeps serving whatever was last packaged.
set -euo pipefail

cd "$(dirname "$0")/.."

# Terraform must run as the admin profile, not the least-privilege dev role that
# .env selects; AWS_PROFILE in the environment would silently downgrade it.
unset AWS_PROFILE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

echo "==> 1/3 build"
./deploy/build.sh

echo
echo "==> 2/3 apply"
terraform -chdir=infra apply "$@"

echo
echo "==> 3/3 publish"
./deploy/publish.sh

echo
echo "==> verifying the running code is this commit"
URL=$(terraform -chdir=infra output -raw public_url)
EXPECTED=$(git rev-parse --short HEAD)
# Lambda serves the previous package for a moment after an update.
for attempt in 1 2 3 4 5 6; do
  LIVE=$(curl -s --max-time 20 "$URL/health" | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  [ "$LIVE" = "$EXPECTED" ] && break
  sleep 5
done

if [ "$LIVE" = "$EXPECTED" ]; then
  echo "    live version $LIVE matches HEAD"
  echo
  echo "Deployed: $URL"
else
  echo "!! DEPLOY IS STALE: live version '$LIVE', expected '$EXPECTED'" >&2
  echo "   The zip did not reach Lambda. Re-run, or check that deploy/app.zip rebuilt." >&2
  exit 1
fi
