# Slice B — magehand post-deploy checks

Implemented the owner-approved rung-1 CLI slice. Read AGENTS.md and CLAUDE.md.

## Schema coordination

Matched `parse_post_checks` in the referenced homelab-post-deploy worktree's
`platform/openclaw/tools/homelab`, and read `1-platform.brief.md`.
`1-platform.report.md` was not present at implementation time. Exact schema:
`version: 1`, 1–20 `checks`, required `method`, `path`, `status`, optional mutually
exclusive `body_contains` / `json_key` (GET only, top-level key presence).
Parser restrictions match the platform, including encoded separator/double-encoding rejection, byte/length bounds, HTTP status
integer bounds, traversal rejection, duplicate/string keys and YAML aliases.

## Changes

- `check` validates optional `deploy/checks.yaml` and permits it in deploy's file
  allowlist. Malformed/unreadable/non-UTF-8 files produce actionable errors with
  a guide reference; absent files remain compatible with legacy apps.
- `status` reads the exact merged dev deploy commit's `homelab/post-deploy`
  context through GitHub. Human output displays the description; JSON includes
  commit, state, description in text and target URL. Failure/error fail status,
  pending participates in watch, absent status remains nonblocking unknown.
- Existing scaffold behavior already preserves all existing files; a regression
  test verifies template `deploy/checks.yaml` survives `new` scaffolding intact.
- README command table and Post-deploy checks guide document the declaration,
  dev-only execution, result handling and next steps.

## Verification

- Initial bare `python3 -m unittest discover -s tests` could not import the
  uninstalled src-layout package. Ran the required discovery with `PYTHONPATH=src`.
- Full suite: 405 tests passed (no network or Docker). Covers schema restrictions,
  optional/malformed files, scaffold preservation, context states and CLI exits,
  JSON and next-step behavior.
- Root, check and status CLI help passed with `PYTHONPATH=src python3 -m magehand`.
- Python compilation and `git diff --check` passed.

No deployment, production checks, workflow edits, commits, pushes or PRs.
Live platform execution was outside this CLI slice; integration uses mocked
GitHub results and the platform's implemented parser contract.
