# AWS deployment

Two hosts, one stack, picked with `host`:

- **`lambda`** (default): Lambda on a container image, triggered by EventBridge
  Scheduler. Cheapest, simplest, capped at 15 minutes.
- **`ecs`**: an ECS Fargate scheduled task. It is the AWS counterpart of the
  Azure Container Apps job: the root `Dockerfile` image runs to completion with
  no time limit. See [ECS Fargate](#ecs-fargate).

The state bucket, secrets, schedule and alerting are shared by both.

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

# 2. Create the two secrets. Outside Terraform on purpose: see below.
#    read -s keeps them out of shell history.
read -rs GRAPH_SECRET && aws ssm put-parameter --type SecureString \
  --name /intune-cmdb-sync/graph-client-secret --value "$GRAPH_SECRET"
read -rs SNOW_SECRET && aws ssm put-parameter --type SecureString \
  --name /intune-cmdb-sync/servicenow-client-secret --value "$SNOW_SECRET"
unset GRAPH_SECRET SNOW_SECRET

# 3. Deploy
cd deploy/aws
cat > terraform.tfvars <<TFVARS
image_uri                = "$REPO:latest"
graph_tenant_id          = "..."
graph_client_id          = "..."
servicenow_instance      = "acme"
servicenow_client_id     = "..."
dry_run                  = true   # required; there is no default

graph_client_secret_parameter      = "/intune-cmdb-sync/graph-client-secret"
servicenow_client_secret_parameter = "/intune-cmdb-sync/servicenow-client-secret"

# Optional, but read these before a first run:
# class_map              = "windows=cmdb_ci_computer;macos=cmdb_ci_computer"
mapping_overrides_file   = "../../mapping-overrides.json"
TFVARS

terraform init
terraform apply
```

Build the image for `linux/amd64`. On an Apple Silicon machine that means
`docker build --platform linux/amd64`, or set `architectures = ["arm64"]` on the
Lambda function — a mismatch fails at invoke time with an unhelpful error.

**The secrets never pass through Terraform.** Given as variables, they would
be written in plaintext to `terraform.tfvars` and to the Terraform state, and
that state has no backend configured here, so it would be a local file.
Referenced by name, Terraform only builds the parameter ARNs; it never reads
the values. Rotating a secret is `aws ssm put-parameter --overwrite`, with no
apply needed.

`dry_run` has no default, deliberately. A default of `false` turns any apply
that forgets it into a live run; `deploy/azure/deploy.sh` removed its default
for the same reason.

`mapping_overrides_file` is read on the machine running Terraform, and its
contents minus `_comment` reach the job as `MAPPING_OVERRIDES_JSON`, because
the image holds no such file. Leave it out and `last_discovered` is sent again,
so every device is an UPDATE on every run. `class_map` becomes `SNOW_CLASS_MAP`,
which **replaces** the built-in map (windows, macos) rather than extending it.

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
| S3 bucket policy | Refuses any request not made over TLS |
| IAM roles ×2 | Execution role, scheduler invoke role |
| Lambda async invoke config | No automatic retries; see below |
| CloudWatch log group | 30-day retention |

The two SSM parameters are **not** created by the stack; you create them first
(step 2). Secrets live in **SSM Parameter Store**, not Secrets Manager:
Standard-tier parameters are free, where Secrets Manager is $0.40 per secret per
month. Only the parameter *names* go into the function's environment; the connector reads the
values at startup, so they are never exposed to
`lambda:GetFunctionConfiguration`.

If your organisation mandates Secrets Manager, the connector reads whatever the
`*_PARAMETER` variables point at through SSM only. To use Secrets Manager
instead, create the secrets there and inject the values with Lambda's
`secrets` extension or a small change to `secrets.py`. ECS can read Secrets
Manager in `valueFrom` directly. Budget $0.80/month for
the two secrets.

S3 versioning is deliberate. It is cheap insurance against a corrupted state file
triggering an unwanted retirement pass.

**One run at a time.** Two overlapping runs would both read and then write
`state.json`. Lambda has `reserved_concurrent_executions = 1`, so a second
invocation is throttled rather than run alongside. New AWS accounts have a
concurrency limit of 10, and AWS refuses any reservation that leaves fewer
than 10 unreserved. There, the apply fails saying so; set
`lambda_reserved_concurrency = -1` and avoid manual invokes during the
schedule window instead.

**No automatic retries.** The scheduler invokes Lambda asynchronously, and
Lambda retries a failed async invocation twice by default. The handler returns
rather than raises, so an ordinary failed sync was never retried, but a timeout
is a failure. Without the async invoke config, a run that hit the 15-minute
ceiling would go three times in a row. A missed day is what the
no-successful-run alarm is for.

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

If you cannot finish in 15 minutes, set `host = "ecs"` (see below). The task
has no timeout and receives the same settings. A daily 10-minute Fargate task
at 0.25 vCPU / 0.5 GB is roughly $0.05/month, so the switch costs nothing but
a re-apply.

## ECS Fargate

```bash
# 1. Build with the aws extra: STATE_PATH=s3://... imports boto3, and the image
#    without it builds fine and then fails on its first run.
aws ecr create-repository --repository-name intune-cmdb-sync
docker build --platform linux/amd64 --build-arg EXTRAS=aws -t "$REPO:v1" .
docker push "$REPO:v1"

# 2. Deploy: the Lambda tfvars above, plus
cat >> terraform.tfvars <<TFVARS
host       = "ecs"
image_uri  = "$REPO:v1"
subnet_ids = ["subnet-..."]
TFVARS
terraform apply
```

The Lambda image (`Dockerfile.lambda`) and the ECS image (the root
`Dockerfile`) are **not interchangeable**. The Lambda one needs the runtime
interface client; the ECS one runs `intune-cmdb-sync` as its entrypoint.

On Apple Silicon, either build `--platform linux/amd64` (emulated, slower) or
build natively and set `cpu_architecture = "ARM64"`, which is also about 20%
cheaper on Fargate. A mismatch fails when the task starts, not when it builds.

### Networking

Fargate always runs in a VPC; unlike Lambda it cannot opt out. The stack
creates a security group with no inbound rules and outbound 443 only.

| | `assign_public_ip` | Egress | Extra cost |
| --- | --- | --- | --- |
| Public subnet (default) | `true` | the task's own public IP | ~$0.01/month for the IP while running |
| Private subnet + NAT gateway | `false` | the NAT's Elastic IP | ~$32/month for the NAT, unless one exists |

Choose the private subnet if policy forbids public IPs, or if the ServiceNow
instance only accepts allowlisted addresses. A task's public IP changes on
every run; a NAT's Elastic IP does not.

### Secrets and roles

ECS resolves the two secrets from SSM at launch and injects them as
`GRAPH_CLIENT_SECRET` / `SNOW_CLIENT_SECRET`. The task definition holds only the
parameter ARNs. There are two roles:

- **Execution role:** used by ECS before the container starts, to pull from
  ECR, write logs and read the two parameters.
- **Task role:** what the connector runs as. It can read and write
  `state.json` and `run-report.json` in the state bucket, and nothing else.

### A one-off dry run

A scheduled task has no invoke payload, so override the environment instead.
`terraform output manual_invoke_command` prints the base command:

```bash
aws ecs run-task ... --overrides '{"containerOverrides":[{"name":"sync",
  "environment":[{"name":"DRY_RUN","value":"true"},
                 {"name":"INTUNE_DEVICE_LIMIT","value":"5"}]}]}'
