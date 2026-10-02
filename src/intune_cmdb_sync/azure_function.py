"""Azure Functions entry point.

Deliberately thin, like `aws_lambda.handler`: the timer trigger in
`deploy/azure/functions/function_app.py` calls `run()`, which calls the same
`main()` the CLI uses, so there is exactly one code path to reason about. This
module holds the logic so it is testable without the `azure-functions` package;
`function_app.py` only binds it to a schedule.

Unlike the Lambda handler, a failed run raises. A timer trigger has no retry
unless a retry policy is configured, and none is, so raising cannot rerun a
sync. What it buys is the invocation showing as Failed in the portal and in
Application Insights, next to the log-based alerts that remain the primary
signal.
"""

from __future__ import annotations

from .__main__ import EXIT_OK, main
from .logging_setup import adopt_host_handlers, reset_run_id


class SyncRunFailed(RuntimeError):
    """A run finished with a non-zero exit code."""

    def __init__(self, exit_code: int) -> None:
        super().__init__(
            f"intune-cmdb-sync exited {exit_code}; see the 'run complete' or "
            "'sync failed' trace with this invocation's run_id"
        )
        self.exit_code = exit_code


def run(argv: list[str] | None = None) -> int:
    """Run one sync inside the Functions host. Raises SyncRunFailed on non-zero."""
    # Must precede main(): main() configures logging, and without this it would
    # replace the worker's handler, the only route to Application Insights.
    adopt_host_handlers()
    # A warm instance runs several invocations in one process; each is its own run.
    reset_run_id()
    exit_code = main(argv or [])
    if exit_code != EXIT_OK:
        raise SyncRunFailed(exit_code)
    return exit_code
