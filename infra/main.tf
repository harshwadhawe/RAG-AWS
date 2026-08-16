terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  titan_model_id = "amazon.titan-embed-text-v2:0"

  # Strip the `us.` inference-profile prefix to get the underlying foundation
  # model id. A cross-region profile needs InvokeModel on BOTH the profile and
  # the foundation model in every region it can route to -- hence the wildcard
  # region in the foundation-model ARN below.
  llm_foundation_model = replace(var.llm_model_id, "/^us\\./", "")
  is_inference_profile = startswith(var.llm_model_id, "us.")
}

# ---------------------------------------------------------------------------
# Vector store
# ---------------------------------------------------------------------------

resource "aws_s3vectors_vector_bucket" "main" {
  vector_bucket_name = "${var.project}-vectors"

  # Required for `terraform destroy` to succeed: without it the bucket refuses
  # to delete while indexes and vectors still live in it.
  force_destroy = true

  encryption_configuration {
    sse_type = "AES256"
  }
}

# Every argument below is "Forces new resource" -- editing dimension,
# distance_metric, or the non-filterable keys destroys and rebuilds the index,
# discarding all stored vectors. Re-ingest after any change here.
resource "aws_s3vectors_index" "docs" {
  index_name         = "docs"
  vector_bucket_name = aws_s3vectors_vector_bucket.main.vector_bucket_name

  data_type       = "float32"
  dimension       = var.embedding_dimension
  distance_metric = "cosine" # recommended for Titan Text Embeddings V2

  metadata_configuration {
    # Chunk text is stored here rather than as filterable metadata: filterable
    # metadata is capped at 2 KB/vector, and we never filter on the body text.
    # `source` and `page` stay filterable (the default) for per-document queries.
    non_filterable_metadata_keys = ["source_text"]
  }
}

# ---------------------------------------------------------------------------
# Application credentials (least privilege)
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "app" {
  statement {
    sid       = "TitanEmbeddings"
    actions   = ["bedrock:InvokeModel"]
    resources = ["arn:aws:bedrock:${var.region}::foundation-model/${local.titan_model_id}"]
  }

  # Generation model, invoked through the provider-agnostic Converse API.
  statement {
    sid     = "GenerationModel"
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = compact([
      "arn:aws:bedrock:*::foundation-model/${local.llm_foundation_model}",
      local.is_inference_profile
      ? "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.llm_model_id}"
      : "",
    ])
  }

  statement {
    sid = "VectorIndexReadWrite"
    actions = [
      "s3vectors:PutVectors",
      "s3vectors:QueryVectors",
      # GetVectors is NOT optional: QueryVectors alone returns only keys and
      # distances. Requesting returnMetadata (which we need for chunk text)
      # fails with AccessDenied without it.
      "s3vectors:GetVectors",
      "s3vectors:ListVectors",
      "s3vectors:DeleteVectors",
      "s3vectors:GetIndex",
    ]
    resources = [aws_s3vectors_index.docs.index_arn]
  }

  statement {
    sid       = "VectorBucketRead"
    actions   = ["s3vectors:GetVectorBucket", "s3vectors:ListIndexes"]
    resources = ["arn:aws:s3vectors:${var.region}:${data.aws_caller_identity.current.account_id}:bucket/${aws_s3vectors_vector_bucket.main.vector_bucket_name}"]
  }
}

# Local development assumes a role rather than holding a long-lived access key.
#
# This closes the last gap in the credential story: Lambda already uses an
# execution role and CI already uses GitHub OIDC, so a static key on a laptop
# was the only durable credential left -- and it lived in two places it should
# not (a .env on disk and, worse, terraform.tfstate in plaintext).
#
# STS issues short-lived credentials instead, and there is nothing to rotate,
# leak, or accidentally commit.
data "aws_iam_policy_document" "dev_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type = "AWS"
      # Delegates to the account: any IAM principal here that is itself granted
      # sts:AssumeRole on this role can assume it.
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
}

resource "aws_iam_role" "dev" {
  name                 = "${var.project}-dev"
  assume_role_policy   = data.aws_iam_policy_document.dev_assume.json
  max_session_duration = 3600
}

# Identical least-privilege policy as the Lambda execution role and CI role --
# one policy document, three consumers, no drift between them.
resource "aws_iam_role_policy" "dev" {
  name   = "${var.project}-dev"
  role   = aws_iam_role.dev.id
  policy = data.aws_iam_policy_document.app.json
}

# ---------------------------------------------------------------------------
# Cost guardrail
# ---------------------------------------------------------------------------

resource "aws_budgets_budget" "monthly" {
  count = var.alert_email == "" ? 0 : 1

  name         = "${var.project}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
