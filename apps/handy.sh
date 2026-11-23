#!/usr/bin/env bash
# Handy 0.10.1. shipped: as its onboarding leaves it (English, the first recommended model, Option+Space,
# hold-or-toggle). best-asr: Qwen3-ASR 1.7B, the catalogue's most accurate model for English. local-cleanup:
# the shipped model plus post-processing through Handy's Custom provider (Ollama, pinned model), on its
# transcribe-with-post-process shortcut. See CONFIGS.md.
. "$(dirname "$0")/common.sh"
install_dmg https://github.com/cjpais/Handy/releases/download/v0.10.1/Handy_0.10.1_aarch64.dmg \
  7cdef5de7d1bfcea70813320b897540cbead2190f5280db6d9760a59e5995463 Handy.app
grant com.pais.handy
# Handy finds catalogue models in the standard Hugging Face cache (managers/model.rs at v0.10.1), pinned to
# the catalogue's revision; a model's id is "<repository>/<file>".
hf() {  # hf <repo> <file> <revision> <sha256>: the model path
  local path
  path=$(uvx --quiet --from huggingface_hub==0.26.2 huggingface-cli download "$1" "$2" --revision "$3" | tail -1)
  echo "$4  $path" | shasum -a 256 -c - >/dev/null
  echo "$path"
}
case $PROFILE in
  best-asr) repo=handy-computer/Qwen3-ASR-1.7B-gguf file=Qwen3-ASR-1.7B-Q8_0.gguf
    hf "$repo" "$file" 92282af1610a2db19d66f2bef1e260f5deca782d \
      9a0d81792dfea2d5f278b8a63deb3ea6e02139ce42c2301f32ea19c4f77526b7 >/dev/null ;;
  *) repo=handy-computer/parakeet-unified-en-0.6b-gguf file=parakeet-unified-en-0.6b-Q8_0.gguf
    hf "$repo" "$file" 7e948f21b7bdbac698d3318db9d350f1096f3b6c \
      4b50b6dd862bf6e346929aaf4f5eaacec003bfa3f56462d6c874b41ef2f38795 >/dev/null ;;
esac
extra=""
if [ "$PROFILE" = local-cleanup ]; then
  ollama_model gemma3:4b
  extra=', "post_process_enabled": true, "post_process_provider_id": "custom", "post_process_models": {"custom": "gemma3:4b"}'
fi
# Choosing a model completes onboarding (settings.rs: onboarding_completed = !selected_model.is_empty()).
dir="$HOME/Library/Application Support/com.pais.handy"
mkdir -p "$dir"
osascript -e 'quit app "Handy"' >/dev/null 2>&1 || true
printf '{"settings": {"selected_model": "%s/%s", "selected_language": "en"%s}}\n' "$repo" "$file" "$extra" >"$dir/settings_store.json"
start Handy.app handy
