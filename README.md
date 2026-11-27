<p align="center">
  <img src="https://raw.githubusercontent.com/davidlarrimore/magehand/main/assets/logo/logo-1024.png" alt="magehand" width="360">
</p>

# magehand

The developer CLI for building apps on [davidlarrimore](https://github.com/davidlarrimore)'s
homelab, the same way on the owner's MacBook (Claude Code) and in OpenClaw's
VM (its coding workers). It reads platform state and runs apps locally. It
never deploys: changes go through pull requests.

It is public because it holds nothing sensitive. Every command needs access
that only the owner has: the private homelab repos on GitHub, the private
`*.lab` network, and an authentik passkey for
OpenBao.

## What magehand needs

magehand is a client for the homelab, not a standalone tool. It reads GitHub,
the homelab's `*.lab` services and the git checkout you run it in, and can do
little without them. `magehand setup` checks most of this once per machine;
`magehand doctor` checks it again at the start of a session.

| It needs | For | Without it |
| --- | --- | --- |
| **git**, and the app's checkout: `origin` is `github.com/davidlarrimore/REPO` | knowing which app you are working on ([below](#which-app-a-command-acts-on)) | name the app (`magehand status APP`); `bucket`, `job`, `fetch` and `icon` refuse |
| **GitHub as the owner**: `gh` signed in (`gh auth login`), or `GITHUB_TOKEN` / `GH_TOKEN`. With a token and no `gh`, magehand reads GitHub's API itself (`status`, `release --dry-run`, `app`, `check`, `doctor` work); `new`, `pr ready`, `pr watch` and `release` need `gh` | nearly everything: the guide, homelab-apps, the app's repo, PRs and releases are private | almost nothing (`setup` and `doctor` say what is missing) |
| **The homelab's network**: `*.lab.davidlarrimore.com` resolving to it (`magehand setup` sends only those lookups to your home gateway) and reachable | OpenBao (the app's keys) and the services a local run calls (LiteLLM) | `login`, `run`, `bucket --env dev\|prod` fail; the GitHub-only commands work |
| **An authentik account in `homelab-admins` with a passkey**, a browser, and local port 8250 free for the sign-in's callback | `magehand login`: the app's LLM and bucket keys | `run` (for an app with an `llm` block) and `bucket --env dev\|prod` can't get their keys |
| **A container runtime**: Docker Desktop, OrbStack, Colima, Rancher Desktop or Podman | `dev`, `run --container`, `check --ui` | those refuse; `run -- CMD` still works for an app whose blocks are only `llm` and `secret` |
| **`uv`** (`brew install uv`), or Python 3.10+ on PATH, and **npm** in a repo with JavaScript/TypeScript | `verify` and the pre-push hook: pre-commit needs Python 3.10+ (magehand itself runs on 3.9), and the platform's ESLint is installed with npm | `verify` stops and says what to install |
| **kubectl** with a context for the cluster (optional) | `status` and `job ls` read the pods; `check` renders the overlays | they read what the platform publishes on GitHub; `check` skips the render with a note |
| **kyverno** and **kubeconform** (optional: `brew install kyverno kubeconform`) | `check` runs the cluster's admission policies and schema check, as CI does | skipped with a note (an error with `--strict`) |
| **The macOS Keychain** (`security`), on a Mac | keeping the sign-in | elsewhere, a file only you can read |
| **Python 3.9+** and uv (or pipx) | installing and upgrading magehand | |

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
reverses that; when the names resolve but macOS's Local Network privacy blocks magehand's Python (Homebrew's or uv's: Apple's own is exempt), it reinstalls magehand on Apple's `/usr/bin/python3` with uv and carries on, or, without the Xcode Command Line Tools, names the setting to change), then signs you in and installs the `homelab-app` skill for Claude Code (and Codex). Last, it
pre-approves commands for Claude Code and Codex from one list, so their routine commands need no prompt. That covers
magehand's read-only commands, `magehand pr ready` and `pr watch`, and `gh`'s read-only `pr`, `run`, `issue` and `repo` commands.
`gh pr merge`, `gh release create|edit|delete|upload`, `gh repo delete` and `git push --tags` are refused (an agent
releases with `magehand release`, when you ask it to, and that prompts). Anything
else prompts as before. For Codex this is a rules file, `rules/magehand.rules` under `$CODEX_HOME` or `~/.codex`,
which `codex execpolicy check` must accept, or it isn't kept (setup then fails and says to update Codex; `doctor` notes it). For Claude Code it is a plugin that loads by itself,
`skills/magehand-approvals/` under `$CLAUDE_CONFIG_DIR` or `~/.claude`, whose PreToolUse hook applies the same list.
It allows a chain of plain commands (`cd app && magehand check`) only when every part is on the list, and never one
with a redirect, `$(…)` or an assignment. `setup --undo` removes them. This is fewer prompts, not a boundary: the
ruleset on `main`, where only your approval merges, is the boundary. Every other command assumes setup is done.
Upgrade with `magehand upgrade` (any command says, at most once a day, when a new version is out): it then runs the
new version's setup quietly, which refreshes the skill and the pre-approved commands and says if `magehand setup`
needs you. (An upgrade from 0.38 or earlier is run by the old version, which doesn't do this: run `magehand
setup` once after it; `magehand doctor` says when the pre-approved commands aren't installed.) In an app repo, `magehand check` says when `AGENTS.md`'s platform rules are out of date; `magehand new
REPO` or `magehand update-agents` refreshes them.

## Commands

`magehand --help` lists them; `magehand help COMMAND` explains one in full,
with examples, and is where the detail lives. `update-agents`, `skill`, `version` and
`help` are named only on its `also:` line: you rarely need them.

In OpenClaw's VM (the copy the platform installs and pins), `run`, `dev`,
`bucket`, `login` and `setup` can't work: it has no container runtime, no
route to OpenBao and no passkey. They exit 1 with why and what does the job
instead (CI's tests and browser check, or the owner). `doctor` there checks
only GitHub and the repo, and `app` shows no sign-in line. Nothing OpenClaw
does needs the sign-in: the platform gives apps their keys. The VM has no `gh`
by design: magehand reads GitHub there with the bot's `GITHUB_TOKEN`, so
`status`, `release --dry-run`, `app` and `check` work as on a Mac.

### Which app a command acts on

magehand works out the app from the git checkout you run it in (from any
directory inside it), never from a setting or a file of its own:

1. The checkout is an **app checkout** when its top has `ui-check.yaml` or
   `deploy/app.yaml` and its `origin` is `github.com/davidlarrimore/REPO`
   (https, `ssh://` or `git@`). Another owner's repo of the same name, a
   look-alike host or a checkout with no `origin` is not one.
2. The app is `deploy/app.yaml`'s `app:` when it sets a valid name, else REPO.

**In an app checkout, every app command acts on that app only.** `app`,
`status`, `check`, `release`, `dev`, `run`, `bucket`, `job`, `fetch` and
`icon` refuse another app's name (exit 1), so nothing run in one app's
directory reaches another app's keys, data, release or deployment. (`check`
also takes the repo's own name, for a repo whose `app:` names its app
differently.) To work on another app, run the command in that app's checkout,
or outside any app checkout.