```

`INTUNE_DEVICE_LIMIT` disables retirement for that run, so a truncated fleet
is never mistaken for vanished devices.

The container's exit code (0, or 3/4 for a failed or degraded run) appears on
the stopped task. The alarms below read the log instead, and work the same as
for Lambda. Logs go to `/ecs/<name>`.

ECS has no equivalent of Lambda's reserved concurrency, so nothing stops a
manual `run-task` overlapping the scheduled one. Check first:
`aws ecs list-tasks --cluster <name> --desired-status RUNNING`.

### Switching an existing Lambda deployment

Upgrading a stack deployed from an earlier version of this template:

- **Secrets:** the old template created the two parameters itself. `removed`
  blocks drop them from the Terraform state **without deleting them**. Replace
  `graph_client_secret` / `servicenow_client_secret` in `terraform.tfvars` with
  the two `*_parameter` variables, set to `/<name>/graph-client-secret` and
  `/<name>/servicenow-client-secret`.
- **`moved` blocks** carry the Lambda resources and the log group to their new
  addresses, so the function is updated in place, not replaced.
- **New:** the bucket policy, the async invoke config and reserved concurrency.
- **Plan first.** The plan should show no destroy for the function, the bucket
  or the log group. This path has only been checked against a mocked provider,
  never against a real account.

Switching an existing stack to `ecs` destroys the function and creates the
task. The state bucket and schedule are kept. The log group is replaced,
because its path changes.

## Run the image locally, without Docker Desktop

Any OCI runtime builds the root `Dockerfile`. On a Mac:

| Tool | Setup |
| --- | --- |
| Colima | `brew install colima docker && colima start`, then the plain `docker` CLI |
| Podman | `brew install podman && podman machine init && podman machine start`; swap `docker` for `podman` |
| Apple `container` | Apple Silicon only; `container system start`, then `container build` / `container run` |

```bash
docker build --build-arg EXTRAS=aws -t intune-cmdb-sync:local .

set -a; source .env; set +a
mkdir -p out
docker run --rm \
  --env-file <(grep -oE '^[A-Z_][A-Z0-9_]*' .env) \
  -v "$PWD/mapping-overrides.json:/home/icsync/mapping-overrides.json:ro" \
  -v "$PWD/out:/out" \
  intune-cmdb-sync:local \
  --dry-run --limit 5 --report-devices --report /out/run.json
```

The `--env-file` there lists variable **names only**, so the container takes
each value from your shell after `source` has parsed it. Pointing
`--env-file` straight at `.env` looks equivalent but is not. Docker does not
strip quotes, so `KEY="a b"` arrives with the quote characters as part of the
value. For `SNOW_SOURCE_FEED` that changes the name IRE uses to recognise the
source.

`mapping-overrides.json` is mounted where the relative
`MAPPING_OVERRIDES_FILE=./mapping-overrides.json` resolves inside the image
(`/home/icsync`). `out/` is the only way to get `run.json` back, because
nothing else survives `--rm`.

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
| `<name>-run-degraded` | a run aborted (`sync failed`), or finished degraded: the mass-retirement guard tripped or the state write failed |

A degraded run still logs `run complete` and can have zero errors, so neither
of the first two alarms sees it. It gets its own filter because it is the
state that leaves the *next* run unable to reason about the fleet.

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
*successful* invocation as far as Lambda is concerned. All three alarms are therefore
driven by metric filters over the structured log, not by platform metrics.

## Teardown

```bash
terraform destroy
```

The state bucket has `force_destroy = false`, so empty it first if you genuinely
want it gone. The two SSM parameters were never Terraform's, so they survive a
destroy; delete them with `aws ssm delete-parameter` if you mean to.
