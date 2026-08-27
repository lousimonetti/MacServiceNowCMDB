"""AWS Lambda entry point.

Deliberately thin: the Lambda handler is just a scheduler trigger wrapped around
the same `main()` the CLI and the container image use, so there is exactly one
code path to reason about.

Note on state: keep this function *out* of a VPC. A VPC-attached Lambda needs a
NAT gateway to reach Microsoft Graph, and that NAT gateway costs more per month
than the rest of this architecture combined. Put the state file in S3
(`STATE_PATH=s3://bucket/key.json`) instead of on EFS.
"""

from __future__ import annotations

from typing import Any

from .__main__ import EXIT_OK, main


def handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    """EventBridge Scheduler target.

    Returns a summary rather than raising, so a partial sync shows up as a
    successful invocation with a non-zero `exit_code` in the logs rather than as
    a Lambda error that the scheduler will retry against a healthy instance.
    """
    argv: list[str] = []
    if isinstance(event, dict):
        if event.get("dry_run"):
            argv.append("--dry-run")
        if event.get("check"):
            argv.append("--check")

    exit_code = main(argv)
    return {
        "exit_code": exit_code,
        "status": "ok" if exit_code == EXIT_OK else "failed",
    }
