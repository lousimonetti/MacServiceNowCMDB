"""`intune-cmdb-query` -- read-only CMDB inspection.

A separate entry point from `intune-cmdb-sync` on purpose. The sync command
writes; this one cannot, and keeping them apart means nobody has to read an
argument list to be sure which is which.

Selection is AND-ed across the flags given, so `--source Intune --os macOS`
means both. With no selector at all it reports every CI carrying the configured
discovery source, which is the connector's own footprint.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from . import __version__
from .cmdb_report import (
    CI_FIELDS,
    DEFAULT_MAX_ROWS,
    Cell,
    CmdbReader,
    CmdbReport,
    analyze,
    by_discovery_source,
    by_field,
    combine,
)
from .config import Config
from .errors import ConfigError, SyncError
from .logging_setup import configure_logging
from .servicenow.client import ServiceNowClient

log = logging.getLogger("intune_cmdb_sync.query")

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_FAILED = 3
EXIT_FINDINGS = 4  # only with --fail-on-findings

# The columns worth a terminal's width. The rest are in --format detail/json.
SUMMARY_COLUMNS: tuple[str, ...] = (
    "name",
    "serial_number",
    "model_id",
    "os_version",
    "assigned_to",
    "install_status",
    "sys_updated_on",
)

MAX_COLUMN_WIDTH = 28


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="intune-cmdb-query",
        description=(
            "Query the ServiceNow CMDB and report what is on the CIs. Read-only: "
            "it issues GET requests against the Table API and never writes."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    select = parser.add_argument_group("selection (AND-ed together)")
    select.add_argument(
        "--table",
        help="CMDB class table to read. Defaults to SNOW_DEFAULT_CLASS, else cmdb_ci_computer.",
    )
    select.add_argument(
        "--source",
        help=(
            "Filter on discovery_source. Defaults to SNOW_DISCOVERY_SOURCE, which scopes "
            "the report to CIs this connector claims."
        ),
    )
    select.add_argument(
        "--all-sources",
        action="store_true",
        help="Do not filter on discovery_source. Reports CIs from any source, including manual.",
    )
    select.add_argument(
        "--serial", action="append", metavar="SERIAL", help="Match serial_number. Repeatable."
    )
    select.add_argument(
        "--name", action="append", metavar="NAME", help="Match name exactly. Repeatable."
    )
    select.add_argument(
        "--intune-id",
        action="append",
        metavar="ID",
        help=(
            "Match the correlation field (SNOW_CORRELATION_FIELD) against an Intune "
            "device id. Repeatable."
        ),
    )
    select.add_argument(
        "--sys-id", action="append", metavar="SYS_ID", help="Match sys_id. Repeatable."
    )
    select.add_argument(
        "--query",
        metavar="ENCODED",
        help=(
            "Raw encoded query, AND-ed with the other selectors "
            "(e.g. 'install_status=1^osLIKEmac'). Passed through unvalidated."
        ),
    )

    output = parser.add_argument_group("output")
    output.add_argument(
        "--fields",
        metavar="A,B,C",
        help="Comma-separated field list to read instead of the connector's own field set.",
    )
    output.add_argument(
        "--format",
        choices=("table", "detail", "json"),
        default="table",
        help="table: one line per CI (default). detail: every field, one CI per block. json: full.",
    )
    output.add_argument(
        "--output", metavar="PATH", help="Write the report to this file instead of stdout."
    )
    output.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_MAX_ROWS,
        metavar="N",
        help=f"Stop after N rows (default {DEFAULT_MAX_ROWS}). Paging is automatic below that.",
    )
    output.add_argument(
        "--no-findings",
        action="store_true",
        help="Skip the validation checks and just list the CIs.",
    )
    output.add_argument(
        "--fail-on-findings",
        action="store_true",
        help="Exit 4 when any validation finding is reported. Off by default; this is a report.",
    )
    output.add_argument("--log-level", help="Override LOG_LEVEL (DEBUG, INFO, WARNING, ERROR).")
    return parser


def _build_query(args: argparse.Namespace, cfg: Config) -> str:
    """Turn the selection flags into one encoded query.

    `by_field` rejects a value list that is entirely blank or contains a `^`,
    which would otherwise inject a condition and widen the match; that surfaces
    here as a plain error rather than a traceback.
    """
    clauses: list[str] = []

    source = args.source or cfg.servicenow.discovery_source
    if not args.all_sources and source:
        clauses.append(by_discovery_source(source))

    if args.serial:
        clauses.append(by_field("serial_number", args.serial))
    if args.name:
        clauses.append(by_field("name", args.name))
    if args.sys_id:
        clauses.append(by_field("sys_id", args.sys_id))
    if args.intune_id:
        correlation_field = cfg.servicenow.correlation_field
        if not correlation_field:
            raise SyncError(
                "--intune-id needs a correlation field; set SNOW_CORRELATION_FIELD to the "
                "column that holds the Intune device id"
            )
        clauses.append(by_field(correlation_field, args.intune_id))
    if args.query:
        clauses.append(args.query)

    query = combine(*clauses)
    if not query:
        # An empty sysparm_query reads the entire class table. That is a
        # legitimate thing to want, but never by accident.
        raise SyncError(
            "no selection: --all-sources with no other filter would read the whole table. "
            "Add a selector, or pass --query to say so explicitly."
        )
    return query


def _fields_for(args: argparse.Namespace, cfg: Config) -> tuple[str, ...]:
    if args.fields:
        return tuple(f.strip() for f in args.fields.split(",") if f.strip())
    fields = list(CI_FIELDS)
    correlation_field = cfg.servicenow.correlation_field
    # The correlation field is instance-specific, so it is not in CI_FIELDS.
    if correlation_field and correlation_field not in fields:
        fields.append(correlation_field)
    return tuple(fields)


# ---- rendering -----------------------------------------------------------


def render(report: CmdbReport, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(report.as_dict(), indent=2)
    if fmt == "detail":
        return _render_detail(report)
    return _render_table(report)


def _header(report: CmdbReport) -> list[str]:
    lines = [f"{len(report.rows)} CI(s) in {report.table} matching {report.query}"]
    if report.truncated:
        lines.append("NOTE: hit the row limit; this is a partial view. Raise --limit.")
    return lines


def _render_table(report: CmdbReport) -> str:
    lines = _header(report)
    columns = [c for c in SUMMARY_COLUMNS if c in (report.fields or ())]
    if report.rows and columns:
        widths = {
            column: min(
                MAX_COLUMN_WIDTH,
                max(len(column), *(len(_show(row, column)) for row in report.rows)),
            )
            for column in columns
        }
        lines.append("")
        lines.append("  ".join(column.ljust(widths[column]) for column in columns))
        lines.append("  ".join("-" * widths[column] for column in columns))
        for row in report.rows:
            lines.append(
                "  ".join(_clip(_show(row, column), widths[column]) for column in columns)
            )
    lines.extend(_render_findings(report))
    return "\n".join(lines)


def _render_detail(report: CmdbReport) -> str:
    lines = _header(report)
    width = max((len(f) for f in report.fields), default=0)
    for row in report.rows:
        lines.append("")
        for name in report.fields:
            cell = row.get(name)
            if cell is None:
                continue
            lines.append(f"  {name.ljust(width)}  {_cell_text(cell)}")
    lines.extend(_render_findings(report))
    return "\n".join(lines)


def _render_findings(report: CmdbReport) -> list[str]:
    if not report.findings:
        return ["", "No findings."]
    lines = ["", f"Findings ({len(report.findings)}):"]
    for finding in report.findings:
        lines.append(f"  [{finding.kind}] {len(finding.sys_ids)} CI(s): {finding.detail}")
        lines.append(f"    sys_ids: {', '.join(finding.sys_ids[:10])}")
        if len(finding.sys_ids) > 10:
            lines.append(f"    ... and {len(finding.sys_ids) - 10} more")
    return lines


def _cell_text(cell: Cell) -> str:
    """Show the label, and the sys_id too when they differ -- both are useful."""
    if cell.display and cell.value and cell.display != cell.value:
        return f"{cell.display}  ({cell.value})"
    return cell.best


def _show(row: dict[str, Cell], column: str) -> str:
    cell = row.get(column)
    return cell.best if cell else ""


def _clip(text: str, width: int) -> str:
    if len(text) <= width:
        return text.ljust(width)
    return text[: width - 1] + "…"


# ---- entry point ---------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        configure_logging(os.environ.get("LOG_LEVEL", "INFO"), os.environ.get("LOG_FORMAT", "text"))
        log.error("%s", exc)
        return EXIT_CONFIG

    configure_logging(args.log_level or cfg.runtime.log_level, cfg.runtime.log_format)

    try:
        try:
            query = _build_query(args, cfg)
        except ValueError as exc:
            raise SyncError(str(exc)) from exc
        fields = _fields_for(args, cfg)
        table = args.table or cfg.servicenow.default_class or "cmdb_ci_computer"

        with ServiceNowClient(cfg.servicenow) as snow:
            reader = CmdbReader(snow, table=table, fields=fields)
            rows, truncated = reader.fetch(query, max_rows=args.limit)

        report = CmdbReport(table=table, query=query, fields=fields, rows=rows, truncated=truncated)
        if not args.no_findings:
            report.findings = analyze(
                rows, cfg.servicenow, correlation_field=cfg.servicenow.correlation_field
            )
    except SyncError as exc:
        log.error("query failed", extra={"error": str(exc)})
        return EXIT_FAILED
    except KeyboardInterrupt:  # pragma: no cover
        log.warning("interrupted")
        return EXIT_FAILED

    _emit(render(report, args.format), args.output)

    if report.findings and args.fail_on_findings:
        return EXIT_FINDINGS
    return EXIT_OK


def _emit(text: str, path: str | None) -> None:
    if not path:
        print(text)
        return
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text + "\n")
    log.info("wrote CMDB report", extra={"path": path})


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
