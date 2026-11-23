#!/usr/bin/env bash
# Vocet 0.2.0, the published notarised DMG (checksum pinned), as its setup leaves it after choosing "On this
# Mac": both local models (OrukQwen 1.7B speech, Gemma 4 E2B cleanup), Right Option, hold to talk; usage
# sharing off. best-asr: the same without cleanup (0.2.0 cleans up only while the Gemma model is present,
# so the file is moved aside; other profiles move it back). Vocet is built by this bench's developer.
. "$(dirname "$0")/common.sh"
install_dmg https://github.com/vocetlabs/dictation-app/releases/download/v0.2.0/Vocet_0.2.0_aarch64.dmg \
  b7a3137e0ddccbb48c8ff21f82ee1e1301a51a51d0b209cfb9366cb7a9068e70 Vocet.app
grant com.dictationapp.desktop
dir="$HOME/Library/Application Support/com.dictationapp.desktop"
aside="$dir/models-bench-off"
osascript -e 'quit app "Vocet"' >/dev/null 2>&1 || true
# The two models from the app's catalogue (crates/core/src/local_models.rs at v0.2.0), pinned and checked.
[ -f "$aside/gemma-4-E2B-it-Q4_K_M.gguf" ] && [ "$PROFILE" != best-asr ] &&
  mkdir -p "$dir/models" && mv "$aside/gemma-4-E2B-it-Q4_K_M.gguf" "$dir/models/"
[ -f "$dir/models/orukqwen-asr-1.7b-Q4_K_M.gguf" ] || fetch \
  https://huggingface.co/vocetlabs/orukqwen-asr-1.7b-gguf/resolve/c947468edd421a9922afbbb7887056f793e5fd20/orukqwen-asr-1.7b-Q4_K_M.gguf \
  057471662f060800a6374708ad679d4abe51775e52ca591a88ea4ade78b273bb "$dir/models/orukqwen-asr-1.7b-Q4_K_M.gguf"
if [ "$PROFILE" = best-asr ]; then
  [ -f "$dir/models/gemma-4-E2B-it-Q4_K_M.gguf" ] && mkdir -p "$aside" && mv "$dir/models/gemma-4-E2B-it-Q4_K_M.gguf" "$aside/"
else
  [ -f "$dir/models/gemma-4-E2B-it-Q4_K_M.gguf" ] || fetch \
    https://huggingface.co/unsloth/gemma-4-E2B-it-GGUF/resolve/0314792d7f1f7e229411f620751375812bb9faf2/gemma-4-E2B-it-Q4_K_M.gguf \
    740185b21d22ceb83a11c3aa62ad5842ef32c70f6096d756bbee85a1e4ec34b8 "$dir/models/gemma-4-E2B-it-Q4_K_M.gguf"
fi
printf '{\n  "version": 2,\n  "hotkey": "RightAlt",\n  "launch_at_login": false,\n  "transcription": "local",\n  "share_usage_events": false,\n  "history_limit": 25\n}\n' \
  >"$dir/settings.json"
touch "$dir/onboarding-completed"
start Vocet.app Vocet
