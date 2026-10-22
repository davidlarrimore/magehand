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
Upgrade with `magehand upgrade` (any command says, at most once a day, when a new version is out). After upgrading, run `magehand update-agents` in each app repo to bring in the template's current `AGENTS.md` rules.

## Commands

`magehand --help` lists them; `magehand COMMAND --help` (or `magehand help
COMMAND`) explains one, with examples.

**Start an app**

| Command | What it does |
| --- | --- |
| `magehand new REPO [--app-name APP] [--no-clone] [--dir DIR] [--json]` | Creates `davidlarrimore/REPO` from `homelab-app-template` (private) with `gh`, as you, and invites OpenClaw's bot `davidlarrimore-bot` with write; OpenClaw accepts by itself. The app is named after the repo unless `--app-name APP` says otherwise (`new lab-website --app-name lab`: hostnames, namespace and image use `lab`): it then commits `deploy/app.yaml` with `app: APP` to the new repo's `main` (before the ruleset exists; an existing `deploy/app.yaml` is never rewritten, only reported). It then clones the repo into `./REPO` (`--dir DIR`, or `--no-clone`; an existing non-empty directory is left alone) and prints two lists: what is left for you, and what the platform does by itself. Registration needs no step: the platform registers the app by itself when `deploy/` (with `base/`) is merged to `main`. It also adds the `main: owner approves` ruleset (a PR needs one approval of its latest push; the owner can bypass it as admin; a refusal only warns). Safe to re-run, and the repair for a repo made without it (`magehand doctor` says when one is): on an existing repo it invites the bot if it is missing, renews a pending invitation, raises a read-only bot to write, adds the ruleset if missing, warns if the repo is public, clones nothing when run in the repo's own checkout, leaves an archived repo (a retired app) alone, and prints only what is left for you (making the repo private, waiting for OpenClaw to accept and requesting its review again, fixing a weaker ruleset), or that nothing is. It first waits (about a minute) for GitHub's asynchronous copy of the template to land (`AGENTS.md`, `.github/workflows/build.yaml`), because a commit or the ruleset on `main` before then makes the copy fail: if the files never appear it stops with nothing else changed (exit 1; re-run), and on an existing repo that lacks them it says how to recover (`gh repo delete` and re-run when nothing is in it, else restore them through a PR); if GitHub can't be asked (rate limit, authorization, network) it says so and changes nothing, rather than calling the repo broken. One of its writes, and to GitHub, never the homelab's configuration |

**Learn the platform and check an app**

