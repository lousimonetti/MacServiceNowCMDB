"""Discovering and validating CMDB classes on the target instance.

`SNOW_CLASS_MAP` maps an Intune `operatingSystem` to a CMDB class, and getting
it wrong fails in two quiet ways. An OS with no entry is skipped with a report
line and nothing else -- the 2026-09-04 runs skipped both macOS devices for
this reason, because setting `SNOW_CLASS_MAP` *replaces* the built-in default
rather than extending it, and the built-in default does contain
`macos=cmdb_ci_computer`. An entry naming a class that does not exist on the
instance is worse: it looks configured and fails on every device of that OS at
write time.

Both are answerable with reads against `sys_db_object`, which is why this lives
here rather than in a runbook: "which class should macOS use" is a question
about the instance, and the instance can be asked.
"""

from __future__ import annotations

import logging

from ..config import ServiceNowConfig
from ..errors import ServiceNowError
from .client import ServiceNowClient

log = logging.getLogger(__name__)

# Every CMDB class is a table whose name starts with this. Querying by prefix
# rather than walking `super_class` keeps it to one request: the hierarchy is
# hundreds of rows deep and a recursive walk would be dozens of round trips for
# a list a human is going to read anyway.
CI_TABLE_PREFIX = "cmdb_ci"


def list_ci_classes(
    client: ServiceNowClient, pattern: str | None = None, *, limit: int = 500
) -> list[dict[str, str]]:
    """CMDB classes on this instance, optionally filtered by name or label."""
    query = f"nameSTARTSWITH{CI_TABLE_PREFIX}"
    if pattern:
        # Match either half: someone looking for a Mac class may know the label
        # ("Computer") or the table name ("cmdb_ci_computer"), not both.
        query += f"^nameLIKE{pattern}^ORlabelLIKE{pattern}^nameSTARTSWITH{CI_TABLE_PREFIX}"
    rows = client.query_table(
        "sys_db_object", query=query, fields=("name", "label"), limit=limit
    )
    classes = [
        {"name": str(row.get("name") or ""), "label": str(row.get("label") or "")}
        for row in rows
        if row.get("name")
    ]
    return sorted(classes, key=lambda c: c["name"])


def class_exists(client: ServiceNowClient, class_name: str) -> bool:
    rows = client.query_table(
        "sys_db_object", query=f"name={class_name}", fields=("name",), limit=1
    )
    # `=` on a string is not reliably case-sensitive in an encoded query, and a
    # class name has to match exactly, so confirm in Python.
    return any(str(row.get("name") or "") == class_name for row in rows)


def verify_class_map(client: ServiceNowClient, cfg: ServiceNowConfig) -> list[str]:
    """Check every configured class exists. Returns problems, worst first.

    A class that does not exist is not a caveat: every device of that OS will
    fail at write time, and the mapping looks correct until then.
    """
    # Grouped by class rather than by OS: the common map sends every OS to
    # cmdb_ci_computer, and checking it once per OS would be the same request
    # repeated.
    by_class: dict[str, list[str]] = {}
    for os_name, class_name in sorted(cfg.class_map.items()):
        by_class.setdefault(class_name, []).append(repr(os_name))
    if cfg.default_class:
        by_class.setdefault(cfg.default_class, []).append("SNOW_DEFAULT_CLASS")

    problems: list[str] = []
    for class_name, sources in by_class.items():
        try:
            if not class_exists(client, class_name):
                problems.append(
                    f"{', '.join(sources)} maps to {class_name!r}, which is not a table on "
                    "this instance. Every device of that OS will fail to write. Run "
                    "`intune-cmdb-sync --list-classes` to see what exists."
                )
        except ServiceNowError as exc:
            problems.append(
                f"could not confirm {class_name!r} exists on this instance ({exc})"
            )
    return problems


def unmapped_os_note(cfg: ServiceNowConfig) -> str | None:
    """Warn when a common OS has no mapping, naming the likely cause.

    Silence here is indistinguishable from intent: skipping iOS on purpose and
    dropping macOS by accident produce the same empty report line.
    """
    common = {"windows", "macos", "ios", "android", "linux"}
    mapped = {key.lower() for key in cfg.class_map}
    missing = sorted(common - mapped)
    if not missing or cfg.default_class:
        return None
    return (
        f"SNOW_CLASS_MAP has no entry for {', '.join(missing)}, so those devices are "
        "skipped. Note that setting SNOW_CLASS_MAP replaces the built-in default "
        "(windows and macos) rather than adding to it, so an entry can go missing by "
        "being left out. Set SNOW_DEFAULT_CLASS to catch everything else, or add the "
        "entries you want."
    )


def format_classes(classes: list[dict[str, str]], cfg: ServiceNowConfig) -> str:
    """Render the class list, marking the ones already configured."""
    if not classes:
        return "No CMDB classes matched.\n"

    in_use = {v: k for k, v in cfg.class_map.items()}
    if cfg.default_class:
        in_use.setdefault(cfg.default_class, "SNOW_DEFAULT_CLASS")

    width = max(len(c["name"]) for c in classes)
    lines = ["", f"CMDB classes on this instance ({len(classes)}):", ""]
    for entry in classes:
        marker = f"  <- SNOW_CLASS_MAP {in_use[entry['name']]!r}" if entry["name"] in in_use else ""
        lines.append(f"  {entry['name']:<{width}}  {entry['label']}{marker}")
    return "\n".join(lines) + "\n"
