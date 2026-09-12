#!/bin/bash
# Bounded retry wrapper used ONLY for the Omamail assignment, to absorb the
# startup race against omarchy-shell itself: Omamail's mailto.sh calls
# `omarchy-shell shell summon omamail`, which fails with "omarchy-shell is
# not running"/"...is not ready" if invoked before the shell process is up --
# which a bare autostart line hits often, since both are started on the same
# hyprland.start event.
#
# argv is passed through verbatim from the generated Lua file, which only
# ever supplies this script's own absolute path plus a fixed, already-
# validated command -- never end-user free text. No shell-string
# construction here: "$@" is expanded as an argv array throughout.
set -u

if [ "$#" -eq 0 ]; then
  exit 2
fi

deadline=$(( $(date +%s) + 30 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  if /usr/bin/setsid -w "$@"; then
    exit 0
  fi
  sleep 0.5
done
exit 1
