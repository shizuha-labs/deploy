#!/usr/bin/env bash
# Invoke from the sanctioned release CI on an isolated Docker daemon.
set -euo pipefail
if [[ ${SHIZUHA_SELFHOST_CI:-} != 1 ]]; then
  echo 'This acceptance runner requires SHIZUHA_SELFHOST_CI=1 and an isolated CI Docker daemon.' >&2
  exit 2
fi
if [[ $# != 2 ]]; then
  echo 'Usage: ci-validate.sh SOURCE_LOCK_JSON NEW_OUTPUT_DIRECTORY' >&2
  exit 2
fi
script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
release_lock=$(realpath "$1")
output=$(realpath -m "$2")
export CI_RUN_ID="${CI_RUN_ID:-$$}"
project="shizuha-oss-ci-${CI_RUN_ID}"
project=${project,,}
project=${project//[^a-z0-9_-]/-}
export SHIZUHA_ACCEPTANCE_URL=http://shizuha.localhost:8080
if ! getent hosts shizuha.localhost >/dev/null; then
  echo "127.0.0.1 shizuha.localhost" >>/etc/hosts
fi
python3 "$script_dir/selfhost.py" prepare --lock "$release_lock" --output "$output"
python3 - "$script_dir" "$release_lock" "$output" <<'PYDELIVERY'
import os, sys
sys.path.insert(0, sys.argv[1])
from delivery_proof import capture_inputs
capture_inputs(sys.argv[2], sys.argv[3], bool(os.environ.get('SHIZUHA_CANDIDATE_SOURCES')))
PYDELIVERY
if [[ -n ${SHIZUHA_CANDIDATE_SOURCES:-} ]]; then
  # Private prepublication CI consumes the exact digest-verified candidate
  # archive. The post-publication job omits this variable and fetches anonymously.
  cp -a -- "$SHIZUHA_CANDIDATE_SOURCES" "$output/sources"
  printf '**/.git\n**/.env\n**/.env.*\n**/node_modules\n**/.venv\n**/__pycache__\n' > "$output/sources/.dockerignore"
else
  python3 "$script_dir/selfhost.py" fetch --lock "$release_lock" --output "$output"
fi
compose=(docker compose --project-name "$project" --env-file "$output/secrets.env" -f "$output/compose.json")
cleanup() {
  code=$?
  if [[ $code != 0 ]]; then
    "${compose[@]}" ps --all >"$output/service-status.txt" || true
    "${compose[@]}" logs --no-color --tail 150 >"$output/services.log" 2>&1 || true
    # Probe the same internal projection routes only after a failed acceptance.
    # Preserve the original failure above; this is evidence, never a warm-up or
    # retry that turns a failed first login into a passing acceptance.
    "${compose[@]}" exec -T id python manage.py shell <<'PY' >"$output/membership-diagnostics.log" 2>&1 || true
import os, time, requests
from django.contrib.auth import get_user_model
subject = get_user_model().objects.filter(username='ossowner').first()
if subject is None:
    raise SystemExit('Acceptance principal not created; projection diagnostic skipped')
for service, suffix in [('ADMIN', 'memberships/'), ('ADMIN', 'project-memberships/'), ('PULSE', 'project-memberships/')]:
    url = os.environ['SHIZUHA_' + service + '_API_URL'] + f'/internal/users/{subject.pk}/' + suffix
    started = time.monotonic()
    try:
        response = requests.get(url, params={'email': 'ossowner@example.org', 'name': 'ossowner'}, timeout=5)
        print(service, suffix, response.status_code, round(time.monotonic() - started, 3), response.text[:1000], flush=True)
    except requests.RequestException as error:
        print(service, suffix, type(error).__name__, round(time.monotonic() - started, 3), str(error), flush=True)
PY
    "${compose[@]}" exec -T runtime-cluster sh -c \
      'kubectl --request-timeout=10s get nodes -o wide; tail -n 150 /var/lib/rancher/k3s/agent/containerd/containerd.log' \
      >"$output/runtime-diagnostics.log" 2>&1 || true
    # An explicit private candidate may retain a failed isolated installation
    # briefly for diagnosis. Public releases and ordinary runs never pause.
    if [[ -n ${SHIZUHA_CANDIDATE_SOURCES:-} && -f ${SHIZUHA_CANDIDATE_SOURCES}/../diagnostic-hold-seconds ]]; then
      hold=$(cat "${SHIZUHA_CANDIDATE_SOURCES}/../diagnostic-hold-seconds")
      if [[ $hold =~ ^[0-9]+$ && $hold -ge 1 && $hold -le 1800 ]]; then
        python3 "$script_dir/evidence.py" "$output" "$code"
        echo "Retaining this failed isolated CI installation for ${hold}s diagnostic access."
        sleep "$hold"
      fi
    fi
  fi
  # The project name was generated here; this can only remove this run's data.
  "${compose[@]}" down --volumes --remove-orphans >"$output/cleanup.log" 2>&1 || true
  python3 "$script_dir/evidence.py" "$output" "$code"
  python3 "$script_dir/collect_evidence.py" "$output"
  if ! python3 "$script_dir/artifact_transport.py" "$output/review-evidence.tar.gz" "$CI_RUN_ID"; then
    code=1
  fi
  exit "$code"
}
trap cleanup EXIT
"${compose[@]}" config --quiet
"${compose[@]}" pull --ignore-buildable >"$output/pull.log" 2>&1
"${compose[@]}" --profile runtime-build build --pull >"$output/build.log" 2>&1
# This daemon belongs only to this CI run. Release build caches before the
# dedicated runtime cluster imports its own copies of the final images.
docker builder prune --all --force >"$output/build-cache-cleanup.log" 2>&1
python3 "$script_dir/storage_budget.py" "$output" >"$output/storage-budget.log" 2>&1
"${compose[@]}" up -d --wait --wait-timeout 600 >"$output/start.log" 2>&1
"${compose[@]}" restart mail-server >"$output/mail-restart.log" 2>&1
python3 "$script_dir/verify.py" --url "$SHIZUHA_ACCEPTANCE_URL" >"$output/health.txt"
"${compose[@]}" images --format json >"$output/images.json"
export SHIZUHA_INSTALLATION_DIR="$output" SHIZUHA_COMPOSE_PROJECT="$project"
python3 "$script_dir/acceptance.py" >"$output/acceptance.log" 2>&1
printf 'Source build, health and authenticated acceptance passed. Evidence: %s\n' "$output"
