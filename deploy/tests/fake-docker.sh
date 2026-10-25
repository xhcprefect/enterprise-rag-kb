#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${OPERATIONS_LOG:?}"
case "$*" in
  *'ps --status running --services api worker'*) printf 'api\nworker\n' ;;
  *pg_dump*)
    [[ ${FAIL_DUMP:-0} != 1 ]] || exit 91
    printf -- '-- PostgreSQL database dump\nSELECT 1;\n'
    ;;
  *psql*)
    cat >/dev/null
    [[ ${FAIL_RESTORE:-0} != 1 ]] || exit 92
    ;;
  *'stop --timeout 120'*|*'start api worker'*) ;;
  *) echo "意外的 Docker 调用: $*" >&2; exit 93 ;;
esac
