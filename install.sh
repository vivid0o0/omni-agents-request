#!/usr/bin/env bash
# install.sh -- OMNI-AGENTS-REQUEST installer
# description: Transactional Linux/macOS user installer and updater with source validation for OAR runtime files, managed responders, and command shims.
# Tags: installer, update, linux, macos, cli, permissions, validation
# date: 2026-07-06

set -Eeuo pipefail

APP_NAME="OMNI-AGENTS-REQUEST"
APP_VERSION="1.1.2"
DEFAULT_REPO="https://github.com/vivid0o0/omni-agents-request"
DEFAULT_REF="main"

INSTALL_DIR_CONFIGURED="${OAR_INSTALL_DIR+x}"
BIN_DIR_CONFIGURED="${OAR_BIN_DIR+x}"
REPO_URL_CONFIGURED="${OAR_REPO_URL+x}"
REPO_REF_CONFIGURED="${OAR_REF+x}"
INSTALL_DIR="${OAR_INSTALL_DIR:-$HOME/.local/share/omni-agents-request}"
BIN_DIR="${OAR_BIN_DIR:-$HOME/.local/bin}"
REPO_URL="${OAR_REPO_URL:-$DEFAULT_REPO}"
REPO_REF="${OAR_REF:-$DEFAULT_REF}"
SOURCE_SHA256="${OAR_SOURCE_SHA256:-}"
MODE="install"
FORCE_COMBINER=0
PYTHON_BIN=""
TEMP_DIR=""
STAGE_DIR=""
BACKUP_DIR=""
REPLACED_INSTALL=0
MANAGED_RESPONDER_FILES=("chatgpt_web.py" "glm_web.py")

logo() {
  cat <<'LOGO'
╔════════════════════════════════════════════╗
║            ____    ___     ____            ║
║           / __ \  /   |   / __ \           ║
║          / / / / / /| |  / /_/ /           ║
║         / /_/ / / ___ | / _, _/            ║
║         \____/ /_/  |_|/_/ |_|             ║
║                                            ║
║           OMNI-AGENTS-REQUEST              ║
╚════════════════════════════════════════════╝
LOGO
}

info() { printf '◆ %s\n' "$*"; }
ok() { printf '✓ %s\n' "$*"; }
fail() { printf '✗ %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

shell_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

absolute_dir() {
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    *) printf '%s/%s\n' "$PWD" "$1" ;;
  esac
}

read_first_line() {
  local file="$1"
  local line=""
  [ -f "$file" ] || return 1
  IFS= read -r line < "$file" || true
  printf '%s\n' "$line"
}

file_sha256() {
  local file="$1"
  if have sha256sum; then
    sha256sum "$file" | awk '{print $1}'
  elif have shasum; then
    shasum -a 256 "$file" | awk '{print $1}'
  else
    fail "no SHA-256 tool is available"
  fi
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --update) MODE="update" ;;
      --check) MODE="check" ;;
      --uninstall) MODE="uninstall" ;;
      --force-combiner) FORCE_COMBINER=1 ;;
      --help|-h) usage; exit 0 ;;
      *) fail "unknown option: $1" ;;
    esac
    shift
  done
}

usage() {
  cat <<USAGE
$APP_NAME installer

Usage:
  ./install.sh [--update] [--check] [--uninstall] [--force-combiner]

Environment:
  OAR_INSTALL_DIR      default: $HOME/.local/share/omni-agents-request
  OAR_BIN_DIR          default: $HOME/.local/bin
  OAR_REPO_URL         default: $DEFAULT_REPO
  OAR_REF              default: $DEFAULT_REF
  OAR_SOURCE_SHA256    optional tarball checksum for archive fallback
USAGE
}

detect_os() {
  case "$(uname -s)" in
    Linux) printf 'linux\n' ;;
    Darwin) printf 'macos\n' ;;
    *) fail "unsupported OS: $(uname -s). OAR supports Linux and macOS." ;;
  esac
}

