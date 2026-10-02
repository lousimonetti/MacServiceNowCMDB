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
  *)
    sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
