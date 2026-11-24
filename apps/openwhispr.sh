#!/usr/bin/env bash
# OpenWhispr 1.12.0, the maintainer's signed release. The profile comes from BENCH_PROFILE (common.sh):
# shipped or best-asr (local-cleanup is the same configuration as shipped). See CONFIGS.md.
#
# shipped: what onboarding leaves for an English speaker who continues as a guest and picks "on this
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
# best-asr: the same speech model (the maintainer's pick for English), cleanup off.
#
# OpenWhispr keeps settings in its window's localStorage, so they are written there through the Chrome
# DevTools Protocol of a one-off launch of the bench's copy with --remote-debugging-port; then the bench's
# copy is started normally. Its main process reads the effective ones from userData/.env (checked before a
# run: settings_source).
. "$(dirname "$0")/common.sh"
app="$APPS/OpenWhispr.app"
# Only this account's processes launched from the bench's copy (by its bundle path), never another copy.
quit_bench_copy() { pkill -TERM -U "$(id -u)" -f "^$app/Contents/MacOS/" 2>/dev/null && sleep 5 || true; }
quit_bench_copy
install_dmg https://github.com/OpenWhispr/openwhispr/releases/download/v1.12.0/OpenWhispr-1.12.0-arm64.dmg \
  4a3ec44a08d6195066ce271d319cd8e43df2d37b6e3afb64b51c8f933abb3350 OpenWhispr.app
grant com.gizmolabs.openwhispr
cache="$HOME/.cache/openwhispr"
# The speech model where its Parakeet manager looks: <cache>/parakeet-models/<model>/ (modelDirUtils.js,
# parakeetServer.js), from the registry's archive (sha256 of the release asset), unpacked once into the
# bench's cache and cloned into place.
name=parakeet-unified-en-0.6b sum=99f63605b3a85a54c250c0869670a687b7d6598a47bf2421515e1f839a76e150
unpacked="${BENCH:-.}/work/cache/openwhispr-$name-$sum"
if [ ! -f "$unpacked/.complete" ]; then
  rm -rf "$unpacked" && mkdir -p "$unpacked"
  fetch https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-parakeet-unified-en-0.6b-int8-non-streaming.tar.bz2 \
    "$sum" "$unpacked.tar.bz2"
  tar -xjf "$unpacked.tar.bz2" -C "$unpacked" --strip-components 1
  rm -f "$unpacked.tar.bz2"
  touch "$unpacked/.complete"
fi
mkdir -p "$cache/parakeet-models" && rm -rf "${cache:?}/parakeet-models/$name"
cp -cR "$unpacked" "$cache/parakeet-models/$name" 2>/dev/null || cp -R "$unpacked" "$cache/parakeet-models/$name"
rm -f "$cache/parakeet-models/$name/.complete"
cleanup='localStorage.setItem("useCleanupModel","false"); localStorage.setItem("cleanupMode","local"); localStorage.setItem("cleanupProvider",""); localStorage.setItem("cleanupModel","");'
if [ "$PROFILE" != best-asr ]; then
  [ "$(sysctl -n hw.memsize)" -ge $((16 * 1024 * 1024 * 1024)) ] ||
    log "warning: under 16 GB onboarding would leave cleanup off; shipped here is the 16 GB+ default"
  # The cleanup model where its model manager looks: <cache>/models/<fileName> (modelManagerBridge.js), from
  # the registry's Hugging Face repository (which names no revision: pinned here to main as of 2026-11-23).
  fetch https://huggingface.co/bartowski/Qwen_Qwen3.5-2B-GGUF/resolve/7d26695454df6de5fbcce2e58681e62dae06ce43/Qwen_Qwen3.5-2B-Q4_K_M.gguf \
    57a1085840f497d764a7fc5d346922dbde961efb54cc792ea81d694fd846a1d8 "$cache/models/Qwen_Qwen3.5-2B-Q4_K_M.gguf"
  cleanup='localStorage.setItem("useCleanupModel","true"); localStorage.setItem("cleanupMode","local"); localStorage.setItem("cleanupProvider","qwen"); localStorage.setItem("cleanupModel","qwen3.5-2b-q4_k_m");'
fi
port=9229
open -g "$app" --args --remote-debugging-port=$port
sleep 15
# The keys onboarding's finish writes for a guest (OnboardingFlow finalizeOnboarding: the guest markers, the
# session cleared, onboardingCompleted; AppRouter shows onboarding again without them), and those
# applyLocalSetupSettings and the onboarding steps write (the cleanup scope's storeKeys from
# config/inferenceScopes.ts; the assistant scope written off as onboarding does).
uv run --quiet "$(dirname "$0")/cdp_eval.py" $port \
  "localStorage.removeItem('onboardingSessionV2'); localStorage.removeItem('onboardingCurrentStep'); localStorage.setItem('authenticationSkipped','true'); localStorage.setItem('skipAuth','true'); localStorage.setItem('onboardingCompleted','true'); localStorage.setItem('useLocalWhisper','true'); localStorage.setItem('localTranscriptionProvider','nvidia'); localStorage.setItem('parakeetModel','$name'); localStorage.setItem('spokenLanguages','[\"en\"]'); localStorage.setItem('preferredLanguage','en'); localStorage.setItem('dictationKey','RightOption'); localStorage.setItem('activationMode','push'); $cleanup localStorage.setItem('useDictationAgent','false'); localStorage.setItem('dictationAgentMode','local'); localStorage.setItem('dictationAgentProvider',''); localStorage.setItem('dictationAgentModel',''); 'ok'"
quit_bench_copy
ud="$HOME/Library/Application Support/open-whispr"
# Onboarding's finish also marks the bundle migrated (postMigrationDetector.js); without the marker a fresh
# profile shows a "Welcome back... permissions" dialog.
date -u +%FT%TZ >"$ud/.bundle-migrated"
# Onboarding's finish saves the hotkey and mode to the environment file (saveAllKeysToEnv; PERSISTED_KEYS in
# environment.js), which startup reads before localStorage. Without it startup registers the key before its
# key listener is up, fails, and falls back to Globe/Fn.
touch "$ud/.env"
sed -i '' '/^DICTATION_KEY=/d; /^ACTIVATION_MODE=/d' "$ud/.env"
printf 'DICTATION_KEY=RightOption\nACTIVATION_MODE=push\n' >>"$ud/.env"
# Debug-level logging, so a failed smoke test shows why (its audio is only the synthetic sentence).
open -g --env OPENWHISPR_LOG_LEVEL=debug "$app"
for _ in $(seq 1 60); do pgrep -U "$(id -u)" -f "^$app/Contents/MacOS/" >/dev/null && break; sleep 1; done
sleep 15
