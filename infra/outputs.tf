output "region" {
  value = var.region
}

output "vector_bucket_name" {
  value = aws_s3vectors_vector_bucket.main.vector_bucket_name
}

output "index_name" {
  value = aws_s3vectors_index.docs.index_name
}

output "embedding_dimension" {
  value = aws_s3vectors_index.docs.dimension
}

# Written to ../.env by deploy/publish.sh.
#
# Contains NO credentials -- only which resources to talk to. AWS credentials
# come from the standard provider chain (the `aws_profile` below), so this file
# is not a secret and cannot leak anything durable if it is mishandled.
output "env_file" {
  description = "Non-secret application config. Safe to write to disk."
  value       = <<-EOT
    AWS_REGION=${var.region}
    AWS_PROFILE=${var.project}
    VECTOR_BUCKET=${aws_s3vectors_vector_bucket.main.vector_bucket_name}
    VECTOR_INDEX=${aws_s3vectors_index.docs.index_name}
    UPLOAD_BUCKET=${aws_s3_bucket.uploads.bucket}
    EMBED_MODEL=${local.titan_model_id}
    EMBED_DIMENSION=${aws_s3vectors_index.docs.dimension}
    LLM_MODEL=${var.llm_model_id}
    LLM_PRICE_IN_PER_1M=${var.llm_price_in_per_1m}
    LLM_PRICE_OUT_PER_1M=${var.llm_price_out_per_1m}
  EOT
}

# Append to ~/.aws/config. boto3 then calls STS to get short-lived credentials
# scoped to the same least-privilege policy the Lambda runs under.
output "aws_profile" {
  description = "AWS CLI profile granting local dev the app's least-privilege role."
  value       = <<-EOT
    [profile ${var.project}]
    role_arn = ${aws_iam_role.dev.arn}
    source_profile = default
    region = ${var.region}
  EOT
}

output "public_url" {
  description = "Public URL of the deployed app."
  value       = try(aws_lambda_function_url.app.function_url, null)
}

output "upload_bucket" {
  description = "Raw PDF landing bucket; objects expire after 7 days."
  value       = try(aws_s3_bucket.uploads.bucket, null)
}

output "github_ci_role_arn" {
  description = "Set as the AWS_CI_ROLE_ARN GitHub Actions secret."
  value       = try(aws_iam_role.github_ci[0].arn, null)
}
