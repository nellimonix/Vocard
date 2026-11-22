#!/bin/sh
set -eu
umask 077
exec python3 "$(dirname "$(readlink -f "$0")")/snapshot.py" "$@"
