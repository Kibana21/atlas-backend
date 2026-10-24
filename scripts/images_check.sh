#!/usr/bin/env bash
# Build and verify the api/worker images (ATLAS-001 B-21, B-24). Used by `make images` and CI `images`.
#
# 1. Build both targets with ATLAS_BUILD_SHA; lint the Dockerfile (hadolint).
# 2. Scan both images (Trivy, from `docker save` archives, no Docker socket): fail on any HIGH/CRITICAL
#    vulnerability that has a fix, on any secret, and on Dockerfile misconfiguration.
# 3. Static checks: non-root user, health check present, no env files/VCS/tests/build output in /app.
# 4. Runtime on an ISOLATED Docker network with its own postgres:15 and redis:7.4 containers (never the
#    owner's shared services; no host ports except the API on 127.0.0.1): bootstrap SQL, migrate,
#    API + worker hardened (--read-only, --cap-drop=ALL, no-new-privileges), health checks, readiness,
#    build info, worker round trip with correlation ID, graceful `docker stop` (exit 0).
# Everything it starts is removed on exit. Credentials are random per run and passed via a 0600 env file.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
SHA="${ATLAS_BUILD_SHA:-$(git rev-parse HEAD)}"
TAG="${IMAGE_TAG:-check}"
API="atlas-api:${TAG}"
WORKER="atlas-worker:${TAG}"
HADOLINT="hadolint/hadolint:v2.14.0@sha256:27086352fd5e1907ea2b934eb1023f217c5ae087992eb59fde121dce9c9ff21e"
TRIVY="aquasec/trivy:0.67.2@sha256:e2b22eac59c02003d8749f5b8d9bd073b62e30fefaef5b7c8371204e0a4b0c08"
POSTGRES="postgres:15.19@sha256:baa22e6286bf832cb147b8bb5aa9d8bc999846d69c21d11bf63f58fbeed1fa48"
REDIS="redis:7.4.9@sha256:a8f08480e1f88f2647fed492d1178c06abb0d0c1fbf02c682a61e2f483fb3954"
RUN="atlas-imgcheck-$$"
WORK="$(mktemp -d)"
chmod 700 "$WORK"
HARDENED=(--read-only --cap-drop=ALL --security-opt=no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,size=16m)