detect_python() {
  local candidate
  for candidate in python3 python; do
    if have "$candidate" && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

install_linux_dependencies() {
  if have apt-get; then
    info "Installing dependencies with apt-get"
    sudo apt-get update
    sudo apt-get install -y python3 curl git ca-certificates tar
  elif have dnf; then
    info "Installing dependencies with dnf"
    sudo dnf install -y python3 curl git ca-certificates tar
  elif have yum; then
    info "Installing dependencies with yum"
    sudo yum install -y python3 curl git ca-certificates tar
  elif have pacman; then
    info "Installing dependencies with pacman"
    sudo pacman -Sy --needed --noconfirm python curl git ca-certificates tar
  elif have zypper; then
    info "Installing dependencies with zypper"
    sudo zypper install -y python3 curl git ca-certificates tar
  else
    fail "no supported Linux package manager found for automatic dependency installation"
  fi
}

install_macos_dependencies() {
  if ! have brew; then
    have curl || fail "curl is required to install Homebrew"
    info "Installing Homebrew"
    NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    [ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"
    [ -x /usr/local/bin/brew ] && eval "$(/usr/local/bin/brew shellenv)"
  fi
  info "Installing dependencies with Homebrew"
  brew install python curl git ca-certificates
}

ensure_dependencies() {
  if ! detect_python >/dev/null 2>&1 || ! have curl || ! have git || ! have tar; then
    case "$(detect_os)" in
      linux) install_linux_dependencies ;;
      macos) install_macos_dependencies ;;
    esac
  fi
  PYTHON_BIN="$(detect_python)" || fail "Python 3.10 or newer is required"
  have curl || fail "curl is required"
  have git || fail "git is required"
  have tar || fail "tar is required"
}

valid_source_dir() {
  local root="$1"
  local responder
  [ -f "$root/main.py" ] || return 1
  [ -f "$root/install.sh" ] || return 1
  [ -f "$root/README.md" ] || return 1
  [ -f "$root/SKILL.md" ] || return 1
  [ -f "$root/LICENSE" ] || return 1
  [ -f "$root/agents/COMBINER.py" ] || return 1
  for responder in "${MANAGED_RESPONDER_FILES[@]}"; do
    [ -f "$root/agents/$responder" ] || return 1
  done
}

valid_install_dir() {
  valid_source_dir "$1" &&
    [ -f "$1/.install/install_dir" ] &&
    [ -f "$1/.install/bin_dir" ]
}

load_install_context() {
  local script_dir="$1"
  local metadata="$script_dir/.install"
  local value
  if [ -z "$INSTALL_DIR_CONFIGURED" ] && valid_install_dir "$script_dir"; then
    INSTALL_DIR="$script_dir"
  fi
  if [ -z "$BIN_DIR_CONFIGURED" ]; then
    value="$(read_first_line "$metadata/bin_dir" 2>/dev/null || true)"
    [ -n "$value" ] && BIN_DIR="$value"
  fi
  if [ -z "$REPO_URL_CONFIGURED" ]; then
    value="$(read_first_line "$metadata/repo_url" 2>/dev/null || true)"
    [ -n "$value" ] && REPO_URL="$value"
  fi
  if [ -z "$REPO_REF_CONFIGURED" ]; then
    value="$(read_first_line "$metadata/ref" 2>/dev/null || true)"
    [ -n "$value" ] && REPO_REF="$value"
  fi
  return 0
}

verify_sha256() {
  local file="$1"
  [ -z "$SOURCE_SHA256" ] && return 0
  local actual
  actual="$(file_sha256 "$file")"
  [ "$actual" = "$SOURCE_SHA256" ] || fail "source archive checksum mismatch"
}

source_dir() {
  local script_dir="$1"
  local temp_dir="$2"
  if [ "$MODE" != "update" ] && valid_source_dir "$script_dir"; then
    printf '%s\n' "$script_dir"
    return 0
  fi
  if [ -d "$REPO_URL" ]; then
    valid_source_dir "$REPO_URL" || fail "source tree is incomplete"
    (cd "$REPO_URL" && pwd -P)
    return 0
  fi
  if git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$temp_dir/src" >/dev/null 2>&1; then
    printf '%s\n' "$temp_dir/src"
    return 0
  fi
  local archive="$temp_dir/source.tar.gz"
  local archive_url="${REPO_URL%.git}/archive/${REPO_REF}.tar.gz"
  info "Fetching source archive: $archive_url"
  curl -fsSL "$archive_url" -o "$archive"
  verify_sha256 "$archive"
  mkdir -p "$temp_dir/src"
  tar -xzf "$archive" -C "$temp_dir/src" --strip-components=1
  printf '%s\n' "$temp_dir/src"
}

validate_source() {
  local src="$1"
  local responder
  valid_source_dir "$src" || fail "source tree is incomplete"
  "$PYTHON_BIN" -m py_compile "$src/main.py" "$src/agents/COMBINER.py" || fail "source Python files failed compilation"
  for responder in "${MANAGED_RESPONDER_FILES[@]}"; do
    "$PYTHON_BIN" -m py_compile "$src/agents/$responder" || fail "managed responder Python files failed compilation"
  done
  bash -n "$src/install.sh" || fail "source installer failed shell syntax validation"
}

harden_tree() {
  local path="$1"
  [ -e "$path" ] || return 0
  if [ -d "$path" ]; then
    find "$path" -type d -exec chmod 700 {} +
    find "$path" -type f -exec chmod 600 {} +
  else
    chmod 600 "$path"
  fi
}

