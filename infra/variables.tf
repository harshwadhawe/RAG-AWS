variable "region" {
  description = "AWS region. Must support both S3 Vectors and Claude on Bedrock."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Name prefix for all resources."
  type        = string
  default     = "llama-rag"
}

variable "embedding_dimension" {
  description = <<-EOT
    Vector dimension. Must match the embedding model exactly.
    amazon.titan-embed-text-v2:0 emits 1024 by default (512 and 256 also valid).
    Changing this REPLACES the index and drops every stored vector.
  EOT
  type        = number
  default     = 1024
}

variable "llm_model_id" {
  description = <<-EOT
    Bedrock model for generation, invoked via the Converse API.
    Llama 3.1+ and Llama 4 are INFERENCE_PROFILE-only, so they need the `us.` prefix;
    Llama 3 (meta.llama3-70b-instruct-v1:0) supports ON_DEMAND on the bare id.
    Meta models need no access request; Anthropic models require a console opt-in.
  EOT
  type        = string
  default     = "us.meta.llama4-scout-17b-instruct-v1:0"
}

variable "alert_email" {
  description = "Email for the monthly cost alert. Leave empty to skip the budget."
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  description = "Monthly spend threshold that triggers the alert email."
  type        = number
  default     = 5
}

variable "max_concurrency" {
  description = <<-EOT
    Reserved concurrency for the public Lambda. The endpoint is unauthenticated
    and every request spends Bedrock tokens, so this bounds the worst case.

    -1 means unreserved (the default). Reserving concurrency is REJECTED when it
    would drop the account's unreserved pool below its minimum -- new AWS accounts
    are capped at 10 total concurrent executions, and that cap is itself the guard.
    Check with: aws lambda get-account-settings --query AccountLimit
    On an account raised to the usual 1000, set this to a small positive number.

    Set to 0 to disable the function entirely without destroying it (kill switch).
  EOT
  type        = number
  default     = -1
}

variable "max_upload_mb" {
  description = <<-EOT
    Maximum size of a single uploaded PDF. Enforced by S3 via a
    content-length-range condition on the presigned POST, and mirrored in the
    browser. Not a platform limit -- uploads bypass Lambda entirely -- so this
    is a cost/abuse choice on a public endpoint.
  EOT
  type        = number
  default     = 64
}

variable "session_ttl_minutes" {
  description = <<-EOT
    How long a visitor's uploaded documents and their vectors survive after the
    last upload. Enforced by a scheduled Lambda -- S3 lifecycle rules are
    day-granular and DynamoDB TTL is best-effort within ~48h, so neither can
    express an hour-scale policy.
  EOT
  type        = number
  default     = 60
}
