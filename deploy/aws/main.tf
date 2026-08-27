# intune-cmdb-sync on AWS: Lambda (container image) triggered by EventBridge Scheduler.
#
# Cost shape (us-east-1 list prices, outside the perpetual free tier):
#   Lambda            1 run/day x 5 min x 1024 MB = ~9,000 GB-s/month.
#                     At $0.0000166667/GB-s that is about              $0.15/mo
#   ECR               ~350 MB image at $0.10/GB-month                  $0.04/mo
#   EventBridge Sched 30 invocations/month; first 14M are free         $0.00/mo
#   S3                a few hundred KB of state plus 60 requests       ~$0.00/mo
#   SSM Parameter Store (Standard) for secrets                         $0.00/mo
#   CloudWatch Logs   a few MB; first 5 GB ingested is free            $0.00/mo
#                                                              -------------------
#                                                                    ~$0.20/month
#
# Two deliberate choices keep it there:
#   * The function is NOT in a VPC. A VPC-attached Lambda needs a NAT gateway to
#     reach Microsoft Graph, and that alone is ~$32/month.
#   * State goes in S3 rather than EFS, because EFS would force the VPC above.
#
# Secrets live in SSM Parameter Store (SecureString), which is free, rather than
# Secrets Manager at $0.40 per secret per month. See deploy/aws/README.md if
# your organisation mandates Secrets Manager.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------- variables

variable "region" {
  description = "AWS region."
  type        = string
  default     = "us-east-1"
}

variable "name" {
  description = "Base name for every resource."
  type        = string
  default     = "intune-cmdb-sync"
}

variable "image_uri" {
  description = "ECR image URI, including tag or digest."
  type        = string
}

variable "schedule_expression" {
  description = "EventBridge Scheduler expression. Default: 03:15 UTC daily."
  type        = string
  default     = "cron(15 3 * * ? *)"
}

variable "graph_tenant_id" {
  description = "Entra tenant ID."
  type        = string
}

variable "graph_client_id" {
  description = "Entra app registration (SPN) client ID."
  type        = string
}

variable "graph_client_secret" {
  description = "Entra app registration client secret."
  type        = string
  sensitive   = true
}

variable "servicenow_instance" {
  description = "ServiceNow instance: short name, host, or full https URL."
  type        = string
}

variable "servicenow_client_id" {
  description = "ServiceNow OAuth client ID."
  type        = string
}

variable "servicenow_client_secret" {
  description = "ServiceNow OAuth client secret."
  type        = string
  sensitive   = true
}

variable "discovery_source" {
  description = "Must match the sys_choice value registered on cmdb_ci.discovery_source."
  type        = string
  default     = "Intune"
}

variable "retire_missing_devices" {
  description = "Retire CIs for devices that have disappeared from Intune."
  type        = bool
  default     = false
}

variable "dry_run" {
  description = "Run without committing anything to the CMDB."
  type        = bool
  default     = false
}

variable "memory_mb" {
  description = "Lambda memory. CPU scales with it, so more memory can be cheaper overall."
  type        = number
  default     = 1024
}

variable "timeout_seconds" {
  description = "Lambda timeout. 900s is the hard ceiling AWS allows."
  type        = number
  default     = 900

  validation {
    condition     = var.timeout_seconds > 0 && var.timeout_seconds <= 900
    error_message = "Lambda supports a maximum timeout of 900 seconds. Very large tenants that cannot finish in 15 minutes should use ECS Fargate instead; see deploy/aws/README.md."
  }
}

variable "alert_email" {
  description = "Email for alerts. Empty disables alerting entirely."
  type        = string
  default     = ""
}

variable "log_retention_days" {
  description = "CloudWatch log retention."
  type        = number
  default     = 30
}

# ------------------------------------------------------------------ secrets

resource "aws_ssm_parameter" "graph_client_secret" {
  name        = "/${var.name}/graph-client-secret"
  description = "Entra app registration client secret for Microsoft Graph."
  type        = "SecureString"
  value       = var.graph_client_secret
}

