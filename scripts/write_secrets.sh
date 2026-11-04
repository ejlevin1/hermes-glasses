#!/usr/bin/env bash
#
# write_secrets.sh - install Config/Secrets.xcconfig from this machine's copy.
#
# Config/Secrets.xcconfig holds the Meta Wearables DAT credentials
# (META_APP_ID + CLIENT_TOKEN) that Config/Hermes.xcconfig #includes and
# Info.plist's MWDAT dict reads. It is gitignored, so a fresh clone - which
# is exactly what a release build runs from - has no copy of it, and without
# one the app builds against the placeholder values in Hermes.xcconfig and
# cannot talk to the glasses (permissionDenied, at runtime, with nothing in
# the build log).
#
# The source is kept outside every checkout:
#   $HERMES_GLASSES_SECRETS, or ~/Development/.env/hermes-glasses-secrets.xcconfig
#
# Nothing is printed from either file: a `cat` here would put the client
# token in a build log. Only paths and a byte count are reported.
#
# Called by the fastlane `beta` and `test` lanes when the destination is
# missing, and safe to run by hand at any time (it overwrites).

set -euo pipefail

# 077 so the copy is 0600 even though `cp -p` is not used: the destination
# may not exist yet, and its mode is created under this umask.
umask 077

SRC="${HERMES_GLASSES_SECRETS:-$HOME/Development/.env/hermes-glasses-secrets.xcconfig}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$REPO_ROOT/Config/Secrets.xcconfig"

if [ ! -f "$SRC" ]; then
  cat >&2 <<MSG
error: no Meta DAT secrets file at
         $SRC

Config/Secrets.xcconfig cannot be written. Either:
  * put the file there (keys: META_APP_ID, CLIENT_TOKEN - see
    Config/Secrets.example.xcconfig), or
  * point HERMES_GLASSES_SECRETS at it:
        HERMES_GLASSES_SECRETS=/path/to/secrets.xcconfig $0

The values come from https://wearables.developer.meta.com/ (project ->
Configuration -> Application ID); Eric's are in 1Password, Infrastructure
item tfxsu24pnilmqbie2wyifef5dq.
MSG
  exit 1
fi

mkdir -p "$REPO_ROOT/Config"
cp "$SRC" "$DEST"
chmod 600 "$DEST"

echo "Wrote $DEST ($(wc -c <"$DEST" | tr -d ' ') bytes) from $SRC"
