#!/bin/bash
# Applies the generated Hyprland config and reports whether it was accepted.
# No arguments. Absolute paths only, own session, bounded time and output.
# Always exits 0; the caller (hlw-helper.py) reads exactly one line of stdout:
# either "OK" or "ERROR:<bounded, single-line detail>". Called only from the
# Python helper, which owns the rollback decision on failure -- never invoked
# directly from QML.
set -uo pipefail

RELOAD_OUT=$(/usr/bin/setsid -w /usr/bin/timeout -k 2 -- 5 /usr/bin/hyprctl reload 2>&1 \
  | /usr/bin/head -c 4097)
if [ "${#RELOAD_OUT}" -gt 4096 ]; then
  echo "ERROR:reload output too large"
  exit 0
fi

ERRS=$(/usr/bin/setsid -w /usr/bin/timeout -k 2 -- 5 /usr/bin/hyprctl configerrors 2>&1 \
  | LC_ALL=C /usr/bin/head -c 513)
if [ "${#ERRS}" -gt 512 ]; then
  echo "ERROR:configerrors output too large"
  exit 0
fi

case "$ERRS" in
  "" | *"no errors"* | *"config ok"* | *"Ok"*)
    echo "OK"
    ;;
  *)
    printf 'ERROR:%s\n' "$(printf '%s' "$ERRS" | tr '\n' ' ')"
    ;;
esac
exit 0
