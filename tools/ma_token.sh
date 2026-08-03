#!/usr/bin/env bash
# Mint a Music Assistant API session token, without ever printing it.
#
# Why this is awkward at all: MA opens its databases with
#
#     PRAGMA locking_mode=exclusive;
#
# so while MA is running no other process can write auth.db, or even read it.
# And `authenticate_with_token` requires a matching row in auth_tokens, so a
# correctly signed JWT on its own is refused. The only window in which a token
# can be inserted is while MA is stopped.
#
# So: stop, insert, start. The token is written to a root-only file on the box
# and copied into the container, and is never echoed, logged, or returned
# through a command line, because a command line is visible to anything that
# can read /proc and to whatever is reading this session's transcript.
#
#     tools/ma_token.sh          mint (6 hours)
#     tools/ma_token.sh revoke   delete it again
set -euo pipefail

BOX=${AMPERE_BOX:-hetzner}
SSH=(ssh -o BatchMode=yes "$BOX")
ACTION=${1:-mint}

"${SSH[@]}" ACTION="$ACTION" bash -euo pipefail -s <<'REMOTE'
DATA=/opt/music-assistant/data
TOKEN_FILE=/opt/ampere/.ma-token
NAME=ampere-tools-cli

alloc() {
  nomad job allocs -json music-assistant 2>/dev/null | python3 -c 'import json,sys
running = [a["ID"] for a in json.load(sys.stdin) if a["ClientStatus"] == "running"]
print(running[0] if running else "")'
}

if [ "$ACTION" = "revoke" ]; then
  rm -f "$TOKEN_FILE"
  A=$(alloc)
  [ -n "$A" ] && docker exec "app-$A" rm -f /tmp/.ma-token 2>/dev/null || true
  echo "==> token file removed (the row expires on its own)"
  exit 0
fi

echo "==> stopping Music Assistant to get write access to auth.db"
nomad job stop -detach music-assistant >/dev/null
for _ in $(seq 1 40); do
  [ -z "$(alloc)" ] && break
  sleep 2
done
[ -z "$(alloc)" ] || { echo "Music Assistant did not stop" >&2; exit 1; }
sleep 3

umask 077
python3 - "$DATA/auth.db" "$TOKEN_FILE" "$NAME" <<'PY'
import hashlib, secrets, sqlite3, sys
from datetime import datetime, timedelta, timezone
import jwt as pyjwt

db_path, out_path, name = sys.argv[1], sys.argv[2], sys.argv[3]
conn = sqlite3.connect(db_path, timeout=30)
secret = conn.execute("SELECT value FROM settings WHERE key = 'jwt_secret'").fetchone()[0]
user_id, username, role = conn.execute(
    "SELECT user_id, username, role FROM users WHERE role = 'admin' LIMIT 1"
).fetchone()

conn.execute("DELETE FROM auth_tokens WHERE name = ?", (name,))
token_id = secrets.token_urlsafe(32)
now = datetime.now(timezone.utc)
expires = now + timedelta(hours=6)
token = pyjwt.encode(
    {"sub": user_id, "jti": token_id, "iat": int(now.timestamp()),
     "exp": int(expires.timestamp()), "username": username, "role": role,
     "token_name": name, "is_long_lived": False},
    secret, algorithm="HS256",
)
conn.execute(
    "INSERT INTO auth_tokens (token_id, user_id, token_hash, name, created_at,"
    " expires_at, is_long_lived) VALUES (?, ?, ?, ?, ?, ?, 0)",
    (token_id, user_id, hashlib.sha256(token.encode()).hexdigest(), name,
     now.isoformat(), expires.isoformat()),
)
conn.commit()
conn.close()
with open(out_path, "w") as handle:
    handle.write(token)
PY
chmod 600 "$TOKEN_FILE"
echo "==> token written to $TOKEN_FILE (not printed)"

echo "==> starting Music Assistant"
nomad job start music-assistant >/dev/null 2>&1 || nomad job run /opt/nomad/music-assistant.nomad.hcl >/dev/null
for _ in $(seq 1 60); do
  A=$(alloc)
  if [ -n "$A" ] && curl -sf -m 3 -o /dev/null http://127.0.0.1:8095/; then
    docker cp "$TOKEN_FILE" "app-$A:/tmp/.ma-token" >/dev/null
    docker exec "app-$A" chmod 600 /tmp/.ma-token
    echo "==> Music Assistant is back, token staged in the container"
    exit 0
  fi
  sleep 3
done
echo "Music Assistant did not come back" >&2
exit 1
REMOTE
