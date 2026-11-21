#!/usr/bin/env bash
# voiceink: installs the signed release. Unattended setup isn't done yet: open the app and finish its onboarding
# by hand (the model and settings in apps/voiceink.json), allowing Microphone and Accessibility.
. "$(dirname "$0")/common.sh"
install_dmg https://github.com/Beingpax/VoiceInk/releases/download/v2.27/VoiceInk.dmg \
  a832f6c3a1655f2e51df112efa45e24fa158601d5abe8689fe48fc6904c771c6 VoiceInk.app
log "now open VoiceInk.app and finish its onboarding (apps/voiceink.json)"