stage_source() {
  local src="$1"
  mkdir -p "$STAGE_DIR/agents" "$STAGE_DIR/responses" "$STAGE_DIR/.install/backups"
  cp "$src/main.py" "$STAGE_DIR/main.py"
  cp "$src/install.sh" "$STAGE_DIR/install.sh"
  cp "$src/README.md" "$STAGE_DIR/README.md"
  cp "$src/SKILL.md" "$STAGE_DIR/SKILL.md"
  cp "$src/LICENSE" "$STAGE_DIR/LICENSE"
  printf '%s\n' "$INSTALL_DIR" > "$STAGE_DIR/.install/install_dir"
  printf '%s\n' "$BIN_DIR" > "$STAGE_DIR/.install/bin_dir"
  printf '%s\n' "$REPO_URL" > "$STAGE_DIR/.install/repo_url"
  printf '%s\n' "$REPO_REF" > "$STAGE_DIR/.install/ref"
  if [ -d "$INSTALL_DIR/.install/backups" ]; then cp -R "$INSTALL_DIR/.install/backups/." "$STAGE_DIR/.install/backups/"; fi
  if [ -d "$INSTALL_DIR/agents" ]; then cp -R "$INSTALL_DIR/agents/." "$STAGE_DIR/agents/"; fi
  if [ -d "$INSTALL_DIR/responses" ]; then cp -R "$INSTALL_DIR/responses/." "$STAGE_DIR/responses/"; fi
  stage_combiner "$src"
  local responder
  for responder in "${MANAGED_RESPONDER_FILES[@]}"; do
    cp "$src/agents/$responder" "$STAGE_DIR/agents/$responder"
  done
  harden_tree "$STAGE_DIR"
  chmod 700 "$STAGE_DIR"
  chmod 700 "$STAGE_DIR/install.sh"
}

stage_combiner() {
  local src="$1"
  local source_combiner="$src/agents/COMBINER.py"
  local staged_combiner="$STAGE_DIR/agents/COMBINER.py"
  local managed_hash_file="$STAGE_DIR/.install/managed_combiner_sha256"
  local previous_hash=""
  local current_hash=""
  local source_hash=""
  source_hash="$(file_sha256 "$source_combiner")"
  previous_hash="$(read_first_line "$INSTALL_DIR/.install/managed_combiner_sha256" 2>/dev/null || true)"

  if [ -f "$staged_combiner" ] && ! cmp -s "$source_combiner" "$staged_combiner"; then
    current_hash="$(file_sha256 "$staged_combiner")"
    if [ "$FORCE_COMBINER" -eq 1 ] || [ -z "$previous_hash" ] || [ "$current_hash" != "$previous_hash" ]; then
      backup_combiner "$staged_combiner"
    fi
  fi

  cp "$source_combiner" "$staged_combiner"
  printf '%s\n' "$source_hash" > "$managed_hash_file"
}

backup_combiner() {
  local source="$1"
  local stamp
  local backup
  local counter=0
  stamp="$(date +%Y%m%d%H%M%S)"
  backup="$STAGE_DIR/.install/backups/COMBINER.local.$stamp.py"
  while [ -e "$backup" ]; do
    counter=$((counter + 1))
    backup="$STAGE_DIR/.install/backups/COMBINER.local.$stamp.$counter.py"
  done
  cp "$source" "$backup"
}

replace_install() {
  local src="$1"
  local parent
  parent="$(dirname "$INSTALL_DIR")"
  mkdir -p "$parent"
  STAGE_DIR="$(mktemp -d "$parent/.oar-stage.XXXXXX")"
  BACKUP_DIR="$(mktemp -d "$parent/.oar-backup.XXXXXX")"
  rmdir "$BACKUP_DIR"
  stage_source "$src"
  if [ -e "$INSTALL_DIR" ]; then
    mv "$INSTALL_DIR" "$BACKUP_DIR"
    REPLACED_INSTALL=1
  fi
  mv "$STAGE_DIR" "$INSTALL_DIR"
  STAGE_DIR=""
}

write_command() {
  local name="$1"
  mkdir -p "$BIN_DIR"
  cat > "$BIN_DIR/$name" <<EOF
#!/usr/bin/env sh
exec $(shell_quote "$PYTHON_BIN") $(shell_quote "$INSTALL_DIR/main.py") "\$@"
EOF
  chmod 700 "$BIN_DIR/$name"
}

