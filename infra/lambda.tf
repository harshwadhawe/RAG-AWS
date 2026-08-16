# Public deployment: Flask on Lambda behind a Function URL.
#
# Run `deploy/build.sh` before `terraform apply` -- it produces deploy/app.zip.

locals {
  package = "${path.module}/../deploy/app.zip"

  # Published by AWS; account 753240598075 is the Lambda Web Adapter owner.
  lwa_layer = "arn:aws:lambda:${var.region}:753240598075:layer:LambdaAdapterLayerArm64:28"
}

resource "random_password" "flask_secret" {
  length  = 48
  special = false
}

# --- Execution role: no access keys anywhere -------------------------------

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.project}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

# Same least-privilege policy the local app user gets -- Bedrock + this index.
resource "aws_iam_role_policy" "lambda" {
  name   = "${var.project}-lambda"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.app.json
}

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Explicit log group so retention is bounded; the implicit one never expires.
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.project}"
  retention_in_days = 14
}

# --- Function --------------------------------------------------------------

resource "aws_lambda_function" "app" {
  function_name = var.project
  role          = aws_iam_role.lambda.arn
  architectures = ["arm64"] # ~20% cheaper than x86_64, and uv cross-compiles for it
  runtime       = "python3.13"

  filename         = local.package
  source_code_hash = filebase64sha256(local.package)

  # The handler is ignored when AWS_LAMBDA_EXEC_WRAPPER is set: the adapter's
  # /opt/bootstrap takes over and launches run.sh instead.
  handler = "run.sh"
  layers  = [local.lwa_layer]

  memory_size = 1024 # CPU scales with memory; below ~1 GB cold start drags
  timeout     = 120

  # Blast-radius cap for a public, unauthenticated endpoint. Defaults to -1
  # (unreserved) because this account's total limit is 10, and reserving from
  # that pool is rejected -- the account cap already bounds a bad day.
  reserved_concurrent_executions = var.max_concurrency

  environment {
    variables = {
      AWS_LAMBDA_EXEC_WRAPPER = "/opt/bootstrap"
      AWS_LWA_INVOKE_MODE     = "response_stream"
      AWS_LWA_PORT            = "8080"

      # No AWS keys: boto3 picks up the execution role automatically.
      VECTOR_BUCKET     = aws_s3vectors_vector_bucket.main.vector_bucket_name
      VECTOR_INDEX      = aws_s3vectors_index.docs.index_name
      EMBED_MODEL       = local.titan_model_id
      EMBED_DIMENSION   = tostring(var.embedding_dimension)
      LLM_MODEL         = var.llm_model_id
      UPLOAD_BUCKET     = aws_s3_bucket.uploads.bucket
      MAX_UPLOAD_MB     = tostring(var.max_upload_mb)
      FLASK_SECRET_KEY  = random_password.flask_secret.result
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

# --- Public URL ------------------------------------------------------------

resource "aws_lambda_function_url" "app" {
  function_name      = aws_lambda_function.app.function_name
  authorization_type = "NONE" # public

  # API Gateway cannot stream responses; Function URLs can. Streaming is what
  # makes generation latency feel acceptable once the UI sends tokens through.
  invoke_mode = "RESPONSE_STREAM"
}
