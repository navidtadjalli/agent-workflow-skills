#!/usr/bin/env bash
set -euo pipefail
umask 077

usage() {
  printf '%s\n' 'usage: ledger.sh <seal|verify|restore|log|path> <initiative-dir>' >&2
}

fail() {
  printf 'ledger: %s\n' "$*" >&2
  exit 2
}

hash_stream() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | awk '{print $1}'
  elif command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 | awk '{print $NF}'
  else
    fail 'need sha256sum, shasum, or openssl'
  fi
}

hash_file() {
  hash_stream < "$1"
}

cmd="${1:-}"
initiative_arg="${2:-}"

if [ -z "$cmd" ] || [ -z "$initiative_arg" ]; then
  usage
  exit 2
fi

[ -d "$initiative_arg" ] || fail "not a directory: $initiative_arg"
initiative="$(cd "$initiative_arg" && pwd -P)"
state="$initiative/prompts/STATE.md"
[ -f "$state" ] || fail "missing STATE.md: $state"

if command -v git >/dev/null 2>&1 && repo="$(git -C "$initiative" rev-parse --show-toplevel 2>/dev/null)"; then
  repo="$(cd "$repo" && pwd -P)"
elif [ "$(basename "$(dirname "$initiative")")" = .tasks ]; then
  repo="$(cd "$initiative/../.." && pwd -P)"
else
  repo="$(dirname "$initiative")"
fi

slug="$(basename "$initiative")"
repo_key="$(printf '%s' "$repo" | hash_stream | cut -c1-16)"
state_base="${TASKFORGE_STATE_HOME:-${XDG_STATE_HOME:-$HOME/.local/state}/taskforge}"
canonical_vault="$state_base/ledgers/$repo_key/$slug"
vault="$canonical_vault"
using_legacy=0

if [ -z "${TASKFORGE_STATE_HOME:-}" ]; then
  legacy_root="${TASKFORGE_LEGACY_STATE_HOME:-$HOME/.claude/taskforge-ledger}"
  legacy_vault="$legacy_root/$slug"
  case "$cmd" in
    verify|restore|log)
      if [ ! -f "$canonical_vault/current.md" ] && [ -f "$legacy_vault/current.md" ]; then
        vault="$legacy_vault"
        using_legacy=1
      fi
      ;;
  esac
fi

snapshots="$vault/snapshots"
sealed="$vault/current.md"
sealed_sha="$vault/current.sha256"
location="$vault/location.txt"

assert_baseline() {
  local expected actual
  [ -f "$sealed" ] || fail "no baseline for $slug; run seal first"
  [ -f "$sealed_sha" ] || fail "baseline checksum missing for $slug"
  expected="$(awk 'NR == 1 {print $1}' "$sealed_sha")"
  actual="$(hash_file "$sealed")"
  [ "$expected" = "$actual" ] || fail "baseline store is inconsistent for $slug"
}

case "$cmd" in
  seal)
    mkdir -p -- "$snapshots"
    timestamp="$(date -u +%Y%m%dT%H%M%SZ).$$"
    snapshot="$snapshots/STATE.$timestamp.md"
    current_tmp="$(mktemp "$vault/.current.XXXXXX")"
    sha_tmp="$(mktemp "$vault/.sha.XXXXXX")"
    location_tmp="$(mktemp "$vault/.location.XXXXXX")"

    cp -- "$state" "$snapshot"
    cp -- "$state" "$current_tmp"
    hash_file "$current_tmp" > "$sha_tmp"
    printf 'repo=%s\ninitiative=%s\n' "$repo" "$initiative" > "$location_tmp"
    mv -- "$current_tmp" "$sealed"
    mv -- "$sha_tmp" "$sealed_sha"
    mv -- "$location_tmp" "$location"
    printf 'ledger: sealed %s @ %s\n' "$slug" "$(cut -c1-12 "$sealed_sha")"
    ;;

  verify)
    assert_baseline
    expected="$(awk 'NR == 1 {print $1}' "$sealed_sha")"
    actual="$(hash_file "$state")"
    if [ "$actual" = "$expected" ]; then
      if [ "$using_legacy" -eq 1 ]; then
        printf 'ledger: intact %s @ %s (legacy baseline; seal to migrate)\n' "$slug" "${actual:0:12}"
      else
        printf 'ledger: intact %s @ %s\n' "$slug" "${actual:0:12}"
      fi
      exit 0
    fi

    printf 'ledger: drift detected for %s\n' "$slug" >&2
    printf 'ledger: baseline=%s current=%s\n' "$expected" "$actual" >&2
    diff -u -- "$sealed" "$state" >&2 || true
    exit 1
    ;;

  restore)
    assert_baseline
    mkdir -p -- "$snapshots"
    timestamp="$(date -u +%Y%m%dT%H%M%SZ).$$"
    cp -- "$state" "$snapshots/STATE.drift.$timestamp.md"
    state_tmp="$(mktemp "$initiative/prompts/.STATE.XXXXXX")"
    cp -- "$sealed" "$state_tmp"
    mv -- "$state_tmp" "$state"
    printf 'ledger: restored %s; drift copy retained in %s\n' "$slug" "$snapshots"
    ;;

  log)
    if [ -d "$snapshots" ]; then
      for snapshot in "$snapshots"/*; do
        [ -f "$snapshot" ] && basename "$snapshot"
      done | sort
    else
      printf 'ledger: no snapshots for %s\n' "$slug"
    fi
    ;;

  path)
    printf '%s\n' "$vault"
    ;;

  *)
    usage
    exit 2
    ;;
esac
