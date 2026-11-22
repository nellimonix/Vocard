#!/bin/sh
set -eu
umask 077
cd "$(dirname "$(readlink -f "$0")")"
exec docker compose -f compose.yml run --rm --no-deps \
    -v "${BACKUP_RECOVERY:-/etc/yamusicbot-backup-recovery}:/recovery:ro" backup "$@"
