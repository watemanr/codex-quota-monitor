#!/usr/bin/env bash
set -euo pipefail
SOURCE="$(cd -- "$(dirname -- "$0")" && pwd)"
TARGET=/opt/codex-quota-monitor
ENV_FILE=/etc/codex-quota-monitor.env
NAME=codex-quota-monitor
RELEASE="$(date -u +%Y%m%d%H%M%S)"
TAG="codex-quota-monitor:$RELEASE"
if [[ $(id -u) != 0 ]]; then printf 'Run with sudo.\n' >&2; exit 1; fi
docker compose version >/dev/null
docker build --pull -t "$TAG" "$SOURCE"
docker run --rm --network none -v "$SOURCE/tests.py:/app/tests.py:ro" "$TAG" python -m unittest tests
install -d -m 755 "$TARGET" "$TARGET/app"
install -d -m 700 -o 10001 -g 10001 "$TARGET/data"
umask 077
python3 - "$ENV_FILE" <<'PY'
import pathlib,secrets,sys
p=pathlib.Path(sys.argv[1]);text=p.read_text() if p.exists() else ''
keys={line.split('=',1)[0]:line.split('=',1)[1] for line in text.splitlines() if '=' in line}
if not text:
    text='UPLOAD_TOKEN='+secrets.token_urlsafe(36)+'\nREAD_TOKEN='+secrets.token_urlsafe(36)+'\nDB_PATH=/data/quota.sqlite3\nRESET_TOLERANCE_SECONDS=300\nMINIMUM_DROP_PP=2\n'
if not keys.get('WEB_PASSWORD') and not keys.get('WEB_PASSWORD_HASH'):
    text=text.rstrip('\n')+'\nWEB_PASSWORD='+secrets.token_urlsafe(36)+'\n'
p.write_text(text)
PY
chmod 600 "$ENV_FILE"
# Preserve manual Compose password/port settings across scripted upgrades.
DEPLOY_DIR=$(mktemp -d "$TARGET/release.XXXXXX")
cp -a "$SOURCE/." "$DEPLOY_DIR/"
if [[ -f "$TARGET/app/docker-compose.yml" ]]; then
  cp "$TARGET/app/docker-compose.yml" "$DEPLOY_DIR/docker-compose.yml"
fi
python3 - "$DEPLOY_DIR/docker-compose.yml" "$TAG" "$RELEASE" <<'PY'
import pathlib,re,sys
p=pathlib.Path(sys.argv[1]);text=p.read_text()
text=re.sub(r'^name:.*$', 'name: codex-quota-'+sys.argv[3], text, count=1, flags=re.M)
text=re.sub(r'^    image:.*$', '    image: '+sys.argv[2], text, count=1, flags=re.M)
p.write_text(text)
PY
chmod 600 "$DEPLOY_DIR/docker-compose.yml"
# Resolve env_file and YAML overrides without printing credentials.
docker compose -f "$DEPLOY_DIR/docker-compose.yml" config --format json | python3 -c 'import json,sys; e=json.load(sys.stdin)["services"]["monitor"]["environment"]; open(sys.argv[1],"w").write("".join(k+"="+str(v)+"\n" for k,v in e.items() if v is not None))' "$DEPLOY_DIR/preflight.env"
docker run --rm --network none --env-file "$DEPLOY_DIR/preflight.env" --tmpfs /data:uid=10001,gid=10001 "$TAG" python -c 'from app import create_app; a=create_app(); assert a.test_client().get("/healthz").status_code==200'
rm -- "$DEPLOY_DIR/preflight.env"
OLD=''
if docker container inspect "$NAME" >/dev/null 2>&1; then
  docker exec "$NAME" python -c 'import sqlite3,time; s=sqlite3.connect("/data/quota.sqlite3"); t=sqlite3.connect("/data/quota-before-upgrade-"+time.strftime("%Y%m%d%H%M%S")+".sqlite3"); s.backup(t); t.close(); s.close(); print("SQLite backup ready")'
  OLD="$NAME-rollback-$RELEASE"
  docker stop "$NAME"
  docker rename "$NAME" "$OLD"
fi
rollback() {
  docker compose -f "$DEPLOY_DIR/docker-compose.yml" down || true
  if [[ -n "$OLD" ]]; then docker rename "$OLD" "$NAME"; docker start "$NAME"; fi
}
if ! docker compose -f "$DEPLOY_DIR/docker-compose.yml" up -d --no-build; then rollback; exit 1; fi
HEALTHY=0
for attempt in {1..15}; do
  if curl -fsS --max-time 3 http://127.0.0.1:8765/healthz; then HEALTHY=1; break; fi
  sleep 2
done
if [[ "$HEALTHY" != 1 ]]; then
  docker logs --tail 30 "$NAME"
  rollback
  exit 1
fi
if [[ "$SOURCE" != "$TARGET/app" ]]; then cp -a "$SOURCE/." "$TARGET/app/"; fi
cp "$DEPLOY_DIR/docker-compose.yml" "$TARGET/app/docker-compose.yml"
chmod 600 "$TARGET/app/docker-compose.yml"
printf '\nDeployment ready: HTTP :8765; Compose in %s/app/docker-compose.yml; keys in %s\n' "$TARGET" "$ENV_FILE"
if [[ -n "$OLD" ]]; then printf 'Previous container retained: %s\n' "$OLD"; fi
