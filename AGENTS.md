# Rules for agents working on magehand

- This repo is **public**: never commit secrets, tokens, IP addresses, email
  addresses or other personal identifiers, not even in tests (use
  192.0.2.0/24 and example.com).
- One module, `src/magehand/cli.py`: stdlib plus PyYAML, Python 3.9+ (no
  `match`, no `X | Y` types).
- It reads and runs locally; it never deploys or writes to the homelab. Keep
  every network call to GitHub and `*.lab`; never add a public homelab
  endpoint.
- Secrets never touch disk or argv (`-e NAME` for docker, Keychain for the
  token).
- `check` rules: each is a mistake an app can make about the platform, with a
  guide section to point at (`see`) and a unit test. Errors only for what
  certainly breaks; anything heuristic is a warning. When the platform changes
  a block's keys or options, update `BLOCK_KEYS` (or the catalog's `keys`).
- Tests: `python -m unittest discover -s tests` (no network, no Docker).
  Update README.md's command table with any command change.