cleanup() {
  docker rm -f "${RUN}-api" "${RUN}-worker" "${RUN}-pg" "${RUN}-redis" >/dev/null 2>&1 || true
  docker network rm "${RUN}-net" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
step() { printf '\n== %s\n' "$*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

step "build (ATLAS_BUILD_SHA=${SHA})"
for target in api worker; do
  docker build --quiet --target "$target" --build-arg "ATLAS_BUILD_SHA=${SHA}" -t "atlas-${target}:${TAG}" . >/dev/null
  echo "built atlas-${target}:${TAG} ($(docker image inspect -f '{{.Size}}' "atlas-${target}:${TAG}" | awk '{printf "%.0f MB", $1/1e6}'))"
done

step "hadolint"
docker run --rm -i "$HADOLINT" < Dockerfile && echo "hadolint: no findings"

step "trivy (fail: fixable HIGH/CRITICAL vulnerabilities, secrets, Dockerfile misconfiguration)"
mkdir -p "$WORK/scan" "$WORK/cache"
cp Dockerfile "$WORK/scan/Dockerfile"
for target in api worker; do
  docker save "atlas-${target}:${TAG}" -o "$WORK/scan/${target}.tar"
  docker run --rm -v "$WORK/scan:/scan" -v "$WORK/cache:/root/.cache/trivy" "$TRIVY" image --quiet \
    --input "/scan/${target}.tar" --scanners vuln,secret --severity HIGH,CRITICAL --format json \
    --output "/scan/${target}.json"
  python3 - "$WORK/scan/${target}.json" "$target" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
results = d.get("Results", [])
vulns = [(r["Type"], x["VulnerabilityID"], x["PkgName"], x["Severity"], x.get("FixedVersion")) for r in results
         for x in r.get("Vulnerabilities") or []]
fixable = [v for v in vulns if v[4]]
secrets = [(r["Target"], s["RuleID"]) for r in results for s in r.get("Secrets") or []]
app = [v for v in vulns if v[0] not in ("debian", "ubuntu", "alpine")]
print(f"{sys.argv[2]}: HIGH/CRITICAL total={len(vulns)} fixable={len(fixable)} python-packages={len(app)} secrets={len(secrets)}"
      " (unfixed OS findings have no upstream fix yet: reported, not gating)")
for v in fixable[:20] + app[:20]:
    print("   ", v)
for s in secrets:
    print("    secret", s)
sys.exit(1 if fixable or app or secrets else 0)
PY
  rm -f "$WORK/scan/${target}.tar"
done
docker run --rm -v "$WORK/scan:/scan" -v "$WORK/cache:/root/.cache/trivy" "$TRIVY" config --quiet \
  --severity HIGH,CRITICAL --exit-code 1 /scan/Dockerfile && echo "trivy config: no HIGH/CRITICAL misconfiguration"

step "static image checks"
for image in "$API" "$WORKER"; do
  uid="$(docker run --rm --entrypoint id "$image" -u)"
  [ "$uid" != "0" ] || fail "$image runs as root"
  [ "$(docker image inspect -f '{{.Config.Healthcheck.Test}}' "$image")" != "[]" ] || fail "$image has no HEALTHCHECK"
  bad="$(docker run --rm --entrypoint sh "$image" -c \
    'find /app -maxdepth 2 \( -name ".env" -o -name ".env.*" -o -name ".git" -o -name tests -o -name build -o -name ".pytest_cache" -o -name ".mypy_cache" -o -name ".ruff_cache" \) -not -path "*/site-packages/*" -print')"
  [ -z "$bad" ] || fail "$image contains: $bad"
  [ "$(docker image inspect -f '{{index .Config.Env}}' "$image" | grep -ciE 'password|secret|token' || true)" = "0" ] || fail "$image env names a secret"
  echo "$image: uid=${uid}, health check present, no env/VCS/test/cache files, no secret in image env"
done

step "runtime on an isolated network"
pw() { python3 -c 'import secrets; print(secrets.token_hex(24))'; }
MIGRATOR_PW="$(pw)"; APP_PW="$(pw)"; TEST_MIGRATOR_PW="$(pw)"; TEST_APP_PW="$(pw)"; SUPER_PW="$(pw)"
docker network create --internal "${RUN}-net" >/dev/null
docker run -d --name "${RUN}-pg" --network "${RUN}-net" --network-alias postgres \
  -e POSTGRES_PASSWORD="$SUPER_PW" -e POSTGRES_HOST_AUTH_METHOD=scram-sha-256 \
  -e POSTGRES_INITDB_ARGS="--auth-local=scram-sha-256 --auth-host=scram-sha-256 --locale=C --encoding=UTF8" "$POSTGRES" >/dev/null
docker run -d --name "${RUN}-redis" --network "${RUN}-net" --network-alias redis "$REDIS" >/dev/null
for _ in $(seq 60); do docker exec "${RUN}-pg" pg_isready -U postgres -q 2>/dev/null && break; sleep 1; done
sleep 2
docker exec -i -e PGPASSWORD="$SUPER_PW" -e ATLAS_MIGRATOR_PASSWORD="$MIGRATOR_PW" -e ATLAS_APP_PASSWORD="$APP_PW" \
  -e ATLAS_TEST_MIGRATOR_PASSWORD="$TEST_MIGRATOR_PW" -e ATLAS_TEST_APP_PASSWORD="$TEST_APP_PW" "${RUN}-pg" \
  psql -U postgres -d postgres -X -q -v ON_ERROR_STOP=1 -f - < scripts/db/bootstrap.sql >/dev/null
echo "bootstrap: four Atlas roles, atlas_db, atlas_test"
ENVFILE="$WORK/atlas.env"
umask 077
cat > "$ENVFILE" <<EOF
ATLAS_ENV=local
ATLAS_DATABASE_URL=postgresql+psycopg://atlas_app:${APP_PW}@postgres:5432/atlas_db
ATLAS_MIGRATION_DATABASE_URL=postgresql+psycopg://atlas_migrator:${MIGRATOR_PW}@postgres:5432/atlas_db
ATLAS_DB_APP_ROLE=atlas_app
ATLAS_REDIS_URL=redis://redis:6379/10
ATLAS_REDIS_KEY_PREFIX=atlas:local:
ATLAS_CORS_ORIGINS=http://localhost:3000
ATLAS_DIAGNOSTICS_ENABLED=true
EOF
docker run --rm --network "${RUN}-net" --env-file "$ENVFILE" "${HARDENED[@]}" "$API" alembic upgrade head 2>&1 | tail -1
docker run -d --name "${RUN}-api" --network "${RUN}-net" --env-file "$ENVFILE" "${HARDENED[@]}" "$API" >/dev/null
docker run -d --name "${RUN}-worker" --network "${RUN}-net" --env-file "$ENVFILE" "${HARDENED[@]}" "$WORKER" >/dev/null
for name in api worker; do
  status=""
  for _ in $(seq 60); do
    status="$(docker inspect -f '{{.State.Health.Status}}' "${RUN}-${name}")"
    [ "$status" = "healthy" ] && break
    [ "$(docker inspect -f '{{.State.Running}}' "${RUN}-${name}")" = "true" ] || break
    sleep 1
  done
  [ "$status" = "healthy" ] || { docker logs "${RUN}-${name}" 2>&1 | tail -20; fail "${name} container not healthy (${status})"; }
  echo "${name}: healthy (read-only root fs, no capabilities, no-new-privileges, uid $(docker exec "${RUN}-${name}" id -u))"
done

# HTTP checks from a throwaway container on the same internal network (the network has no host route).
API_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "${RUN}-api")"
docker run --rm -i --network "${RUN}-net" --read-only --cap-drop=ALL "$API" python - "$SHA" "$API_IP" <<'PY'
import json, sys, urllib.error, urllib.request
sha, ip = sys.argv[1], sys.argv[2]
def get(path, cid=None):
    req = urllib.request.Request(f"http://{ip}:8000{path}", headers={"X-Correlation-ID": cid} if cid else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.headers, json.loads(e.read())
s, h, b = get("/api/v1/health/live", "imgcheck_live01")
assert (s, b) == (200, {"status": "ok"}) and h["X-Correlation-ID"] == "imgcheck_live01", (s, b)
assert h["Content-Type"] == "application/json" and h["X-Content-Type-Options"] == "nosniff", dict(h)
assert "server" not in {k.lower() for k in h}, "server header exposed"
s, h, b = get("/api/v1/health/ready")
assert (s, b) == (200, {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}), (s, b)
s, h, b = get("/api/v1/diagnostics/build")
assert s == 200 and b["backend_sha"] == sha and b["db_migration_revision"] == "0001_baseline", b
s, h, b = get("/api/v1/nope")
assert s == 404 and b["error"]["code"] == "NOT_FOUND" and b["error"]["correlation_id"] == h["X-Correlation-ID"], b
print(f"http: live 200, ready 200 {{database: ok, redis: ok}}, build sha {sha[:12]} rev 0001_baseline, unknown route 404 envelope")
PY

MARKER="img_$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
CID="req_imgcheck$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
docker run --rm --network "${RUN}-net" --env-file "$ENVFILE" "${HARDENED[@]}" "$API" python -c "
import asyncio
from atlas.core.settings import get_settings
from atlas.workers.broker import build_broker
from atlas.workers.producer import enqueue_ping
async def main():
    broker, tasks = build_broker(get_settings())
    await broker.startup()
    await enqueue_ping(tasks, '${MARKER}', '${CID}')
    await broker.shutdown()
asyncio.run(main())
"
value=""
for _ in $(seq 75); do
  value="$(docker exec "${RUN}-redis" redis-cli -n 10 GET "atlas:local:ping:${MARKER}")"
  [ -n "$value" ] && break
  sleep 0.2
done
[ "$value" = "$CID" ] || { docker logs "${RUN}-worker" 2>&1 | tail -20; fail "worker round trip: marker missing or wrong"; }
docker logs "${RUN}-worker" 2>&1 | grep -q "\"correlation_id\": \"${CID}\"" || fail "worker log lacks the correlation ID"
echo "worker: round trip OK on DB 10 (marker = correlation ID ${CID}), correlation ID in worker logs"

step "graceful shutdown (docker stop, SIGTERM)"
for name in api worker; do
  start=$(date +%s)
  docker stop -t 20 "${RUN}-${name}" >/dev/null
  code="$(docker inspect -f '{{.State.ExitCode}}' "${RUN}-${name}")"
  took=$(( $(date +%s) - start ))
  [ "$code" = "0" ] || { docker logs "${RUN}-${name}" 2>&1 | tail -10; fail "${name} exited ${code} on SIGTERM"; }
  echo "${name}: exit 0 after SIGTERM in ${took}s"
done
docker logs "${RUN}-api" 2>&1 | grep -q "Application shutdown complete" || fail "api did not shut down cleanly"
docker logs "${RUN}-worker" 2>&1 | grep -q '"event": "worker_stopped"' || fail "worker did not shut down cleanly"
if docker logs "${RUN}-api" 2>&1 | grep -q -e "$APP_PW" -e "$MIGRATOR_PW"; then fail "api log contains a password"; fi
if docker logs "${RUN}-worker" 2>&1 | grep -q -e "$APP_PW" -e "$MIGRATOR_PW"; then fail "worker log contains a password"; fi
echo "logs: clean shutdown messages; no credential in api/worker logs"

printf '\nOK: images built, scanned and verified\n'
