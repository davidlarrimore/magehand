# magehand

The developer CLI for building apps on [davidlarrimore](https://github.com/davidlarrimore)'s
homelab, the same way on the owner's MacBook (Claude Code) and in OpenClaw's
VM (its coding workers). It reads platform state and runs apps locally. It
never deploys: changes go through pull requests.

It is public because it holds nothing sensitive. Every command needs access
that only the owner has: the private homelab repos on GitHub, the private
`*.lab` network, and an authentik passkey for
OpenBao.

## Install

```sh
brew install uv            # once
uv tool install magehand
magehand setup             # once per machine
```

`magehand setup` checks GitHub (`gh auth login` first) and Docker (Docker
Desktop or OrbStack, for database blocks), makes sure the homelab's `*.lab`
names resolve (if they don't, it asks for the homelab's DNS address, your home
gateway, and points only `*.lab` lookups at it; `magehand setup --undo`
reverses that), then signs you in and installs the `homelab-app` skill for Claude Code (and Codex). Every other command assumes this is done.
Upgrade with `uv tool upgrade magehand`.

## Commands

| Command | What it does |
| --- | --- |
| `magehand guide [topic ["section"]]` | The app platform guide, live from homelab-apps `docs/platform/`; `magehand guide catalog` for blocks and models |
| `magehand guide search WORDS` | The guide sections (homelab-apps `docs/platform/` and `AGENTS.md`) that best match a question, e.g. `magehand guide search web search` |
| `magehand app [name]` | The app's blocks, every env var its pod gets and where it comes from, its URLs. In an app repo the name is the repo's |
| `magehand check [name] [--code DIR] [--apps-dir DIR] [--manifests-only]` | The platform's rules for the app's code and its homelab-apps deployment: block keys and options, `optional: true` keys, Secrets that aren't blocks, the image, env the code reads but the pod doesn't set, direct AI/search provider calls, own login, database files. Each finding names the guide section; exit 1 on errors. Run before every push; CI (homelab-apps `build-app.yaml`) runs it too, with `--apps-dir` |
| `magehand login` | Sign in to OpenBao through authentik (passkey) with role `dev`: read-only access to agent apps' LiteLLM keys, 1h (8h max). The token is kept in the macOS Keychain |
| `magehand dev up\|down\|status` | Docker containers for the app's `postgres`/`redis` blocks (the blocks' own pinned images) on a per-app network, and local values for `secret` blocks |
| `magehand run [--no-proxy] -- CMD` | Runs CMD with the env the app's pod gets: values from its Deployment, the LLM key from OpenBao, databases from `dev up`. Precedence: manifest < the repo's `.magehand.env` (committed, non-secret overrides such as `WEB_DIR=./web`) < your shell < secrets. A proxy on `127.0.0.1:8080` adds the `X-authentik-*` headers Traefik would, as you |
| `magehand run --container [--no-build] [-- CMD]` | Builds the repo's Dockerfile and runs the image as its pod runs: read-only, `/tmp` tmpfs, the pod's user, no capabilities, exactly the pod's env. Secrets go in as `-e NAME` only, never in the image, argv or on disk |
| `magehand setup [--undo] [--dns ADDRESS]` | Once per machine: GitHub, Docker, `*.lab` name resolution (the only step that may ask for your password), sign-in |
| `magehand skill [--install]` | The `homelab-app` skill: tells Claude Code and Codex to use `app`, `guide search` and `check` while working on app code. `setup` installs it (`$CLAUDE_CONFIG_DIR` or `~/.claude`, and `$CODEX_HOME` or `~/.codex` if Codex is installed) |
| `magehand doctor` | Checks GitHub access, that `openbao.lab` is reachable, the sign-in and Docker |
| `magehand version` | The installed version |

## Development

```sh
uv venv && uv pip install -e .
.venv/bin/python -m unittest discover -s tests
```

Python 3.9+ (the Mac's system Python works). Stdlib plus PyYAML. Releases:
bump `version` in `pyproject.toml`, merge, then tag `vX.Y.Z`. The release
workflow builds and publishes to PyPI through trusted publishing (no stored
token). OpenClaw's VM installs a pinned version from homelab
`platform/openclaw/vm/versions.env`.

Never commit secrets, tokens, IP addresses or email addresses: this repo is
public.
