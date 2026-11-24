# Configurations, protocol and changes

Disclosure: this bench is built and run by the developer of Vocet, one of the benchmarked apps. This file
records every app's settings and the reason for each, so anyone can check them and maintainers can correct
them.

**Status: proposed (BENCH2, 2026-11-23).** It freezes when it is merged, before the first scored host run.
After that, any change gets a dated entry below and the affected configurations are run again.

## Protocol (the same for every app and every run)

- Voice Arena's timing: 300 ms of silence before and after the clip, 0.5 s after the app opens the
  microphone, a 4 s tail before the release, text settled when unchanged for 2 s, **30 s** deadline. CI runs
  use the same timing and report no latency (they are reproducibility checks, not headline numbers).
- **The deadline starts at stop-recording** (the key release, or the second tap of a toggle app).
- **Failures.** A clip with no text by the deadline (`empty`, `late`) is scored as an empty hypothesis for
  WER (every reference word an error) and as a failure for ZER (not sendable). `partial` (still changing at
  the deadline) is scored as it stood. The failure rate (share of clips not `ok`) is shown in every table.
- **Late pastes.** After any outcome but `ok`, the harness watches for late text for 5 s before the next clip,
  and restarts a toggle or hybrid app, so a late paste can't land in the next clip.
- **Conditional columns.** AMEND is pooled over clips with output only (a clip with no output still counts
  as not sendable in ZER). Latency (key release to the final text, p50 and p95) is over `ok` clips only.
  Read both with the failure rate. ZER and AMEND are estimates from one pinned judge (Claude Opus 5.5,
  amend-va 0.2.1 prompt `v1`); a second judge from another family checks them on a sample.
- **No credit after the deadline.** The text is frozen at stop + 30 s: a read counts only if it starts
  before the deadline and completes by it. After any outcome but `ok`, the app is restarted (any mode),
  so a pending paste can't reach the next clip.
- **One app at a time.** Before each job every benchmarked app is quit. Readiness binds to the processes
  launched from the bench's own copy of the app (and their children): the microphone counts as open only
  when one of them captures audio and no other process does. There is no device-wide fallback: if CoreAudio
  can't say who is capturing (before macOS 14), the attempt is an infrastructure fault.
- **Scored under its label only.** Before a run, the app's settings are read back from where the app keeps
  them and must match the profile; its model files must be present (their sha256 is recorded) and the files
  it must not have must be absent (e.g. no Gemma for Vocet `best-asr`); an Ollama cleanup model must have
  the pinned manifest digest. A profile with an open `verify` item runs only as a trial
  (`--unverified-ok`), recorded in `run.json`; `report` refuses trials.
- **Comparable runs only.** `report` pools and ranks runs only with the same protocol, harness version,
  manifest, dataset, clip selection, machine label and machine (chip, memory, macOS build) and the same
  judge; the passes of one configuration must also share the app binary, release, model files, settings
  and settings files (their sha256).
- **Separate columns:** spoken-text WER, intended-text WER, ZER, AMEND, failure rate, p50 and p95 latency,
  peak resident memory (the app and its child processes, sampled after each clip) and model disk size.
