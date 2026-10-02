"""Azure Functions app: one timer trigger that runs intune-cmdb-sync.

All logic lives in `intune_cmdb_sync.azure_function`, which is tested; this file
only binds it to a schedule. deploy.sh copies it to the root of the zip package
alongside host.json and the installed dependencies.

The schedule is an app setting, not a literal, so changing it is a redeploy of
settings rather than of code. It is a six-field NCRONTAB expression in UTC:
Flex Consumption does not support WEBSITE_TIME_ZONE.
"""

import azure.functions as func

from intune_cmdb_sync.azure_function import run

app = func.FunctionApp()


# run_on_startup stays False: True would also fire on every cold start and
# every deployment, i.e. unscheduled syncs whenever the platform scales or moves
# the app.
@app.timer_trigger(schedule="%SYNC_SCHEDULE%", arg_name="timer", run_on_startup=False)
def intune_cmdb_sync(timer: func.TimerRequest) -> None:
    run()
