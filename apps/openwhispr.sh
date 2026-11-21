#!/usr/bin/env bash
# openwhispr: installs the signed release. Unattended setup isn't done yet: open the app and finish its onboarding
# by hand (the model and settings in apps/openwhispr.json), allowing Microphone and Accessibility.
. "$(dirname "$0")/common.sh"
install_dmg https://github.com/OpenWhispr/openwhispr/releases/download/v1.12.0/OpenWhispr-1.12.0-arm64.dmg \
  4a3ec44a08d6195066ce271d319cd8e43df2d37b6e3afb64b51c8f933abb3350 OpenWhispr.app
log "now open OpenWhispr.app and finish its onboarding (apps/openwhispr.json)"
