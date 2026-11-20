#!/usr/bin/env bash
# The platform's secrets check (gitleaks, MIT; homelab#611), the pre-commit hook `gitleaks`. With a range (pre-commit's
# --from-ref/--to-ref: a PR in CI, the commits a push sends in `magehand verify`) it scans those commits, so a secret
# added and removed again in them still counts. Without one (--all-files) it scans the tracked files at HEAD.
# A false positive: `gitleaks:allow` on its line, or its fingerprint in the repo's .gitleaksignore (gitleaks prints it).
set -euo pipefail
config="$PWD/.platform-quality/gitleaks.toml"
flags=(--no-banner --redact --verbose --exit-code 1 --config "$config")
if [ -n "${PRE_COMMIT_FROM_REF:-}" ] && [ -n "${PRE_COMMIT_TO_REF:-}" ]; then
  exec gitleaks git "${flags[@]}" --log-opts="$PRE_COMMIT_FROM_REF..$PRE_COMMIT_TO_REF" .
fi
tree=$(mktemp -d)
trap 'rm -rf "$tree"' EXIT
git archive HEAD | tar -x -C "$tree"
cd "$tree"  # paths relative to the repo, as in .gitleaksignore
gitleaks dir "${flags[@]}" .
