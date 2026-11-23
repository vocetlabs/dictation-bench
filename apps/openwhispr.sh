#!/usr/bin/env bash
# OpenWhispr 1.12.0, as its onboarding leaves it for an English speaker who chooses "on this device":
# onboarding done, local transcription with the model its setup table picks for English (Parakeet Unified
# EN 0.6B, sherpa-onnx int8) downloaded, activation at its default (tap to toggle). Its default key, Globe/Fn,
# is read below the event system and can't be pressed by software, so the key is Right Option, which its
# hotkey manager accepts as a modifier-only key.
# OpenWhispr keeps settings in its window's localStorage, so they are written there through the Chrome
# DevTools Protocol of a one-off launch with --remote-debugging-port, then the app is started normally.
. "$(dirname "$0")/common.sh"
install_dmg https://github.com/OpenWhispr/openwhispr/releases/download/v1.12.0/OpenWhispr-1.12.0-arm64.dmg \
  4a3ec44a08d6195066ce271d319cd8e43df2d37b6e3afb64b51c8f933abb3350 OpenWhispr.app
grant com.gizmolabs.openwhispr
# The model where OpenWhispr's Parakeet manager looks: ~/.cache/openwhispr/parakeet-models/<model>/
# (modelDirUtils.js, parakeetServer.js), from the registry's archive (its sha256 from the release asset).
models="$HOME/.cache/openwhispr/parakeet-models"
name=parakeet-unified-en-0.6b
tmp=$(mktemp -d)
fetch https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-parakeet-unified-en-0.6b-int8-non-streaming.tar.bz2 \
  99f63605b3a85a54c250c0869670a687b7d6598a47bf2421515e1f839a76e150 "$tmp/model.tar.bz2"
tar -xjf "$tmp/model.tar.bz2" -C "$tmp"
mkdir -p "$models" && rm -rf "${models:?}/$name"
mv "$tmp/sherpa-onnx-nemo-parakeet-unified-en-0.6b-int8-non-streaming" "$models/$name"
port=9229
open -g "$APPS/OpenWhispr.app" --args --remote-debugging-port=$port
sleep 15
uv run --quiet "$(dirname "$0")/cdp_eval.py" $port \
  'localStorage.setItem("onboardingCompleted","true"); localStorage.setItem("useLocalWhisper","true"); localStorage.setItem("localTranscriptionProvider","nvidia"); localStorage.setItem("parakeetModel","parakeet-unified-en-0.6b"); localStorage.setItem("dictationKey","RightOption"); "ok"'
pkill -TERM -x OpenWhispr || true
sleep 5
# Debug-level logging, so a failed smoke test shows why (its audio is only the synthetic sentence).
open -g --env OPENWHISPR_LOG_LEVEL=debug "$APPS/OpenWhispr.app"
for _ in $(seq 1 60); do pgrep -x OpenWhispr >/dev/null && break; sleep 1; done
sleep 15
