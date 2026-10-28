#!/bin/sh
# Daily device-map check, run by a wake shell task on archserver.
# Wake tasks get a bare environment, hence the explicit PATH and absolute paths.
# `check` republishes the admin channel itself when a duty changed state; this
# script adds the one thing the channel cannot do: tell the operator, who runs
# the machines, that something it thinks is running is not.
PATH="$HOME/.claude/bin:/usr/local/bin:/usr/bin:/bin"
repo="$(dirname "$0")/.."
out="$("$repo/.venv/bin/hotline-registry" -q devices check --changes-only 2>&1)"
status=$?
if [ "$status" -eq 1 ]; then
    bad="$(printf '%s\n' "$out" | grep '❌' | cut -c1-200)"
    hotline --to hotline-80 --no-wait "device map: daily check found contradicted duties. The admin channel has the full map.
$bad
Re-run: hotline-registry devices check" >/dev/null 2>&1
fi
exit "$status"
