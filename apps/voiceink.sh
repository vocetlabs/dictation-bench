#!/usr/bin/env bash
# VoiceInk 2.27, as its onboarding leaves it: onboarding done, its required local model (Parakeet TDT
# 0.6B v3, Core ML, through FluidAudio) downloaded and chosen, the primary recording shortcut Right Option
# (onboarding asks for one; recording mode left at its default).
. "$(dirname "$0")/common.sh"
install_dmg https://github.com/Beingpax/VoiceInk/releases/download/v2.27/VoiceInk.dmg \
  a832f6c3a1655f2e51df112efa45e24fa158601d5abe8689fe48fc6904c771c6 VoiceInk.app
grant com.prakashjoshipax.VoiceInk
# FluidAudio keeps models in Application Support/FluidAudio/Models/<repo folder> (MLModelConfigurationUtils);
# the compiled models and vocabulary, pinned to a revision of the repository.
models="$HOME/Library/Application Support/FluidAudio/Models/parakeet-tdt-0.6b-v3-coreml"
uvx --quiet --from huggingface_hub==0.26.2 huggingface-cli download FluidInference/parakeet-tdt-0.6b-v3-coreml \
  --revision 7dd20fe6b1797d35f5e3307e8b1732d9a178edfe --include "*.mlmodelc/*" "*.json" --local-dir "$models" >/dev/null
d=com.prakashjoshipax.VoiceInk
defaults write $d hasCompletedOnboardingV2 -bool true
defaults write $d CurrentTranscriptionModel -string parakeet-tdt-0.6b-v3
# The shortcut as ShortcutStore saves it: JSON of Shortcut (modifier-only, key 61 = Right Option, Option flag).
defaults write $d Shortcut_primaryRecording -data \
  "$(printf '{"kind":"modifierOnly","keyCode":61,"modifierFlagsRawValue":524288}' | xxd -p | tr -d '\n')"
start VoiceInk.app VoiceInk
