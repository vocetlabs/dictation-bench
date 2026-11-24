#!/usr/bin/env bash
# Muesli 0.8.6, as its onboarding leaves it: onboarding done, its default model (Parakeet Unified EN 0.6B,
# Core ML through FluidAudio: the int8 encoder, decoder, joint and vocabulary its download plan requires)
# in place, every other setting at its default (Right Option, hold). best-asr is the same configuration
# (Muesli recommends Parakeet Unified for English). local-cleanup turns on its own on-device cleanup
# (muesli Cleanup Lite), which the app downloads itself. See CONFIGS.md.
. "$(dirname "$0")/common.sh"
install_dmg https://github.com/Muesli-HQ/muesli/releases/download/v0.8.6/Muesli-0.8.6.dmg \
  673b182fd75953fb48939af61db5f853c946e9c96ae65b6d75f100b0c1df7cf0 Muesli.app
grant com.muesli.app
models="$HOME/Library/Application Support/FluidAudio/Models/parakeet-unified-en-0.6b-coreml"
hf_folder FluidInference/parakeet-unified-en-0.6b-coreml d32e972dd4315f1dc3f6be28fb2aab0ab3e80358 "$models" \
  "parakeet_unified_encoder_int8.mlmodelc/*" "parakeet_unified_decoder.mlmodelc/*" \
  "parakeet_unified_joint_decision_single_step.mlmodelc/*" "vocab.json" "metadata.json" "config.json"
# Muesli's settings: Application Support/Muesli/config.json; keys it lacks take their defaults.
mkdir -p "$HOME/Library/Application Support/Muesli"
if [ "$PROFILE" = local-cleanup ]; then
  printf '{"has_completed_onboarding": true, "enable_post_processor": true, "post_processor_backend": "local", "active_post_processor_id": "qwen35-postproc-v4"}\n' \
    >"$HOME/Library/Application Support/Muesli/config.json"
else
  printf '{"has_completed_onboarding": true}\n' >"$HOME/Library/Application Support/Muesli/config.json"
fi
start Muesli.app Muesli
if [ "$PROFILE" = local-cleanup ]; then  # Cleanup Lite v14 (docs/models/cleanup-models.json at v0.8.6)
  restart_after_download Muesli.app Muesli "$HOME/.cache/muesli/models/postproc-qwen35-postproc-v4/model.safetensors" \
    799973748 "local-cleanup: Cleanup Lite (Models > Cleanup > Cleanup Lite > Download, ~790 MB)"
fi