- **Infrastructure faults** (focus lost, a failed read, playback, no microphone, text that stopped changing
  before the clip's speech ended: a negative e2t means another paste) are retried up to 3 times;
  app outcomes never are.

## Profiles

- `shipped` (the headline): the onboarding default. Shown in two groups: **raw speech** apps and **speech +
  local cleanup** apps (an app whose default includes on-device AI cleanup).
- `best-asr` ("maintainer-recommended local ASR", the speech track, led by spoken-text WER): the local speech
  model the app's maintainer recommends for English accuracy, language English, AI cleanup off.
- `local-cleanup`: the shipped speech model held fixed; only cleanup changes, to the app's own on-device
  cleanup. Where an app supports Ollama and has no cleanup of its own, Ollama with one pinned local model
  (`gemma3:4b`) is used and labelled. Apple Intelligence requires macOS 26 (the host has macOS 15), so it is
  not used.
- Local models only: cloud features stay out of these tables.

A profile can be the **same configuration** as another (then it is run once, under that name) or **n/a**.

| App (release)               | Profile       | Speech model                                   | Cleanup                                                  | Settings applied                                                                                                                                            | Reason                                                                                                                                                                                                         |
| --------------------------- | ------------- | ---------------------------------------------- | -------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Handy 0.10.1                | shipped       | Parakeet Unified EN 0.6B, Q8_0 GGUF            | off (rule-based filler removal on, its default)          | `selected_model` parakeet-unified-en-0.6b-Q8_0, `selected_language` en; Option+Space, hold-or-toggle                                                        | Onboarding's first recommended English model ("Best all-around choice for English dictation")                                                                                                                  |
| Handy 0.10.1                | best-asr      | Qwen3-ASR 1.7B, Q8_0 GGUF                      | off                                                      | `selected_model` Qwen3-ASR-1.7B-Q8_0, `selected_language` en                                                                                                | The catalogue's "Top accuracy across languages. Best on Apple Silicon"; lowest English error of the curated models (FLEURS en 3.23%) within its 10 s wait for the "More accurate" pick                         |
| Handy 0.10.1                | local-cleanup | Parakeet Unified EN 0.6B (held)                | Ollama `gemma3:4b`, Handy's default prompt               | `post_process_enabled` true, provider `custom` (base URL localhost:11434, Ollama), model `gemma3:4b`; key Option+Shift+Space (transcribe with post-process) | Handy has no on-device cleanup model of its own; its Custom provider's default is Ollama                                                                                                                       |
| Muesli 0.8.6                | shipped       | Parakeet Unified EN 0.6B, Core ML (FluidAudio) | off                                                      | `has_completed_onboarding` true; Right Option, hold                                                                                                         | Onboarding default                                                                                                                                                                                             |
| Muesli 0.8.6                | best-asr      | same as shipped                                | off                                                      | same as shipped                                                                                                                                             | Muesli marks Parakeet Unified recommended: "The best English dictation. Lowest error rate"                                                                                                                     |
| Muesli 0.8.6                | local-cleanup | Parakeet Unified (held)                        | muesli Cleanup Lite (fine-tuned Qwen 3.5 0.8B, v14, MLX) | `enable_post_processor` true, `post_processor_backend` local, `active_post_processor_id` qwen35-postproc-v4                                                 | Muesli's own default on-device cleanup                                                                                                                                                                         |
| FluidVoice 1.6.13           | shipped       | Parakeet TDT v3, Core ML ("FluidVoice Pick")   | off                                                      | `OnboardingCompleted`, `SelectedSpeechModel` parakeet-tdt; Right Option, toggle                                                                             | Onboarding default on Apple silicon                                                                                                                                                                            |
| FluidVoice 1.6.13           | best-asr      | Whisper Large                                  | off                                                      | `SelectedSpeechModel` whisper-large, `EnableAIProcessing` false                                                                                             | Its model list rates Whisper Large and Qwen3 ASR 1.7B highest (accuracyPercent 1.00, above Cohere Transcribe 0.98); Whisper Large is the one described as "Best possible accuracy" and comes first in the list |
| FluidVoice 1.6.13           | local-cleanup | Parakeet TDT v3 (held)                         | Ollama `gemma3:4b`                                       | `EnableAIProcessing` true, `SelectedProviderID` ollama, `SelectedAIModel` gemma3:4b                                                                         | Its own Private AI provider is unavailable in the open-source build; Apple Intelligence needs macOS 26                                                                                                         |
| Vocet 0.2.0 (notarised DMG) | shipped       | OrukQwen 1.7B Q4_K_M                           | Gemma 4 E2B Q4_K_M, on device                            | `transcription` local, Right Option, hold; usage sharing off                                                                                                | Setup's "On this Mac" choice (cleanup is part of it)                                                                                                                                                           |
| Vocet 0.2.0                 | best-asr      | OrukQwen 1.7B Q4_K_M                           | off                                                      | as shipped, Gemma model file moved aside                                                                                                                    | Its only local speech model; 0.2.0 has no cleanup switch and cleans up only while the Gemma model is present                                                                                                   |
| Vocet 0.2.0                 | local-cleanup | same as shipped                                | same as shipped                                          | same as shipped                                                                                                                                             | Its on-device cleanup is its shipped default                                                                                                                                                                   |
| VoiceInk 2.27 (release DMG, trial) | shipped | Parakeet V3, Core ML (FluidAudio) | off | `hasCompletedOnboardingV2`, `CurrentTranscriptionModel` parakeet-tdt-0.6b-v3; starter Dictation mode (language auto, live transcription on, text formatting on, enhancement off); Right Option, hybrid | Onboarding's local path requires Parakeet V3; Right Option because onboarding asks for a shortcut and has none by default |
| VoiceInk 2.27 | best-asr | Parakeet Ultra, Core ML | off | `CurrentTranscriptionModel` and the mode's model parakeet-ultra, language en | No model is marked recommended; the v2.27 registry rates Ultra 0.95 (V3 0.94) and describes it as "post-trained ... for more accurate transcription"; first listed of the models tied at 0.95 and the one described as more accurate (FluidVoice's rule) |
| VoiceInk 2.27 | local-cleanup | Parakeet V3 (held) | VoiceInk Refine V1 (MLX, on device, its own prompt) | the mode's `isAIEnhancementEnabled` true, `selectedAIProvider` VoiceInk Refine | VoiceInk's own on-device cleanup (needs 16 GB of memory) |
| OpenWhispr 1.12.0 | shipped | Parakeet Unified EN 0.6B, sherpa-onnx int8 | Qwen3.5 2B Q4_K_M, llama.cpp, on device | guest, local; `.env`: `PARAKEET_MODEL`, `DICTATION_LANGUAGE` en, `CLEANUP_PROVIDER` local, `LOCAL_CLEANUP_MODEL` qwen3.5-2b-q4_k_m, `DICTATION_KEY` RightOption, `ACTIVATION_MODE` push | Its local setup's English pick, with cleanup switched on by default on every Mac with 16 GB+; hold because onboarding sets push-to-talk for new users; Right Option because Globe/Fn can't be pressed by software |
| OpenWhispr 1.12.0 | best-asr | same as shipped | off | as shipped, `CLEANUP_PROVIDER` and `LOCAL_CLEANUP_MODEL` unset | The setup table's pick for English is the maintainer's recommendation |
| OpenWhispr 1.12.0 | local-cleanup | same as shipped | same as shipped | same as shipped | Its on-device cleanup is part of its shipped default (16 GB+) |

