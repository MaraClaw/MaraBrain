#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -f "$script_dir/.env" ]; then
    set -a
    . "$script_dir/.env"
    set +a
fi

: "${MARABRAIN_BEARER_SECRET:?MARABRAIN_BEARER_SECRET is required}"
if [ "${#MARABRAIN_BEARER_SECRET}" -lt 32 ]; then
    printf '%s\n' 'MARABRAIN_BEARER_SECRET must contain at least 32 characters' >&2
    exit 1
fi

MARABRAIN_IMAGE=${MARABRAIN_IMAGE:-marabrain:latest}
MARABRAIN_CONTAINER=${MARABRAIN_CONTAINER:-marabrain}
MARABRAIN_HOST_PORT=${MARABRAIN_HOST_PORT:-9110}
MARABRAIN_ALLOWED_HOSTS=${MARABRAIN_ALLOWED_HOSTS-'["127.0.0.1","localhost","[::1]"]'}
MARABRAIN_ALLOWED_ORIGINS=${MARABRAIN_ALLOWED_ORIGINS-'[]'}
case $MARABRAIN_HOST_PORT in
    ''|*[!0-9]*) printf '%s\n' 'MARABRAIN_HOST_PORT must be a port from 1 to 65535' >&2; exit 1 ;;
esac
if [ "$MARABRAIN_HOST_PORT" -lt 1 ] || [ "$MARABRAIN_HOST_PORT" -gt 65535 ]; then
    printf '%s\n' 'MARABRAIN_HOST_PORT must be a port from 1 to 65535' >&2
    exit 1
fi
MARABRAIN_DATA_VOLUME=${MARABRAIN_DATA_VOLUME:-marabrain-data}
case $MARABRAIN_DATA_VOLUME in
    [!A-Za-z0-9]*|*[!A-Za-z0-9_.-]*) printf '%s\n' 'MARABRAIN_DATA_VOLUME must be a Docker volume name' >&2; exit 1 ;;
esac
export MARABRAIN_BEARER_SECRET MARABRAIN_ALLOWED_HOSTS MARABRAIN_ALLOWED_ORIGINS

docker build --tag "$MARABRAIN_IMAGE" "$script_dir"
docker run --rm --init --read-only --tmpfs /tmp --cap-drop ALL \
    --security-opt no-new-privileges \
    --env MARABRAIN_TRANSPORT=http \
    --env MARABRAIN_BEARER_SECRET --env MARABRAIN_ALLOWED_HOSTS --env MARABRAIN_ALLOWED_ORIGINS \
    "$MARABRAIN_IMAGE" python -c 'from marabrain.config import Settings; Settings()'
exec docker run --name "$MARABRAIN_CONTAINER" --init --read-only --tmpfs /tmp \
    --cap-drop ALL --security-opt no-new-privileges --stop-timeout 10 \
    --mount "type=volume,source=$MARABRAIN_DATA_VOLUME,target=/data" \
    --publish "127.0.0.1:$MARABRAIN_HOST_PORT:9110" \
    --env MARABRAIN_TRANSPORT=http \
    --env MARABRAIN_BEARER_SECRET --env MARABRAIN_ALLOWED_HOSTS --env MARABRAIN_ALLOWED_ORIGINS \
    "$MARABRAIN_IMAGE"
