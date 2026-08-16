#!/usr/bin/env bash
# Verify `terraform destroy` actually removed everything -- and that the things
# which are *meant* to outlive a teardown are still there.
#
#   ./deploy/verify_teardown.sh
#
# Queries AWS directly rather than reading Terraform state: an empty state proves
# Terraform forgot the resource, not that AWS deleted it.
#
# Discovery is tag-driven (the provider's default_tags stamp Project=<project> on
# everything it creates), so this does not drift as resources are added. The
# earlier hardcoded version checked 7 resources while Terraform managed 37, and
# would happily report "TEARDOWN COMPLETE" with three Lambdas still running.
set -uo pipefail

REGION="${AWS_REGION:-us-east-1}"
PROJECT="${PROJECT:-llama-rag}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)

echo "account $ACCOUNT / region $REGION / project $PROJECT"
echo
echo "Tagged resources (Project=$PROJECT)"

mapfile -t tagged < <(aws resourcegroupstaggingapi get-resources \
  --tag-filters "Key=Project,Values=$PROJECT" \
  --query 'ResourceTagMappingList[].ResourceARN' --output text 2>/dev/null | tr '\t' '\n' | grep -v '^$')

if [ "${#tagged[@]}" -eq 0 ]; then
  echo "  gone     (none)"
else
  for arn in "${tagged[@]}"; do echo "  PRESENT  ${arn#arn:aws:}"; done
fi

# IAM roles are inconsistently covered by the tagging API, so check them by name.
echo
echo "IAM roles"
iam_present=0
for role in "$PROJECT-lambda" "$PROJECT-dev" "$PROJECT-github-ci"; do
  if aws iam get-role --role-name "$role" >/dev/null 2>&1; then
    echo "  PRESENT  role/$role"; iam_present=$((iam_present + 1))
  else
    echo "  gone     role/$role"
  fi
done

remaining=$(( ${#tagged[@]} + iam_present ))

# --- Things that SHOULD survive a teardown --------------------------------
# Deliberately outside Terraform so a destroy/rebuild cycle does not lose them.
echo
echo "Expected to survive (not Terraform-managed)"

param="${LANGSMITH_KEY_PARAM:-/$PROJECT/langsmith-api-key}"
if aws ssm get-parameter --name "$param" --query 'Parameter.Type' --output text >/dev/null 2>&1; then
  echo "  ok       ssm $param  (tracing key -- survives, so rebuilds keep tracing)"
else
  echo "  MISSING  ssm $param  -- deploy.sh will recreate it from .env.local"
fi

[ -f .env.local ] && echo "  ok       .env.local (local settings + LangSmith key)" \
                  || echo "  MISSING  .env.local"

aws configure list-profiles 2>/dev/null | grep -qx "$PROJECT" \
  && echo "  ok       aws profile '$PROJECT' (role ARN is name-stable, so it works again after rebuild)" \
  || echo "  MISSING  aws profile '$PROJECT'"

echo
echo "-------------------------------------------"
if [ "$remaining" -eq 0 ]; then
  echo "TEARDOWN COMPLETE -- no project resources remain."
else
  echo "$remaining project resource(s) still exist (expected before destroy)."
fi
