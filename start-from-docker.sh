#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -f "$script_dir/.env" ]; then
    set -a
    . "$script_dir/.env"
    set +a
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
export MARABRAIN_STARTUP_TIMEOUT_SECONDS MARABRAIN_OPERATION_TIMEOUT_SECONDS
export MARABRAIN_MAX_TRANSIENT_PROCESSES MARABRAIN_MAX_CAPTURE_BYTES MARABRAIN_MAX_TOTAL_CAPTURE_BYTES
export MARABRAIN_MAX_WORKSPACE_TRANSIENT_PROCESSES MARABRAIN_MAX_WORKSPACE_CAPTURE_BYTES
export MARABRAIN_MAX_INPUT_BYTES MARABRAIN_MAX_TOTAL_INPUT_BYTES
export MARABRAIN_MAX_RESIDENT_WORKSPACES
export MARABRAIN_DOMAIN_COMMAND_CAPACITY LLM_COMPRESSION_CACHE_MAX_ENTRIES LLM_COMPRESSION_CACHE_MAX_BYTES LLM_COMPRESSION_CACHE_TOTAL_MAX_BYTES
export MARABRAIN_WORKSPACE_ROOT
export EMBEDDING_MODEL EMBEDDING_DIMS EMBEDDING_PROVIDER_URL EMBEDDING_API_KEY
export LLM_MODEL LLM_PROVIDER_URL LLM_API_KEY LLM_PROVIDER
export LLM_REASONING_EFFORT LLM_TEMPERATURE LLM_MAX_INPUT_TOKENS LLM_MAX_TOKENS LLM_TIMEOUT_SECONDS

