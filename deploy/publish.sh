#!/usr/bin/env bash
# Sync everything that depends on deployed infrastructure, from Terraform.
#
#   ./deploy/publish.sh          # after terraform apply
#   ./deploy/publish.sh --down   # after terraform destroy
#
# Terraform is the source of truth for resource names and URLs. Nothing else
# should hardcode them: the Function URL changes on every recreate, and the
# uploads bucket embeds the account id. This pushes the current values to the
# two places that can't read Terraform state directly -- the README and the
# GitHub Actions environment.
set -euo pipefail

cd "$(dirname "$0")/.."
README=README.md
BEGIN='<!-- deploy:url -->'
END='<!-- /deploy:url -->'

replace_block() { # replace_block <content>
  python3 - "$1" <<'PY'
import pathlib, re, sys
content, readme = sys.argv[1], pathlib.Path("README.md")
text = readme.read_text()
block = f"<!-- deploy:url -->\n{content}\n<!-- /deploy:url -->"
if "<!-- deploy:url -->" in text:
    text = re.sub(r"<!-- deploy:url -->.*?<!-- /deploy:url -->", block, text, flags=re.S)
else:  # first run: insert after the tagline
    lines = text.split("\n")
    lines.insert(6, block)
    text = "\n".join(lines)
readme.write_text(text)
PY
}

if [ "${1:-}" = "--down" ]; then
  replace_block "*Not currently deployed — see [Deploy publicly](#deploy-publicly) to launch it in three commands.*"
  echo "==> README marked as not deployed"
  exit 0
fi

cd infra
URL=$(terraform output -raw public_url 2>/dev/null || true)
[ -n "$URL" ] || { echo "no public_url output -- has terraform apply run?" >&2; exit 1; }

REGION=$(terraform output -raw region)
VBUCKET=$(terraform output -raw vector_bucket_name)
VINDEX=$(terraform output -raw index_name)
VDIM=$(terraform output -raw embedding_dimension)
CI_ROLE=$(terraform output -raw github_ci_role_arn 2>/dev/null || true)
cd ..

replace_block "**[Live demo →]($URL)**"
echo "==> README live demo link -> $URL"

# Local app config. No credentials in here by design -- see the aws_profile
# output; boto3 resolves short-lived credentials by assuming the dev role.
(cd infra && terraform output -raw env_file) > .env
echo "==> .env written ($(grep -c . .env) settings, no secrets)"

PROFILE_NAME=$(grep -m1 '^AWS_PROFILE=' .env | cut -d= -f2)
if ! aws configure list-profiles 2>/dev/null | grep -qx "$PROFILE_NAME"; then
  echo
  echo "!! AWS profile '$PROFILE_NAME' is not configured. Append this to ~/.aws/config:"
  echo
  (cd infra && terraform output -raw aws_profile) | sed 's/^/   /'
  echo
fi

# GitHub Actions can't read local Terraform state, so push the values it needs.
# Non-secret values become repository variables; only the role ARN is a secret.
if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
  gh variable set AWS_REGION       --body "$REGION"  >/dev/null
  gh variable set VECTOR_BUCKET    --body "$VBUCKET" >/dev/null
  gh variable set VECTOR_INDEX     --body "$VINDEX"  >/dev/null
  gh variable set EMBED_DIMENSION  --body "$VDIM"    >/dev/null
  echo "==> GitHub repo variables synced"
  if [ -n "$CI_ROLE" ]; then
    gh secret set AWS_CI_ROLE_ARN --body "$CI_ROLE" >/dev/null
    echo "==> AWS_CI_ROLE_ARN secret synced"
  fi
else
  echo "!! gh not authenticated -- set these repo variables manually:"
  printf '   AWS_REGION=%s\n   VECTOR_BUCKET=%s\n   VECTOR_INDEX=%s\n   EMBED_DIMENSION=%s\n' \
    "$REGION" "$VBUCKET" "$VINDEX" "$VDIM"
  [ -n "$CI_ROLE" ] && printf '   secret AWS_CI_ROLE_ARN=%s\n' "$CI_ROLE"
fi

echo
echo "Commit the README change so the published link matches what is deployed."
