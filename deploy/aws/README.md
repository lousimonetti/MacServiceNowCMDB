# AWS deployment

Lambda on a container image, triggered by EventBridge Scheduler.

## The two decisions that matter

**The function is not in a VPC.** A VPC-attached Lambda needs a NAT gateway to
reach `graph.microsoft.com`, and a NAT gateway is roughly $32/month plus data
processing — over a hundred times the cost of everything else here. Out of a VPC,
Lambda has direct internet egress and the connector only ever makes outbound
HTTPS calls to two public APIs.

**State goes in S3, not EFS.** EFS would force the VPC above. `STATE_PATH` accepts
an `s3://` URL for exactly this reason.

If your security posture requires egress through an inspected path, budget for
the NAT gateway or use ECS Fargate in a subnet that already has one — do not
discover it after the fact.

## Deploy

```bash
# 1. Build and push the image
aws ecr create-repository --repository-name intune-cmdb-sync
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-1
REPO="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/intune-cmdb-sync"

aws ecr get-login-password --region $REGION \
  | docker login --username AWS --password-stdin "$REPO"

docker build -f deploy/aws/Dockerfile.lambda -t "$REPO:latest" .
docker push "$REPO:latest"

# 2. Deploy
cd deploy/aws
cat > terraform.tfvars <<TFVARS
image_uri                = "$REPO:latest"
graph_tenant_id          = "..."
graph_client_id          = "..."
graph_client_secret      = "..."
servicenow_instance      = "acme"
servicenow_client_id     = "..."
servicenow_client_secret = "..."
dry_run                  = true
TFVARS

terraform init
terraform apply
```

Build the image for `linux/amd64`. On an Apple Silicon machine that means
`docker build --platform linux/amd64`, or set `architectures = ["arm64"]` on the
Lambda function — a mismatch fails at invoke time with an unhelpful error.

Verify, then set `dry_run = false` and re-apply:

```bash
aws lambda invoke --function-name intune-cmdb-sync \
  --cli-binary-format raw-in-base64-out \
  --payload '{"dry_run":true}' /dev/stdout
```

## What gets created

| Resource | Purpose |
| --- | --- |
| Lambda function (container image) | The sync |
| EventBridge Scheduler schedule | Daily trigger, 15-minute flexible window |
| S3 bucket | State file, versioned, encrypted, non-current versions expire at 30 days |
| SSM parameters (SecureString) ×2 | Graph and ServiceNow secrets |
| IAM roles ×2 | Execution role, scheduler invoke role |
| CloudWatch log group | 30-day retention |

Secrets live in **SSM Parameter Store**, not Secrets Manager: Standard-tier
parameters are free, where Secrets Manager is $0.40 per secret per month. Only
the parameter *names* go into the function's environment; the connector reads the
values at startup, so they are never exposed to
`lambda:GetFunctionConfiguration`.

If your organisation mandates Secrets Manager, the connector reads whatever the
`*_PARAMETER` variables point at through SSM only. To use Secrets Manager
instead, drop the two `aws_ssm_parameter` resources, create
`aws_secretsmanager_secret` resources, and inject the values with Lambda's
`secrets` extension or a small change to `secrets.py`. Budget $0.80/month for
the two secrets.

S3 versioning is deliberate. It is cheap insurance against a corrupted state file
triggering an unwanted retirement pass.

## Cost

List prices, us-east-1, one 5-minute run per day at 1024 MB, outside the
perpetual free tier.

| | Usage/month | Cost |
| --- | --- | --- |
| Lambda | ~9,000 GB-s | **~$0.15** |
| ECR | ~350 MB stored | **~$0.04** |
| EventBridge Scheduler | 30 invocations | **$0.00** — first 14M free |
| S3 | <1 MB, ~60 requests | **~$0.00** |
| SSM Parameter Store | Standard tier | **$0.00** |
| CloudWatch Logs | a few MB | **$0.00** — first 5 GB free |
| **Total** | | **~$0.20/month** |

Counter-intuitively, raising `memory_mb` can *lower* the bill: Lambda scales CPU
with memory, so a run that finishes in half the time at double the memory costs
the same and finishes sooner. 1024 MB is a reasonable starting point.

## The 15-minute ceiling

Lambda's hard maximum timeout is 900 seconds, and Terraform validates this.

A run is dominated by ServiceNow round-trips: at `SNOW_BATCH_SIZE=100` a
10,000-device fleet is ~100 IRE requests, comfortably inside the limit. Two
things push you over it:

- `INTUNE_FETCH_HARDWARE_DETAIL=true`, which adds one Graph call *per device*.
- Sustained throttling on a large tenant.

If you cannot finish in 15 minutes, move to an ECS Fargate scheduled task, which
has no timeout. It uses the same image and the same environment variables; only
the trigger changes. A daily 10-minute Fargate task at 0.25 vCPU / 0.5 GB is
roughly $0.05/month, so the migration costs nothing but the Terraform.

## Operating

```bash
# run now
aws lambda invoke --function-name intune-cmdb-sync /dev/stdout

# recent logs
aws logs tail /aws/lambda/intune-cmdb-sync --since 1h --format short

# the run summary from the last 7 days
aws logs filter-log-events \
  --log-group-name /aws/lambda/intune-cmdb-sync \
  --filter-pattern '{ $.msg = "run complete" }' \
  --start-time $(( ($(date +%s) - 604800) * 1000 )) \
  --query 'events[].message' --output text
```

### Alerting

Set `alert_email` and the alarms are created for you, with an SNS topic:

```hcl
alert_email = "ops@example.com"
```

| Alarm | Fires when |
| --- | --- |
| `<name>-no-successful-run` | no `run complete` line in 24 hours |
| `<name>-device-errors` | a run finished with `errors > 0` |

Leave it unset and no alerting resources are created — deliberately, so a
deployment is never *almost* monitored. AWS emails a subscription confirmation
link; the topic delivers nothing until it is clicked.

Two details worth knowing, because both are easy to get wrong by hand:

**The absence alarm sets `treat_missing_data = "breaching"`.** Its entire
purpose is the case where nothing was logged, which produces no datapoints.
With CloudWatch's default handling the alarm sits in `INSUFFICIENT_DATA`
forever and never fires — precisely when you need it.

**Lambda's own `Errors` metric will not fire for a failed sync.** The handler
returns a summary rather than raising, so that the scheduler does not retry a
partial sync against a healthy instance — which means a failed run is a
*successful* invocation as far as Lambda is concerned. Both alarms are therefore
driven by metric filters over the structured log, not by platform metrics.

## Teardown

```bash
terraform destroy
```

The state bucket has `force_destroy = false`, so empty it first if you genuinely
want it gone.
