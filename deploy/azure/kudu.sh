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
kudu() {
  local method="$1" path="$2"
  shift 2
  local tls=()
  # Behind TLS inspection deploy.sh exports the combined bundle; use it here too.
  [[ -n "${REQUESTS_CA_BUNDLE:-}" ]] && tls=(--cacert "$REQUESTS_CA_BUNDLE")
  curl --silent --show-error --fail-with-body "${tls[@]+"${tls[@]}"}" \
    --resolve "${KUDU_HOST}:443:${KUDU_IP}" \
    --header "Authorization: Bearer ${KUDU_TOKEN}" \
    --request "$method" "$@" \
    "https://${KUDU_HOST}${path}"
}

# kudu_reachable
#   One cheap call, so an unreachable endpoint fails with an explanation rather
#   than as a stalled upload.
kudu_reachable() {
  kudu GET /api/environment --max-time 20 --output /dev/null 2>/dev/null
}

kudu_unreachable_help() {
  cat >&2 <<HELP
error: cannot reach Kudu for ${KUDU_HOST} at its private endpoint ${KUDU_IP}.
       Check, from this machine:
         curl -sS -o /dev/null -w '%{http_code}\\n' --max-time 10 \\
           --resolve ${KUDU_HOST}:443:${KUDU_IP} https://${KUDU_HOST}/
       000 or a timeout means no route to ${KUDU_IP}: connect Zscaler Private
       Access or the VPN. 401/403 means the route works but your login lacks
       Contributor or Website Contributor on the app.
HELP
}
