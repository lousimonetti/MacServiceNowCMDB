# Offline checks of the host toggle and the settings pass-through. The AWS
# provider is mocked, so this needs no credentials and creates nothing:
#   terraform init -backend=false && terraform test
# Needs Terraform 1.7+ (mock_provider), the same floor as the stack's removed blocks.

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_data "aws_subnet" {
    defaults = { vpc_id = "vpc-0123456789abcdef0" }
  }

  # Generated values are random strings; the provider validates ARNs.
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }
  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:us-east-1:123456789012:function:mock" }
  }
  mock_resource "aws_ecs_cluster" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:cluster/mock" }
  }
  mock_resource "aws_ecs_task_definition" {
    defaults = {
      arn                  = "arn:aws:ecs:us-east-1:123456789012:task-definition/mock:1"
      arn_without_revision = "arn:aws:ecs:us-east-1:123456789012:task-definition/mock"
    }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::mock" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:us-east-1:123456789012:mock" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:mock" }
  }
}

variables {
  image_uri            = "123456789012.dkr.ecr.us-east-1.amazonaws.com/intune-cmdb-sync:test"
  graph_tenant_id      = "tenant"
  graph_client_id      = "graph-client"
  servicenow_instance  = "example"
  servicenow_client_id = "snow-client"
  dry_run              = true

  graph_client_secret_parameter      = "/intune-cmdb-sync/graph-client-secret"
  servicenow_client_secret_parameter = "/intune-cmdb-sync/servicenow-client-secret"
}

run "lambda_is_the_default_host" {
  command = apply

  assert {
    condition     = length(aws_lambda_function.sync) == 1 && length(aws_ecs_task_definition.sync) == 0
    error_message = "the default host must create the Lambda and no ECS resources"
  }

  assert {
    condition     = aws_lambda_function.sync[0].environment[0].variables["DRY_RUN"] == "true"
    error_message = "dry_run must reach the function as DRY_RUN"
  }

  assert {
    condition     = !contains(keys(aws_lambda_function.sync[0].environment[0].variables), "MAPPING_OVERRIDES_JSON")
    error_message = "MAPPING_OVERRIDES_JSON must be absent when no overrides file is given"
  }

  assert {
    condition     = !contains(keys(aws_lambda_function.sync[0].environment[0].variables), "SNOW_CLASS_MAP")
    error_message = "an unset class_map must leave the built-in map in effect, not send an empty one"
  }

  assert {
    condition     = aws_cloudwatch_log_group.sync.name == "/aws/lambda/intune-cmdb-sync"
    error_message = "Lambda writes to /aws/lambda/<name>"
  }

  assert {
    condition     = aws_lambda_function.sync[0].environment[0].variables["GRAPH_CLIENT_SECRET_PARAMETER"] == "/intune-cmdb-sync/graph-client-secret"
    error_message = "Lambda must receive the parameter name, which the connector resolves at startup"
  }

  assert {
    condition     = aws_lambda_function_event_invoke_config.sync[0].maximum_retry_attempts == 0
    error_message = "a timed-out run must not be retried by Lambda's async retry"
  }

  assert {
    condition     = aws_lambda_function.sync[0].reserved_concurrent_executions == 1
    error_message = "two Lambda runs must never overlap on state.json"
  }

  assert {
    condition = anytrue([
      for st in jsondecode(aws_s3_bucket_policy.state.policy).Statement :
      st.Effect == "Deny" && st.Condition.Bool["aws:SecureTransport"] == "false"
    ])
    error_message = "the state bucket must refuse non-TLS requests"
  }

  assert {
    condition     = length(aws_cloudwatch_metric_alarm.run_degraded) == 0
    error_message = "no alert_email means no alerting resources at all"
  }
}

run "alerting_covers_degraded_runs" {
  command = apply

  variables {
    alert_email = "ops@example.com"
  }

  assert {
    condition     = strcontains(aws_cloudwatch_log_metric_filter.run_degraded[0].pattern, "run completed in a degraded state")
    error_message = "a degraded run logs run complete, so it needs its own filter"
  }

  assert {
    condition     = strcontains(aws_cloudwatch_log_metric_filter.run_degraded[0].pattern, "sync failed")
    error_message = "an aborted run must alert too"
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.run_degraded[0].threshold == 0 && aws_cloudwatch_metric_alarm.run_degraded[0].comparison_operator == "GreaterThanThreshold"
    error_message = "a single degraded run must fire the alarm"
  }
}

run "ecs_host_passes_overrides_and_class_map" {
  command = apply

  variables {
    host                   = "ecs"
    subnet_ids             = ["subnet-0123456789abcdef0"]
    class_map              = "windows=cmdb_ci_computer;macos=cmdb_ci_computer"
    mapping_overrides_file = "tests/overrides.json"
  }

  assert {
    condition     = length(aws_lambda_function.sync) == 0 && length(aws_ecs_task_definition.sync) == 1
    error_message = "host = ecs must create the task definition and no Lambda"
  }

  assert {
    condition     = jsondecode(local.sync_env["MAPPING_OVERRIDES_JSON"]) == { drop = ["last_discovered"] }
    error_message = "overrides must reach the task minus _comment"
  }

  assert {
    condition     = local.sync_env["SNOW_CLASS_MAP"] == "windows=cmdb_ci_computer;macos=cmdb_ci_computer"
    error_message = "class_map must pass through as-is"
  }

  assert {
    condition = alltrue([
      for c in jsondecode(aws_ecs_task_definition.sync[0].container_definitions) :
      length(c.secrets) == 2 && !anytrue([for e in c.environment : endswith(e.name, "SECRET")])
    ])
    error_message = "secrets must arrive through ECS secrets, never as plain environment values"
  }

  assert {
    condition = alltrue([
      for c in jsondecode(aws_ecs_task_definition.sync[0].container_definitions) :
      contains([for x in c.secrets : x.valueFrom], "arn:aws:ssm:us-east-1:123456789012:parameter/intune-cmdb-sync/graph-client-secret")
    ])
    error_message = "the parameter ARN must be built from the name with exactly one slash"
  }

  assert {
    condition     = aws_cloudwatch_log_group.sync.name == "/ecs/intune-cmdb-sync"
    error_message = "ECS writes to /ecs/<name>"
  }

  assert {
    condition     = length(aws_scheduler_schedule.daily.target[0].ecs_parameters) == 1
    error_message = "the schedule must target the ECS task"
  }
}

run "ecs_without_subnets_is_refused" {
  command = plan

  variables {
    host = "ecs"
  }

  expect_failures = [data.aws_subnet.ecs]
}
