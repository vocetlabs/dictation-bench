#!/usr/bin/env bash
# Handy 0.10.1 (signed release) with its first recommended model, as its onboarding would leave it:
# English, push-to-talk on Option+Space (defaults), the model downloaded and chosen.
. "$(dirname "$0")/common.sh"
APP_VERSION=0.10.1
install_dmg https://github.com/cjpais/Handy/releases/download/v0.10.1/Handy_0.10.1_aarch64.dmg \
  7cdef5de7d1bfcea70813320b897540cbead2190f5280db6d9760a59e5995463 Handy.app
grant_app com.pais.handy
dir="$HOME/Library/Application Support/com.pais.handy"
# Parakeet Unified EN 0.6B, the catalogue's first recommendation, at its default quant (Q8_0). Handy finds
# catalogue models in the standard Hugging Face cache (managers/model.rs at v0.10.1), so the official client
# puts it there, pinned to the catalogue's revision; its id is "<repository>/<file>".
repo=handy-computer/parakeet-unified-en-0.6b-gguf
file=parakeet-unified-en-0.6b-Q8_0.gguf
path=$(uvx --quiet --from huggingface_hub==0.26.2 huggingface-cli download "$repo" "$file" \
  --revision 7e948f21b7bdbac698d3318db9d350f1096f3b6c | tail -1)
echo "4b50b6dd862bf6e346929aaf4f5eaacec003bfa3f56462d6c874b41ef2f38795  $path" | shasum -a 256 -c - >/dev/null
# Choosing a model completes onboarding (settings.rs: onboarding_completed = !selected_model.is_empty()).
mkdir -p "$dir"
printf '{"settings": {"selected_model": "%s/%s", "push_to_talk": true, "selected_language": "en"}}\n' "$repo" "$file" \
  >"$dir/settings_store.json"
open -g /Applications/Handy.app
sleep 15
pgrep -x handy >/dev/null || { log "Handy didn't start"; exit 1; }
echo "app=handy version=$APP_VERSION model=parakeet-unified-en-0.6b-Q8_0 model_size_mb=731"
