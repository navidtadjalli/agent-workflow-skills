#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' "usage: ./install.sh [--all|--claude|--agents] [--force] [--dry-run]"
  printf '%s\n' ""
  printf '%s\n' "  --all       link into Claude Code and open Agent Skills user locations (default)"
  printf '%s\n' "  --claude    link into ~/.claude/skills"
  printf '%s\n' "  --agents    link into ~/.agents/skills"
  printf '%s\n' "  --force     back up conflicting destinations, then link"
  printf '%s\n' "  --dry-run   print intended changes only"
}

install_claude=0
install_agents=0
force=0
dry_run=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --all)
      install_claude=1
      install_agents=1
      ;;
    --claude) install_claude=1 ;;
    --agents) install_agents=1 ;;
    --force) force=1 ;;
    --dry-run) dry_run=1 ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'error: unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [ "$install_claude" -eq 0 ] && [ "$install_agents" -eq 0 ]; then
  install_claude=1
  install_agents=1
fi

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
skills_dir="$repo_dir/skills"
state_base="${XDG_STATE_HOME:-$HOME/.local/state}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_root="$state_base/agent-workflow-skills/backups/$stamp"
had_conflict=0

for skill in taskforge orchestrate; do
  if [ ! -f "$skills_dir/$skill/SKILL.md" ]; then
    printf 'error: missing source skill: %s\n' "$skills_dir/$skill/SKILL.md" >&2
    exit 2
  fi
done

ensure_dir() {
  local dir="$1"
  if [ -d "$dir" ]; then
    return
  fi
  if [ "$dry_run" -eq 1 ]; then
    printf 'would create directory: %s\n' "$dir"
  else
    mkdir -p -- "$dir"
  fi
}

preflight_one() {
  local root="$1"
  local skill src dest current

  for skill in taskforge orchestrate; do
    src="$skills_dir/$skill"
    dest="$root/$skill"

    if [ -L "$dest" ]; then
      current="$(readlink "$dest")"
      [ "$current" = "$src" ] && continue
    fi

    if [ -e "$dest" ] || [ -L "$dest" ]; then
      printf 'conflict: %s exists (rerun with --force to back it up)\n' "$dest" >&2
      had_conflict=1
    fi
  done
}

install_one() {
  local root="$1"
  local label="$2"
  local skill src dest current backup

  ensure_dir "$root"

  for skill in taskforge orchestrate; do
    src="$skills_dir/$skill"
    dest="$root/$skill"

    if [ -L "$dest" ]; then
      current="$(readlink "$dest")"
      if [ "$current" = "$src" ]; then
        printf 'already linked: %s -> %s\n' "$dest" "$src"
        continue
      fi
    fi

    if [ -e "$dest" ] || [ -L "$dest" ]; then
      if [ "$force" -ne 1 ]; then
        printf 'error: destination changed after preflight: %s\n' "$dest" >&2
        exit 2
      fi

      backup="$backup_root/$label/$skill"
      if [ "$dry_run" -eq 1 ]; then
        printf 'would back up: %s -> %s\n' "$dest" "$backup"
      else
        mkdir -p -- "$(dirname "$backup")"
        mv -- "$dest" "$backup"
      fi
    fi

    if [ "$dry_run" -eq 1 ]; then
      printf 'would link: %s -> %s\n' "$dest" "$src"
    else
      ln -s -- "$src" "$dest"
      printf 'linked: %s -> %s\n' "$dest" "$src"
    fi
  done
}

if [ "$force" -eq 0 ]; then
  [ "$install_claude" -eq 0 ] || preflight_one "$HOME/.claude/skills"
  [ "$install_agents" -eq 0 ] || preflight_one "$HOME/.agents/skills"
  [ "$had_conflict" -eq 0 ] || exit 1
fi

if [ "$install_claude" -eq 1 ]; then
  install_one "$HOME/.claude/skills" claude
fi

if [ "$install_agents" -eq 1 ]; then
  install_one "$HOME/.agents/skills" agents
fi

if [ "$force" -eq 1 ] && [ -d "$backup_root" ]; then
  printf 'backups: %s\n' "$backup_root"
fi
