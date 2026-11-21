#!/usr/bin/env bash
# One CI job: set up the machine and one app, run one shard of the clips in accuracy-only mode, and leave
# in out/: wer.json (numbers only, published), results.csv.cms (per-clip transcripts, encrypted to
# ci/results-cert.pem, for judging ZER where the private key is), app.txt (version and model).
#
#   ci/run_shard.sh <app> <k/n>
set -euo pipefail
app=${1:?app}; shard=${2:?k/n}
here=$(cd "$(dirname "$0")" && pwd)
. "$here/common.sh"
out=$BENCH/out; mkdir -p "$out"
machine_setup
"$here/setup_$app.sh" | tee "$out/app.txt"
cd "$BENCH"
uv run --quiet harness.py run --app "$app" --mode local --clips clips -o "$out/results.csv" \
  --accuracy-only --machine github-macos-15 --shard "$shard"
uv run --quiet harness.py wer "$out/results.csv" clips/manifest.csv >"$out/wer.json"
encrypt "$out/results.csv" "$out/results.csv.cms"
rm -f "$out/results.csv"
cat "$out/wer.json"