resource "aws_ssm_parameter" "servicenow_client_secret" {
  name        = "/${var.name}/servicenow-client-secret"
  description = "ServiceNow OAuth client secret."
  type        = "SecureString"
  value       = var.servicenow_client_secret
}

# -------------------------------------------------------------------- state

resource "aws_s3_bucket" "state" {
  bucket        = "${var.name}-state-${data.aws_caller_identity.current.account_id}"
  force_destroy = false
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  # Versioning is the cheap insurance policy against a corrupted state file
  # triggering an unwanted retirement pass.
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }

  depends_on = [aws_s3_bucket_versioning.state]
}

# --------------------------------------------------------------------- IAM

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.name}-lambda"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "lambda" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda.arn}:*"]
  }

  statement {
    sid     = "ReadSecrets"
    actions = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = [
      aws_ssm_parameter.graph_client_secret.arn,
      aws_ssm_parameter.servicenow_client_secret.arn,
    ]
  }

  statement {
    sid       = "DecryptSecrets"
    actions   = ["kms:Decrypt"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.region}.amazonaws.com"]
    }
  }

  statement {
    sid     = "StateObject"
    actions = ["s3:GetObject", "s3:PutObject"]
    resources = [
      "${aws_s3_bucket.state.arn}/state.json",
      "${aws_s3_bucket.state.arn}/run-report.json",
    ]
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${var.name}-lambda"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}

# ------------------------------------------------------------------ compute

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "sync" {
  function_name = var.name
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = var.image_uri
  memory_size   = var.memory_mb
  timeout       = var.timeout_seconds

  environment {
    variables = {
      GRAPH_AUTH_MODE  = "client_secret"
      GRAPH_TENANT_ID  = var.graph_tenant_id
      GRAPH_CLIENT_ID  = var.graph_client_id
      INTUNE_OWNERSHIP = "company"

      SNOW_INSTANCE         = var.servicenow_instance
      SNOW_AUTH_MODE        = "oauth_client_credentials"
      SNOW_CLIENT_ID        = var.servicenow_client_id
      SNOW_WRITE_MODE       = "identify_reconcile"
      SNOW_DISCOVERY_SOURCE = var.discovery_source
      SNOW_RETIRE_MISSING   = tostring(var.retire_missing_devices)

      STATE_PATH = "s3://${aws_s3_bucket.state.id}/state.json"
      DRY_RUN    = tostring(var.dry_run)
      LOG_FORMAT = "json"
      LOG_LEVEL  = "INFO"

      # The run report is the only per-device record of what happened, and a
      # Lambda's filesystem does not outlive the invocation, so it goes to the
      # same bucket as the state file.
      RUN_REPORT_PATH    = "s3://${aws_s3_bucket.state.id}/run-report.json"
      RUN_REPORT_DEVICES = "true"

      # Without this a run where every device failed still exits 0.
      FAIL_ON_ERROR = "true"

      # The connector reads these two at startup from SSM Parameter Store
      # (see src/intune_cmdb_sync/secrets.py). Only the parameter *names* live
      # here; the values never enter the function configuration, where anyone
      # with lambda:GetFunctionConfiguration could read them.
      GRAPH_CLIENT_SECRET_PARAMETER = aws_ssm_parameter.graph_client_secret.name
      SNOW_CLIENT_SECRET_PARAMETER  = aws_ssm_parameter.servicenow_client_secret.name
    }
  }

  depends_on = [
    aws_iam_role_policy.lambda,
    aws_cloudwatch_log_group.lambda,
  ]
}

# ---------------------------------------------------------------- scheduler

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  name = "${var.name}-scheduler"
  role = aws_iam_role.scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.sync.arn
    }]
  })
}

