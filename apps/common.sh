# Sourced by apps/<app>.sh. BENCH_CI=1 means a CI runner (SIP off, passwordless sudo): permissions are
# granted by writing the TCC rows System Settings would write. Locally, grant them when macOS asks.
set -euo pipefail
PROFILE=${BENCH_PROFILE:-shipped}  # apps/<app>.<profile>.json: shipped, best-asr or local-cleanup
USER_TCC="$HOME/Library/Application Support/com.apple.TCC/TCC.db"
SYS_TCC="/Library/Application Support/com.apple.TCC/TCC.db"
log() { echo "setup: $*" >&2; }
# The bench's own app folder, always <bench>/work/Applications (macos.apps_dir), with no override:
# /Applications is shared with the owner, so the bench never installs into or deletes from it. A host run
# has already cleared the app's settings and data in the bench account (isolation.py); setup writes fresh ones.
APPS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/work/Applications"
mkdir -p "$APPS"

# install_dmg <url> <sha256> <App.app>: the maintainer's signed release into the bench's app folder.
install_dmg() {
  local dmg mnt
  dmg="${BENCH:-.}/work/dmg/$2.dmg"  # kept by checksum: a host run sets an app up again for each profile
  if ! echo "$2  $dmg" | shasum -a 256 -c - >/dev/null 2>&1; then
    mkdir -p "$(dirname "$dmg")"
    curl -fsSL -o "$dmg" "$1"
    echo "$2  $dmg" | shasum -a 256 -c - >/dev/null
  fi
  mnt=$(hdiutil attach -nobrowse -readonly "$dmg" | awk -F'\t' '/\/Volumes\// {print $NF}' | tail -1)
  rm -rf "${APPS:?}/$3"
  ditto "$mnt/$3" "$APPS/$3"
  hdiutil detach -quiet "$mnt"
}

# fetch <url> <sha256> <dest>: downloaded once into the bench's cache (by checksum), then cloned into
# place (APFS clone: no extra space), since a host run starts each job from fresh app data.
fetch() {
  local cached="${BENCH:-.}/work/cache/$2"
  if ! echo "$2  $cached" | shasum -a 256 -c - >/dev/null 2>&1; then
    mkdir -p "$(dirname "$cached")"
    curl -fsSL -o "$cached" "$1"
    echo "$2  $cached" | shasum -a 256 -c - >/dev/null
  fi
  mkdir -p "$(dirname "$3")"
  cp -c "$cached" "$3" 2>/dev/null || cp "$cached" "$3"
}

# hf_folder <repo> <revision> <dest> <include...>: a Hugging Face model folder, downloaded once into the
# bench's cache and cloned into <dest> (the app's own folder, fresh for each job).
hf_folder() {
  local repo=$1 rev=$2 dest=$3 cache="${BENCH:-.}/work/cache/hf/${1//\//_}@$2"; shift 3
  uvx --quiet --from huggingface_hub==0.26.2 huggingface-cli download "$repo" --revision "$rev" \
    --local-dir "$cache" --include "$@" >/dev/null
  mkdir -p "$(dirname "$dest")"
  rm -rf "$dest"
  cp -cR "$cache" "$dest" 2>/dev/null || cp -R "$cache" "$dest"
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
  open -g "$APPS/$1"
  for _ in $(seq 1 60); do  # the copy launched from the bench folder, not another of the same name
    pgrep -f "$APPS/$1/Contents/MacOS/" >/dev/null && { sleep 10; return 0; }; sleep 1
  done
  log "$2 didn't start"; return 1
}

# ollama_model <name>: the pinned Ollama model for a local-cleanup profile, pulled if missing (Ollama must be
# installed and serving on localhost:11434). Its digest goes into the setup log.
ollama_model() {
  command -v ollama >/dev/null || { log "local-cleanup needs Ollama: brew install ollama && ollama serve"; return 1; }
  ollama list | awk 'NR > 1 {print $1}' | grep -qx "$1" || ollama pull "$1"
  log "ollama model: $(ollama list | awk -v m="$1" '$1 == m {print $1, $2}')"
}
