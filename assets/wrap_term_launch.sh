#!/usr/bin/env sh

cat ~/.local/state/helysium/sequences.txt 2>/dev/null

exec "$@"
