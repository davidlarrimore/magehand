# Rules for agents working on magehand

- This repo is **public**: never commit secrets, tokens, IP addresses, email
  addresses or other personal identifiers, not even in tests (use
  192.0.2.0/24 and example.com).
- One module, `src/magehand/cli.py`: stdlib plus PyYAML, Python 3.9+ (no
  `match`, no `X | Y` types).
- It reads and runs locally; it never deploys or changes the homelab's
  configuration. Keep every network call to GitHub and `*.lab`, with one
  scoped exception: `magehand bucket` talks to the S3 endpoint the platform
  hands the app for *its own* bucket (R2 for dev and prod, the local
  Garage for `--env local`), because that is where the app's data is. The
  one cluster call is read-only `kubectl get` (`status`: dev's pods and Argo
  application), skipped without kubectl and by `--no-cluster`. Its writes
  are `magehand new` (an app's repo on GitHub, and its platform setup: the
  bot with write and the approval ruleset, made true on any non-archived repo
  from the template; it never removes anything, edits an existing ruleset or
  changes visibility) and `magehand release` (a tag
  and release on an app's repo), both as the owner through `gh`, and
  `magehand bucket cp` / `sync`, which put objects into that app's own
  bucket only (the app of the checkout you are in; `--app` may only name it), on an explicit command, with the keys of the environment
  asked for (`--env prod` writes production data), and never delete.
- Secrets never touch argv (`-e NAME` for docker; the token goes to the
  Keychain on stdin, `security -i`) and never touch disk, except the token's
  0600 file off macOS, where the standard library has no credential store.
- `check` rules: each is a mistake an app can make about the platform, with a
  guide section to point at (`see`) and a unit test. Errors only for what
  certainly breaks; anything heuristic is a warning. When the platform changes
  a block's keys or options, update `BLOCK_KEYS` (or the catalog's `keys`).
- Tests: `python -m unittest discover -s tests` (no network, no Docker).
  Update README.md's command table with any command change.

## CLI conventions

magehand follows the [Command Line Interface Guidelines](https://clig.dev/),
POSIX exit-status conventions and the
[Heroku CLI style guide](https://devcenter.heroku.com/articles/cli-style-guide).
A new command or option follows the same rules (tests in `CommandLine`):

- Parse with argparse (`build_parser`): every command has `-h/--help` with a
  description and examples; the root help groups commands by task
  (`GROUPS`), one short line each (lowercase, no period, under 80 columns).
- One name per idea across commands: `-a/--app APP` for the app (a lone,
  obvious positional `APP` is fine too), `--json` for machine-readable output
  (a JSON object on stdout) on every command a script or agent reads,
  `--runtime`, `--code`, `--apps-dir`. Prefer flags to positionals.
- Settings: flag > environment variable (`MAGEHAND_*`) > `~/.config/magehand/
  config.json` > default. Never read secrets from flags or env vars of ours.
- Results to stdout, messages and progress to stderr. Exit 0 success, 1
  failure, 2 usage error (`usage(command, message)` or argparse), 130 Ctrl-C.
- Mistakes get a suggestion ("did you mean"), and errors say how to fix
  them. Say what changed when state changes.
- Prompt only when stdin is a terminal; otherwise fail and name the flag that
  answers the question (as `setup --dns`).
- Network calls have timeouts; commands are safe to re-run.
- Keep accepting forms that were released (e.g. `runtime use docker`) unless
  a major version removes them.
