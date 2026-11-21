#!/usr/bin/env bash
# fluidvoice: installs the signed release. Unattended setup isn't done yet: open the app and finish its onboarding
# by hand (the model and settings in apps/fluidvoice.json), allowing Microphone and Accessibility.
. "$(dirname "$0")/common.sh"
install_dmg https://github.com/altic-dev/FluidVoice/releases/download/v1.6.13/Fluid-oss-1.6.13.dmg \
  8da47aba78de572973c287ab5e33d9eefc4b8938f69bbff99249e28900ce49aa FluidVoice.app
log "now open FluidVoice.app and finish its onboarding (apps/fluidvoice.json)"
