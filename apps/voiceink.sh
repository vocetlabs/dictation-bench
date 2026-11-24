#!/usr/bin/env bash
# VoiceInk 2.27, the maintainer's notarised release, within its built-in 7-day trial (owner decision,
# BENCH2: no `make local` build). The profile comes from BENCH_PROFILE (common.sh). See CONFIGS.md.
#
# Every profile starts from what onboarding leaves for a person who chooses local transcription
# (OnboardingCoordinator.transcriptionSetupKind .local at v2.27): onboarding done; the starter "Dictation"
# mode (StarterModeFactory: live transcription on, text formatting on, AI enhancement off, output paste);
# the primary recording shortcut Right Option (onboarding asks for one and has no default), recording mode
# left at its default, hybrid.
# - shipped: onboarding's required local model, Parakeet V3, language "auto" (validLanguageOrFallback).
# - best-asr: Parakeet Ultra, language English. VoiceInk's model registry rates it highest of its Parakeet
#   models (accuracy 0.95, V3 0.94) and describes it as "Parakeet V3 post-trained by Moondream for more
#   accurate transcription"; of the models tied at 0.95 it is the first listed and the one described as more
#   accurate (the rule CONFIGS.md uses for FluidVoice).
# - local-cleanup: shipped's speech settings plus AI enhancement with VoiceInk Refine V1, VoiceInk's own
#   on-device MLX model (no key). VoiceInk offers Refine only on Apple silicon with 16 GB of memory or more.
#
# The trial: LicenseManager stores its start date in the login Keychain at the first launch, and after 7
# days every paste starts with "Your trial has ended..." (TranscriptionDelivery). The bench never writes the
# Keychain; the harness stops a run whose paste holds that notice (refusal_text). Check it in a VM; on a
# host, launch VoiceInk only in the account and on the night of its runs.
. "$(dirname "$0")/common.sh"
d=com.prakashjoshipax.VoiceInk
install_dmg https://github.com/Beingpax/VoiceInk/releases/download/v2.27/VoiceInk.dmg \
  a832f6c3a1655f2e51df112efa45e24fa158601d5abe8689fe48fc6904c771c6 VoiceInk.app
grant $d
# Models where VoiceInk's own Download button puts them (watched in a VM): FluidAudio at the commit 2.27
# pins (ee78635d) keeps them in Application Support/FluidAudio/Models/<Repo.folderName> (the repository
# name without "-coreml") and fetches Repo.revision "main", pinned here to main as of 2026-11-23; only the
# files it fetches (ModelNames.ASR.requiredModelsV3, the vocabularies, config).
fa="$HOME/Library/Application Support/FluidAudio/Models"
asr_files=("Preprocessor.mlmodelc/*" "Encoder.mlmodelc/*" "Decoder.mlmodelc/*" "JointDecisionv3.mlmodelc/*"
  config.json parakeet_vocab.json parakeet_v3_vocab.json)
case $PROFILE in
  best-asr) model=parakeet-ultra language=en
    hf_folder FluidInference/parakeet-ultra-coreml 95eaa59a39d4394f047a4dc5cce480388a60d1b6 "$fa/parakeet-ultra" \
      "${asr_files[@]}" ;;
  *) model=parakeet-tdt-0.6b-v3 language=auto
    hf_folder FluidInference/parakeet-tdt-0.6b-v3-coreml 7dd20fe6b1797d35f5e3307e8b1732d9a178edfe \
      "$fa/parakeet-tdt-0.6b-v3" "${asr_files[@]}" ;;
esac
# VoiceInk counts a Parakeet model as downloaded only with FluidAudio's voice-activity model beside it.
hf_folder FluidInference/silero-vad-coreml b419383c55c110e2c9271fa6ee0ea83d03c70d96 "$fa/silero-vad" \
  "silero-vad-unified-256ms-v6.2.1.mlmodelc/*" config.json
rm -rf "$fa"/*/.cache

enhance=false provider=null ai_model=null
if [ "$PROFILE" = local-cleanup ]; then
  [ "$(sysctl -n hw.memsize)" -ge $((16 * 1024 * 1024 * 1024)) ] ||
    log "warning: VoiceInk offers Refine only with 16 GB of memory or more; this Mac has less"
  # VoiceInkRefineModelDownloader's files, straight into its folder (snapshotDirectory is the model root at
  # 2.27); with no integrity record VoiceInk checks their sizes. model.safetensors: the sha256 2.27 pins.
  refine="$HOME/Library/Application Support/$d/VoiceInkRefine"
  hf_folder beingpax/VoiceInk-Refine-V1 ad665418d3850e379e29236e66be3ddc0ac0bf04 "$refine" "*"
  rm -rf "$refine/.cache"
  echo "3cdfe2f506f71f2c5d5af23aa8fe3572989d2f364a7317322220621a86aaf5f6  $refine/model.safetensors" |
    shasum -a 256 -c - >/dev/null
  enhance=true provider='"VoiceInk Refine"' ai_model='"VoiceInk Refine V1"'
fi

defaults write $d hasCompletedOnboardingV2 -bool true
defaults write $d CurrentTranscriptionModel -string "$model"
# The shortcut as ShortcutStore saves it: JSON of Shortcut (modifier-only, key 61 = Right Option, Option).
defaults write $d Shortcut_primaryRecording -data \
  "$(printf '{"kind":"modifierOnly","keyCode":61,"modifierFlagsRawValue":524288}' | xxd -p | tr -d '\n')"
# The starter "Dictation" mode as ModeManager stores it (ModeConfig's JSON), made the active mode. Without
# a mode VoiceInk refuses to record ("No mode configured"). With enhancement on and no prompt, VoiceInk
# assigns its default prompt at launch; Refine uses its own system prompt.
mode=10000000-0000-0000-0000-000000000001
json=$(printf '[{"id":"%s","name":"Dictation","isAIEnhancementEnabled":%s,"selectedTranscriptionModelName":"%s","isRealtimeTranscriptionEnabled":true,"selectedLanguage":"%s","isTextFormattingEnabled":true,"useClipboardContext":false,"useSelectedTextContext":false,"useScreenCapture":false,"selectedAIProvider":%s,"selectedAIModel":%s,"outputMode":"paste","isEnabled":true,"isDefault":true}]' \
  "$mode" "$enhance" "$model" "$language" "$provider" "$ai_model")
defaults write $d modeConfigurationsV2 -data "$(printf '%s' "$json" | xxd -p | tr -d '\n')"
defaults write $d activeConfigurationId -string "$mode"
start VoiceInk.app VoiceInk