Every run records, in `run.json`: the app version, the release (DMG) sha256, the app binary's sha256, each
model file's sha256 (a Core ML folder: the hash of its files' hashes) and the total model size, the backend,
the profile's settings, the settings files' sha256, peak resident memory, and the machine (chip, memory,
macOS version and build, power source, load average).

**Verified in the bench VM** (2026-11-23/24, `dictation-vm-benchapps`, smoke plus 10 clips each, unscored):
Handy's Models page tags Qwen3-ASR 1.7B as "More accurate" (`handy.best-asr`); Handy pastes Ollama-cleaned
text (`handy.local-cleanup`; `ollama list` shows `gemma3:4b` as 68ceab7e468a, the pinned digest); Muesli
downloads Cleanup Lite and cleans dictations (`muesli.local-cleanup`); Vocet 0.2.0 without the Gemma file
pastes OrukQwen text and doesn't download Gemma again (`vocet.best-asr`). Their `verify` items are cleared.
`voiceink.shipped`, `voiceink.best-asr` (Parakeet Ultra) and `openwhispr.best-asr` pass the smoke and 10 clips
there too (10 clips decide nothing about accuracy; VoiceInk's pick follows its registry).

**Verify on the host** (each profile's `verify` field, which blocks scored runs until cleared; no VM can
do these, so a 30 s smoke in the bench account before the profile's first night): `voiceink.local-cleanup`
(Refine needs 16 GB; the VMs have 8 GB), `openwhispr.shipped` (its Qwen3.5 2B cleanup takes over 60 s on a
VM's paravirtual GPU), `fluidvoice.best-asr` (Whisper Large needs 8 GB of free memory; the 8 GB VM refuses it, so a 16 GB Mac; its
gguf then goes into `model.files`) and `fluidvoice.local-cleanup` (the preseeded Ollama provider isn't
honoured: select and verify it once in FluidVoice's AI settings, then check the text is cleaned). A model an
app downloads itself (Muesli's Cleanup Lite, FluidVoice's Whisper Large) is loaded only after a restart:
setup waits for the download to complete (`BENCH_DOWNLOAD_WAIT`, default 1 h) and restarts the app.

**VoiceInk's trial.** The official release runs a 7-day trial that starts at its first launch (the start
date goes into the login Keychain of the account it runs in, which the bench never writes). After it, every
paste starts with "Your trial has ended. Upgrade to VoiceInk Pro..." A paste holding that notice
(`refusal_text`) at any point, including the quiet period after a partial paste, records the clip as
`refused`, which no score accepts, and stops the run (a smoke test that shows it stops too). All smoke tests
and pre-night checks happen in the VM, where `BENCH_CI=1 ./bench setup --app voiceink...` launches it. Any
other setup never launches VoiceInk: on the host only `host-run` (in the bench account) does, through a nonce
file it creates and deletes itself (`work/host-run.gate`), first on its first night, so its three nights fall
inside the trial.

## The bench Mac (host runs)

Host runs happen in the bench's own macOS account on the owner's Mac: the Standard user `vocetbench`
(fixed, no override) with no admin rights and no Apple ID, logged in as the active console
session for the night. The owner's Keychain, settings, app data and documents are in another account, out of
the bench's reach. `host-run` and `restore-host` refuse to start as any other user or as an administrator.

- `/Applications` is shared with the owner: the bench never installs into it, deletes from it or quits an
  app from it. Apps are installed only into the bench's own folder (`work/Applications`; setup and host runs
  refuse if it, `work` or the bench folder is a symlink, or its real path is outside the account's home) and
  quit by PID, only processes whose own executable is in there (never a descendant from elsewhere), never by
  process name. A host run refuses to
  start while a benchmarked app runs in the bench account from anywhere else.
- Each job deletes the selected app's state in the bench account (its data folders and preference domains,
  each profile's `state`, all inside the account's home), then setup writes the profile's. Model downloads
  are cached in `work/cache` and cloned in.
- The default input device (which CoreAudio may share between accounts) is saved by its UID and put back
  and read back at the end, also on Ctrl-C, SIGTERM and SIGHUP; signals wait while it is saved or restored.
  The text corrections `bench machine` turns off are the bench account's own preferences and stay off.
- A lock (`work/host.lock`) allows one `host-run` or `restore-host` at a time. If a run is cut off anyway,
  `./bench restore-host` puts the input back (`work/host-input.json`) and quits the bench's apps, even when
  the input can't be put back (then it reports the failure); a new host run refuses to start until it has.
- TextEdit must be closed, or show only the bench's scratch document (`work/scratch.txt`, matched by its
  exact path); the bench never quits it.
