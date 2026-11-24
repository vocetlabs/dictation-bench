#!/usr/bin/env bash
# VoiceInk 2.27, the maintainer's notarised release, within its built-in 7-day trial (owner decision,
# BENCH2: no `make local` build). Usage: apps/voiceink.sh <shipped|best-asr|local-cleanup>; the profile
# scripts apps/voiceink.<profile>.sh call it.
#
# Every profile starts from what onboarding leaves for a person who chooses local transcription
# (OnboardingCoordinator.transcriptionSetupKind .local at v2.27):
# - onboarding done, its required local model Parakeet V3 (parakeet-tdt-0.6b-v3, Core ML via FluidAudio)
#   downloaded and chosen;
# - the starter "Dictation" mode (StarterModeFactory): language "auto" (validLanguageOrFallback("auto")),
#   live transcription on (TranscriptionRealtimeSupport.isEnabled with no mode value), text formatting
#   on, AI enhancement off, output paste;
# - the primary recording shortcut. Onboarding has no default and asks for one: Right Option. The
#   recording mode stays at its default, hybrid (a hold past the threshold is push-to-talk).
# Profiles change only what their name says:
# - best-asr: the same model, language English (Parakeet V3 takes a language hint,
#   FluidAudioModelManager.languageHint). VoiceInk marks no local model as recommended; its onboarding
#   requires Parakeet V3, so that is the maintainer's recommendation.
# - local-cleanup: the shipped speech settings plus AI enhancement with VoiceInk Refine V1, its own
#   on-device MLX model (beingpax/VoiceInk-Refine-V1 at the revision 2.27 pins), no key. VoiceInk offers
#   Refine only on Apple silicon with at least 16 GB of memory (VoiceInkRefineService.minimumMemoryBytes).
#
# The trial starts at the first launch: LicenseManager stores the start date in the keychain when none is
# there. Once it ends, VoiceInk puts "Your trial has ended..." in front of every paste
# (TranscriptionDelivery.deliverableText), so runs must finish within 7 days of setup.
. "$(dirname "$0")/common.sh"
profile=${1:?usage: voiceink.sh <shipped|best-asr|local-cleanup>}
case $profile in shipped | best-asr | local-cleanup) ;; *) log "unknown profile $profile"; exit 2 ;; esac
d=com.prakashjoshipax.VoiceInk
pkill -TERM -x VoiceInk 2>/dev/null && sleep 3 || true
install_dmg https://github.com/Beingpax/VoiceInk/releases/download/v2.27/VoiceInk.dmg \
  a832f6c3a1655f2e51df112efa45e24fa158601d5abe8689fe48fc6904c771c6 VoiceInk.app
grant $d
# hf <repo> <args>: a pinned download, quiet unless it fails.
hf() {
  local err
  err=$(mktemp)
  HF_HUB_DISABLE_PROGRESS_BARS=1 uvx --quiet --from huggingface_hub==0.26.2 huggingface-cli download "$@" >/dev/null 2>"$err" ||
    { cat "$err" >&2; rm -f "$err"; return 1; }
  rm -f "$err"
}
# What VoiceInk's own download writes (its "Download" button, watched in a VM): FluidAudio at the commit
# 2.27 pins (ee78635d) keeps models in Application Support/FluidAudio/Models/<Repo.folderName>, the
# repository name without "-coreml", and fetches Repo.revision "main": pinned here to main as of
# 2026-11-23. Only the files it fetches (ModelNames.ASR.requiredModelsV3 with the int8 encoder, the
# vocabularies and config; 469 MB).
fa="$HOME/Library/Application Support/FluidAudio/Models"
hf FluidInference/parakeet-tdt-0.6b-v3-coreml --revision 7dd20fe6b1797d35f5e3307e8b1732d9a178edfe \
  --include "Preprocessor.mlmodelc/*" "Encoder.mlmodelc/*" "Decoder.mlmodelc/*" "JointDecisionv3.mlmodelc/*" \
  config.json parakeet_vocab.json parakeet_v3_vocab.json --local-dir "$fa/parakeet-tdt-0.6b-v3"
# VoiceInk counts a Parakeet model as downloaded only with FluidAudio's voice-activity model beside it.
hf FluidInference/silero-vad-coreml --revision b419383c55c110e2c9271fa6ee0ea83d03c70d96 \
  --include "silero-vad-unified-256ms-v6.2.1.mlmodelc/*" config.json --local-dir "$fa/silero-vad"
rm -rf "$fa/parakeet-tdt-0.6b-v3/.cache" "$fa/silero-vad/.cache"

language=auto enhance=false provider=null model=null
if [ "$profile" = best-asr ]; then language=en; fi
if [ "$profile" = local-cleanup ]; then
  [ "$(sysctl -n hw.memsize)" -ge $((16 * 1024 * 1024 * 1024)) ] ||
    log "warning: VoiceInk offers Refine only with 16 GB of memory or more; this Mac has less"
  # The files VoiceInkRefineModelDownloader fetches, straight into its folder (snapshotDirectory is the
  # model root at 2.27); with no integrity record VoiceInk checks the sizes. model.safetensors is
  # checked against the sha256 VoiceInk pins.
  refine="$HOME/Library/Application Support/$d/VoiceInkRefine"
  hf beingpax/VoiceInk-Refine-V1 --revision ad665418d3850e379e29236e66be3ddc0ac0bf04 --local-dir "$refine"
  rm -rf "$refine/.cache"
  echo "3cdfe2f506f71f2c5d5af23aa8fe3572989d2f364a7317322220621a86aaf5f6  $refine/model.safetensors" |
    shasum -a 256 -c - >/dev/null
  enhance=true provider='"VoiceInk Refine"' model='"VoiceInk Refine V1"'
fi

defaults write $d hasCompletedOnboardingV2 -bool true
defaults write $d CurrentTranscriptionModel -string parakeet-tdt-0.6b-v3
# The shortcut as ShortcutStore saves it: JSON of Shortcut (modifier-only, key 61 = Right Option, Option).
defaults write $d Shortcut_primaryRecording -data \
  "$(printf '{"kind":"modifierOnly","keyCode":61,"modifierFlagsRawValue":524288}' | xxd -p | tr -d '\n')"
# The starter "Dictation" mode as ModeManager stores it (ModeConfig's JSON), made the active mode. Without
# a mode VoiceInk refuses to record ("No mode configured"). With enhancement on and no prompt, VoiceInk
# assigns its default prompt at launch; Refine uses its own system prompt.
mode=10000000-0000-0000-0000-000000000001
json=$(printf '[{"id":"%s","name":"Dictation","isAIEnhancementEnabled":%s,"selectedTranscriptionModelName":"parakeet-tdt-0.6b-v3","isRealtimeTranscriptionEnabled":true,"selectedLanguage":"%s","isTextFormattingEnabled":true,"useClipboardContext":false,"useSelectedTextContext":false,"useScreenCapture":false,"selectedAIProvider":%s,"selectedAIModel":%s,"outputMode":"paste","isEnabled":true,"isDefault":true}]' \
  "$mode" "$enhance" "$language" "$provider" "$model")
defaults write $d modeConfigurationsV2 -data "$(printf '%s' "$json" | xxd -p | tr -d '\n')"
defaults write $d activeConfigurationId -string "$mode"
start VoiceInk.app VoiceInk