**Outside an app checkout** (`homelab`, `homelab-apps`, any other directory)
name the app: `magehand status elvis`. `bucket`, `job`, `fetch` and `icon`
work only in the app's checkout, because they work on its files or its data.

**Platform commands** involve no app and work anywhere: `guide`, `setup`,
`doctor`, `upgrade`, `login`, `runtime`, `skill`, `version`, `help`, `new`,
`pr` (the PR of the repo you are in) and `update-agents` (the checkout it is
given).

The sign-in is not per app: one `magehand login` per machine covers every
app, read-only, for LLM and bucket keys. The scoping is magehand's own rule:
it stops a mistake made in the wrong directory, yours or a coding agent's. It
is not a boundary against a program running as you that reads the Keychain
itself.

Every command that leads somewhere ends with one line, `next: ...`: the one
thing to do next (`next: nothing` when there is nothing), and its `--json`
has it as `"next"`. Only commands whose output is the result itself (`guide`,
`run`, `bucket`, `runtime`, `login`, `skill`, `version`, `help`) don't.

**Start an app**

| Command | What it does |
| --- | --- |
| `magehand new REPO` | Create an app's repo for the platform, or repair one |
| `magehand icon set FILE / icon generate` | Set the app's icon (its tile in the app library) |

**Learn the platform and check an app**

