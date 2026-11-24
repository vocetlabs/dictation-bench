# Mac dictation app bench

Runs the same public speech clips through Mac dictation apps the way a person uses them: press the app's
key, speak into the microphone, release, and read what the app pastes. Every app gets the same audio,
timing, target window and scoring. MIT licensed ([LICENSE](LICENSE)).

**Disclosure.** This bench is built and run by the developer of [Vocet](https://github.com/vocetlabs/dictation-app),
one of the benchmarked apps. Every app, Vocet included, is installed from its published release and
configured as described in [CONFIGS.md](CONFIGS.md), which records each setting and the reason for it.
Every results table repeats this note. Maintainers are invited to correct their app's configuration.

The repository holds code, app configurations and numeric results. It never holds the clips, their
reference texts or any app's transcripts: the clips are downloaded from their source at run time, and only
numbers leave a run.

## What it measures

For each clip, in a logged-in macOS session:

1. A plain-text TextEdit document is emptied, brought to the front and checked.
2. The app's key is pressed with real key events (`CGEventPost` at the HID tap; a modifier is sent as
   `flagsChanged`, as a keyboard sends it): key-down for hold-to-talk apps, a tap for toggle apps.
3. The harness waits until a process opens the virtual microphone (**BlackHole 2ch**, the default input),
   then 0.5 s.
4. The clip, padded with 300 ms of silence at each end, plays into BlackHole. From this moment the document
   is read every 20 ms, so text that appears while speaking is seen too.
5. 4 s after the clip ends, the key is released (or tapped again).
6. Reading continues until the text has not changed for 2 s, or 30 s after the release.

**Outcomes.** `ok` (the text settled), `partial` (still changing at the deadline; scored as it stood),
`empty` (no text), `late` (text only after the deadline; scored as no output). These are the app's results
and are never retried. A fault of the harness or machine (the document lost the focus, a failed
Accessibility read, failed playback, no process opened the microphone) is retried, after the app is made
idle again. After any outcome but `ok` the harness waits a quiet period, and restarts a toggle app whose
recording state is then unknown, so a late paste can't land in the next clip.

**Accuracy.** Word error rate (WER) against each clip's **spoken text** (verbatim: speech accuracy, which
leads the speech track) and its **intended text** (what the speaker meant to write), in separate columns. Text is Unicode-normalized (NFKC),
typographic quotes and dashes are folded to ASCII, case and punctuation are ignored. A clip with no output
counts every reference word as an error. A run is scored only if every selected clip has exactly one app
outcome; otherwise `score` refuses and says why.

