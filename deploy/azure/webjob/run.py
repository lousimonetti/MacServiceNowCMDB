"""Scheduled WebJob entry point: one intune-cmdb-sync run per invocation.

deploy.sh puts this file, with a settings.job holding the schedule, at
App_Data/jobs/triggered/intune-cmdb-sync/ in the package. The connector and its
Linux wheels go in packages/ at the package root, which App Service unpacks to
/home/site/wwwroot.

Python rather than run.sh, so that nothing depends on zip deploy preserving an
executable bit. The packages are found by absolute path because Kudu copies a
triggered job's folder to a temporary directory before running it, so a path
relative to this file points nowhere. Kudu, not the app's container, runs the
job, so HOME is not reliably /home there; WEBROOT_PATH (set by Kudu) comes
first, then the fixed App Service location, then HOME.
"""

import os
import sys

_candidates = [
    os.environ.get("WEBROOT_PATH"),
    "/home/site/wwwroot",
    os.path.join(os.environ.get("HOME", "/home"), "site", "wwwroot"),
]
for _root in filter(None, _candidates):
    _packages = os.path.join(_root, "packages")
    if os.path.isdir(os.path.join(_packages, "intune_cmdb_sync")):
        sys.path.insert(0, _packages)
        break
else:
    # A bare ModuleNotFoundError says nothing about where it looked.
    print("intune-cmdb-sync: connector package not found. Looked in:", file=sys.stderr)
    for _root in filter(None, _candidates):
        _packages = os.path.join(_root, "packages")
        _state = "exists" if os.path.isdir(_packages) else "missing"
        print(f"  {_packages} ({_state})", file=sys.stderr)
    print(
        f"  HOME={os.environ.get('HOME')!r} WEBROOT_PATH={os.environ.get('WEBROOT_PATH')!r}\n"
        "The code deployment may not have finished; check: webjob.sh deployments",
        file=sys.stderr,
    )
    sys.exit(3)

from intune_cmdb_sync.appservice_job import run  # noqa: E402

sys.exit(run())