| Command | What it does |
| --- | --- |
| `magehand guide [TOPIC ["SECTION"]] / guide search WORDS` | Read the platform guide (homelab-apps `docs/platform/`): a topic, one section, or the sections that best answer a question |
| `magehand app [APP]` | The app's blocks, the env vars its pod gets and where each comes from, its URLs |
| `magehand status [APP] [--watch]` | Where the app is: set up, registered, built, on dev and prod |
| `magehand check [APP] [--ui]` | Check the app against the platform's rules, `build.yaml` exactly the template's included (before every push; `--ui` runs CI's browser check) |
| `magehand verify` | Run the platform's static checks on what you'll push, as CI does |
| `magehand api PATH [--prod]` | Call the app's API on dev or production, as you |

**Ship an app**

| Command | What it does |
| --- | --- |
| `magehand spec check N / spec verify N` | Ask OpenClaw whether an issue's spec is buildable, or whether a PR meets it |
| `magehand pr ready [N] / pr watch [N]` | Mark a green PR ready and request OpenClaw's review, then follow it until it needs you or is done (findings, a failed check, or a verify verdict for its head that isn't `met`) |
| `magehand pr answer [N] [K]` | List the findings of OpenClaw's latest review (answered ones marked), or answer finding K with a PR comment (`-m TEXT`, `--body-file FILE`): the condition of the review's bar it fails and the fact that shows it, or the issue it's parked in. Then `pr ready` again: OpenClaw judges the answers on the same head, with no push and no CI run |
| `magehand release [APP]` | Cut the app's production release (the next `vX.Y.Z`), only when the owner asks |
| `magehand job add / rm / ls` | Add, remove and list the app's scheduled jobs (CronJobs) |
| `magehand fetch init / fetch rm` | Scaffold the app's fetch zone (fetching URLs nobody vouches for) |

**Run an app locally**

| Command | What it does |
| --- | --- |
| `magehand run -- CMD / run --container` | Run a command, or the app's image, with its pod's env |
| `magehand dev up / down / status` | Local containers for the app's postgres, redis and bucket blocks |
| `magehand bucket ls / cp / sync / url / usage` | Read and write the app's object storage |
| `magehand runtime` | Show or set the container runtime |
| `magehand login` | Sign in to OpenBao and app APIs with your passkey |

**This machine**

| Command | What it does |
| --- | --- |
| `magehand setup` | Set this machine up (once) |
| `magehand doctor` | Check this machine, the sign-in and this app repo's setup |
| `magehand hooks install / status / remove` | The pre-push hook that runs `verify` |
| `magehand upgrade` | Upgrade magehand, its skill and approvals |
| `magehand rules` | Write the platform's working rules for agents (`magehand guide workflow`) into this app checkout as `.magehand/workflow.md`, which its CLAUDE.md imports; kept out of Git. `status`, `check`, `verify`, `new` and a new Claude Code session do it too; run it before a headless agent run on a fresh clone |
| `magehand update-agents, skill, version, help` | Rarely needed: refresh AGENTS.md from the template (and CLAUDE.md's import of the rules), print the skill, the version, help |

**Everywhere:** `-a/--app APP` names the app (default: the app repo you're
in; in an app's repo it can only name that app, see "Which app a command acts
on"); `--json` prints JSON for scripts and agents; exit status 0 is success,
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
| `MAGEHAND_AGENT_TOKEN` | A lab agent's token file (the homelab's builder or Assistant): `api` calls dev apps as that agent; `--prod`, `release` and `login` are refused (a developer's access to dev only) |
| `MAGEHAND_AGENT_LLM_KEY` | A lab agent's own model key file: `run` gives the app that key and the gateway in place of the app's own key |
| `MAGEHAND_LAB_ADDRESS` | Connect `*.lab` names to this address (an agent's VM, where they resolve to 127.0.0.1 publicly); the host name and TLS check stay the real ones |
| `GITHUB_TOKEN`, `GH_TOKEN` | GitHub access (else `gh auth token`). Without `gh` installed, magehand reads GitHub's API with it directly; `new`, `pr ready`, `pr watch` and `release` still need `gh` |

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