resource "aws_scheduler_schedule" "daily" {
  name                         = var.name
  schedule_expression          = var.schedule_expression
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    # A CMDB sync does not care about the exact minute, and a flexible window
    # spreads load away from the top-of-hour spike on the ServiceNow instance.
    mode                      = "FLEXIBLE"
    maximum_window_in_minutes = 15
  }

  target {
    arn      = aws_lambda_function.sync.arn
    role_arn = aws_iam_role.scheduler.arn

    retry_policy {
      maximum_retry_attempts       = 1
      maximum_event_age_in_seconds = 3600
    }
  }
}

# ------------------------------------------------------------------ outputs

output "function_name" {
  value = aws_lambda_function.sync.function_name
}

output "state_bucket" {
  value = aws_s3_bucket.state.id
}

output "log_group" {
  value = aws_cloudwatch_log_group.lambda.name
}

output "manual_invoke_command" {
  description = "Run the sync immediately, without waiting for the schedule."
  value       = "aws lambda invoke --function-name ${aws_lambda_function.sync.function_name} --cli-binary-format raw-in-base64-out --payload '{\"dry_run\":true}' /dev/stdout"
}

# ---------------------------------------------------------------------------
# Alerting
#
# Log-based rather than metric-based: the run summary is a structured JSON line,
# and what is worth alerting on -- did it run at all, did devices fail -- lives
# in its fields rather than in Lambda's platform metrics. Lambda's own Errors
# metric would not fire here anyway: the handler returns a summary rather than
# raising, so a failed sync is a successful invocation.
# ---------------------------------------------------------------------------

locals {
  enable_alerts = var.alert_email != ""
}

resource "aws_sns_topic" "alerts" {
  count = local.enable_alerts ? 1 : 0
  name  = "${var.name}-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  count     = local.enable_alerts ? 1 : 0
  topic_arn = aws_sns_topic.alerts[0].arn
  protocol  = "email"
  endpoint  = var.alert_email
  # AWS emails a confirmation link; the subscription is inert until clicked.
}

resource "aws_cloudwatch_log_metric_filter" "run_complete" {
  count          = local.enable_alerts ? 1 : 0
  name           = "${var.name}-run-complete"
  log_group_name = aws_cloudwatch_log_group.lambda.name
  pattern        = "{ $.msg = \"run complete\" }"

  metric_transformation {
    name          = "RunsCompleted"
    namespace     = "IntuneCmdbSync"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_log_metric_filter" "device_errors" {
  count          = local.enable_alerts ? 1 : 0
  name           = "${var.name}-device-errors"
  log_group_name = aws_cloudwatch_log_group.lambda.name
  pattern        = "{ $.msg = \"run complete\" && $.errors > 0 }"

  metric_transformation {
    name          = "DeviceErrors"
    namespace     = "IntuneCmdbSync"
    value         = "$.errors"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "no_successful_run" {
  count             = local.enable_alerts ? 1 : 0
  alarm_name        = "${var.name}-no-successful-run"
  alarm_description = <<-DESC
    No completed run in the last 24 hours. Either the schedule stopped firing or
    every attempt failed before finishing. Nothing else will tell you: the CMDB
    simply goes stale.
  DESC

  namespace           = "IntuneCmdbSync"
  metric_name         = "RunsCompleted"
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"

  # The point of this alarm is the case where nothing was logged at all, which
  # produces no datapoints. Without this the alarm sits in INSUFFICIENT_DATA
  # forever and never fires -- precisely when it is most needed.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts[0].arn]
  ok_actions    = [aws_sns_topic.alerts[0].arn]
}

resource "aws_cloudwatch_metric_alarm" "device_errors" {
  count             = local.enable_alerts ? 1 : 0
  alarm_name        = "${var.name}-device-errors"
  alarm_description = <<-DESC
    A run finished but individual devices failed to write. Read the per-device
    outcomes in run-report.json in the state bucket; error_samples in the
    summary line carries the first twenty.
  DESC

  namespace           = "IntuneCmdbSync"
  metric_name         = "DeviceErrors"
  statistic           = "Sum"
  period              = 21600
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # A quiet period here genuinely means no errors were reported.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts[0].arn]
}

output "alerts_enabled" {
  value = local.enable_alerts
}
