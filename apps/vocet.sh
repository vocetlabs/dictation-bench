#!/usr/bin/env bash
# Vocet, as its setup leaves it after choosing "On this Mac": both local models downloaded, Right Option,
# hold to talk. The app is built from source (BENCH_VOCET_APP=<path to Vocet.app>), since no signed
# release exists for the commit under test.
. "$(dirname "$0")/common.sh"
repo=https://github.com/vocetlabs/dictation-app
commit=a7c7a46d94662d68623c6e630a147714e7f6e274
app=${BENCH_VOCET_APP:-}
if [ -z "$app" ]; then  # build the pinned commit, ad-hoc signed (the release certificate isn't public)
  src=$(mktemp -d)/dictation-app
  git clone --quiet --filter=blob:none "$repo" "$src"
  git -C "$src" -c advice.detachedHead=false checkout --quiet "$commit"
  case "$(node -v 2>/dev/null)" in v24.*) ;; *)  # the repo's engines field: Node 24
    brew install --quiet node@24 && export PATH="$(brew --prefix node@24)/bin:$PATH" ;; esac
  (cd "$src" && rustup toolchain install && npm ci --no-audit --no-fund &&
    npm run tauri build -- --bundles app --config '{"bundle":{"macOS":{"signingIdentity":"-"}}}' 2>&1 | tail -n 40)
  [ -d "$src/target/release/bundle/macos/Vocet.app" ] || { log "the Vocet build failed"; exit 1; }
  app=$src/target/release/bundle/macos/Vocet.app
fi
rm -rf /Applications/Vocet.app 2>/dev/null || sudo rm -rf /Applications/Vocet.app
ditto "$app" /Applications/Vocet.app 2>/dev/null || sudo ditto "$app" /Applications/Vocet.app
grant com.dictationapp.desktop
dir="$HOME/Library/Application Support/com.dictationapp.desktop"
# The two models from the app's catalogue (crates/core/src/local_models.rs), pinned and checked.
[ -f "$dir/models/orukqwen-asr-1.7b-Q4_K_M.gguf" ] || fetch \
  https://huggingface.co/vocetlabs/orukqwen-asr-1.7b-gguf/resolve/c947468edd421a9922afbbb7887056f793e5fd20/orukqwen-asr-1.7b-Q4_K_M.gguf \
  057471662f060800a6374708ad679d4abe51775e52ca591a88ea4ade78b273bb "$dir/models/orukqwen-asr-1.7b-Q4_K_M.gguf"
[ -f "$dir/models/gemma-4-E2B-it-Q4_K_M.gguf" ] || fetch \
  https://huggingface.co/unsloth/gemma-4-E2B-it-GGUF/resolve/0314792d7f1f7e229411f620751375812bb9faf2/gemma-4-E2B-it-Q4_K_M.gguf \
  740185b21d22ceb83a11c3aa62ad5842ef32c70f6096d756bbee85a1e4ec34b8 "$dir/models/gemma-4-E2B-it-Q4_K_M.gguf"
printf '{\n  "version": 2,\n  "hotkey": "RightAlt",\n  "launch_at_login": false,\n  "transcription": "local",\n  "share_usage_events": false,\n  "history_limit": 25\n}\n' \
  >"$dir/settings.json"
touch "$dir/onboarding-completed"
start Vocet.app Vocet
