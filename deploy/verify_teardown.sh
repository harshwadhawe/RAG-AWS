#!/usr/bin/env bash
# Inventory every AWS component this project creates.
#
# Run before `terraform destroy` to snapshot, and after to confirm nothing
# survived. Deliberately queries AWS directly rather than reading Terraform
# state -- an empty state proves Terraform forgot the resource, not that AWS
# deleted it.
#
# Usage:  ./deploy/verify_teardown.sh
set -uo pipefail

REGION="${AWS_REGION:-us-east-1}"
PROJECT="${PROJECT:-llama-rag}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)

present=0
absent=0

check() { # check <label> <command...>
  local label="$1"; shift
  if out=$("$@" 2>&1); then
    echo "  PRESENT  $label"
    present=$((present + 1))
  else
    case "$out" in
      *NotFound*|*NoSuchBucket*|*NoSuchEntity*|*does\ not\ exist*|*ResourceNotFound*|*NotFoundException*|*404*)
        echo "  gone     $label"; absent=$((absent + 1)) ;;
      *)
        echo "  ?ERROR   $label -- ${out%%$'\n'*}"; present=$((present + 1)) ;;
    esac
  fi
}

echo "account $ACCOUNT / region $REGION / project $PROJECT"
echo
echo "S3 Vectors"
check "vector bucket $PROJECT-vectors" \
  aws s3vectors get-vector-bucket --vector-bucket-name "$PROJECT-vectors" --region "$REGION"
check "vector index  $PROJECT-vectors/docs" \
  aws s3vectors get-index --vector-bucket-name "$PROJECT-vectors" --index-name docs --region "$REGION"

echo
echo "S3"
check "uploads bucket $PROJECT-uploads-$ACCOUNT" \
  aws s3api head-bucket --bucket "$PROJECT-uploads-$ACCOUNT" --region "$REGION"

echo
echo "Lambda"
check "function $PROJECT" \
  aws lambda get-function --function-name "$PROJECT" --region "$REGION"
check "function $PROJECT-ingest" \
  aws lambda get-function --function-name "$PROJECT-ingest" --region "$REGION"
check "function URL  $PROJECT" \
  aws lambda get-function-url-config --function-name "$PROJECT" --region "$REGION"

echo
echo "IAM"
check "role   $PROJECT-lambda" aws iam get-role --role-name "$PROJECT-lambda"
check "role   $PROJECT-dev"    aws iam get-role --role-name "$PROJECT-dev"

echo
echo "CloudWatch"
for lg in "/aws/lambda/$PROJECT" "/aws/lambda/$PROJECT-ingest"; do
  if [ -n "$(aws logs describe-log-groups --log-group-name-prefix "$lg" \
        --region "$REGION" --query "logGroups[?logGroupName=='$lg'].logGroupName" --output text 2>/dev/null)" ]; then
    echo "  PRESENT  log group $lg"; present=$((present + 1))
  else
    echo "  gone     log group $lg"; absent=$((absent + 1))
  fi
done

echo
echo "Budgets"
if aws budgets describe-budget --account-id "$ACCOUNT" --budget-name "$PROJECT-monthly" >/dev/null 2>&1; then
  echo "  PRESENT  budget $PROJECT-monthly"; present=$((present + 1))
else
  echo "  gone     budget $PROJECT-monthly"; absent=$((absent + 1))
fi

echo
echo "-------------------------------------------"
echo "present: $present   gone: $absent"
[ "$present" -eq 0 ] && echo "TEARDOWN COMPLETE -- nothing left." \
                     || echo "Resources still exist (expected before destroy)."
