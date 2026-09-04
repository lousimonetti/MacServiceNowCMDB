"""Command-line entry point."""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime

from . import __version__
from .config import Config
from .errors import ConfigError, SyncError
from .graph import GraphClient
from .logging_setup import configure_logging
from .models import RunReport
from .servicenow.client import ServiceNowClient
from .servicenow.probe import format_report, probe_endpoints
from .servicenow.writers import verify_write_access
from .storage import build_state_store
from .sync import SyncRunner

log = logging.getLogger("intune_cmdb_sync")

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_FAILED = 3
EXIT_PARTIAL = 4  # ran, but did not do its whole job


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="intune-cmdb-sync",
        description=(
            "Sync corporate-owned Microsoft Intune devices into the ServiceNow CMDB "
            "through the Identification and Reconciliation Engine."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Read from Intune and run IRE identification against ServiceNow without "
            "committing anything. Overrides DRY_RUN=false."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help=(
            "Process at most N devices. A testing aid for a first run against a real "
            "instance: it keeps the write small enough to inspect by hand. Retirement "
            "is skipped while a limit is set, because a truncated device list makes "
            "the rest of the fleet look like it disappeared. Overrides "
            "INTUNE_DEVICE_LIMIT."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate configuration and connectivity to both systems, then exit.",
    )
    parser.add_argument(
        "--check-api",
        action="store_true",
        help=(
            "Probe each ServiceNow endpoint the connector can use, one HTTP method at a "
            "time, and report which are allowed and which are refused -- and by what: the "
            "OAuth REST gate, a role/ACL, or a missing API. Writes nothing (the probes "
            "cannot create a CI even when fully authorized) and does not touch Graph. Run "
            "this when a run fails with 'User Not Authorized'. Exits 3 if the endpoint the "
            "configured write mode uses is refused."
        ),
    )
    parser.add_argument("--log-level", help="Override LOG_LEVEL (DEBUG, INFO, WARNING, ERROR).")
    parser.add_argument("--log-format", choices=("json", "text"), help="Override LOG_FORMAT.")
    parser.add_argument(
        "--report",
        help="Write the JSON run report to this path. Overrides RUN_REPORT_PATH.",
    )
    parser.add_argument(
        "--report-devices",
        action="store_true",
        help="Include the per-device outcome list in the JSON report.",
    )
    parser.add_argument(
        "--fail-on-error",
        action="store_true",
        help="Exit non-zero if any individual device failed to write.",
    )
    return parser


@contextlib.contextmanager
def _apply_overrides(args: argparse.Namespace) -> Iterator[None]:
    """Apply CLI flags as environment overrides, for this call only.

    Configuration is read from the environment, so a flag has to become one. But
    the change must not outlive the call: `aws_lambda.handler` invokes `main()`
    repeatedly in a warm container, and a permanent mutation meant one
    invocation with `dry_run` left every later invocation silently in dry-run
    mode. Flags stay one-way -- absence never clears a value set by the
    environment -- so `--dry-run` forces a dry run and its absence defers to
    DRY_RUN.
    """
    previous: dict[str, str | None] = {}

    def override(name: str, value: str | None) -> None:
        if value is None:
            return
        previous[name] = os.environ.get(name)
        os.environ[name] = value

    override("DRY_RUN", "true" if args.dry_run else None)
    override("LOG_LEVEL", args.log_level)
    override("LOG_FORMAT", args.log_format)
    override("RUN_REPORT_PATH", args.report)
    override("INTUNE_DEVICE_LIMIT", None if args.limit is None else str(args.limit))
    override("RUN_REPORT_DEVICES", "true" if args.report_devices else None)
    override("FAIL_ON_ERROR", "true" if args.fail_on_error else None)

    try:
        yield
    finally:
        for name, old in previous.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old


def _write_report(report: RunReport, location: str | None, include_devices: bool) -> None:
    """Persist the run report to a local path or an s3:// URL.

    Routed through storage.py so the AWS deployment can keep reports somewhere
    that outlives the invocation; a Lambda's filesystem does not.
    """
    if not location:
        return
    store = build_state_store(location)
    if store is None:
        return
    try:
        store.write(json.dumps(report.as_dict(include_outcomes=include_devices), indent=2))
        log.info("wrote run report", extra={"path": location, "devices_included": include_devices})
    except Exception as exc:
        # Losing the report must not fail a run that otherwise did its job, but
        # it does mean the diagnostics for this run no longer exist.
        report.warnings.append(f"could not write run report to {location}: {exc}")
        log.error("could not write run report", extra={"path": location, "error": str(exc)})


def _check_api(cfg: Config) -> int:
    """Report, per endpoint and per method, what this credential may call.

    `--check` stops at the first refusal and cannot say whether the next
    endpoint would have behaved the same way. The blocker this exists for --
    an OAuth client refused at the REST gate -- is bound per API and per HTTP
    method, so the actionable fact is the *shape* of the refusals across the
    whole matrix, not any single one of them.

    Graph is deliberately not contacted: this is for the half of the setup that
    is blocked, and it should still run when Graph is down.
    """
    with ServiceNowClient(cfg.servicenow) as snow:
        report = probe_endpoints(snow, cfg.servicenow)

    # Straight to stdout, not the logger: this is a table for a human to read
    # and forward to a ServiceNow admin, and JSON log formatting would shred it.
    print(format_report(report))

    if report.write_path_ok:
        log.info(
            "write endpoint reachable",
            extra={"write_mode": cfg.servicenow.write_mode},
        )
        return EXIT_OK
    log.error(
        "the endpoint this write mode uses is not callable by these credentials",
        extra={"write_mode": cfg.servicenow.write_mode},
    )
    return EXIT_FAILED


def _check(cfg: Config) -> int:
    """Validate configuration and both connections, writing nothing.

    Read access is the easy half. The failures that actually bite on a new
    instance -- a missing `itil` role, an unregistered discovery source -- are
    on the write path, so that is simulated too.
    """
    with ServiceNowClient(cfg.servicenow) as snow:
        identity = snow.verify_connectivity()
        log.info("ServiceNow reachable", extra={"instance": identity.get("instance")})

        write_access = verify_write_access(snow, cfg.servicenow)
        if write_access.verified:
            log.info("ServiceNow write path verified", extra={"detail": write_access.detail})
            for caveat in write_access.caveats:
                # `verified` means the write path is callable, not that the
                # first run will succeed. Anything in that gap has to be said
                # out loud, or a pass reads as a guarantee it is not.
                log.warning("write path caveat", extra={"detail": caveat})
        else:
            # Not proven broken, but not proven working either. Saying "passed"
            # here would be the whole point of this check, inverted.
            log.warning(
                "ServiceNow write path NOT verified", extra={"detail": write_access.detail}
            )

    with GraphClient(cfg.graph) as graph:
        devices = graph.iter_managed_devices()
        first = next(devices, None)
        log.info(
            "Microsoft Graph reachable",
            extra={
                "sample_device": (first or {}).get("deviceName"),
                "found_any": first is not None,
            },
        )
    if write_access.verified:
        log.info("configuration and connectivity check passed")
        return EXIT_OK
    log.warning(
        "configuration and connectivity check passed, except the write path, "
        "which could not be simulated on this instance"
    )
    return EXIT_PARTIAL


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with _apply_overrides(args):
        return _run(args)


def _run(args: argparse.Namespace) -> int:
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        configure_logging(os.environ.get("LOG_LEVEL", "INFO"), os.environ.get("LOG_FORMAT", "text"))
        log.error("%s", exc)
        return EXIT_CONFIG

    configure_logging(cfg.runtime.log_level, cfg.runtime.log_format)
    log.info(
        "starting intune-cmdb-sync",
        extra={
            "version": __version__,
            "dry_run": cfg.runtime.dry_run,
            "write_mode": cfg.servicenow.write_mode,
            "ownership": cfg.graph.ownership,
            "graph_auth": cfg.graph.auth_mode,
            "snow_auth": cfg.servicenow.auth_mode,
        },
    )

    try:
        if args.check_api:
            return _check_api(cfg)

        if args.check:
            return _check(cfg)

        with GraphClient(cfg.graph) as graph, ServiceNowClient(cfg.servicenow) as snow:
            runner = SyncRunner(cfg, graph=graph, snow=snow)
            try:
                report = runner.run()
            except SyncError as exc:
                # Write the partial report before giving up. A failed run is
                # exactly when the per-device detail is worth having, and
                # returning here without it used to throw it away.
                log.error("sync failed", extra={"error": str(exc)})
                partial = runner.report
                partial.degrade(f"run aborted: {exc}")
                partial.finished_at = datetime.now(UTC)
                _write_report(
                    partial, cfg.runtime.run_report_path, cfg.runtime.report_devices
                )
                return EXIT_FAILED
    except SyncError as exc:
        # Raised while building the clients, before there is a runner or report.
        log.error("sync failed", extra={"error": str(exc)})
        return EXIT_FAILED
    except KeyboardInterrupt:  # pragma: no cover
        log.warning("interrupted")
        return EXIT_FAILED

    _write_report(report, cfg.runtime.run_report_path, cfg.runtime.report_devices)

    summary = report.summary()
    log.info("run complete", extra=summary)
    for warning in report.warnings:
        log.warning("%s", warning)

    if report.degraded:
        # A tripped guard or a lost state file means the next run cannot be
        # trusted to behave correctly. Exiting 0 hides that from the scheduler.
        log.error(
            "run completed in a degraded state",
            extra={"conditions": len(report.degraded)},
        )
        return EXIT_PARTIAL
    if report.errors and cfg.runtime.fail_on_error:
        return EXIT_PARTIAL
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
