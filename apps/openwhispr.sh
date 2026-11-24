#!/usr/bin/env bash
# OpenWhispr 1.12.0, the maintainer's signed release. Usage: apps/openwhispr.sh <shipped|best-asr>; the
# profile scripts apps/openwhispr.<profile>.sh call it.
#
# shipped: what onboarding leaves for an English speaker who signs in as a guest and picks "on this
# device" (the local setup step, src/config/localSetup.ts + components/onboarding/localSetupSettings.ts):
# - dictation: the setup table's English pick, Parakeet Unified EN 0.6B (sherpa-onnx int8, NVIDIA engine);
# - language English (the languages step: one spoken language becomes preferredLanguage);
# - cleanup: the table's cleanup model, Qwen3.5 2B Q4_K_M on llama.cpp, switched ON on every Mac with
#   16 GB of memory or more (startingSwitches.cleanup is on unless the machine row says startsOn false;
#   under 16 GB the row is unavailable and onboarding writes cleanup off). The bench Macs have 16 GB+.
# - the assistant row is "unavailable (too slow)" on the M4 Pro, so onboarding writes it off;
# - activation hold-to-talk: onboarding sets "push" for new users where the key reports a release;
# - key Right Option. Its macOS default, Globe/Fn, is read below the event system and can't be pressed by
#   software; its hotkey manager accepts Right Option as a modifier-only key with push-to-talk.
# best-asr: the same speech model (the maintainer's pick for English), cleanup off. local-cleanup would be
# the same settings as shipped (speech fixed, its own cleanup on), so there is no separate profile.
#
# OpenWhispr keeps settings in its window's localStorage, so they are written there through the Chrome
# DevTools Protocol of a one-off launch with --remote-debugging-port, then the app is started normally and
# copies them into its own environment file.
. "$(dirname "$0")/common.sh"
profile=${1:?usage: openwhispr.sh <shipped|best-asr>}
case $profile in shipped | best-asr) ;; *) log "unknown profile $profile"; exit 2 ;; esac
pkill -TERM -x OpenWhispr 2>/dev/null && sleep 3 || true
install_dmg https://github.com/OpenWhispr/openwhispr/releases/download/v1.12.0/OpenWhispr-1.12.0-arm64.dmg \
  4a3ec44a08d6195066ce271d319cd8e43df2d37b6e3afb64b51c8f933abb3350 OpenWhispr.app
grant com.gizmolabs.openwhispr
cache="$HOME/.cache/openwhispr"
# The speech model where its Parakeet manager looks: <cache>/parakeet-models/<model>/ (modelDirUtils.js,
# parakeetServer.js), from the registry's archive (sha256 of the release asset).
name=parakeet-unified-en-0.6b
if [ ! -f "$cache/parakeet-models/$name/.bench-ok" ]; then
  tmp=$(mktemp -d)
  fetch https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-parakeet-unified-en-0.6b-int8-non-streaming.tar.bz2 \
    99f63605b3a85a54c250c0869670a687b7d6598a47bf2421515e1f839a76e150 "$tmp/model.tar.bz2"
  tar -xjf "$tmp/model.tar.bz2" -C "$tmp"
  mkdir -p "$cache/parakeet-models" && rm -rf "${cache:?}/parakeet-models/$name"
  mv "$tmp/sherpa-onnx-nemo-parakeet-unified-en-0.6b-int8-non-streaming" "$cache/parakeet-models/$name"
  touch "$cache/parakeet-models/$name/.bench-ok"
  rm -rf "$tmp"
fi
cleanup='localStorage.setItem("useCleanupModel","false"); localStorage.setItem("cleanupMode","local"); localStorage.setItem("cleanupProvider",""); localStorage.setItem("cleanupModel","");'
if [ "$profile" = shipped ]; then
  [ "$(sysctl -n hw.memsize)" -ge $((16 * 1024 * 1024 * 1024)) ] ||
    log "warning: under 16 GB onboarding would leave cleanup off; shipped here is the 16 GB+ default"
  # The cleanup model where its model manager looks: <cache>/models/<fileName> (modelManagerBridge.js),
  # from the registry's Hugging Face repository (which names no revision: pinned here to main as of
  # 2026-11-23, checked by sha256).
  gguf="$cache/models/Qwen_Qwen3.5-2B-Q4_K_M.gguf" sum=57a1085840f497d764a7fc5d346922dbde961efb54cc792ea81d694fd846a1d8
  echo "$sum  $gguf" | shasum -a 256 -c - >/dev/null 2>&1 ||
    fetch https://huggingface.co/bartowski/Qwen_Qwen3.5-2B-GGUF/resolve/7d26695454df6de5fbcce2e58681e62dae06ce43/Qwen_Qwen3.5-2B-Q4_K_M.gguf \
      "$sum" "$gguf"
  cleanup='localStorage.setItem("useCleanupModel","true"); localStorage.setItem("cleanupMode","local"); localStorage.setItem("cleanupProvider","qwen"); localStorage.setItem("cleanupModel","qwen3.5-2b-q4_k_m");'
fi
port=9229
open -g "$APPS/OpenWhispr.app" --args --remote-debugging-port=$port
sleep 15
# The keys onboarding's finish writes for a guest (OnboardingFlow finalizeOnboarding: the guest markers,
# the session cleared, onboardingCompleted; AppRouter shows onboarding again without them), and those
# applyLocalSetupSettings and the onboarding steps write (the cleanup scope's storeKeys from
# config/inferenceScopes.ts; the assistant scope written off as onboarding does).
uv run --quiet "$(dirname "$0")/cdp_eval.py" $port \
  "localStorage.removeItem('onboardingSessionV2'); localStorage.removeItem('onboardingCurrentStep'); localStorage.setItem('authenticationSkipped','true'); localStorage.setItem('skipAuth','true'); localStorage.setItem('onboardingCompleted','true'); localStorage.setItem('useLocalWhisper','true'); localStorage.setItem('localTranscriptionProvider','nvidia'); localStorage.setItem('parakeetModel','$name'); localStorage.setItem('spokenLanguages','[\"en\"]'); localStorage.setItem('preferredLanguage','en'); localStorage.setItem('dictationKey','RightOption'); localStorage.setItem('activationMode','push'); $cleanup localStorage.setItem('useDictationAgent','false'); localStorage.setItem('dictationAgentMode','local'); localStorage.setItem('dictationAgentProvider',''); localStorage.setItem('dictationAgentModel',''); 'ok'"
pkill -TERM -x OpenWhispr || true
sleep 5
# Onboarding's finish also marks the bundle migrated (postMigrationDetector.js); without the marker a
# fresh profile shows a "Welcome back... permissions" dialog.
ud="$HOME/Library/Application Support/open-whispr"
date -u +%FT%TZ >"$ud/.bundle-migrated"
# Onboarding's finish saves the hotkey and mode to the environment file (saveAllKeysToEnv; PERSISTED_KEYS in
# environment.js), which startup reads before localStorage. Without it startup registers the key before
# its key listener is up, fails, and falls back to Globe/Fn.
touch "$ud/.env"
sed -i '' '/^DICTATION_KEY=/d; /^ACTIVATION_MODE=/d' "$ud/.env"
printf 'DICTATION_KEY=RightOption\nACTIVATION_MODE=push\n' >>"$ud/.env"
# Debug-level logging, so a failed smoke test shows why (its audio is only the synthetic sentence).
open -g --env OPENWHISPR_LOG_LEVEL=debug "$APPS/OpenWhispr.app"
for _ in $(seq 1 60); do pgrep -x OpenWhispr >/dev/null && break; sleep 1; done
sleep 15