set --
if [ -n "${MARABRAIN_BEARER_SECRET_FILE:-}" ]; then
    case $MARABRAIN_BEARER_SECRET_FILE in
        /*) ;; *) printf '%s\n' 'MARABRAIN_BEARER_SECRET_FILE must be an absolute path' >&2; exit 1 ;;
    esac
    case $MARABRAIN_BEARER_SECRET_FILE in
        *,*) printf '%s\n' 'Secret file paths cannot contain commas' >&2; exit 1 ;;
    esac
    if [ ! -f "$MARABRAIN_BEARER_SECRET_FILE" ] || [ ! -r "$MARABRAIN_BEARER_SECRET_FILE" ]; then
        printf '%s\n' 'Credential file must be a readable regular file' >&2; exit 1
    fi
    set -- "$@" --mount "type=bind,source=$MARABRAIN_BEARER_SECRET_FILE,target=/run/secrets/marabrain_bearer,readonly" \
        --env MARABRAIN_BEARER_SECRET_FILE=/run/secrets/marabrain_bearer
fi
if [ -n "${EMBEDDING_API_KEY_FILE:-}" ]; then
    case $EMBEDDING_API_KEY_FILE in
        /*) ;; *) printf '%s\n' 'EMBEDDING_API_KEY_FILE must be an absolute path' >&2; exit 1 ;;
    esac
    case $EMBEDDING_API_KEY_FILE in
        *,*) printf '%s\n' 'Secret file paths cannot contain commas' >&2; exit 1 ;;
    esac
    if [ ! -f "$EMBEDDING_API_KEY_FILE" ] || [ ! -r "$EMBEDDING_API_KEY_FILE" ]; then
        printf '%s\n' 'Credential file must be a readable regular file' >&2; exit 1
    fi
    set -- "$@" --mount "type=bind,source=$EMBEDDING_API_KEY_FILE,target=/run/secrets/embedding_api_key,readonly" \
        --env EMBEDDING_API_KEY_FILE=/run/secrets/embedding_api_key
fi

docker build --tag "$MARABRAIN_IMAGE" "$script_dir"
docker run --rm --init --read-only --tmpfs /tmp --cap-drop ALL \
    --security-opt no-new-privileges \
    "$@" \
    --env MARABRAIN_TRANSPORT=http \
    --env MARABRAIN_ENGINE_MODE=managed --env MARABRAIN_MAX_RESIDENT_WORKSPACES \
    --env MARABRAIN_BEARER_SECRET --env MARABRAIN_ALLOWED_HOSTS --env MARABRAIN_ALLOWED_ORIGINS \
    --env MARABRAIN_ALLOWED_DOMAINS --env MARABRAIN_HEALTH_TIMEOUT_SECONDS --env MARABRAIN_COMMAND_TIMEOUT_SECONDS \
    --env MARABRAIN_WORKSPACE_ROOT \
    --env MARABRAIN_STARTUP_TIMEOUT_SECONDS --env MARABRAIN_OPERATION_TIMEOUT_SECONDS \
    --env MARABRAIN_MAX_TRANSIENT_PROCESSES --env MARABRAIN_MAX_CAPTURE_BYTES --env MARABRAIN_MAX_TOTAL_CAPTURE_BYTES \
    --env MARABRAIN_MAX_WORKSPACE_TRANSIENT_PROCESSES --env MARABRAIN_MAX_WORKSPACE_CAPTURE_BYTES \
    --env MARABRAIN_MAX_INPUT_BYTES --env MARABRAIN_MAX_TOTAL_INPUT_BYTES \
    --env MARABRAIN_DOMAIN_COMMAND_CAPACITY --env LLM_COMPRESSION_CACHE_MAX_ENTRIES --env LLM_COMPRESSION_CACHE_MAX_BYTES \
    --env LLM_COMPRESSION_CACHE_TOTAL_MAX_BYTES \
    --env MARABRAIN_ENGINE_STARTUP_TIMEOUT_SECONDS --env MARABRAIN_LOG_LEVEL --env EMBEDDING_API_KEY \
    --env LLM_MODEL --env LLM_PROVIDER_URL --env LLM_API_KEY --env LLM_PROVIDER \
    --env LLM_REASONING_EFFORT --env LLM_TEMPERATURE --env LLM_MAX_INPUT_TOKENS --env LLM_MAX_TOKENS --env LLM_TIMEOUT_SECONDS \
    "$MARABRAIN_IMAGE" python -c 'import os; from marabrain.config import Settings; from marabrain.credentials import resolve_secret; Settings(); resolve_secret(os.environ.get("EMBEDDING_API_KEY"), os.environ.get("EMBEDDING_API_KEY_FILE"))'
exec docker run --rm --name "$MARABRAIN_CONTAINER" --init --read-only --tmpfs /tmp \
    --cap-drop ALL --security-opt no-new-privileges --stop-timeout 10 \
    "$@" \
    --mount "type=volume,source=$MARABRAIN_DATA_VOLUME,target=/data" \
    --publish "127.0.0.1:$MARABRAIN_HOST_PORT:9110" \
    --env MARABRAIN_TRANSPORT=http \
    --env MARABRAIN_ENGINE_MODE=managed --env MARABRAIN_MAX_RESIDENT_WORKSPACES \
    --env MARABRAIN_BEARER_SECRET --env MARABRAIN_ALLOWED_HOSTS --env MARABRAIN_ALLOWED_ORIGINS \
    --env MARABRAIN_ALLOWED_DOMAINS --env MARABRAIN_HEALTH_TIMEOUT_SECONDS --env MARABRAIN_COMMAND_TIMEOUT_SECONDS \
    --env MARABRAIN_WORKSPACE_ROOT \
    --env MARABRAIN_STARTUP_TIMEOUT_SECONDS --env MARABRAIN_OPERATION_TIMEOUT_SECONDS \
    --env MARABRAIN_MAX_TRANSIENT_PROCESSES --env MARABRAIN_MAX_CAPTURE_BYTES --env MARABRAIN_MAX_TOTAL_CAPTURE_BYTES \
    --env MARABRAIN_MAX_WORKSPACE_TRANSIENT_PROCESSES --env MARABRAIN_MAX_WORKSPACE_CAPTURE_BYTES \
    --env MARABRAIN_MAX_INPUT_BYTES --env MARABRAIN_MAX_TOTAL_INPUT_BYTES \
    --env MARABRAIN_DOMAIN_COMMAND_CAPACITY --env LLM_COMPRESSION_CACHE_MAX_ENTRIES --env LLM_COMPRESSION_CACHE_MAX_BYTES \
    --env LLM_COMPRESSION_CACHE_TOTAL_MAX_BYTES \
    --env MARABRAIN_ENGINE_STARTUP_TIMEOUT_SECONDS --env MARABRAIN_LOG_LEVEL \
    --env EMBEDDING_MODEL --env EMBEDDING_DIMS --env EMBEDDING_PROVIDER_URL --env EMBEDDING_API_KEY \
    --env LLM_MODEL --env LLM_PROVIDER_URL --env LLM_API_KEY --env LLM_PROVIDER \
    --env LLM_REASONING_EFFORT --env LLM_TEMPERATURE --env LLM_MAX_INPUT_TOKENS --env LLM_MAX_TOKENS --env LLM_TIMEOUT_SECONDS \
    "$MARABRAIN_IMAGE"
