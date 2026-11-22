#!/bin/sh
set -eu
umask 077
cd "$(dirname "$(readlink -f "$0")")"
if [ "${1:-}" = restore ] && [ "${2##*.}" = age ]; then
    exec docker compose -f compose.yml run --rm --no-deps \
        -v "${BACKUP_RECOVERY:-/etc/yamusicbot-backup-recovery}:/recovery:ro" backup "$@"
fi
exec docker compose -f compose.yml run --rm --no-deps backup "$@"
