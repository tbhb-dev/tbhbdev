#!/bin/sh
# Reconcile the owner's org role on full-project up, using recovery Basic
# auth only on curl's stdin. Never print API bodies or credentials.
set -eu
umask 077

fail() {
  echo 'grafana-owner: owner reconciliation failed' >&2
  exit 1
}

password=$(cat /run/o11y/grafana_admin_password)
response=$(mktemp)
trap 'rm -f "$response"' EXIT
# Renderer validation limits the login to characters safe in JSON and curl config.
login=${O11Y_GRAFANA_OWNER_LOGIN:?}
throwaway=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
request() {
  status=$(
    {
      printf 'user = "admin:%s"\n' "$password"
      if [ -n "${3:-}" ]; then
        data=$(printf '%s' "$3" | sed 's/"/\\"/g')
        printf 'data = "%s"\n' "$data"
      fi
    } | curl -sS -m 30 -K - -X "$1" -H 'Content-Type: application/json' \
      -o "$response" -w '%{http_code}' "http://grafana:3000$2"
  ) || fail
  [ "$status" = 200 ] || [ "$status" = "${4:-200}" ] || fail
}
# Creating an existing login returns 412. Both paths then look up its id.
request POST /api/admin/users "{\"login\":\"$login\",\"email\":\"$login\",\"password\":\"$throwaway\"}" 412
lookup_user() {
  status=$(
    printf 'user = "admin:%s"\n' "$password" |
      curl -sS -m 30 -K - -G --data-urlencode "loginOrEmail=$login" \
        -o "$response" -w '%{http_code}' http://grafana:3000/api/users/lookup
  ) || fail
  [ "$status" = 200 ] || fail
  id=$(sed -n 's/.*"id":\([0-9][0-9]*\).*/\1/p' "$response")
  [ -n "$id" ] || fail
}
lookup_user
request PATCH "/api/org/users/$id" '{"role":"Admin"}'
echo 'grafana-owner: org Admin reconciled'
# The image has no JSON tool. Split the flat org-user objects with tr and
# read their scalar fields with sed, using the same API format as the lookup.
request GET /api/org/users
viewers=$(tr -d '\n' <"$response" | tr '}' '\n' |
  sed -n '/"role"[[:space:]]*:[[:space:]]*"Viewer"/p')
printf '%s\n' "$viewers" | while IFS= read -r user; do
  [ -n "$user" ] || continue
  user_login=$(printf '%s' "$user" | sed -n 's/.*"login"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  [ "$user_login" != "$login" ] || continue
  user_id=$(printf '%s' "$user" | sed -n 's/.*"userId"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p')
  [ -n "$user_id" ] || fail
  if ! (request GET "/api/users/$user_id") 2>/dev/null; then
    echo "grafana-owner: user lookup failed for id $user_id; skipping" >&2
    continue
  fi
  server_admin=$(tr -d '\n' <"$response" |
    sed -n 's/.*"isGrafanaAdmin"[[:space:]]*:[[:space:]]*\(true\).*/\1/p')
  [ "$server_admin" != true ] || continue
  request PATCH "/api/org/users/$user_id" '{"role":"Editor"}'
done
echo 'grafana-owner: org Viewers promoted to Editor'

# The generated 1Password password contains only letters and digits.
agents_password=$(cat /run/o11y/grafana_agents_password)
login=agents
request POST /api/admin/users "{\"login\":\"agents\",\"name\":\"agents\",\"password\":\"$agents_password\"}" 412
created_status=$status
lookup_user
if [ "$created_status" = 412 ]; then
  request PUT "/api/admin/users/$id/password" "{\"password\":\"$agents_password\"}"
fi
request PATCH "/api/org/users/$id" '{"role":"Editor"}'
echo 'grafana-owner: agents Editor reconciled'