**Judge metrics (optional; judge estimates).** ZER (share of clips that need no edit) and AMEND (pooled character edits /
characters), from [amend-va](https://pypi.org/project/amend-va/) 0.2.1's prompt `v1` and its `char_edits`,
asked of one pinned judge, Claude Opus 5.5 (reasoning effort high), through
[OpenRouter's Batch API](https://openrouter.ai/docs/guides/features/batch-api): the model's `:batch`
variant, the same model at about half the price. All prompts of a run go into one batch, polled once a
minute; most batches finish within minutes, the API allows up to 24 hours. The judge runs only when
`JUDGE_API_KEY` is set (`./bench judge out/<run>`); otherwise the summary says "judge metrics: not computed
(no key)". Sent to the judge, for each clip with text: the clip's intended text, its spoken text and the
app's draft. Clips with no output are not sent and get no ZER credit. Only numbers are kept (`judge.json`,
which records the `:batch` variant); the judge's corrections stay in the local, git-ignored `.judge-cache/`,
keyed by model, prompt version and scorer version, and a batch still pending is resumed by the next run
instead of submitted again: its id is kept in the run folder (`.judge-batch-pending.json`) until its answers
are cached, and a run that finds one collects and caches all of that batch's answers before submitting
anything new. An exclusive lock (`flock` on `.judge-batch.lock`, which the OS releases when a judge exits) stops two
judges submitting for one run. If the
API no longer knows the pending batch (HTTP 404/410: expired, deleted, or another key's), the run stops and
keeps the record; `./bench judge out/<run> --drop-pending` removes it. If the reply to a submit itself is
lost, a rerun pays for that batch again. No headline is given unless every clip has a judgement. Compare
runs only under the same judge. In a repository of your own, add a `JUDGE_API_KEY` secret and pass it to the workflow's
Judge step (`env: JUDGE_API_KEY: ${{ secrets.JUDGE_API_KEY }}`).

Other paths to the same judge: `--via openrouter-sync` (OpenRouter's synchronous API, full price; it shares
the batch judge's cache), and `--via claude-cli`, which asks `claude-opus-5-5` through a logged-in
[Claude Code](https://claude.com/claude-code) CLI (`claude -p`, effort high, tools off, from an empty
folder, no API key in its environment, one call at a time) on a Claude subscription and writes
`judge-claude-cli.json`; tables name the path that scored a run. `./bench judge out/<run> --no-cache --limit
10` re-asks ten clips and reports how many verdicts match the cached ones, without touching the cache.

**Speed** (quiet real Macs only, not CI): `post_stop` = key release to the last change of the text (the
results tables' latency: the deadline also starts at the release); `e2t` = end of speech in the clip to the
last change. p50 and p95 over `ok` clips, so they are conditional: read them with the failure rate (share
of clips not `ok`) beside them. AMEND is pooled over clips with output only. The rules are fixed in
[CONFIGS.md](CONFIGS.md) before any scored run. End of speech is the end of the last 30 ms frame whose energy
is at least 10% of the clip's loud frames.

## Run it on GitHub (accuracy)

Fork the repository, enable Actions, and run the **Bench** workflow with an app and `smoke` (one synthetic
dictation) or `full` (all clips). A push smoke-tests the apps in `ci-plan.json`. A GitHub-hosted macOS runner has a logged-in session, so the job installs
BlackHole, installs the app's signed release, sets it up as its onboarding would, grants its permissions,
runs the smoke dictation as a gate, then the clips, and scores in the job. Runs there use the same protocol
and timing as a Mac but report no latency (`--accuracy-only`), since a shared runner's speed says little
about a Mac. CI is for reproducibility checks; the headline numbers come from the host runs below. The job uploads `score.json`, `run.json` and (with a judge key) `judge.json` only.

Apps with an unattended setup (`hosted_setup: true` in `apps/<app>.<profile>.json`): the `shipped` profiles of
Handy, Muesli, FluidVoice and Vocet. OpenWhispr and VoiceInk are set up in a Lume VM (below) for checks; their
configs say why they have no unattended CI setup.

## Run it on a Mac (accuracy and speed)

Needs macOS 15 on Apple silicon, [uv](https://docs.astral.sh/uv/), [BlackHole 2ch](https://github.com/ExistentialAudio/BlackHole)
installed (system output left on the speakers), and Accessibility for the terminal you run it from.

```sh
./bench fetch                         # the clips and references from their source (pinned, sha256 checked)
./bench machine                       # BlackHole as default input, automatic corrections off
./bench setup --app handy --overwrite # a test Mac or VM: install into work/Applications and configure; allow Microphone/Accessibility
./bench run --app handy --smoke       # one synthetic dictation: is the setup right?
./bench run --app handy --full        # smoke gate, all clips, then score -> out/handy-local/score.json
./bench verify out/handy-local/score.json
./bench table out/*/score.json
```

For speed numbers, use a quiet Mac: no other VMs or builds; note the load average and free memory before and
after. Quit every other dictation app (several share the same key).

**Profiles.** Each app has three configurations (`apps/<app>.<profile>.json`, local models only):
`shipped` (the onboarding default: the headline, in two groups, raw speech and speech with local cleanup),
`best-asr` (the maintainer-recommended local speech model, English, cleanup off; the speech track, led by
spoken-text WER) and `local-cleanup` (the shipped speech model held fixed, the app's on-device cleanup on).
`--app handy` is `handy.shipped`; `--app handy:best-asr` picks a profile. A profile can be "n/a" or the same
configuration as another (then it isn't run twice). [CONFIGS.md](CONFIGS.md) lists them with reasons.

**Host runs** (the headline numbers, on one Mac, overnight, logged in as the bench's own Standard account,
`vocetbench`):

```sh
./bench host-run --jobs handy:shipped,muesli:shipped,fluidvoice:shipped,vocet:shipped --passes 1 --first-pass 1
./bench judge out/host/<stamp>/pass1/handy.shipped --via claude-cli   # each run, then:
./bench report out/host/*/pass*/*                                     # tables for this README
./bench judge-check out/host/<stamp>/pass1/*                          # a second judge (JUDGE_API_KEY)
```

The owner's data stays in their own account: `host-run` refuses to start as any user but `BENCH_USER`
(default `vocetbench`) or as an administrator. Apps go into the bench's own folder and are quit by PID, never
from `/Applications`; each job clears the app's state in the bench account and sets it up afresh; see
[CONFIGS.md](CONFIGS.md). Close TextEdit first. `./bench restore-host` puts the input device back if a run is
cut off. `host-run` refuses to start while a virtual machine runs, logs the load average before and after each job,
sets each app up for its profile, rotates the app order each pass, gives every app in a pass the same seeded
clip order, writes each pass to its own folder, runs one warm-up dictation (the smoke sentence) before each
app's clips, records the chip, memory, macOS build and power source (`host.json`, `run.json`), and puts the
input device back (by UID) when it ends. `report` prints each configuration's passes,
their mean, 95% intervals from a bootstrap that resamples speakers (all of a speaker's clips together), and
paired differences between neighbours in each group's ranking; a difference whose interval includes zero is
reported as unresolved. `judge-check` asks a second judge from another model family (pinned in `judge.py`)
the same prompt on a seeded sample of at least 40 clips per run, stratified by the first judge's verdict,
and reports agreement, Cohen's kappa, both ZERs and any rank changes; the clips they disagree on go to
`work/judge-check/` (local only) for a small blinded human check.

### In a Lume VM

App setups grant permissions by writing the TCC rows System Settings would write (`BENCH_CI=1`), which
needs System Integrity Protection off and `sudo`. A VM for that, cloned once from a logged-in macOS 15
image that has BlackHole 2ch, uv and Accessibility for Terminal (here `dictation-vm-golden`):

```sh
lume clone dictation-vm-golden dictation-vm-benchapps && lume set dictation-vm-benchapps --memory 8GB --cpu 4
lume run dictation-vm-benchapps --no-display --detach --recovery-mode true   # then over its VNC:
#   Options > Continue > (language) > Utilities > Terminal: csrutil disable, y, the admin password;
#   shutdown -h now. (`lume sip off <vm>` automates this when its menu clicks land.)
lume run dictation-vm-benchapps --no-display --detach
```

Then copy the bench and `clips/` into the VM and run each step from the VM's Terminal (Accessibility lives
there): `./bench machine`; then, after `sudo -v`, `BENCH_CI=1 ./bench setup --app <app>:<profile> --overwrite`
(which also writes the app's TCC rows; `BENCH_CI=1` is honoured only in a virtual machine) and
`./bench run --app <app>:<profile> --smoke`. Quit the previous app first: a run refuses to start while another
benchmarked app runs. Allow Terminal's one-time microphone prompt (VNC). An 8 GB VM
can't run VoiceInk Refine (it needs 16 GB), and its paravirtual GPU makes on-device LLM cleanup far slower
than a Mac (OpenWhispr's Qwen3.5 2B takes over 60 s), so those profiles are marked verify-on-host.

`python3 bench.py dry-run` runs the loop against a simulated app; `./test.sh` runs the tests (standard library
only, no macOS).

## Apps

Each app runs its maintainer's published release (Vocet too: its notarised 0.2.0 DMG, checksum pinned), and
its `shipped` profile uses the defaults its onboarding leaves: its default key and activation mode, and its
default or first recommended local model. `apps/<app>.<profile>.json` pins the release (URL, sha256, tag),
the model and its files, and the settings the profile applies; `apps/<app>.sh` applies them. Every run
records the app's version, release (DMG) sha256, binary hash, model files' sha256 and disk size, backend,
settings files' hashes, peak resident memory and the machine in `run.json`.

| App                                                    | Release               | Key, mode                                                                | Local model                                         |
| ------------------------------------------------------ | --------------------- | ------------------------------------------------------------------------ | --------------------------------------------------- |
| [Handy](https://github.com/cjpais/Handy)               | 0.10.1                | Option+Space, hold-or-toggle (held here)                                 | Parakeet Unified EN 0.6B, Q8_0 GGUF (731 MB)        |
| [Muesli](https://github.com/Muesli-HQ/muesli)          | 0.8.6                 | Right Option, hold                                                       | Parakeet Unified EN 0.6B, Core ML (~565 MB)         |
| [FluidVoice](https://github.com/altic-dev/FluidVoice)  | 1.6.13                | Right Option, toggle                                                     | Parakeet TDT 0.6B, Core ML (~450 MB)                |
| [VoiceInk](https://github.com/Beingpax/VoiceInk)       | 2.27 (7-day trial)    | Right Option (onboarding asks), hybrid (held here)                       | Parakeet V3, Core ML (469 MB); profiles: `voiceink.{shipped,best-asr,local-cleanup}` |
| [OpenWhispr](https://github.com/OpenWhispr/openwhispr) | 1.12.0                | Right Option (its Globe/Fn default can't be pressed by software), hold   | Parakeet Unified EN 0.6B, sherpa-onnx int8 (631 MB) + Qwen3.5 2B cleanup (shipped on 16 GB+); profiles: `openwhispr.{shipped,best-asr}` |
| [Vocet](https://github.com/vocetlabs/dictation-app)    | 0.2.0 (notarised DMG) | Right Option, hold                                                       | OrukQwen 1.7B + Gemma 4 E2B cleanup                 |

Not yet: [Hex](https://github.com/kitlangton/Hex) and [OpenSuperWhisper](https://github.com/Starmel/OpenSuperWhisper)
(both MIT, signed releases; their setups are still to be written). Out of scope: Buzz and Vibe, which
transcribe files rather than dictate.

These are the `shipped` profiles; [CONFIGS.md](CONFIGS.md) has all of them. Corrections to an app's
configuration are welcome from its maintainers.

## Results

Pending: the owner's overnight host runs on one Mac (3 `shipped` passes per app on different nights, one pass
per other profile, as planned in [CONFIGS.md](CONFIGS.md)). This section will hold the output of
`./bench report`, the hardware, and links to each run's `score.json`, `run.json` and judge files.

Disclosure: this bench is built and run by the developer of Vocet, one of the benchmarked apps.

## Files

| File                   | What it does                                                                           |
| ---------------------- | -------------------------------------------------------------------------------------- |
| `bench`, `bench.py`    | the one command (above)                                                                |
| `harness.py`           | the clip loop, outcomes and timing (pure Python; runs against a stub app in tests)     |
| `macos.py`             | key events, BlackHole, playback, the TextEdit target, app restarts                     |
| `score.py`             | WER, the completeness checks, `score.json`, the verifier and tables                    |
| `judge.py`             | the optional judge metrics (ZER, AMEND) and the second judge                           |
| `isolation.py`         | host runs: the bench account check, clearing app state, the lock, strict prefs, signals |
| `report.py`            | passes, means, speaker-bootstrap intervals, the judge check's agreement                |
| `CONFIGS.md`           | every profile's settings and reasons, the frozen protocol, the pass plan, changes      |
| `fetch_clips.py`       | downloads the dataset (pinned revision, sha256 checked) into `clips/`                  |
| `apps/`                | one config per app and profile, one setup script per app; `common.sh` helpers          |
| `test_*.py`, `test.sh` | offline tests                                                                          |

`clips/`, `out/` (per-clip results with transcripts), `.judge-cache/` and `work/` are git-ignored.

**`score.json`** holds the app, its effective configuration, the run fingerprint (everything the results
depend on: config, binary hash, harness version, timing, dataset, machine), the digest of the results it
was computed from, and per clip: the outcome, edit and word counts against both references, and latencies.
`./bench verify` recomputes every headline number from those per-clip numbers.

## Data

Clips and references: _Dictation Dataset_, public test split, Hugging Face `VoiceArena/Dictation_Dataset`,
revision `ab671bb2`, 151 clips, licensed CC BY-NC 4.0 by its authors. They are fetched at run time and
never redistributed here; the dataset's licence governs their use. This project is not affiliated with the
dataset's authors. It is built by the developer of Vocet, one of the apps (see the disclosure above).
