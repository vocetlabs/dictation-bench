#!/usr/bin/env bash
# muesli: installs the signed release. Unattended setup isn't done yet: open the app and finish its onboarding
# by hand (the model and settings in apps/muesli.json), allowing Microphone and Accessibility.
. "$(dirname "$0")/common.sh"
install_dmg https://github.com/Muesli-HQ/muesli/releases/download/v0.8.6/Muesli-0.8.6.dmg \
  673b182fd75953fb48939af61db5f853c946e9c96ae65b6d75f100b0c1df7cf0 Muesli.app
log "now open Muesli.app and finish its onboarding (apps/muesli.json)"
