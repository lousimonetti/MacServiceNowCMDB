#!/usr/bin/env bash
# Operate the intune-cmdb-sync WebJob through the app's private endpoint.
#
#   webjob.sh run          start a run now (returns at once; see `history`)
#   webjob.sh history      recent runs: status, start, duration
#   webjob.sh output       the latest run's output
#   webjob.sh report       the latest run-report.json
#   webjob.sh state        state.json (device id -> CI sys_id)
#   webjob.sh deployments  recent code deployments
#   webjob.sh files [DIR]  list wwwroot, or a folder in it (e.g. packages)
#   webjob.sh schedule ["SEC MIN HOUR DAY MONTH WEEKDAY"]
#                          show settings.job, or change its schedule in place (UTC)
#
# Needs RESOURCE_GROUP and NAME_PREFIX, the same values deploy.sh used, plus
# CA_BUNDLE if you are behind Zscaler and it is not already handled. The app has
# public network access disabled, so `az webapp webjob ...` and the portal's
# Kudu tools may be refused; this reaches Kudu at the private IP (kudu.sh).

case ":${SHELLOPTS:-}:" in
  *:posix:*) exec bash "$0" "$@" ;;
esac
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"

set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }

WEBJOB_NAME="intune-cmdb-sync"
DATA_PATH="/api/vfs/data/intune-cmdb-sync"
[[ -n "${RESOURCE_GROUP:-}" ]] || die "RESOURCE_GROUP must be set (e.g. azc-obm-development)"
NAME_PREFIX="${NAME_PREFIX:-intunecmdb}"
command -v jq >/dev/null || die "jq is required"

APP_NAME=$(az webapp list --resource-group "$RESOURCE_GROUP" \
    --query "[?starts_with(name, '${NAME_PREFIX}-app-')].name | [0]" --output tsv)
[[ -n "$APP_NAME" ]] || die "no app named ${NAME_PREFIX}-app-* in ${RESOURCE_GROUP}"

# shellcheck source=kudu.sh
. "$(dirname "$0")/kudu.sh"
kudu_init "$RESOURCE_GROUP" "$APP_NAME" "${NAME_PREFIX}-app-pe" || exit 1
kudu_reachable || { kudu_unreachable_help; exit 1; }

case "${1:-}" in
  run)
    kudu POST "/api/triggeredwebjobs/${WEBJOB_NAME}/run" --output /dev/null
    echo "started ${WEBJOB_NAME} on ${APP_NAME}; follow it with: $0 history"
    ;;
  history)
    kudu GET "/api/triggeredwebjobs/${WEBJOB_NAME}/history" \
      | jq -r '["STATUS", "STARTED", "DURATION"], (.runs[:10][] | [.status, .start_time, .duration]) | @tsv' \
      | column -t -s $'\t'
    ;;
  output)
    url=$(kudu GET "/api/triggeredwebjobs/${WEBJOB_NAME}/history" | jq -r '.runs[0].output_url // empty')
    [[ -n "$url" ]] || die "no runs yet"
    kudu GET "/${url#https://*/}" | tail -200
    ;;
  report)
    kudu GET "${DATA_PATH}/run-report.json" | jq .
    ;;
  state)
    kudu GET "${DATA_PATH}/state.json" | jq .
    ;;
  deployments)
    kudu GET /api/deployments \
      | jq -r '.[:5][] | [.received_time, (.status | tostring), .status_text // "", .message // ""] | @tsv'
    ;;
  files)
    kudu GET "/api/vfs/site/wwwroot/${2:+${2%/}/}" | jq -r '.[] | [.mime, .name] | @tsv'
    ;;
  schedule)
    job_file="/api/vfs/site/wwwroot/App_Data/jobs/triggered/${WEBJOB_NAME}/settings.job"
    if [[ -z "${2:-}" ]]; then
      kudu GET "$job_file" | jq .
    else
      # Same rule as deploy.sh: six fields, seconds first. Kudu would accept five
      # and then never fire.
      read -ra fields <<<"$2"
      [[ ${#fields[@]} -eq 6 ]] \
        || die "schedule must have six fields (sec min hour day month weekday), got ${#fields[@]}: '$2'"
      # Keep every other key (is_singleton) and replace only the schedule.
      current=$(kudu GET "$job_file")
      echo "$current" | jq -e 'type == "object"' >/dev/null || die "settings.job is not a JSON object"
      echo "$current" | jq --arg s "$2" '.schedule = $s' \
        | kudu PUT "$job_file" --header 'If-Match: *' \
            --header 'Content-Type: application/json' --data-binary @- --output /dev/null
      echo "schedule is now:"
      kudu GET "$job_file" | jq .
      echo "Temporary: the next deploy.sh rewrites it from SCHEDULE. Set SCHEDULE='$2' in your environment file too."
      echo "Confirm Kudu picked it up: $0 history after the new time."
    fi
    ;;
  *)
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
