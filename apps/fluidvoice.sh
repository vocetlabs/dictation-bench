#!/usr/bin/env bash
# FluidVoice 1.6.13. shipped: as its onboarding leaves it on Apple silicon (its default model "FluidVoice
# Pick": Parakeet TDT v3, Core ML, through FluidAudio; Right Option; toggle). best-asr: Whisper Large
# (its highest-rated model, 'Best possible accuracy'), which the app downloads itself. local-cleanup: the shipped model plus AI enhancement
# through its Ollama provider (pinned model), which has to be verified once in the app. See CONFIGS.md.
. "$(dirname "$0")/common.sh"
install_dmg https://github.com/altic-dev/FluidVoice/releases/download/v1.6.13/Fluid-oss-1.6.13.dmg \
  8da47aba78de572973c287ab5e33d9eefc4b8938f69bbff99249e28900ce49aa FluidVoice.app
grant com.FluidApp.app
# FluidAudio's model folder (ParakeetSpeechModelCatalog v3: folder parakeet-tdt-0.6b-v3-coreml; an existing
# complete cache is used as is). The compiled models and vocabulary, pinned to a revision.
models="$HOME/Library/Application Support/FluidAudio/Models/parakeet-tdt-0.6b-v3-coreml"
hf_folder FluidInference/parakeet-tdt-0.6b-v3-coreml 7dd20fe6b1797d35f5e3307e8b1732d9a178edfe "$models" \
  "*.mlmodelc/*" "*.json"
d=com.FluidApp.app
defaults write $d OnboardingCompleted -bool true
case $PROFILE in
  best-asr)
    defaults write $d SelectedSpeechModel -string whisper-large
    defaults write $d EnableAIProcessing -bool false
    ;;
  local-cleanup)
    ollama_model gemma3:4b
    defaults write $d SelectedSpeechModel -string parakeet-tdt
    defaults write $d EnableAIProcessing -bool true
    defaults write $d SelectedProviderID -string ollama
    defaults write $d SelectedAIModel -string gemma3:4b
    log "local-cleanup: verify the Ollama provider once in FluidVoice's AI settings (it stores a fingerprint)" ;;
  *)
    defaults write $d SelectedSpeechModel -string parakeet-tdt
    defaults write $d EnableAIProcessing -bool false ;;
esac
start FluidVoice.app FluidVoice
if [ "$PROFILE" = best-asr ]; then  # needs 8 GB free memory to load (verify on a 16 GB host)
  restart_after_download FluidVoice.app FluidVoice "$HOME/Library/Caches/WhisperModels/whisper-large-v3-Q8_0.gguf" \
    1668741440 "best-asr: Whisper Large (Settings > Voice Engine > Whisper Large)"
fi
