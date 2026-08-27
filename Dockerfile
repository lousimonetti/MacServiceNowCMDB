# Runtime image for schedulers that run a container to completion:
# Azure Container Apps Jobs, ECS/Fargate scheduled tasks, Kubernetes CronJob, cron.
#
# For AWS Lambda use deploy/aws/Dockerfile.lambda instead — it needs the Lambda
# runtime interface baked in.

FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

# Build a wheel, then install it into a self-contained prefix we can copy into
# the runtime stage. Keeps build tooling out of the shipped image.
RUN pip install --no-cache-dir build hatchling \
 && python -m build --wheel --outdir /wheels \
 && pip install --no-cache-dir --prefix=/install /wheels/*.whl


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_FORMAT=json

# Certificates only — no shell utilities, no package manager cache.
#
# The run user is "icsync", not "sync": Debian already ships a system user
# called sync (uid 4), and useradd fails rather than warning.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --create-home --uid 10001 icsync

COPY --from=build /install /usr/local

# The state file lives here when a volume is mounted (Azure Files, EFS, emptyDir).
RUN mkdir -p /var/lib/intune-cmdb-sync && chown icsync:icsync /var/lib/intune-cmdb-sync
VOLUME ["/var/lib/intune-cmdb-sync"]

USER icsync
WORKDIR /home/icsync

# No secrets, no arguments baked in: everything comes from the environment.
ENTRYPOINT ["intune-cmdb-sync"]
