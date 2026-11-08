#!/bin/sh
set -eu

# Any command other than the image default runs directly, without the daemon.
if [ "$#" -ne 1 ] || [ "$1" != marabrain ]; then
    exec "$@"
fi

export MARABRAIN_ENGINE_MODE=managed
exec "$@"
