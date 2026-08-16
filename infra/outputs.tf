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

# Run `terraform output -raw env_file > ../.env` to configure the app.
output "env_file" {
  description = "Ready-to-write .env contents for the application."
  sensitive   = true
  value       = <<-EOT
    AWS_REGION=${var.region}
    AWS_ACCESS_KEY_ID=${aws_iam_access_key.app.id}
    AWS_SECRET_ACCESS_KEY=${aws_iam_access_key.app.secret}
    VECTOR_BUCKET=${aws_s3vectors_vector_bucket.main.vector_bucket_name}
    VECTOR_INDEX=${aws_s3vectors_index.docs.index_name}
    EMBED_MODEL=amazon.titan-embed-text-v2:0
    EMBED_DIMENSION=${aws_s3vectors_index.docs.dimension}
    LLM_MODEL=${var.llm_model_id}
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
