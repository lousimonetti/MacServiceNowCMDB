"""Scheduled WebJob entry point: one intune-cmdb-sync run per invocation.

deploy.sh puts this file, with a settings.job holding the schedule, at
App_Data/jobs/triggered/intune-cmdb-sync/ in the package. The connector and its
Linux wheels go in packages/ at the package root, which App Service unpacks to
/home/site/wwwroot.

Python rather than run.sh, so that nothing depends on zip deploy preserving an
executable bit. The path to the packages is absolute because Kudu copies a
triggered job's folder to a temporary directory before running it, so a path
relative to this file would point nowhere.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.environ.get("HOME", "/home"), "site", "wwwroot", "packages"))

from intune_cmdb_sync.appservice_job import run

sys.exit(run())
