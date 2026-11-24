# Shared by truenas-test-setup.sh, truenas-voice-setup.sh and hub-autoupdate.sh, so every way of
# (re)starting the hub starts it the same way. Source it; it defines functions only.

# hub_build <source dir with Dockerfile> <tag> <version>
hub_build() {
  docker build -q --label app=kidspc-hub --build-arg HUB_VERSION="$3" -t "$2" "$1" >/dev/null
}

# hub_repo_version <checkout>: the commit it's on. As the checkout's owner, because root's git refuses
# a repository someone else owns.
hub_repo_version() {
  local owner; owner="$(stat -c %U "$1")"
  if [ "$owner" = "$(id -un)" ]; then git -C "$1" rev-parse HEAD 2>/dev/null || echo dev
  else runuser -u "$owner" -- git -C "$1" rev-parse HEAD 2>/dev/null || echo dev; fi
}

# hub_start <image> <host port> <data dir, e.g. /mnt/NAS/apps/kidspc-data>
hub_start() {
  docker rm -f kidspc-hub >/dev/null 2>&1 || true
  # host.docker.internal: the voice server, if set up
  docker run -d --name kidspc-hub --restart unless-stopped -p "$2:8080" \
    --add-host host.docker.internal:host-gateway -v "$3/hub:/data" "$1" >/dev/null
}

# hub_healthy <host port> [expected version] [seconds to wait]: the hub answers /healthz, its database
# opens, and (if given) it runs the expected version. Waits for it to come up.
hub_healthy() {
  local want="${2:-}" body
  for _ in $(seq "${3:-60}"); do
    body="$(curl -fs --max-time 3 "http://127.0.0.1:$1/healthz" 2>/dev/null || true)"
    if [[ "$body" == *'"ok":true'* && ( -z "$want" || "$body" == *"\"version\":\"$want\""* ) ]]; then
      return 0
    fi
    # A version that crashes on start keeps being restarted by Docker: no need to wait the whole minute.
    [ "$(docker inspect -f '{{.RestartCount}}' kidspc-hub 2>/dev/null || echo 0)" -ge 2 ] && return 1
    sleep 1
  done
  return 1
}

# The cron job (and anything else) takes this lock so an automatic update and a manual run never overlap.
hub_lock() {
  exec 9>"$1/.hub-update.lock"
  flock -w "${2:-600}" 9
}
