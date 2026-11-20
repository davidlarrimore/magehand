#!/usr/bin/env bash
# The platform's secrets check (gitleaks, MIT; homelab#611), the pre-commit hook `gitleaks`.
#
# What it scans:
# - the commits being checked: pre-commit's --from-ref/--to-ref, or PLATFORM_CHECKS_RANGE (BASE..HEAD), which CI and
#   `magehand verify` set because they run every file when the rules change and pre-commit then passes no range.
#   A secret added and removed again in them still counts; a merge commit's own changes (a conflict resolution)
#   are scanned against its first parent;
# - with no pre-commit range (--all-files), also every tracked file at HEAD, export-ignore or not.
#
# gitleaks itself: its release binary from GitHub, checked against the sha256 pinned below, kept in
# ~/.cache/platform-checks (no Go toolchain, no module proxy). To bump: the version and the four sums from the
# release's gitleaks_<version>_checksums.txt.
# A false positive: `gitleaks:allow` in a comment on its line, or its fingerprint (gitleaks prints it) in the repo's
# .gitleaksignore.
set -euo pipefail
version=8.30.1
case "$(uname -s)_$(uname -m)" in
  Darwin_arm64) asset=darwin_arm64 sum=b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5 ;;
  Darwin_x86_64) asset=darwin_x64 sum=dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709 ;;
  Linux_aarch64 | Linux_arm64) asset=linux_arm64 sum=e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080 ;;
  Linux_x86_64) asset=linux_x64 sum=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb ;;
  *) echo "gitleaks: no pinned build for $(uname -s) $(uname -m)" >&2; exit 2 ;;
esac

scratch=()
# shellcheck disable=SC2329  # called by the trap
cleanup() { [ ${#scratch[@]} -eq 0 ] || rm -rf "${scratch[@]}"; }
trap cleanup EXIT
sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi; }

cache="${XDG_CACHE_HOME:-$HOME/.cache}/platform-checks/gitleaks-$version-$asset"
gitleaks="$cache/gitleaks"
if [ ! -x "$gitleaks" ]; then
  download=$(mktemp -d); scratch+=("$download")
  curl -fsSL --retry 3 -o "$download/gitleaks.tar.gz" \
    "https://github.com/gitleaks/gitleaks/releases/download/v$version/gitleaks_${version}_$asset.tar.gz"
  got=$(sha256 "$download/gitleaks.tar.gz" | cut -d' ' -f1)
  if [ "$got" != "$sum" ]; then
    echo "gitleaks: the download's sha256 is $got, not the pinned $sum: not used" >&2
    exit 2
  fi
  tar -xzf "$download/gitleaks.tar.gz" -C "$download" gitleaks
  mkdir -p "$cache"
  mv "$download/gitleaks" "$gitleaks.$$" && mv -f "$gitleaks.$$" "$gitleaks"  # whole, or not there
fi

config="$PWD/.platform-quality/gitleaks.toml"
flags=(--no-banner --redact --verbose --exit-code 1 --config "$config")
range=""
if [ -n "${PRE_COMMIT_FROM_REF:-}" ] && [ -n "${PRE_COMMIT_TO_REF:-}" ]; then
  range="$PRE_COMMIT_FROM_REF..$PRE_COMMIT_TO_REF"
elif [ -n "${PLATFORM_CHECKS_RANGE:-}" ]; then
  range="$PLATFORM_CHECKS_RANGE"
fi
status=0
if [ -n "$range" ]; then
  "$gitleaks" git "${flags[@]}" --log-opts="--diff-merges=first-parent $range" . || status=1
fi
if [ -z "${PRE_COMMIT_FROM_REF:-}" ]; then
  tree=$(mktemp -d); scratch+=("$tree" "$tree.index")
  # From the index, not `git archive`: export-ignore attributes don't hide a tracked file here.
  GIT_INDEX_FILE="$tree.index" git read-tree HEAD
  GIT_INDEX_FILE="$tree.index" git checkout-index --all --force --prefix="$tree/"
  (cd "$tree" && "$gitleaks" dir "${flags[@]}" .) || status=1  # paths relative to the repo, as in .gitleaksignore
fi
exit "$status"
