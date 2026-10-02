# Reach an App Service app's Kudu (scm) API through its private endpoint.
# Sourced by deploy.sh and webjob.sh; bash only.
#
# The landing zone requires public network access disabled on every App Service
# app, so Kudu -- deploys, WebJob runs and history, the file API -- is reachable
# only at the app's private endpoint. The deploying laptop has a route to that
# address (Zscaler Private Access) but its DNS does not resolve privatelink
# zones, so each call pins the scm hostname to the endpoint's private IP with
# curl --resolve. TLS is still verified against the real hostname. Nothing is
# written to /etc/hosts and no DNS is changed.
#
# Authentication is the caller's Entra login, the same App Service token
# `az webapp deploy` uses; basic-auth publishing is off.

# kudu_init RESOURCE_GROUP APP PRIVATE_ENDPOINT_NAME
#   Sets KUDU_HOST, KUDU_IP and KUDU_TOKEN.
kudu_init() {
  local rg="$1" app="$2" endpoint="$3" nic
  KUDU_HOST=$(az webapp show --resource-group "$rg" --name "$app" \
      --query "hostNameSslStates[?hostType=='Repository'].name | [0]" --output tsv) \
    || { echo "error: could not read app ${app}" >&2; return 1; }
  nic=$(az network private-endpoint show --resource-group "$rg" --name "$endpoint" \
      --query "networkInterfaces[0].id" --output tsv) \
    || { echo "error: private endpoint ${endpoint} not found in ${rg}" >&2; return 1; }
  KUDU_IP=$(az network nic show --ids "$nic" \
      --query "ipConfigurations[0].privateIPAddress" --output tsv)
  [[ -n "$KUDU_IP" ]] || { echo "error: private endpoint ${endpoint} has no IP yet" >&2; return 1; }
  KUDU_TOKEN=$(az account get-access-token --resource https://appservice.azure.com \
      --query accessToken --output tsv) \
    || { echo "error: could not get an App Service token from az" >&2; return 1; }
}

# kudu METHOD PATH [curl args...]
#   Calls https://$KUDU_HOST$PATH at $KUDU_IP. Prints the body; fails on HTTP >= 400.
#
#   No --cacert, even behind Zscaler: this path goes over Zscaler Private Access,
#   which does not inspect TLS, so Kudu presents its real certificate and the
#   system curl's own trust store verifies it (seen on 2026-10-02). deploy.sh's
#   combined bundle is for az and pip, which do not use the OS store.
kudu() {
  local method="$1" path="$2"
  shift 2
  curl --silent --show-error --fail-with-body \
    --resolve "${KUDU_HOST}:443:${KUDU_IP}" \
    --header "Authorization: Bearer ${KUDU_TOKEN}" \
    --request "$method" "$@" \
    "https://${KUDU_HOST}${path}"
}

# kudu_reachable
#   One cheap authenticated call, so a failure is explained before any upload.
#   /api/deployments, not /api/environment: on a Linux app the latter returns
#   Kudu's HTML dashboard with HTTP 500 (2026-10-02), so it fails on a healthy app.
#   Not /api/settings either, whose body can carry app settings.
#   Leaves the HTTP status in KUDU_STATUS and curl's own error in KUDU_ERROR,
#   which kudu_unreachable_help reports.
KUDU_STATUS=""
KUDU_ERROR=""
kudu_reachable() {
  local err
  err=$(mktemp)
  KUDU_STATUS=$(kudu GET /api/deployments --max-time 20 --output /dev/null \
      --write-out '%{http_code}' 2>"$err") && { rm -f "$err"; return 0; }
  KUDU_ERROR=$(head -c 300 "$err")
  rm -f "$err"
  return 1
}

kudu_unreachable_help() {
  local cause
  case "${KUDU_STATUS:-000}" in
    000) cause="no HTTP response: no route to ${KUDU_IP}, or TLS failed. Connect
       Zscaler Private Access or the VPN; curl said: ${KUDU_ERROR:-nothing}" ;;
    401) cause="401: Kudu refused the token. Re-run 'az login'; the token comes from
       az account get-access-token --resource https://appservice.azure.com" ;;
    403) cause="403: the route and token work, but your login lacks Contributor or
       Website Contributor on the app" ;;
    *)   cause="HTTP ${KUDU_STATUS}: ${KUDU_ERROR:-no detail}" ;;
  esac
  cat >&2 <<HELP
error: Kudu for ${KUDU_HOST} at its private endpoint ${KUDU_IP} did not answer
       an authenticated request.
       ${cause}
       To repeat the check by hand:
         TOKEN=\$(az account get-access-token --resource https://appservice.azure.com --query accessToken -o tsv)
         curl -sS -o /dev/null -w '%{http_code}\\n' --max-time 15 \\
           --resolve ${KUDU_HOST}:443:${KUDU_IP} -H "Authorization: Bearer \$TOKEN" \\
           https://${KUDU_HOST}/api/deployments
HELP
}
