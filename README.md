# Mac dictation app bench

Runs the same public speech clips through Mac dictation apps the way a person uses them: press the app's
key, speak into the microphone, release, and read what the app pastes. Every app gets the same audio,
timing, target window and scoring. MIT licensed ([LICENSE](LICENSE)).

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

**Accuracy.** Word error rate (WER) against each clip's **intended text** (what the speaker meant to
write, the headline) and its **spoken text** (verbatim, a diagnostic). Text is Unicode-normalized (NFKC),
typographic quotes and dashes are folded to ASCII, case and punctuation are ignored. A clip with no output
counts every reference word as an error. A run is scored only if every selected clip has exactly one app
outcome; otherwise `score` refuses and says why.

**Judge metrics (optional).** ZER (share of clips that need no edit) and AMEND (pooled character edits /
characters), from [amend-va](https://pypi.org/project/amend-va/) 0.2.1's prompt `v1` and its `char_edits`,
asked of one pinned judge, `anthropic/claude-opus-5.5` (reasoning effort high), through OpenRouter's
OpenAI-compatible API. They are computed only when `JUDGE_API_KEY` is set (`./bench judge out/<run>`);
otherwise the summary says "judge metrics: not computed (no key)". Sent to the judge, for each clip with
text: the clip's intended text, its spoken text and the app's draft. Clips with no output are not sent and
get no ZER credit. Only numbers are kept (`judge.json`); the judge's corrections stay in the local,
git-ignored `.judge-cache/`, keyed by model, prompt version and scorer version. No headline is given unless
every clip has a judgement. `BENCH_JUDGE_MODEL` and `BENCH_JUDGE_URL` change the judge; compare runs only
under the same judge. In a repository of your own, add a `JUDGE_API_KEY` secret and pass it to the
workflow's Judge step (`env: JUDGE_API_KEY: ${{ secrets.JUDGE_API_KEY }}`).

**Speed** (quiet real Macs only, not CI): `e2t` = end of speech in the clip to the last change of the text
(primary); `post_stop` = key release to the last change. p50 and p95 over `ok` clips, with the failure
rate (share of clips not `ok`) beside them. End of speech is the end of the last 30 ms frame whose energy
is at least 10% of the clip's loud frames.

## Run it on GitHub (accuracy)

Fork the repository, enable Actions, and run the **Bench** workflow with an app and `smoke` (one synthetic
dictation) or `full` (all clips). A push smoke-tests the apps in `ci-plan.json`. A GitHub-hosted macOS runner has a logged-in session, so the job installs
BlackHole, installs the app's signed release, sets it up as its onboarding would, grants its permissions,
runs the smoke dictation as a gate, then the clips, and scores in the job. Runs there are accuracy-only
(`--accuracy-only`: a longer text deadline, no latency), since a shared runner's speed says little about a
Mac. The job uploads `score.json`, `run.json` and (with a judge key) `judge.json` only.

Apps with an unattended setup (`hosted_setup: true` in `apps/<app>.json`): Handy, Muesli, FluidVoice,
VoiceInk and Vocet (built from the pinned commit). OpenWhispr is set up by hand on a local Mac for now.

## Run it on a Mac (accuracy and speed)

Needs macOS 15 on Apple silicon, [uv](https://docs.astral.sh/uv/), [BlackHole 2ch](https://github.com/ExistentialAudio/BlackHole)
installed (system output left on the speakers), and Accessibility for the terminal you run it from.

```sh
./bench fetch                         # the clips and references from their source (pinned, sha256 checked)
./bench machine                       # BlackHole as default input, automatic corrections off
./bench setup --app handy             # install and configure the app; allow Microphone/Accessibility when asked
./bench run --app handy --smoke       # one synthetic dictation: is the setup right?
./bench run --app handy --full        # smoke gate, all clips, then score -> out/handy-local/score.json
./bench verify out/handy-local/score.json
./bench table out/*/score.json
```

For speed numbers, use a quiet Mac: no other VMs or builds; note the load average and free memory before and
after. Quit every other dictation app (several share the same key).

`python3 bench.py dry-run` runs the loop against a simulated app; `./test.sh` runs the tests (standard library
only, no macOS).

## Apps

Each app runs its maintainer's signed release (Vocet: built from source at the commit in its config) with
the defaults its onboarding leaves: its default key and activation mode, and its default or first
recommended local model. `apps/<app>.json` pins the release (URL, sha256, tag), the model, and what is
known about its settings; `apps/<app>.sh` applies them. Every run records the app's binary hash and the
hash of its settings files in `run.json`.

| App                                                    | Release                      | Key, mode                                | Local model                                  |
| ------------------------------------------------------ | ---------------------------- | ---------------------------------------- | -------------------------------------------- |
| [Handy](https://github.com/cjpais/Handy)               | 0.10.1                       | Option+Space, hold-or-toggle (held here) | Parakeet Unified EN 0.6B, Q8_0 GGUF (731 MB) |
| [Muesli](https://github.com/Muesli-HQ/muesli)          | 0.8.6                        | Right Option, hold                       | Parakeet Unified EN 0.6B, Core ML (~565 MB)  |
| [FluidVoice](https://github.com/altic-dev/FluidVoice)  | 1.6.13                       | Right Option, toggle                     | Parakeet TDT 0.6B, Core ML (~450 MB)         |
| [VoiceInk](https://github.com/Beingpax/VoiceInk)       | 2.27                         | chosen in onboarding                     | Parakeet TDT 0.6B v3, Core ML (~450 MB)      |
| [OpenWhispr](https://github.com/OpenWhispr/openwhispr) | 1.12.0                       | Fn (Globe), toggle                       | its onboarding's recommendation              |
| [Vocet](https://github.com/vocetlabs/dictation-app)  | built at the config's commit | Right Option, hold                       | OrukQwen 1.7B + Gemma 4 E2B cleanup          |

Corrections to an app's configuration are welcome from its maintainers.

## Files

| File                   | What it does                                                                       |
| ---------------------- | ---------------------------------------------------------------------------------- |
| `bench`, `bench.py`    | the one command (above)                                                            |
| `harness.py`           | the clip loop, outcomes and timing (pure Python; runs against a stub app in tests) |
| `macos.py`             | key events, BlackHole, playback, the TextEdit target, app restarts                 |
| `score.py`             | WER, the completeness checks, `score.json`, the verifier and tables                |
| `judge.py`             | the optional judge metrics (ZER, AMEND)                                            |
| `fetch_clips.py`       | downloads the dataset (pinned revision, sha256 checked) into `clips/`              |
| `apps/`                | one config and one setup script per app; `common.sh` holds their helpers           |
| `test_*.py`, `test.sh` | offline tests                                                                      |

`clips/`, `out/` (per-clip results with transcripts), `.judge-cache/` and `work/` are git-ignored.

**`score.json`** holds the app, its effective configuration, the run fingerprint (everything the results
depend on: config, binary hash, harness version, timing, dataset, machine), the digest of the results it
was computed from, and per clip: the outcome, edit and word counts against both references, and latencies.
`./bench verify` recomputes every headline number from those per-clip numbers.

## Data

Clips and references: _Dictation Dataset_, public test split, Hugging Face `VoiceArena/Dictation_Dataset`,
revision `ab671bb2`, 151 clips, licensed CC BY-NC 4.0 by its authors. They are fetched at run time and
never redistributed here; the dataset's licence governs their use. This project is not affiliated with the
dataset's authors or with any of the apps.
