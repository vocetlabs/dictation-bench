#!/usr/bin/env bash
# Muesli 0.8.6, as its onboarding leaves it: onboarding done, its default model (Parakeet Unified EN 0.6B,
# Core ML through FluidAudio: the int8 encoder, decoder, joint and vocabulary its download plan requires)
# in place, every other setting at its default (Right Option, hold).
. "$(dirname "$0")/common.sh"
install_dmg https://github.com/Muesli-HQ/muesli/releases/download/v0.8.6/Muesli-0.8.6.dmg \
  673b182fd75953fb48939af61db5f853c946e9c96ae65b6d75f100b0c1df7cf0 Muesli.app
grant com.muesli.app
models="$HOME/Library/Application Support/FluidAudio/Models/parakeet-unified-en-0.6b-coreml"
uvx --quiet --from huggingface_hub==0.26.2 huggingface-cli download FluidInference/parakeet-unified-en-0.6b-coreml \
  --revision d32e972dd4315f1dc3f6be28fb2aab0ab3e80358 --local-dir "$models" \
  --include "parakeet_unified_encoder_int8.mlmodelc/*" "parakeet_unified_decoder.mlmodelc/*" \
  "parakeet_unified_joint_decision_single_step.mlmodelc/*" "vocab.json" "metadata.json" "config.json" >/dev/null
# Muesli's settings: Application Support/Muesli/config.json; keys it lacks take their defaults.
mkdir -p "$HOME/Library/Application Support/Muesli"
printf '{"has_completed_onboarding": true}\n' >"$HOME/Library/Application Support/Muesli/config.json"
start Muesli.app Muesli
