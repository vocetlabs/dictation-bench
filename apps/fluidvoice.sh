#!/usr/bin/env bash
# FluidVoice 1.6.13, as its onboarding leaves it on Apple silicon: onboarding done, its default model
# ("FluidVoice Pick": Parakeet TDT v3, Core ML, through FluidAudio) downloaded and chosen, the default key
# (Right Option) and activation (toggle).
. "$(dirname "$0")/common.sh"
install_dmg https://github.com/altic-dev/FluidVoice/releases/download/v1.6.13/Fluid-oss-1.6.13.dmg \
  8da47aba78de572973c287ab5e33d9eefc4b8938f69bbff99249e28900ce49aa FluidVoice.app
grant com.FluidApp.app
# FluidAudio's model folder (ParakeetSpeechModelCatalog v3: folder parakeet-tdt-0.6b-v3-coreml; an existing
# complete cache is used as is). The compiled models and vocabulary, pinned to a revision.
models="$HOME/Library/Application Support/FluidAudio/Models/parakeet-tdt-0.6b-v3-coreml"
uvx --quiet --from huggingface_hub==0.26.2 huggingface-cli download FluidInference/parakeet-tdt-0.6b-v3-coreml \
  --revision 7dd20fe6b1797d35f5e3307e8b1732d9a178edfe --include "*.mlmodelc/*" "*.json" --local-dir "$models" >/dev/null
d=com.FluidApp.app
defaults write $d OnboardingCompleted -bool true
defaults write $d SelectedSpeechModel -string parakeet-tdt
start FluidVoice.app FluidVoice