- `./bench setup` outside CI and a host run refuses to overwrite an app's existing settings unless
  `--overwrite` (a test machine or VM).

Not yet covered here: Hex and OpenSuperWhisper (setups still to write). Buzz and Vibe are out of scope
(file transcription, not dictation).

## Passes (predeclared; no adaptive extra passes)

`shipped`: 3 passes per app, on different nights, the app order rotated. Every other profile that is run on
its own: 1 pass. Budget: 6 passes per night (about 72 min each).

| Night | Jobs                                                                                      |
| ----- | ----------------------------------------------------------------------------------------- |
| 1     | pass 1 of handy, muesli, fluidvoice, vocet (shipped); handy.best-asr, fluidvoice.best-asr |
| 2     | pass 2 of the four shipped (order rotated); handy.local-cleanup, muesli.local-cleanup     |
| 3     | pass 3 of the four shipped (order rotated); fluidvoice.local-cleanup, vocet.best-asr      |
| 4     | pass 1 of voiceink, openwhispr (shipped); voiceink.best-asr, openwhispr.best-asr, voiceink.local-cleanup |
| 5     | pass 2 of voiceink, openwhispr (shipped, order rotated)                                   |
| 6     | pass 3 of voiceink, openwhispr (shipped, order rotated)                                   |

For example, night 1: `./bench host-run --jobs handy:shipped,muesli:shipped,fluidvoice:shipped,vocet:shipped,handy:best-asr,fluidvoice:best-asr --passes 1 --first-pass 1`
(night 2 and 3: `--first-pass 2` and `3`, which also rotate the order and change the shared clip order).
Nights 4 to 6 are consecutive, so VoiceInk's three nights fall inside its 7-day trial from its first launch
on night 4: `./bench host-run --jobs voiceink:shipped,openwhispr:shipped,voiceink:best-asr,openwhispr:best-asr,voiceink:local-cleanup --passes 1 --first-pass 1`,
then `--jobs voiceink:shipped,openwhispr:shipped` with `--first-pass 2` and `3`. `openwhispr.local-cleanup`
is the same configuration as shipped and isn't run on its own.

## Changes

- 2026-11-23 (BENCH2): profiles introduced (`apps/<app>.<profile>.json`), reasons above. Vocet is installed
  from its published notarised 0.2.0 DMG instead of a source build, like every other app. One protocol for
  CI and the Mac: the CI-only 60 s deadline and 3 s settle are removed. Earlier results (before this date)
  used the old protocol and a source build of Vocet and are not comparable.
- 2026-11-23 (BENCH2): VoiceInk 2.27 (release DMG, trial) and OpenWhispr 1.12.0 added. Against BENCH1's
  configs: OpenWhispr activation hold (its onboarding now defaults new users to push-to-talk; was tap), its
  shipped profile includes its default 16 GB+ local cleanup; VoiceInk's models go where FluidAudio at its
  pinned commit looks (`Models/parakeet-tdt-0.6b-v3`, `Models/silero-vad`), and its mode has live
  transcription on as onboarding writes it.
- 2026-11-23 (BENCH2 review): harness version 3. A read that straddles the deadline no longer counts, so
  runs with harness version 2 don't pool with later ones. Ollama cleanup models are checked against their
  full manifest digest. Host runs move to the bench's own macOS account.
- 2026-11-23 (BENCH2 review): harness version 4. The refusal check reads every snapshot, including a read
  that crosses the deadline (it isn't scored, but a notice in it refuses the clip); runs and `partial` rows
  from earlier versions don't resume or pool.
