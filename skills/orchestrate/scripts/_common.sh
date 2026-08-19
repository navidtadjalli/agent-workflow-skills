#!/usr/bin/env bash
set -euo pipefail

die() {
  printf 'error: %s\n' "$*" >&2
  exit 2
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

normalize_profile() {
  case "$1" in
    strong|t1) printf '%s\n' strong ;;
    balanced|t2) printf '%s\n' balanced ;;
    fast|t3) printf '%s\n' fast ;;
    *) die "profile must be strong|balanced|fast (or t1|t2|t3); got: $1" ;;
  esac
}

default_effort() {
  case "$1" in
    strong) printf '%s\n' high ;;
    balanced) printf '%s\n' medium ;;
    fast) printf '%s\n' low ;;
    *) die "unknown normalized profile: $1" ;;
  esac
}

load_task() {
  local task_arg="$1"
  local task_file

  case "$task_arg" in
    -)
      cat
      ;;
    @*)
      task_file="${task_arg#@}"
      [ -f "$task_file" ] || die "task file not found: $task_file"
      cat -- "$task_file"
      ;;
    *)
      printf '%s\n' "$task_arg"
      ;;
  esac
}

prepare_output() {
  local output="$1"
  mkdir -p -- "$(dirname "$output")"
}

write_status() {
  local output="$1"
  local value="$2"
  local status_file="${output}.status"
  local status_tmp="${status_file}.$$"

  printf '%s\n' "$value" > "$status_tmp"
  mv -- "$status_tmp" "$status_file"
}

is_true() {
  case "${1:-}" in
    1|true|TRUE|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}
