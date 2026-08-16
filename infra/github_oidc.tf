# GitHub Actions access via OIDC -- no long-lived AWS keys in repository secrets.
#
# Actions presents a short-lived OIDC token; AWS STS exchanges it for temporary
# credentials scoped to this repository. Nothing to rotate, nothing to leak.

variable "github_repo" {
  description = "owner/name of the GitHub repository allowed to assume the CI role. Empty disables CI access."
  type        = string
  default     = "harshwadhawe/RAG-AWS"
}

locals {
  enable_ci = var.github_repo != ""
}

# One OIDC provider per account. If the account already has one for GitHub,
# import it rather than creating a second: terraform import \
#   aws_iam_openid_connect_provider.github arn:aws:iam::<acct>:oidc-provider/token.actions.githubusercontent.com
resource "aws_iam_openid_connect_provider" "github" {
  count = local.enable_ci ? 1 : 0

  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

data "aws_iam_policy_document" "github_assume" {
  count = local.enable_ci ? 1 : 0

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github[0].arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    # Scoped to this repository. Without this condition ANY GitHub repository
    # on the internet could assume the role.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repo}:*"]
    }
  }
}

resource "aws_iam_role" "github_ci" {
  count = local.enable_ci ? 1 : 0

  name               = "${var.project}-github-ci"
  assume_role_policy = data.aws_iam_policy_document.github_assume[0].json
  # CI jobs are short; cap the session accordingly.
  max_session_duration = 3600
}

# CI runs the evals, so it needs exactly what the app needs -- read/write on the
# index plus model invocation. Same policy document, no extra grants.
resource "aws_iam_role_policy" "github_ci" {
  count = local.enable_ci ? 1 : 0

  name   = "${var.project}-github-ci"
  role   = aws_iam_role.github_ci[0].id
  policy = data.aws_iam_policy_document.app.json
}