write_path_exports() {
  mkdir -p "$HOME/.config/fish/conf.d"
  local quoted_bin
  quoted_bin="$(shell_quote "$BIN_DIR")"
  local file
  for file in "$HOME/.profile" "$HOME/.bashrc" "$HOME/.zshrc"; do
    touch "$file"
    if ! grep -F "$BIN_DIR" "$file" >/dev/null 2>&1; then
      cat >> "$file" <<EOF

# OMNI-AGENTS-REQUEST
OAR_BIN_DIR=$quoted_bin
case ":\$PATH:" in
  *":\$OAR_BIN_DIR:"*) ;;
  *) export PATH="\$OAR_BIN_DIR:\$PATH" ;;
esac
unset OAR_BIN_DIR
EOF
    fi
  done
  cat > "$HOME/.config/fish/conf.d/oar.fish" <<EOF
set -l oar_bin_dir $quoted_bin
contains -- \$oar_bin_dir \$PATH; or set -gx PATH \$oar_bin_dir \$PATH
EOF
}

check_install() {
  local failed=0
  [ -f "$INSTALL_DIR/main.py" ] || { printf 'missing %s\n' "$INSTALL_DIR/main.py" >&2; failed=1; }
  [ -f "$INSTALL_DIR/install.sh" ] || { printf 'missing %s\n' "$INSTALL_DIR/install.sh" >&2; failed=1; }
  [ -f "$INSTALL_DIR/agents/COMBINER.py" ] || { printf 'missing %s\n' "$INSTALL_DIR/agents/COMBINER.py" >&2; failed=1; }
  [ -f "$INSTALL_DIR/agents/chatgpt_web.py" ] || { printf 'missing %s\n' "$INSTALL_DIR/agents/chatgpt_web.py" >&2; failed=1; }
  [ -f "$INSTALL_DIR/agents/glm_web.py" ] || { printf 'missing %s\n' "$INSTALL_DIR/agents/glm_web.py" >&2; failed=1; }
  [ -x "$BIN_DIR/oar" ] || { printf 'missing %s\n' "$BIN_DIR/oar" >&2; failed=1; }
  [ -x "$BIN_DIR/omni-agents-request" ] || { printf 'missing %s\n' "$BIN_DIR/omni-agents-request" >&2; failed=1; }
  if [ "$failed" -eq 0 ]; then
    [ "$("$BIN_DIR/oar" --version)" = "$APP_VERSION" ] || failed=1
  fi
  [ "$failed" -eq 0 ] && ok "install check passed"
  return "$failed"
}

finalize_install() {
  if [ -n "$BACKUP_DIR" ] && [ -d "$BACKUP_DIR" ]; then
    rm -rf "$BACKUP_DIR"
  fi
  BACKUP_DIR=""
  REPLACED_INSTALL=0
}

rollback() {
  local status="$1"
  [ "$status" -eq 0 ] && return 0
  if [ -n "$STAGE_DIR" ] && [ -d "$STAGE_DIR" ]; then rm -rf "$STAGE_DIR"; fi
  if [ "$REPLACED_INSTALL" -eq 1 ] && [ -n "$BACKUP_DIR" ] && [ -d "$BACKUP_DIR" ]; then
    rm -rf "$INSTALL_DIR"
    mv "$BACKUP_DIR" "$INSTALL_DIR"
    printf 'rolled back previous install\n' >&2
  fi
}

cleanup() {
  local status="$?"
  rollback "$status"
  [ -n "$TEMP_DIR" ] && [ -d "$TEMP_DIR" ] && rm -rf "$TEMP_DIR"
  exit "$status"
}

uninstall_oar() {
  valid_install_dir "$INSTALL_DIR" || fail "refusing to uninstall: $INSTALL_DIR is not an OAR install"
  rm -rf "$INSTALL_DIR"
  rm -f "$BIN_DIR/oar" "$BIN_DIR/omni-agents-request"
  ok "removed OAR files"
}

main() {
  parse_args "$@"
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
  load_install_context "$script_dir"
  INSTALL_DIR="$(absolute_dir "$INSTALL_DIR")"
  BIN_DIR="$(absolute_dir "$BIN_DIR")"
  logo
  case "$MODE" in
    uninstall) uninstall_oar; exit 0 ;;
    check) check_install; exit $? ;;
  esac
  trap cleanup EXIT
  ensure_dependencies
  TEMP_DIR="$(mktemp -d)"
  local src
  src="$(source_dir "$script_dir" "$TEMP_DIR")"
  validate_source "$src"
  replace_install "$src"
  write_command oar
  write_command omni-agents-request
  write_path_exports
  check_install
  finalize_install
  ok "$APP_NAME $APP_VERSION installed"
  info "Commands: oar, omni-agents-request"
  info "Install dir: $INSTALL_DIR"
}

main "$@"