| Command | What it does |
| --- | --- |
| `magehand guide [TOPIC ["SECTION"]]` | The app platform guide, live from homelab-apps `docs/platform/`: `magehand guide` (overview), `guide recipes`, `guide blocks "Web search"` (one section), `guide catalog` (blocks and models) |
| `magehand guide search WORDS` | The guide sections (`docs/platform/` and homelab-apps `AGENTS.md`) that best answer a question, e.g. `magehand guide search web search` |
| `magehand update-agents [--check] [--code DIR] [--json]` | Updates this repo's `AGENTS.md` from `homelab-app-template`: everything above the `## This app` heading is replaced with the template's current text; the heading and the app's own notes below it are kept. Writes only that file (commit it yourself). `--check` writes nothing and exits 1 when it is out of date. Refuses a file with no `## This app` heading |
| `magehand app [APP] [--json]` | The app's blocks, every env var its pod gets and where it comes from, its URLs. In an app repo with `deploy/app.yaml` they come from the checkout's `deploy/` (the app's deployment config, which merging copies into homelab-apps); otherwise from homelab-apps `apps/<app>/`. Says when the app isn't registered on the platform yet, or an `llm` block isn't deployed yet: until then it has no LLM key |
| `magehand status [APP] [--watch] [--timeout MIN] [--no-cluster] [--json]` | Where the latest `main` commit of the app has got to, from GitHub (through `gh`) and, when `kubectl` has a context, the cluster: the image build the platform deploys (the Build workflow's pull_request or workflow_dispatch run for the commit, or for the merged PR when it has main's exact tree; the push run is only the security scan), the deploy PR `homelab app deploy-dev` opens in homelab-apps, and the owner's latest published `vX.Y.Z` release (as the platform picks it) with its prod deploy PR and the commits on `main` since. A GitHub query that fails is reported as an error (exit 1), never as "nothing yet". With `kubectl`, the dev line reads the pods (and the Argo application) with read-only `kubectl get` and says whether they run the image the deploy PR pinned, are ready, and are synced and healthy; a pod that can't start fails the stage. Without it (or with `--no-cluster`) it only says the deploy merged. `--watch` waits until build, the dev deploy and (when readable) the pods are done; exit 1 on a failed stage or when `--timeout` (default 20 min) passes When Argo CD cannot read the app's repo (it is not in the GitHub App installation) the dev stage fails and says how to fix it; any other render error shows as pending with Argo's message. |
| `magehand release [APP] [--major\|--minor\|--patch] [--dry-run] [--confirm] [--no-cluster] [--json]` | Cuts the app's production release: reads the merged commits on `main` since the owner's last `vX.Y.Z` release, picks the next version (Conventional Commit types, `!` or a `BREAKING CHANGE:` footer, and blocks or models added or removed in `deploy/app.yaml`; with no signal it says the patch is a guess; `--major`/`--minor`/`--patch` override it), and drafts notes grouped by kind with PR links. It refuses (exit 1) unless `gh` is signed in as the owner, the app is in homelab's production register (`platform/prod-apps/apps.yaml`), `deploy/prod/` exists, main's checks are green and its security scan ran and passed, its image is built and scanned, the dev deploy is done and dev's pods run it (unreadable pods only warn), (your ruleset already enforces review at merge). When every check passes it creates the tag and release as you through `gh` without asking: `--confirm` asks first, `--dry-run` creates nothing (`--yes` is accepted and no longer needed). The platform then deploys the release; follow it with `status`. Another of its writes, and to GitHub, never the homelab's configuration |
| `magehand job add NAME --schedule CRON [--timezone ZONE] [--timeout DURATION] [--memory SIZE] [--json] -- COMMAND...`, `job rm NAME`, `job ls [--no-cluster] [--json]` | Scheduled work for the app whose repo you are in: a job is the app's own image run with another command on a schedule, with the Deployment's env (its blocks and keys), in a fresh pod that exits when the command does. `add` writes `deploy/base/job-NAME.yaml`, a CronJob with what the platform needs (`concurrencyPolicy: Forbid`, a start deadline, the timeout (default 10m), one retry, three runs kept, a memory limit (default 256Mi, at most 1Gi), the schedule in `--timezone` (default `Etc/UTC`)), pods labelled so the app's Service never selects them, and lists it in `deploy/base/kustomization.yaml` (keeping its comments); `rm` removes both. Merging deploys it to dev with the rest of `deploy/`; failed and missed runs alert the owner by themselves. `ls` lists the checkout's jobs and, with read-only `kubectl get`, dev's CronJobs and their last runs (newest first, with how long each took) and the `kubectl logs` command for the last one (`--no-cluster` skips the read). It writes only files in the checkout. Make the command safe to run twice, exit non-zero on failure and write nothing then |
| `magehand check [APP] [--code DIR] [--apps-dir DIR] [--manifests-only] [--json]` | The platform's rules for the app's code and its deployment (`deploy/` in the checkout when it has one, else homelab-apps): block keys and options, `optional: true` keys, Secrets nothing makes, the image, files in `deploy/` a merge won't deploy (it holds `app.yaml`, `base/`, and the overlays `dev/` and `prod/`, whose namespaces must be `<app>-dev` and `<app>`; once `base/` exists, a missing `dev/kustomization.yaml` is an error, as in CI: nothing registers or deploys without it), env the code reads but the pod doesn't set, models the code names that its llm block doesn't request (warnings), secrets in the committed `.magehand.env`, direct AI/search provider calls, own login, database files, schedulers inside the app's process (warnings). Each CronJob in `deploy/` must have `concurrencyPolicy: Forbid`, a start deadline, a timeout and `backoffLimit` 0 to 2, run the app's own image, map only Secrets that exist, carry pod labels the app's Service doesn't select and, in `base/`, be listed in its kustomization; a bare Job is a warning. An `app:` in `deploy/app.yaml` must be a valid app name that homelab-apps hasn't registered to another repo, and a repo registered under another name must keep saying so (checked against `--apps-dir` in CI). Also warns when the repo's `AGENTS.md` platform rules are behind `homelab-app-template` (`magehand update-agents` fixes it; skipped offline). Also warns when the checked-out branch's PR was already merged and the branch has new commits since (a squash merge keeps other hashes, so `git log main..` still lists the old ones): start a fresh branch from `origin/main` (skipped on `main`, on a detached HEAD, with an open PR for the branch, and without `gh`). Each finding names the guide section; exit 1 on errors. `--manifests-only` skips the code rules. Run before every push; CI runs it too (`--apps-dir`) For an app whose `apps/<app>/app.yaml` in homelab-apps pins a commit of its own repo (`source: {repo, ref, image}`), `app` and `check` read its `deploy/base` from that repo at the ref, and `check --apps-dir` on a homelab-apps checkout reports that the manifests are in the app repo and checked by its build (exit 0). |

**Run an app locally**

| Command | What it does |
| --- | --- |
| `magehand run [-a APP] [--no-proxy] -- CMD` | Runs CMD with the env the app's pod gets: values from its Deployment, the LLM key from OpenBao, databases from `dev up`. Precedence: manifest < the repo's `.magehand.env` (committed, non-secret overrides such as `WEB_DIR=./web`) < your shell < secrets. A proxy on `127.0.0.1:8080` adds the `X-authentik-*` headers Traefik would, as you. With no key in OpenBao it says why (app not registered, block not deployed yet, key being created). In an app's repo, `-a` can't name another app: that would run this code on the other app's keys and budget |
| `magehand run --container [--no-build] [-- CMD]` | Builds the repo's Dockerfile and runs the image as its pod runs: read-only, `/tmp` tmpfs, the pod's user, no capabilities, exactly the pod's env. Secrets go in as `-e NAME` only, never in the image, argv or on disk |
| `magehand bucket [--env local\|dev\|prod] ls\|cp\|sync\|url\|usage ...` | Read and write the buckets of the app whose repo you are in (nothing else; `--app` may only name that app) with the keys the platform hands the app: `ls BUCKET[/PREFIX]`, `cp FILE BUCKET/KEY` (and back), `sync DIR BUCKET/PREFIX` (uploads what changed, never deletes), `url BUCKET/KEY` (a public bucket), `usage [BUCKET]` (objects and bytes against the 1 GB cap). `dev` (default) and `prod` read the keys from OpenBao (`magehand login`); `local` uses the Garage `dev up` started; an `internal` bucket is only reachable with `--env local` or from code in the cluster. Uploads set `Content-Type` and can set `Cache-Control`. **A write to an app's own bucket** (`cp`, `sync`; with `--env prod` it is production data), on an explicit command, never a delete; its S3 endpoint (R2, or the local Garage) is the one network destination besides GitHub and `*.lab`. `BUCKET/KEY` is always the remote side; write a local path that starts with a bucket name as `./PATH`. `--json` prints one object for every action |
| `magehand dev [up\|down\|status] [APP]` | Containers for the app's `postgres`/`redis`/`bucket` blocks (the blocks' own pinned images; a bucket is the platform's Garage with one 1 GB bucket and a key limited to it, its data in a volume that survives a restart) on a per-app network, and, for `secret` blocks, a value made for each run (never stored; set the variable in your shell to keep one stable across runs). A state file from an older magehand is cleaned of its passwords and generated values the first time it is read. No secret is written to magehand's state file: a database's password is its container's own environment, read back when a run needs it. A public bucket's local `url` is the S3 path and needs the key. `down` deletes the local data |
| `magehand runtime [docker\|podman\|none\|auto] [--json]` | Shows or sets the container runtime for `dev` and `run --container`. Detected without sudo (`<cli> info`): the `docker` CLI of Docker Desktop, OrbStack, Colima or Rancher Desktop (dockerd), or Podman. `auto` (the default) uses the first running one; `none` means no containers (`magehand run -- CMD` still works for apps whose blocks are only `llm` and `secret`) |
| `magehand login [--check [--min N]] [--json]` | Signs in to OpenBao through authentik (passkey) with role `dev`: read-only access to agent apps' LiteLLM keys, 1h (8h max). The token is kept in the macOS Keychain (passed on stdin, never as an argument), elsewhere in a file only you can read; `MAGEHAND_BAO_TOKEN` still works but is deprecated. `doctor`, `app` and `run` show the time left (a warning under 10 minutes); an expired session in a terminal offers to sign in again and carries on. `--check` only reports the time left and exits 1 when signed out or under N minutes (default 10), so an agent can ask you to sign in before a long task |

**This machine**

| Command | What it does |
| --- | --- |
| `magehand setup [--dns ADDRESS] [--undo]` | Once per machine: GitHub, the container runtime, `*.lab` name resolution (the only step that may ask for your password; without a terminal, pass `--dns`), sign-in, the skill |
| `magehand doctor [--json]` | Checks GitHub access, that `openbao.lab` is reachable and the sign-in; in an app's repo, also that OpenClaw's bot can write to it (a read-only or absent bot fails: `gh pr edit --add-reviewer` then exits 0 and adds nobody; a pending invitation is a note), that the repo is private, and that it has the approval ruleset (an archived repo, a retired app, is not checked), active, covering the default branch and requiring an approval (`magehand new REPO`, run in the checkout, invites the bot and adds a missing ruleset but never changes an existing, weaker one: fix that in Settings > Rules; the ruleset and invitations are only readable by an admin, and anything unreadable is a note, not a failure); shows the container runtime (missing is fine unless the app has database blocks) |
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
