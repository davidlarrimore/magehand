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

`magehand setup` checks GitHub (`gh auth login` first) and which container
runtime this machine has (only needed for database blocks; see `magehand
runtime` below), makes sure the homelab's `*.lab`
names resolve (if they don't, it asks for the homelab's DNS address, your home
gateway, and points only `*.lab` lookups at it; `magehand setup --undo`
reverses that), then signs you in and installs the `homelab-app` skill for Claude Code (and Codex). Every other command assumes this is done.
Upgrade with `magehand upgrade` (any command says, at most once a day, when a new version is out).

## Commands

`magehand --help` lists them; `magehand COMMAND --help` (or `magehand help
COMMAND`) explains one, with examples.

**Start an app**

| Command | What it does |
| --- | --- |
| `magehand new APP [--json]` | Creates `davidlarrimore/APP` from `homelab-app-template` (private) with `gh`, as you, and invites OpenClaw's bot `davidlarrimore-bot` with write; OpenClaw accepts by itself. Safe to re-run: on an existing repo it only adds the bot, and warns if the repo is public. Its one write, and to GitHub, never the homelab |

**Learn the platform and check an app**

| Command | What it does |
| --- | --- |
| `magehand guide [TOPIC ["SECTION"]]` | The app platform guide, live from homelab-apps `docs/platform/`: `magehand guide` (overview), `guide recipes`, `guide blocks "Web search"` (one section), `guide catalog` (blocks and models) |
| `magehand guide search WORDS` | The guide sections (`docs/platform/` and homelab-apps `AGENTS.md`) that best answer a question, e.g. `magehand guide search web search` |
| `magehand app [APP] [--json]` | The app's blocks, every env var its pod gets and where it comes from, its URLs. In an app repo with `deploy/app.yaml` they come from the checkout's `deploy/` (the app's deployment config, which merging copies into homelab-apps); otherwise from homelab-apps `apps/<app>/`. Says when the app isn't registered on the platform yet, or an `llm` block isn't deployed yet: until then it has no LLM key |
| `magehand check [APP] [--code DIR] [--apps-dir DIR] [--manifests-only] [--json]` | The platform's rules for the app's code and its deployment (`deploy/` in the checkout when it has one, else homelab-apps): block keys and options, `optional: true` keys, Secrets nothing makes, the image, files in `deploy/` a merge won't deploy (it holds `app.yaml`, `base/`, and the overlays `dev/` and `prod/`, whose namespaces must be `<app>-dev` and `<app>`), env the code reads but the pod doesn't set, models the code names that its llm block doesn't request (warnings), secrets in the committed `.magehand.env`, direct AI/search provider calls, own login, database files. Each finding names the guide section; exit 1 on errors. `--manifests-only` skips the code rules. Run before every push; CI runs it too (`--apps-dir`) |

**Run an app locally**

| Command | What it does |
| --- | --- |
| `magehand run [-a APP] [--no-proxy] -- CMD` | Runs CMD with the env the app's pod gets: values from its Deployment, the LLM key from OpenBao, databases from `dev up`. Precedence: manifest < the repo's `.magehand.env` (committed, non-secret overrides such as `WEB_DIR=./web`) < your shell < secrets. A proxy on `127.0.0.1:8080` adds the `X-authentik-*` headers Traefik would, as you. With no key in OpenBao it says why (app not registered, block not deployed yet, key being created). In an app's repo, `-a` can't name another app: that would run this code on the other app's keys and budget |
| `magehand run --container [--no-build] [-- CMD]` | Builds the repo's Dockerfile and runs the image as its pod runs: read-only, `/tmp` tmpfs, the pod's user, no capabilities, exactly the pod's env. Secrets go in as `-e NAME` only, never in the image, argv or on disk |
| `magehand dev [up\|down\|status] [APP]` | Containers for the app's `postgres`/`redis` blocks (the blocks' own pinned images) on a per-app network, and local values for `secret` blocks. `down` deletes the local data |
| `magehand runtime [docker\|podman\|none\|auto] [--json]` | Shows or sets the container runtime for `dev` and `run --container`. Detected without sudo (`<cli> info`): the `docker` CLI of Docker Desktop, OrbStack, Colima or Rancher Desktop (dockerd), or Podman. `auto` (the default) uses the first running one; `none` means no containers (`magehand run -- CMD` still works for apps whose blocks are only `llm` and `secret`) |
| `magehand login` | Signs in to OpenBao through authentik (passkey) with role `dev`: read-only access to agent apps' LiteLLM keys, 1h (8h max). The token is kept in the macOS Keychain |

**This machine**

| Command | What it does |
| --- | --- |
| `magehand setup [--dns ADDRESS] [--undo]` | Once per machine: GitHub, the container runtime, `*.lab` name resolution (the only step that may ask for your password; without a terminal, pass `--dns`), sign-in, the skill |
| `magehand doctor [--json]` | Checks GitHub access, that `openbao.lab` is reachable and the sign-in; shows the container runtime (missing is fine unless the app has database blocks) |
| `magehand upgrade` | Upgrades magehand the way it was installed (`uv tool upgrade`, `pipx upgrade` or pip in its venv) and refreshes the skill if you installed it. Never automatic: you run it when a command tells you (stderr, at most once a day, never in CI or scripts) that a newer version is out. In OpenClaw's VM it refuses: that copy is pinned in homelab `platform/openclaw/vm/versions.env` |
| `magehand skill [--install]` | The `homelab-app` skill, which tells Claude Code and Codex to use `app`, `guide search` and `check` while working on app code. `setup` installs it (`$CLAUDE_CONFIG_DIR` or `~/.claude`, and `$CODEX_HOME` or `~/.codex` if Codex is installed) |
| `magehand version`, `magehand -V` | The installed version, and whether a newer one exists |

**Everywhere:** `-a/--app APP` names the app (default: the app repo you're
in); `--json` prints JSON for scripts and agents; exit status 0 is success,
1 a failure (or `check` errors), 2 a usage error (with a "did you mean"),
130 Ctrl-C. Messages go to stderr, results to stdout.

**Settings**, highest first: flags, then environment variables, then
`~/.config/magehand/config.json` (written by `magehand runtime`).

| Variable | Effect |
| --- | --- |
| `MAGEHAND_RUNTIME` | `docker`, `podman` or `none` (overridden by `--runtime`) |
| `MAGEHAND_NO_UPDATE_CHECK` | Set: no new-version notice |
| `MAGEHAND_APPS_DIR` | Read this homelab-apps checkout instead of GitHub (`check --apps-dir` sets it) |
| `MAGEHAND_PROXY_PORT` | The identity proxy's port (default 8080) |
| `GITHUB_TOKEN`, `GH_TOKEN` | GitHub access (else `gh auth token`) |

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
