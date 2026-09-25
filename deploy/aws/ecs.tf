# ECS Fargate scheduled task: the AWS counterpart of the Azure Container Apps
# job. Created only when host = "ecs". It runs the root Dockerfile image (built
# with --build-arg EXTRAS=aws) to completion, with no 15-minute ceiling.
#
# Cost shape (us-east-1 list prices), one 5-minute run per day:
#   Fargate           0.25 vCPU + 0.5 GB x 2.5 h/month                 ~$0.03/mo
#   Public IPv4       $0.005/h, only while the task runs               ~$0.01/mo
#   ECR               ~200 MB image at $0.10/GB-month                  ~$0.02/mo
#   Scheduler, S3, SSM, CloudWatch Logs: as in main.tf                 ~$0.00/mo
#                                                              -------------------
#                                                                    ~$0.06/month
#
# Unlike Lambda, a Fargate task always runs in a VPC. The default here is a
# public subnet with assign_public_ip = true and a security group with no
# inbound rules: outbound HTTPS to Graph and ServiceNow with no NAT gateway,
# which would otherwise cost ~$32/month. Use a private subnet behind a NAT
# gateway instead (assign_public_ip = false) if policy forbids public IPs, or if
# the ServiceNow instance only accepts allowlisted source addresses: an Elastic
# IP on the NAT gives the task a fixed egress address, which a public task IP
# is not.

variable "subnet_ids" {
  description = "ECS only. Subnets the task may run in, all in one VPC. Public subnets unless assign_public_ip = false."
  type        = list(string)
  default     = []
}

variable "assign_public_ip" {
  description = "ECS only. true for a public subnet with no NAT; false for a private subnet behind a NAT gateway."
  type        = bool
  default     = true
}

variable "ecs_cpu" {
  description = "ECS only. Fargate CPU units. 256 = 0.25 vCPU."
  type        = number
  default     = 256
}

variable "ecs_memory_mb" {
  description = "ECS only. Fargate memory; must be a valid pairing with ecs_cpu."
  type        = number
  default     = 512
}

variable "cpu_architecture" {
  description = "ECS only. X86_64 or ARM64; must match the image. ARM64 is cheaper and builds natively on Apple Silicon."
  type        = string
  default     = "X86_64"

  validation {
    condition     = contains(["X86_64", "ARM64"], var.cpu_architecture)
    error_message = "cpu_architecture must be X86_64 or ARM64."
  }
}

# ------------------------------------------------------------------ network

data "aws_subnet" "ecs" {
  count = local.use_ecs ? 1 : 0
  id    = element(concat(var.subnet_ids, [""]), 0)

  lifecycle {
    precondition {
      condition     = length(var.subnet_ids) > 0
      error_message = "host = \"ecs\" needs subnet_ids: a Fargate task always runs in a VPC."
    }
  }
}

resource "aws_security_group" "ecs" {
  count       = local.use_ecs ? 1 : 0
  name        = "${var.name}-task"
  description = "intune-cmdb-sync task: outbound HTTPS only, nothing inbound."
  vpc_id      = data.aws_subnet.ecs[0].vpc_id

  # No ingress block: the task only makes outbound calls, to Graph, ServiceNow,
  # S3, SSM, ECR and CloudWatch Logs -- all HTTPS.
  egress {
    description = "HTTPS to Graph, ServiceNow and AWS APIs"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# --------------------------------------------------------------------- IAM

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

# The execution role is what ECS itself uses before the container starts: pull
# the image, create the log stream, and resolve the secrets into env vars.
resource "aws_iam_role" "ecs_execution" {
  count              = local.use_ecs ? 1 : 0
  name               = "${var.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  count      = local.use_ecs ? 1 : 0
  role       = aws_iam_role.ecs_execution[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ecs_execution_secrets" {
  statement {
    sid     = "ReadSecrets"
    actions = ["ssm:GetParameters"]
    resources = [
      local.graph_secret_parameter_arn,
      local.servicenow_secret_parameter_arn,
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
}

resource "aws_iam_role_policy" "ecs_execution_secrets" {
  count  = local.use_ecs ? 1 : 0
  name   = "${var.name}-ecs-secrets"
  role   = aws_iam_role.ecs_execution[0].id
  policy = data.aws_iam_policy_document.ecs_execution_secrets.json
}

# The task role is what the connector itself runs as. Secrets arrive as env
# vars, so all it needs is the two state-bucket objects.
resource "aws_iam_role" "ecs_task" {
  count              = local.use_ecs ? 1 : 0
  name               = "${var.name}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "ecs_task" {
  statement {
    sid     = "StateObject"
    actions = ["s3:GetObject", "s3:PutObject"]
    resources = [
      "${aws_s3_bucket.state.arn}/state.json",
      "${aws_s3_bucket.state.arn}/run-report.json",
    ]
  }
}

resource "aws_iam_role_policy" "ecs_task" {
  count  = local.use_ecs ? 1 : 0
  name   = "${var.name}-ecs-task"
  role   = aws_iam_role.ecs_task[0].id
  policy = data.aws_iam_policy_document.ecs_task.json
}

# ------------------------------------------------------------------ compute

resource "aws_ecs_cluster" "sync" {
  count = local.use_ecs ? 1 : 0
  name  = var.name
}

resource "aws_ecs_task_definition" "sync" {
  count                    = local.use_ecs ? 1 : 0
  family                   = var.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.ecs_cpu
  memory                   = var.ecs_memory_mb
  execution_role_arn       = aws_iam_role.ecs_execution[0].arn
  task_role_arn            = aws_iam_role.ecs_task[0].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([{
    name      = "sync"
    image     = var.image_uri
    essential = true

    environment = [for k, v in local.sync_env : { name = k, value = v }]

    # ECS resolves these from SSM at launch. The task definition holds only the
    # parameter ARNs, so the values are not readable via DescribeTaskDefinition.
    secrets = [
      { name = "GRAPH_CLIENT_SECRET", valueFrom = local.graph_secret_parameter_arn },
      { name = "SNOW_CLIENT_SECRET", valueFrom = local.servicenow_secret_parameter_arn },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.sync.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "sync"
      }
    }
  }])

  depends_on = [aws_iam_role_policy.ecs_execution_secrets]
}

locals {
  ecs_run_task_command = local.use_ecs ? join(" ", [
    "aws ecs run-task --cluster ${var.name} --launch-type FARGATE",
    "--task-definition ${var.name}",
    "--network-configuration 'awsvpcConfiguration={subnets=[${join(",", var.subnet_ids)}],securityGroups=[${aws_security_group.ecs[0].id}],assignPublicIp=${var.assign_public_ip ? "ENABLED" : "DISABLED"}}'",
  ]) : null
}
