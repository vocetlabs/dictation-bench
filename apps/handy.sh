#!/usr/bin/env bash
# Handy 0.10.1, as its onboarding leaves it: English, the first recommended model downloaded and chosen,
# every other setting at its fresh-install default (Option+Space, hold-or-toggle).
. "$(dirname "$0")/common.sh"
install_dmg https://github.com/cjpais/Handy/releases/download/v0.10.1/Handy_0.10.1_aarch64.dmg \
  7cdef5de7d1bfcea70813320b897540cbead2190f5280db6d9760a59e5995463 Handy.app
grant com.pais.handy
# Handy finds catalogue models in the standard Hugging Face cache (managers/model.rs at v0.10.1), pinned to
# the catalogue's revision; a model's id is "<repository>/<file>".
repo=handy-computer/parakeet-unified-en-0.6b-gguf
file=parakeet-unified-en-0.6b-Q8_0.gguf
path=$(uvx --quiet --from huggingface_hub==0.26.2 huggingface-cli download "$repo" "$file" \
  --revision 7e948f21b7bdbac698d3318db9d350f1096f3b6c | tail -1)
echo "4b50b6dd862bf6e346929aaf4f5eaacec003bfa3f56462d6c874b41ef2f38795  $path" | shasum -a 256 -c - >/dev/null
# Choosing a model completes onboarding (settings.rs: onboarding_completed = !selected_model.is_empty()).
dir="$HOME/Library/Application Support/com.pais.handy"
mkdir -p "$dir"
printf '{"settings": {"selected_model": "%s/%s", "selected_language": "en"}}\n' "$repo" "$file" >"$dir/settings_store.json"
start Handy.app handy
