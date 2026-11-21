#!/usr/bin/env bash
# Shared by the CI setup scripts (sourced). A GitHub-hosted macOS runner has a logged-in GUI session,
# SIP off and passwordless sudo, so everything a person would click through is done here directly:
# BlackHole as the default input, no automatic corrections, permissions as TCC rows.
set -euo pipefail
BENCH=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
USER_TCC="$HOME/Library/Application Support/com.apple.TCC/TCC.db"
SYS_TCC="/Library/Application Support/com.apple.TCC/TCC.db"

log() { echo "ci: $*" >&2; }

# tcc_grant <service> <bundle id>: one allowed row, the way System Settings writes it.
tcc_grant() {
  local db=$USER_TCC
  [ "$1" = kTCCServiceAccessibility ] && db=$SYS_TCC
  sudo sqlite3 "$db" "INSERT OR REPLACE INTO access (service, client, client_type, auth_value, auth_reason, auth_version, indirect_object_identifier, flags, last_modified) VALUES ('$1', '$2', 0, 2, 3, 1, 'UNUSED', 0, CAST(strftime('%s','now') AS INTEGER));"
}

# grant_app <bundle id>: Microphone and Accessibility (and Input Monitoring, which some hotkey code asks for).
grant_app() {
  tcc_grant kTCCServiceMicrophone "$1"
  tcc_grant kTCCServiceAccessibility "$1"
  tcc_grant kTCCServiceListenEvent "$1"
  tcc_grant kTCCServicePostEvent "$1"
}

# install_dmg <url> <sha256> <App.app name>: into /Applications.
install_dmg() {
  local dmg=/tmp/app.dmg mnt
  curl -fsSL -o "$dmg" "$1"
  echo "$2  $dmg" | shasum -a 256 -c - >/dev/null
  mnt=$(hdiutil attach -nobrowse -readonly "$dmg" | awk -F'\t' '/\/Volumes\// {print $NF}' | tail -1)
  sudo rm -rf "/Applications/$3"
  sudo ditto "$mnt/$3" "/Applications/$3"
  hdiutil detach -quiet "$mnt"
  rm -f "$dmg"
}

# fetch_verified <url> <sha256> <dest>
fetch_verified() {
  mkdir -p "$(dirname "$3")"
  curl -fsSL -o "$3" "$1"
  echo "$2  $3" | shasum -a 256 -c - >/dev/null
}

machine_setup() {
  command -v uv >/dev/null || brew install --quiet uv
  brew install --quiet --cask blackhole-2ch
  sudo killall coreaudiod || true
  sleep 5
  for k in NSAutomaticCapitalizationEnabled NSAutomaticPeriodSubstitutionEnabled \
    NSAutomaticSpellingCorrectionEnabled NSAutomaticQuoteSubstitutionEnabled NSAutomaticDashSubstitutionEnabled \
    NSAutomaticTextReplacementEnabled NSAutomaticInlinePredictionEnabled NSAutomaticTextCompletionEnabled; do
    defaults write -g "$k" -bool false
  done
  (cd "$BENCH" && uv run --quiet harness.py set-input "BlackHole 2ch")
  (cd "$BENCH" && uv run --quiet fetch_clips.py clips >/dev/null)
}

# encrypt <file> <out>: to the bench's public certificate (ci/results-cert.pem); only the holder of the
# matching private key can read it. Per-clip transcripts never leave the runner in the clear.
encrypt() {
  local ossl
  ossl=$(brew --prefix openssl@3 2>/dev/null)/bin/openssl
  [ -x "$ossl" ] || ossl=openssl
  "$ossl" cms -encrypt -binary -aes256 -in "$1" -outform DER -out "$2" "$BENCH/ci/results-cert.pem"
}
