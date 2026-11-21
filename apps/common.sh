# Sourced by apps/<app>.sh. BENCH_CI=1 means a CI runner (SIP off, passwordless sudo): permissions are
# granted by writing the TCC rows System Settings would write. Locally, grant them when macOS asks.
set -euo pipefail
USER_TCC="$HOME/Library/Application Support/com.apple.TCC/TCC.db"
SYS_TCC="/Library/Application Support/com.apple.TCC/TCC.db"
log() { echo "setup: $*" >&2; }

# install_dmg <url> <sha256> <App.app>: the maintainer's signed release into /Applications.
install_dmg() {
  local dmg mnt
  dmg=$(mktemp -d)/app.dmg
  curl -fsSL -o "$dmg" "$1"
  echo "$2  $dmg" | shasum -a 256 -c - >/dev/null
  mnt=$(hdiutil attach -nobrowse -readonly "$dmg" | awk -F'\t' '/\/Volumes\// {print $NF}' | tail -1)
  rm -rf "/Applications/$3" 2>/dev/null || sudo rm -rf "/Applications/$3"
  ditto "$mnt/$3" "/Applications/$3" 2>/dev/null || sudo ditto "$mnt/$3" "/Applications/$3"
  hdiutil detach -quiet "$mnt"
}

# fetch <url> <sha256> <dest>
fetch() {
  mkdir -p "$(dirname "$3")"
  curl -fsSL -o "$3" "$1"
  echo "$2  $3" | shasum -a 256 -c - >/dev/null
}

# grant <bundle id>: Microphone and Accessibility (CI only).
grant() {
  [ -n "${BENCH_CI:-}" ] || { log "grant Microphone and Accessibility to $1 when macOS asks"; return 0; }
  local svc db
  for svc in kTCCServiceMicrophone kTCCServiceAccessibility kTCCServiceListenEvent kTCCServicePostEvent; do
    db=$USER_TCC; [ "$svc" = kTCCServiceMicrophone ] || db=$SYS_TCC
    sudo sqlite3 "$db" "INSERT OR REPLACE INTO access (service, client, client_type, auth_value, auth_reason, auth_version, indirect_object_identifier, flags, last_modified) VALUES ('$svc', '$1', 0, 2, 3, 1, 'UNUSED', 0, CAST(strftime('%s','now') AS INTEGER));"
  done
}

# start <App.app> <process>: open in the background and wait for the process.
start() {
  open -g "/Applications/$1"
  for _ in $(seq 1 60); do pgrep -x "$2" >/dev/null && { sleep 10; return 0; }; sleep 1; done
  log "$2 didn't start"; return 1
}
