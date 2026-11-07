#!/bin/sh
set -eu

# Any command other than the image default runs directly, without the daemon.
if [ "$#" -ne 1 ] || [ "$1" != marabrain ]; then
    exec "$@"
fi

crystalline config set service.http false
crystalline serve &
daemon=$!
"$@" &
app=$!

stop() {
    kill -TERM "$app" "$daemon" 2>/dev/null || true
}
trap stop TERM INT

# A trapped signal interrupts wait early; keep waiting until the app is reaped.
while :; do
    status=0
    wait "$app" || status=$?
    kill -0 "$app" 2>/dev/null || break
done

kill -TERM "$daemon" 2>/dev/null || true
while kill -0 "$daemon" 2>/dev/null; do
    wait "$daemon" || true
done
exit "$status"
