# Direct-to-S3 upload path.
#
# Browser -> presigned POST -> S3 -> event -> ingestion Lambda -> S3 Vectors.
# Keeps uploads off Lambda entirely (no 6 MB invocation limit) and moves
# embedding off the request path (no HTTP timeout on a large PDF).

resource "aws_s3_bucket" "uploads" {
  bucket        = "${var.project}-uploads-${data.aws_caller_identity.current.account_id}"
  force_destroy = true # raw PDFs are reproducible input, not durable state
}

resource "aws_s3_bucket_public_access_block" "uploads" {
  bucket                  = aws_s3_bucket.uploads.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# The vectors are the durable artifact; the source PDF is only needed long
# enough to ingest it. Expiring them bounds storage cost and data retention.
resource "aws_s3_bucket_lifecycle_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  rule {
    id     = "expire-raw-uploads"
    status = "Enabled"
    filter {}
    expiration { days = 7 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}

# The browser POSTs from the Function URL origin, so S3 needs to allow it.
resource "aws_s3_bucket_cors_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  cors_rule {
    allowed_methods = ["POST"]
    allowed_origins = [trimsuffix(aws_lambda_function_url.app.function_url, "/")]
    allowed_headers = ["*"]
    max_age_seconds = 3000
  }
}

# --- Ingestion function ----------------------------------------------------

# Same zip as the web app, different entry point: no web server, no adapter
# layer, just the S3 event handler. One build, two functions.
resource "aws_lambda_function" "ingest" {
  function_name = "${var.project}-ingest"
  role          = aws_iam_role.lambda.arn
  architectures = ["arm64"]
  runtime       = "python3.13"

  filename         = local.package
  source_code_hash = filebase64sha256(local.package)
  handler          = "ingest.handler"

  memory_size = 1024
  # Embedding is one Titan call per chunk; a large PDF is thousands of chunks.
  timeout = 900

  environment {
    variables = {
      VECTOR_BUCKET   = aws_s3vectors_vector_bucket.main.vector_bucket_name
      VECTOR_INDEX    = aws_s3vectors_index.docs.index_name
      EMBED_MODEL     = local.titan_model_id
      EMBED_DIMENSION = tostring(var.embedding_dimension)
    }
  }

  depends_on = [aws_cloudwatch_log_group.ingest]
}

resource "aws_cloudwatch_log_group" "ingest" {
  name              = "/aws/lambda/${var.project}-ingest"
  retention_in_days = 14
}

resource "aws_lambda_permission" "allow_s3" {
  statement_id  = "AllowExecutionFromS3"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingest.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.uploads.arn
}

resource "aws_s3_bucket_notification" "uploads" {
  bucket = aws_s3_bucket.uploads.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.ingest.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "incoming/"
    filter_suffix       = ".pdf"
  }

  depends_on = [aws_lambda_permission.allow_s3]
}

# --- Permissions -----------------------------------------------------------

# Both functions share the execution role: the web app signs presigned POSTs
# (which requires s3:PutObject on the credentials doing the signing) and the
# ingestion function reads the uploaded object back.
resource "aws_iam_role_policy" "uploads" {
  name = "${var.project}-uploads"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
      Resource = "${aws_s3_bucket.uploads.arn}/incoming/*"
    }]
  })
}
