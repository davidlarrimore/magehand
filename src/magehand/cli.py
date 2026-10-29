"""magehand: build homelab apps from anywhere, the same way everywhere.

The developer CLI for apps on the homelab, used by the owner (Claude Code on
the MacBook) and by OpenClaw's coding workers. `magehand --help` lists the
commands; `magehand COMMAND --help` explains each, with examples.

Its writes are `magehand new` (an app's repo on GitHub) and `magehand release` (a tag and release on an
app's repo), both as the owner, and `magehand pr ready` (a PR marked ready and its review requested), as
whoever gh is signed in as.

Private by design: it talks only to GitHub and the private *.lab network,
never to a public homelab endpoint. Everyday commands assume they can
connect; `magehand setup` checks that once per machine and, if needed, points
this Mac's *.lab lookups at the homelab's DNS. Its OpenBao role `dev` can
only read the LiteLLM keys of agent apps (homelab
platform/openbao/policies/dev-read.hcl).
"""
import argparse
import base64
import datetime
import difflib
import errno
import fcntl
import hashlib
import mimetypes
import hmac
import http.client
import http.server
import json
import os
import pathlib
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess  # nosec B404  # runs docker, git, gh and the user's own command
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET  # nosec B405  # parses only S3 responses from the bucket's own endpoint
from importlib.metadata import PackageNotFoundError, version as package_version

import yaml

HOMELAB_REPO = 'davidlarrimore/homelab'
APPS_REPO = 'davidlarrimore/homelab-apps'
OPENBAO = os.environ.get('MAGEHAND_OPENBAO', 'https://openbao.lab.davidlarrimore.com')
LAB = 'lab.davidlarrimore.com'
HOME = pathlib.Path.home()
STATE = pathlib.Path(os.environ.get('XDG_STATE_HOME', HOME / '.local/state')) / 'magehand'
CACHE = pathlib.Path(os.environ.get('XDG_CACHE_HOME', HOME / '.cache')) / 'magehand'
KEYCHAIN_SERVICE = 'magehand'
CALLBACK = 'http://localhost:8250/oidc/callback'
PROXY_PORT = int(os.environ.get('MAGEHAND_PROXY_PORT', '8080'))
# Block type -> the Secret its chart hands over (platform/building-blocks/*).
NOT_SET_UP = "this machine isn't set up for the homelab: run `magehand setup`"
# macOS 15+ Local Network privacy: a program that isn't Apple's (Homebrew's or uv's Python) gets the permission
# of the app it runs under (iTerm, Terminal, VS Code), and a refused connection fails as "No route to host",
# which reads like DNS. Apple's own programs (its Python among them) are exempt, so setup moves magehand onto it.
LOCAL_NETWORK = ("macOS's Local Network privacy is keeping magehand's Python off your home network (Apple's own "
                 "curl gets through)")
LOCAL_NETWORK_PERMISSION = ('allow the app you run magehand in (iTerm, Terminal, VS Code...) in System Settings > '
                            'Privacy & Security > Local Network, then quit and reopen that app')
APPLE_PYTHON = '/usr/bin/python3'  # with the Xcode Command Line Tools; without them it only offers to install them
APPLE_PREFIXES = ('/usr/bin/', '/System/', '/Library/Developer/CommandLineTools/', '/Applications/Xcode')
RESOLVER = pathlib.Path('/etc/resolver') / LAB
SECRET_NAME = {'postgres': '{}-connection', 'redis': '{}-connection', 'llm': '{}-connection', 'secret': '{}',
               'bucket': '{}-connection'}
# Local object storage for `bucket` blocks: the same Garage the platform runs for `internal`
# buckets (one node, in a container, one bucket and key per block), so code written against it
# works in the cluster. Pinned by digest; Garage's admin API sets it up (the image has no shell).
GARAGE_IMAGE = ('dxflrs/garage:v2.4.1@sha256:'
                '9c96caa2612d3411acc5b0e6701fb238dbfba33e533a6d7d3d811a4b12d0d020')
GARAGE_CONFIG = '''metadata_dir = "/data/meta"
data_dir = "/data/data"
db_engine = "sqlite"
replication_factor = 1
rpc_bind_addr = "127.0.0.1:3901"
rpc_public_addr = "127.0.0.1:3901"

[s3_api]
s3_region = "garage"
api_bind_addr = "[::]:3900"

[admin]
api_bind_addr = "[::]:3903"
'''
GARAGE_QUOTA = 1_000_000_000   # the platform's per-bucket cap, so a local run hits it too
# A homelab-apps checkout to read instead of GitHub (CI has one; `magehand check --apps-dir`).
APPS_DIR = os.environ.get('MAGEHAND_APPS_DIR')


def die(msg):
    sys.exit(f'magehand: {msg}')


def next_line(step):
    """The last line of a command's human output: the one thing to do next, so neither a person nor an agent
    has to work it out ('nothing' when there is nothing to do). --json carries the same as "next" (null)."""
    return f'next: {step or "nothing"}'


# --- GitHub (read-only: the guide, app manifests, block images) ---------------

def github_token():
    token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
    if token:
        return token
    if shutil.which('gh'):
        out = subprocess.run(['gh', 'auth', 'token'], capture_output=True, text=True)  # nosec B603 B607
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    die('no GitHub access: run `gh auth login` (or set GITHUB_TOKEN)')


def github_file(repo, path, required=True, ref='main'):
    """A file on main (or at `ref`), cached for offline use; None if it doesn't exist."""
    if repo == APPS_REPO and APPS_DIR:
        local = pathlib.Path(APPS_DIR) / path
        if local.is_file():
            return local.read_text()
        if required:
            die(f'{local} not found')
        return None
    cached = CACHE / repo / (path if ref == 'main' else f'@{ref}/{path}')
    req = urllib.request.Request(
        f'https://api.github.com/repos/{repo}/contents/{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref)}',
        headers={'Authorization': f'Bearer {github_token()}', 'Accept': 'application/vnd.github.raw',
                 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'magehand'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310  # fixed https URL
            text = resp.read().decode()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            if required:
                die(f'{repo}: {path} not found at {ref}')
            return None
        text = None
    except OSError:
        text = None
    if text is None:
        if cached.is_file():
            print(f'magehand: GitHub unreachable, using the cached {path}', file=sys.stderr)
            return cached.read_text()
        die(f'cannot read {repo}/{path} from GitHub and nothing is cached')
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(text)
    return text


def github_dir(repo, path, ref='main'):
    """Names in a directory on main (or at `ref`) ([] if it doesn't exist)."""
    if repo == APPS_REPO and APPS_DIR:
        local = pathlib.Path(APPS_DIR) / path
        return sorted(child.name for child in local.iterdir()) if local.is_dir() else []
    req = urllib.request.Request(
        f'https://api.github.com/repos/{repo}/contents/{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref)}',
        headers={'Authorization': f'Bearer {github_token()}', 'Accept': 'application/vnd.github+json',
                 'User-Agent': 'magehand'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310  # fixed https URL
            return sorted(item['name'] for item in json.load(resp) if isinstance(item, dict))
    except (urllib.error.HTTPError, OSError, ValueError):
        return []


def github_listing(repo, path, ref='main'):
    """(names, None) in a directory on main through gh, as the rest of status reads GitHub; ([], None) when it
    doesn't exist (HTTP 404); (None, why) when it couldn't be read. github_dir() reports both as empty."""
    code, out, err = gh_probe('api', f'repos/{repo}/contents/{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref)}')
    if code != 0:
        return ([], None) if code is not None and 'HTTP 404' in err else (None, err or 'gh failed')
    try:
        got = json.loads(out or '[]')
    except ValueError:
        return None, 'unreadable answer from GitHub'
    return (sorted(item['name'] for item in got if isinstance(item, dict)), None) if isinstance(got, list) else ([], None)


def registration_lookup(name):
    """(spec, None) of the app's homelab-apps apps/<name>/app.yaml on main; (None, None) when it isn't registered
    (HTTP 404); (None, why) when it couldn't be read. registration() exits on that, which status can't afford."""
    if APPS_DIR:
        local = pathlib.Path(APPS_DIR) / 'apps' / name / 'app.yaml'
        if not local.is_file():
            return None, None
        out = local.read_text()
    else:
        code, out, err = gh_probe('api', '-H', 'Accept: application/vnd.github.raw',
                                  f'repos/{APPS_REPO}/contents/apps/{name}/app.yaml')
        if code != 0:
            if code is None or 'HTTP 404' not in err:
                return None, err or 'gh failed'
            # GitHub answers 404 for a repo this account can't see, too: "not registered" only if it can
            seen, _, why = gh_probe('api', f'repos/{APPS_REPO}', '--jq', '.full_name')
            return (None, None) if seen == 0 else (None, f'cannot see {APPS_REPO} ({why or "gh failed"})')
    try:
        spec = yaml.safe_load(out) or {}
    except yaml.YAMLError:
        return None, f'apps/{name}/app.yaml is not valid YAML'
    return (spec, None) if isinstance(spec, dict) else (None, f'apps/{name}/app.yaml is not a mapping')


# --- The app ------------------------------------------------------------------

def current_app(root=None):
    """The app this checkout is: its deploy/app.yaml `app:` if it sets one, else the repo's name."""
    if remote_repo(root) is None:
        die('not in a git checkout; pass the app name')
    name = checkout_app(root)
    if not name:
        die('this is a platform repo; pass the app name')
    return name


# An app repo with deploy/app.yaml keeps its deployment config there:
# deploy/app.yaml (its block requests) and deploy/base/ (its Kustomize base).
# Merging copies them into homelab-apps apps/<app>/ (homelab app deploy-dev).
DEPLOY = 'deploy'
DEPLOY_DIRS = ('base', 'dev', 'prod')


def deploy_dir(root):
    """root/deploy if the checkout at `root` keeps its deployment config there, else None."""
    path = pathlib.Path(root) / DEPLOY
    return path if (path / 'app.yaml').is_file() else None


def repo_root(name):
    """The top of this git checkout if it is app `name`'s repo, else None."""
    top = checkout_top()
    return top if top and checkout_app() == name else None


class PinnedApp(Exception):
    """The app's deployment is not in homelab-apps: apps/<name>/app.yaml pins a commit of its own repo, and
    this homelab-apps checkout (--apps-dir) has no copy of the manifests to read."""

    def __init__(self, name, pin):
        super().__init__(name)
        self.name, self.pin = name, pin


def pinned_source(spec):
    """The `source: {repo, ref, image}` of a pinned app.yaml (docs/pinned-refs-plan.md), or None."""
    source = (spec or {}).get('source')
    return source if isinstance(source, dict) and source.get('ref') else None


def load_app(name, root=None):
    """The app's block requests and pod spec: from `root`/deploy/ when that checkout has one, else from
    homelab-apps apps/<name>/. `where` says which."""
    local = deploy_dir(root) if root else None
    pinned = None
    if local:
        where = DEPLOY
        spec = yaml.safe_load((local / 'app.yaml').read_text()) or {}
        files = [(f'{DEPLOY}/base/{f.name}', f.read_text()) for f in sorted((local / 'base').glob('*.y*ml'))]
    else:
        where = f'homelab-apps apps/{name}'
        spec = registration(name)
        if spec is None:
            die(f'{name} has no deployment yet: no deploy/app.yaml in this checkout and not registered on the platform '
                f'(no homelab-apps apps/{name}/). Write deploy/app.yaml and deploy/base/ (`magehand guide search '
                'deploy`); the platform registers the app by itself when they are merged to main')
        pin = pinned_source(spec)
        if pin:
            if APPS_DIR:  # a homelab-apps checkout holds only the pin; the app repo's build checks the manifests
                raise PinnedApp(name, pin)
            repo = f"{OWNER}/{pin.get('repo') or name}"
            ref = str(pin['ref'])
            where = f'{repo} deploy/ at {ref[:7]}'
            names = [n for n in github_dir(repo, 'deploy/base', ref) if n.endswith(('.yaml', '.yml'))]
            if not names:
                die(f'{name} is pinned to {repo} at {ref[:7]}, which has no deploy/base/*.yaml')
            files = [(f'{DEPLOY}/base/{n}', github_file(repo, f'deploy/base/{n}', ref=ref)) for n in names]
            pinned = pin
        else:
            files = [(f'{where}/base/deployment.yaml', github_file(APPS_REPO, f'apps/{name}/base/deployment.yaml'))]
    found = []
    for path, text in files:
        try:
            found += [(path, d) for d in yaml.safe_load_all(text) if isinstance(d, dict) and d.get('kind') == 'Deployment']
        except yaml.YAMLError as error:
            die(f'{path} is not valid YAML: {str(error).splitlines()[0] if str(error) else error}')
    if not found:
        die(f'{where}/base has no Deployment')
    path, deployment = found[0]
    pod = deployment['spec']['template']['spec']
    return {'name': name, 'spec': spec, 'services': spec.get('services') or {}, 'container': pod['containers'][0],
            'pod': pod, 'where': where, 'deployment_file': path, 'root': local.parent if local else None,
            'pin': pinned, 'selector': ((deployment['spec'].get('selector') or {}).get('matchLabels') or {})}


def app_here(name=None):
    """load_app for `name` (default: this checkout's app), from this checkout's deploy/ when it is that app's repo."""
    name = name or current_app()
    return load_app(name, repo_root(name))


def registration(name):
    """The app's spec as deployed (homelab-apps apps/<name>/app.yaml on main), or None if it isn't registered."""
    text = github_file(APPS_REPO, f'apps/{name}/app.yaml', required=False)
    return None if text is None else (yaml.safe_load(text) or {})


def llm_key_state(name, block, deployed):
    """Where block's dev key stands, from the deployed spec (None: not registered): 'unregistered',
    'undeployed' (the block isn't on main yet) or 'deployed' (the key exists or is being created)."""
    if deployed is None:
        return 'unregistered'
    return 'deployed' if ((deployed.get('services') or {}).get(block) or {}).get('type') == 'llm' else 'undeployed'


def no_llm_key(name, block, path, status):
    """Why OpenBao has no dev key at `path`, and what to do instead of borrowing another app's."""
    state = llm_key_state(name, block, registration(name))
    if state == 'unregistered':
        why = (f'{name} isn\'t registered on the platform yet (no homelab-apps apps/{name}/), so it has no dev key. '
               'It registers by itself when deploy/ (app.yaml and base/) is merged to main, within about 5 '
               'minutes. Until then run without the LLM (mock it, as the tests do)')
    elif state == 'undeployed':
        why = (f'block {block} is in deploy/app.yaml but not deployed yet: its key is created when the change is '
               'merged and dev deploys (`magehand app` shows the state). Until then mock the LLM')
    else:
        why = (f'block {block} is deployed, but the key broker hasn\'t created its key yet (about 2 minutes after the '
               'deploy); try again shortly')
    return (f'cannot read {path} (HTTP {status}): {why}. Never use another app\'s key: its budget and models '
            'are that app\'s (`magehand guide local-dev`)')


def git_out(args, root=None):
    """Stdout of `git [-C root] ARGS` (stripped), or None if git fails."""
    cmd = ['git', *(['-C', str(root)] if root else []), *args]
    out = subprocess.run(cmd, capture_output=True, text=True)  # nosec B603 B607
    return out.stdout.strip() if out.returncode == 0 else None


def remote_repo(root=None):
    """The repo name (no owner, no .git) of the checkout's origin remote, or None without one."""
    url = git_out(['remote', 'get-url', 'origin'], root)
    if not url:
        return None
    name = url.rstrip('/').rsplit('/', 1)[-1].rsplit(':', 1)[-1]
    return name[:-4] if name.endswith('.git') else name


def origin_repo(root):
    """OWNER/NAME of the GitHub repo that the checkout at `root` has as origin, or None (another host, none)."""
    url = git_out(['remote', 'get-url', 'origin'], root)
    # github.com exactly, in each form git accepts: https, ssh:// and scp-style (never a look-alike host or a path)
    m = re.match(r'(?:https://(?:[^@/]+@)?github\.com/|ssh://git@github\.com(?::22)?/|git@github\.com:)'
                 r'([\w.-]+)/([\w.-]+?)(?:\.git)?/?$', url or '')
    return f'{m.group(1)}/{m.group(2)}' if m else None


def checkout_top(root=None):
    """The top directory of the git checkout at `root` (default: here), or None outside one."""
    top = git_out(['rev-parse', '--show-toplevel'], root)
    return pathlib.Path(top) if top else None


def app_override(root):
    """deploy/app.yaml's optional top-level `app:` (a string) in the checkout at `root`, or None. It names
    the app when its repo is named differently (`magehand new REPO --app-name APP`)."""
    local = deploy_dir(root) if root else None
    if not local:
        return None
    try:
        spec = yaml.safe_load((local / 'app.yaml').read_text())
    except (OSError, yaml.YAMLError):
        return None
    value = spec.get('app') if isinstance(spec, dict) else None
    return value if isinstance(value, str) else None


def checkout_app(root=None):
    """The app whose repo this checkout is: deploy/app.yaml's `app:` when it sets a valid one, else the
    repo's name. None outside a git checkout and in the platform repos."""
    repo = remote_repo(root)
    if repo is None or repo in ('homelab', 'homelab-apps'):
        return None
    top = checkout_top(root)
    override = app_override(top) if top else None
    return override if override and APP_NAME.match(override) else repo


def app_repo_here():
    """The app whose code this checkout is (an app repo has ui-check.yaml or deploy/app.yaml), or None."""
    top = subprocess.run(['git', 'rev-parse', '--show-toplevel'], capture_output=True, text=True)  # nosec B603 B607
    if top.returncode != 0:
        return None
    root = pathlib.Path(top.stdout.strip())
    return checkout_app() if (root / 'ui-check.yaml').is_file() or deploy_dir(root) else None


def block_of(app, secret_name):
    """(block name, type) whose Secret this is, or (None, None)."""
    for name, svc in app['services'].items():
        kind = (svc or {}).get('type')
        if kind in SECRET_NAME and SECRET_NAME[kind].format(name) == secret_name:
            return name, kind
    return None, None


def env_plan(app):
    """[(VAR, 'value', text) | (VAR, block, type, key)] from the pod spec."""
    plan = []
    for item in app['container'].get('env') or []:
        if 'value' in item:
            plan.append((item['name'], 'value', str(item['value'])))
            continue
        ref = ((item.get('valueFrom') or {}).get('secretKeyRef')) or {}
        block, kind = block_of(app, ref.get('name'))
        plan.append((item['name'], block, kind, ref.get('key')) if block
                    else (item['name'], 'unknown', ref.get('name') or '?'))
    return plan


def app_port(app):
    for item in app['container'].get('env') or []:
        if item.get('name') == 'PORT' and 'value' in item:
            return int(item['value'])
    ports = app['container'].get('ports') or []
    return int(ports[0]['containerPort']) if ports else 8000


# --- OpenBao (role dev: read-only LiteLLM keys) ---------------------------------

def bao(method, path, token=None, body=None, soft=False):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['X-Vault-Token'] = token
    req = urllib.request.Request(f'{OPENBAO}/v1/{path}', method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310  # OpenBao URL (https, *.lab)
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as error:
        return error.code, None
    except OSError as error:
        if soft:  # an optional lookup: the caller reports it as unknown
            return 0, None
        die(f'cannot reach {OPENBAO}: ' + (f'{LOCAL_NETWORK}: {local_network_step()}' if local_network_blocked(error)
                                            else NOT_SET_UP))


TOKEN_CHARS = re.compile(r'^[A-Za-z0-9._~+/=-]+$')  # what an OpenBao token is made of
_warned_env = []


def keychain_add(token):
    """Put the token in the macOS Keychain without it ever being an argument: `security -i` reads the
    command from stdin, so `ps` never shows the token."""
    if not TOKEN_CHARS.match(token):
        die('OpenBao returned a token in a form magehand will not store; sign in again: magehand login')
    command = f'add-generic-password -U -s {KEYCHAIN_SERVICE} -a openbao-token -w {token}\n'
    subprocess.run(['security', '-i'], input=command, text=True,  # nosec B603 B607
                   check=True, capture_output=True)


def store_token(token, identity):
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE.chmod(0o700)  # an older release made it world-readable
    if sys.platform == 'darwin':
        keychain_add(token)
    else:
        # No credential store in the standard library: a file only its owner can read, never created wider.
        path = STATE / 'openbao-token'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(token)
    (STATE / 'identity.json').write_text(json.dumps(identity))


def saved_token():
    if os.environ.get('MAGEHAND_BAO_TOKEN'):
        if not _warned_env:  # still honoured (it was released), but a secret in the environment is exposed
            _warned_env.append(True)
            print('magehand: MAGEHAND_BAO_TOKEN is deprecated and will be ignored in 1.0; '
                  'unset it and sign in with `magehand login`', file=sys.stderr)
        return os.environ['MAGEHAND_BAO_TOKEN']
    if sys.platform == 'darwin':
        out = subprocess.run(['security', 'find-generic-password', '-s', KEYCHAIN_SERVICE,  # nosec B603 B607
                              '-a', 'openbao-token', '-w'], capture_output=True, text=True)
        return out.stdout.strip() if out.returncode == 0 else None
    path = STATE / 'openbao-token'
    return path.read_text().strip() if path.is_file() else None


SIGNIN_WARN = 600  # seconds: warn when the session has less than this left
NO_EXPIRY = 10 ** 9
UNREACHABLE = -1


def signin_left(token=None, soft=False):
    """Seconds left on the OpenBao session, or None when not signed in (no token, or it expired). With soft,
    UNREACHABLE when OpenBao can't be reached, instead of exiting (for informational output)."""
    token = token or saved_token()
    if not token:
        return None
    status, body = bao('GET', 'auth/token/lookup-self', token, soft=soft)
    if soft and status == 0:
        return UNREACHABLE
    if status != 200:
        return None
    ttl = ((body or {}).get('data') or {}).get('ttl')
    return ttl if isinstance(ttl, int) and ttl > 0 else NO_EXPIRY  # ttl 0: the token never expires


def minutes(seconds):
    if seconds >= NO_EXPIRY:
        return 'no expiry'
    return 'under 1 min' if seconds < 60 else f'{seconds // 60} min'


def valid_token():
    """The session token. Reports the time left (a warning under 10 minutes); an expired session in a terminal
    offers to sign in again and carries on, otherwise it says how to."""
    token = saved_token()
    left = signin_left(token)
    if left is None and sys.stdin.isatty() and sys.stderr.isatty():
        answer = input('magehand: not signed in, or the session expired. Sign in again now? [Y/n] ').strip().lower()
        if answer in ('', 'y', 'yes'):
            token = cmd_login(None)  # use the fresh token: a stale MAGEHAND_BAO_TOKEN would win in saved_token()
            left = signin_left(token)
    if left is None:
        die('not signed in, or the token expired: run `magehand login`')
    note = f'magehand: signed in, {minutes(left)} left'
    if left < SIGNIN_WARN:
        note += '; `magehand login` renews it before a long task'
    print(note, file=sys.stderr)
    return token


def identity():
    path = STATE / 'identity.json'
    return json.loads(path.read_text()) if path.is_file() else {}


def login_check(a):
    left = signin_left()
    need = a.min * 60
    ok = left is not None and left >= need
    if a.json:
        print(json.dumps({'signed_in': left is not None, 'seconds_left': left or 0, 'min_minutes': a.min, 'ok': ok}))
    elif left is None:
        print('magehand: not signed in: run `magehand login`', file=sys.stderr)
    else:
        print(f'signed in, {minutes(left)} left' + ('' if ok else f' (less than {a.min} min): run `magehand login`'))
    sys.exit(0 if ok else 1)


def cmd_login(args):
    """Sign in through authentik; returns the new token (kept in the Keychain, never printed)."""
    if getattr(args, 'check', False):
        login_check(args)
    as_json = getattr(args, 'json', False)
    status, body = bao('POST', 'auth/oidc/oidc/auth_url', body={'role': 'dev', 'redirect_uri': CALLBACK})
    url = ((body or {}).get('data') or {}).get('auth_url')
    if status != 200 or not url:
        die(f'OpenBao gave no login URL (HTTP {status}); is the `dev` OIDC role set up? (scripts/setup-openbao.py)')
    params = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != '/oidc/callback':
                self.send_response(404)
                self.end_headers()
                return
            params.update(urllib.parse.parse_qsl(parsed.query))
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'magehand: signed in. Return to the terminal.')

        def log_message(self, *args):
            pass

    with http.server.HTTPServer(('localhost', 8250), Callback) as server:
        server.timeout = 5
        print('Sign in through authentik in your browser (passkey)...', file=sys.stderr)
        webbrowser.open(url)
        deadline = time.monotonic() + 300
        while 'code' not in params and time.monotonic() < deadline:
            server.handle_request()
    if 'code' not in params:
        die('sign-in timed out or was refused')
    query = urllib.parse.urlencode({k: params[k] for k in ('state', 'code') if k in params})
    status, body = bao('GET', f'auth/oidc/oidc/callback?{query}')
    if status != 200:
        die(f'OpenBao refused the sign-in (HTTP {status})')
    auth = body['auth']
    meta = auth.get('metadata') or {}
    email = meta.get('email') or auth.get('display_name', '').removeprefix('oidc-')
    who = {'email': email, 'uid': auth.get('entity_id', ''), 'username': email.split('@')[0],
           'name': meta.get('name') or email.split('@')[0], 'groups': 'homelab-admins'}
    store_token(auth['client_token'], who)
    lease = auth.get('lease_duration', 3600)
    if as_json:
        print(json.dumps({'signed_in': True, 'email': email, 'policies': auth.get('policies', []),
                          'seconds_left': lease}))
    else:
        print(f'Signed in as {email or "?"} (policy {", ".join(auth.get("policies", []))}, {lease // 3600}h).')
    return auth['client_token']


# --- guide and app --------------------------------------------------------------

STOP_WORDS = {'the', 'and', 'for', 'how', 'can', 'what', 'with', 'app', 'apps', 'does', 'into', 'from', 'this',
              'that', 'use', 'get', 'add', 'make', 'need', 'want', 'should', 'when', 'why', 'are', 'not', 'you'}


def sections(text):
    """[(heading, body)] of a Markdown page, split at its ## and ### headings (the intro is "")."""
    out, heading, lines = [], '', []
    for line in text.splitlines():
        if re.match(r'#{2,3} ', line):
            out.append((heading, '\n'.join(lines).strip()))
            heading, lines = line.lstrip('#').strip(), []
        else:
            lines.append(line)
    out.append((heading, '\n'.join(lines).strip()))
    return [(h, b) for h, b in out if b]


def search_sections(pages, query, limit=3):
    """The best-matching sections [(score, page, heading, body)] for a free-text query."""
    terms = [t for t in re.findall(r'[a-z0-9_]+', query.lower()) if len(t) > 2 and t not in STOP_WORDS]
    scored = []
    for page, text in pages.items():
        for heading, body in sections(text):
            head, low = heading.lower(), body.lower()
            hits = [t for t in terms if t in head or t in low]
            if hits:
                score = len(hits) * 10 + sum(3 * head.count(t) + min(low.count(t), 5) for t in hits)
                scored.append((score, page, heading, body))
    return sorted(scored, key=lambda s: -s[0])[:limit]


def guide_pages(topics):
    pages = {t: github_file(APPS_REPO, f'docs/platform/{t}.md') for t in topics}
    pages['homelab-apps AGENTS.md'] = github_file(APPS_REPO, 'AGENTS.md')
    return pages


def cmd_guide(a):
    args = ([a.topic] if a.topic else []) + a.rest
    topics = [n[:-3] for n in github_dir(APPS_REPO, 'docs/platform') if n.endswith('.md')]
    if not topics:  # before docs/platform exists
        print(github_file(APPS_REPO, 'AGENTS.md'))
        return
    topic = args[0] if args else 'README'
    if topic == 'search':
        if len(args) < 2:
            usage('guide', 'search needs words, e.g. magehand guide search web search')
        hits = search_sections(guide_pages(topics), ' '.join(args[1:]))
        if not hits:
            die('nothing in the guide matches; try other words, `magehand guide` for the topics, or ask the owner '
                '(and say in your PR what you looked for)')
        for _, page, heading, body in hits:
            print(f'=== {page}' + (f', "{heading}"' if heading else '') + ' ===')
            lines = body.splitlines()
            print('\n'.join(lines[:60]) + (f'\n[... {len(lines) - 60} more lines: magehand guide {page} '
                                             f'"{heading}"]' if len(lines) > 60 else ''))
            print()
        return
    if topic == 'catalog':
        print(github_file(APPS_REPO, 'catalog.yaml'))
        return
    if topic not in topics:
        close = difflib.get_close_matches(topic, topics + ['catalog', 'search'], n=1)
        usage('guide', f'no topic {topic!r}' + (f"; did you mean '{close[0]}'?" if close else '')
              + f' (topics: {", ".join(topics)}, catalog; or: magehand guide search WORDS)')
    text = github_file(APPS_REPO, f'docs/platform/{topic}.md')
    if len(args) > 1:  # one section: magehand guide blocks "Web search"
        want = ' '.join(args[1:]).lower()
        found = [(h, b) for h, b in sections(text) if h.lower() == want] or \
                [(h, b) for h, b in sections(text) if want in h.lower()]
        if not found:
            die(f'no section {want!r} in {topic}; sections: {", ".join(h for h, _ in sections(text) if h)}')
        print(f'## {found[0][0]}\n\n{found[0][1]}')
        return
    print(text)
    if not args:
        print(f'\nMore: magehand guide <topic> ["section"]  ({", ".join(t for t in topics if t != "README")}, '
              'catalog), or magehand guide search WORDS')


def cmd_app(a):
    app = app_here(a.app_flag or a.app)
    name = app['name']
    source = (app['spec'].get('source') or {}).get('repo') or (remote_repo(app['root']) if app['root'] else None)
    deployed = registration(name) if app['root'] else app['spec']
    keys = {block: llm_key_state(name, block, deployed)
            for block, svc in app['services'].items() if (svc or {}).get('type') == 'llm'}
    left = None if install_method() == 'managed' else signin_left(soft=True)
    step = ('merge deploy/ (app.yaml, base/, dev/) to main; the platform then registers the app '
            '(`magehand status` follows it)' if deployed is None else None)
    if a.json:
        env = []
        for entry in env_plan(app):
            if entry[1] == 'value':
                env.append({'name': entry[0], 'value': entry[2]})
            elif entry[1] == 'unknown':
                env.append({'name': entry[0], 'secret': entry[2]})
            else:
                env.append({'name': entry[0], 'block': entry[1], 'type': entry[2], 'key': entry[3]})
        print(json.dumps({'name': name, 'code': f'davidlarrimore/{source}' if source or app['root'] else f'homelab-apps apps/{name}/image',
                          'deployment': app['where'], 'dev_url': f'https://{name}-dev.{LAB}',
                          'port': app_port(app), 'registered': deployed is not None, 'blocks': app['services'],
                          'llm_keys': keys, 'env': env,
                          'signin_seconds_left': left if left != UNREACHABLE else None, 'next': step}, indent=1))
        return
    deployment = ('deploy/ in this repo (merging copies it to homelab-apps apps/%s)' % name if app['root']
                  else app['where'])
    print(f'{name}  (code: {"davidlarrimore/" + source if source else "homelab-apps apps/" + name + "/image"};'
          f' deployment: {deployment})')
    print(f'  dev:      https://{name}-dev.{LAB}')
    if install_method() == 'managed':
        pass  # OpenClaw's VM: no sign-in to report, and nothing it does needs one
    elif left == UNREACHABLE:
        print(f'  sign-in:  unknown ({OPENBAO} is not reachable)')
    elif left is None:
        print('  sign-in:  not signed in (`magehand login`; run needs it for LLM keys)')
    else:
        print(f'  sign-in:  {minutes(left)} left' + ('  (`magehand login` renews it)' if left < SIGNIN_WARN else ''))
    if deployed is None:
        print(f'  not registered yet (no homelab-apps apps/{name}/): no dev deploy and no LLM key until deploy/ '
              'is merged to main (the platform then registers it, within about 5 minutes)')
    for preview in github_dir(APPS_REPO, f'previews/{name}'):
        if preview.endswith('.yaml'):
            print(f'  preview:  https://{name}-dev-{preview[:-5]}.{LAB}')
    print('Blocks (app.yaml services:):')
    if not app['services']:
        print('  none; request one in ' + ('deploy/app.yaml' if app['root'] else 'homelab-apps apps/%s/app.yaml' % name)
              + ' (`magehand guide blocks`)')
    for block, svc in app['services'].items():
        opts = ', '.join(f'{k}={v}' for k, v in (svc or {}).items() if k != 'type')
        state = {'undeployed': '  not deployed yet: no key until merged', 'unregistered': '  no key: app not registered'}
        print(f'  {block}: {(svc or {}).get("type")}{"  (" + opts + ")" if opts else ""}{state.get(keys.get(block), "")}')
        if (svc or {}).get('type') == 'bucket':
            for line in bucket_summary(name, block, svc):
                print('      ' + line)
    print(f'Env (the pod spec; locally: magehand run, app port {app_port(app)}):')
    for entry in env_plan(app):
        if entry[1] == 'value':
            print(f'  {entry[0]} = {entry[2]}')
        elif entry[1] == 'unknown':
            print(f'  {entry[0]} <- Secret {entry[2]} (not a block; magehand run leaves it unset)')
        else:
            print(f'  {entry[0]} <- block {entry[1]} ({entry[2]}) key {entry[3]}')
    print(next_line(step))


# --- status: where a merge to main has got to ---------------------------------------
# Everything comes from GitHub through gh (read-only), following the platform's own rules (homelab
# `homelab app deploy-dev` / `deploy-prod`): the image is the Build workflow's pull_request or
# workflow_dispatch run for main's commit (or for the merged PR's head when it has main's exact tree: a
# push to main only runs the security scan); dev's deploy PR is deploy-dev/<app> marked with the commit;
# prod follows the owner's latest published vX.Y.Z release and its deploy-prod/<app> PR.

BUILD_WORKFLOW = '.github/workflows/build.yaml'
BUILD_EVENTS = ('pull_request', 'workflow_dispatch')
RELEASE_TAG = re.compile(r'^v\d+(\.\d+){0,2}$')
DEPLOY_MARK = re.compile(r'<!-- homelab deploy-dev (\w+) (\S+)')
PROD_TITLE = re.compile(r': release (\S+) to production$')
STATUS_MARK = {'ok': '✓', 'pending': '…', 'failed': '✗', 'unknown': '?', 'error': '!'}


def gh_query(*args):
    """(parsed JSON, None) of `gh ARGS`, or (None, why) when gh fails (no access, rate limit, offline): a failed
    query is not an empty result."""
    code, out, err = gh_probe(*args)
    if code != 0:
        return None, err or 'gh failed'
    try:
        return json.loads(out), None
    except ValueError:
        return None, 'gh returned something that is not JSON'


def clock(stamp):
    """HH:MMZ of an ISO-8601 UTC time from GitHub."""
    return (stamp or '')[11:16] + 'Z'


def seconds_between(start, end):
    fmt = '%Y-%m-%dT%H:%M:%SZ'
    try:
        return int((datetime.datetime.strptime(end, fmt) - datetime.datetime.strptime(start, fmt)).total_seconds())
    except (TypeError, ValueError):
        return None


def query_error(stage, what, why):
    return {'stage': stage, 'state': 'error',
            'text': f'could not read {what}: {why} (`gh auth status`; is this one of your repos?)'}


def build_stage(runs, head):
    """The image build of `head`, from the app repo's Build runs (API workflow_runs; those for main's commit
    and for the merged PR's head with main's tree). Mirrors the controller: any success is built, else a
    running one is building, else the newest failed one failed."""
    builds = [r for r in runs if r.get('path') == BUILD_WORKFLOW and r.get('event') in BUILD_EVENTS]
    newest = lambda rs: max(rs, key=lambda r: r.get('created_at') or '', default=None)  # noqa: E731
    done = newest([r for r in builds if r.get('status') == 'completed' and r.get('conclusion') == 'success'])
    if done:
        took = seconds_between(done.get('run_started_at') or done.get('created_at'), done.get('updated_at'))
        span = f' {took // 60}m{took % 60:02d}s' if took is not None else ''
        why = ('built for the PR, reused: main has its exact tree' if done['event'] == 'pull_request'
               else 'built by a dispatched build of main')
        return {'stage': 'build', 'state': 'ok', 'text': f'image built{span} ({why})'}
    running = newest([r for r in builds if r.get('status') != 'completed'])
    if running:
        return {'stage': 'build', 'state': 'pending', 'text': f'building: {running.get("html_url", "")}'}
    failed = newest(builds)
    if failed:
        return {'stage': 'build', 'state': 'failed',
                'text': f'the image build of {head[:7]} failed ({failed["event"]}): {failed.get("html_url", "")}'}
    return {'stage': 'build', 'state': 'pending',
            'text': f'no image build of {head[:7]} yet (the platform starts one when no PR build has main\'s tree)'}


def failing_checks(rollup):
    """Names of the finished entries of a PR's statusCheckRollup that aren't a pass (not_green_checks without
    the result): CheckRuns (conclusion, name) and StatusContexts (state, context) both appear in it."""
    return [entry.rsplit(' (', 1)[0] for entry in not_green_checks(rollup)]


# A finished check is a pass only with one of these conclusions (GitHub's CheckConclusionState): anything
# else, ACTION_REQUIRED and STALE included, or one GitHub adds later, is not green.
GREEN_CONCLUSIONS = ('SUCCESS', 'NEUTRAL', 'SKIPPED')


def not_green_checks(rollup):
    """'name (result)' for each finished entry of a PR's statusCheckRollup that isn't a pass: a CheckRun whose
    conclusion isn't in GREEN_CONCLUSIONS, a StatusContext in FAILURE or ERROR."""
    out = []
    for c in rollup or []:
        name = c.get('name') or c.get('context') or '?'
        if c.get('state') in ('FAILURE', 'ERROR'):
            out.append(f'{name} ({c["state"].lower()})')
        elif (c.get('conclusion') or c.get('status') == 'COMPLETED') and c.get('conclusion') not in GREEN_CONCLUSIONS:
            out.append(f'{name} ({(c.get("conclusion") or "no conclusion").lower()})')
    return out


def pending_checks(rollup):
    """Names of the entries of a PR's statusCheckRollup that haven't finished: CheckRuns not COMPLETED and
    StatusContexts still PENDING or EXPECTED."""
    return [c.get('name') or c.get('context') or '?' for c in rollup or []
            if (c.get('status') and c.get('status') != 'COMPLETED') or c.get('state') in ('PENDING', 'EXPECTED')]


def deploy_marks(prs):
    """[(pr, commit sha, image)] for the deploy PRs whose body carries the deploy-dev marker."""
    out = []
    for pr in prs or []:
        m = DEPLOY_MARK.search(pr.get('body') or '')
        if m:
            out.append((pr, m.group(1), m.group(2)))
    return out


def deploy_stage(prs, head):
    """Dev's deploy PR (newest first) for the commit `head`."""
    for pr, sha, image in deploy_marks(prs):
        if sha != head:
            continue
        ref = f'homelab-apps#{pr["number"]}'
        digest = image.rsplit('@', 1)[-1][:19]
        if pr.get('state') == 'MERGED':
            return {'stage': 'deploy', 'state': 'ok', 'text': f'dev: {ref} merged {clock(pr.get("mergedAt"))}',
                    'image': image, 'pr': pr['number'], 'digest': digest}
        if pr.get('state') == 'CLOSED':
            return {'stage': 'deploy', 'state': 'failed', 'text': f'dev: {ref} was closed without merging'}
        bad = failing_checks(pr.get('statusCheckRollup'))
        if bad:
            return {'stage': 'deploy', 'state': 'failed', 'text': f'dev: {ref} has failing checks: {", ".join(bad)}'}
        return {'stage': 'deploy', 'state': 'pending', 'text': f'dev: {ref} open, it merges itself once its checks pass'}
    return {'stage': 'deploy', 'state': 'pending',
            'text': f'dev: no deploy PR for {head[:7]} yet (opened after the build, within a few minutes)'}


def eligible_release(releases):
    """The owner's newest published release vX.Y.Z (drafts, prereleases, other tags and other authors'
    releases never deploy production), or None. `releases` is GitHub's list, newest first."""
    for r in releases or []:
        if r.get('draft') or r.get('prerelease') or not RELEASE_TAG.match(r.get('tag_name') or ''):
            continue
        if (r.get('author') or {}).get('login') == OWNER:
            return r
    return None


def prod_running(prod, recorded):
    """A merged production deploy (`prod`, an ok prod stage) with what the platform's GitHub Deployment for the
    release's commit says (`recorded`, platform_deployment's answer): running, failed, or still rolling out.
    Without one, the merge is all there is to show."""
    if not recorded or prod.get('state') != 'ok':
        return prod
    since = prod['text'].split(' deployed', 1)[1]
    if recorded['state'] == 'success':
        return {**prod, 'text': f'{prod["tag"]} running (the platform saw every pod Ready, '
                                f'{clock(recorded["at"])}){since}'}
    if recorded['state'] == 'failure':
        return {**prod, 'state': 'failed',
                'text': f'{prod["tag"]}: {recorded["description"] or "the rollout failed"} (the platform, '
                        f'{clock(recorded["at"])}){since}'}
    if recorded['state'] in ('in_progress', 'queued', 'pending'):
        return {**prod, 'state': 'pending', 'text': f'{prod["tag"]} rolling out{since}'}
    return prod


def prod_stage(tag, ahead, prs):
    """Prod: the latest eligible release, whether its deploy PR merged, and the commits on main since."""
    if not tag:
        return {'stage': 'prod', 'state': 'unknown',
                'text': "no release yet (the owner's GitHub Release vX.Y.Z deploys to prod)", 'released': False}
    since = f'  ({ahead} commit{"" if ahead == 1 else "s"} on main since {tag})' if ahead else ''
    mine = [p for p in prs or [] if (PROD_TITLE.search(p.get('title') or '') or [None, None])[1] == tag]
    if mine and mine[0].get('state') == 'MERGED':
        return {'stage': 'prod', 'state': 'ok', 'text': f'{tag} deployed{since}', 'tag': tag, 'ahead': ahead, 'released': True}
    if mine and mine[0].get('state') == 'OPEN':
        return {'stage': 'prod', 'state': 'pending', 'text': f'{tag}: homelab-apps#{mine[0]["number"]} is open{since}',
                'tag': tag, 'ahead': ahead, 'released': True}
    return {'stage': 'prod', 'state': 'unknown', 'text': f'{tag} released; its deploy PR was not found{since}',
            'tag': tag, 'ahead': ahead, 'released': True}


BLOCK_LABEL = 'homelab.davidlarrimore.com/block'  # on a building block's pods (postgres, redis): not the app's image

STUCK_PODS = ('CrashLoopBackOff', 'ImagePullBackOff', 'ErrImagePull', 'CreateContainerConfigError',
              'InvalidImageName', 'RunContainerError')


def kubectl_json(*args):
    """(parsed JSON, None) of a read-only `kubectl get ARGS -o json`, or (None, why): no kubectl, no context,
    no access. The one call magehand makes to the cluster, and it only reads."""
    if not shutil.which('kubectl'):
        return None, 'kubectl is not installed'
    try:
        out = subprocess.run(['kubectl', 'get', *args, '-o', 'json', '--request-timeout=10s'],  # nosec B603 B607
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None, 'kubectl did not answer'
    if out.returncode != 0:
        return None, out.stderr.strip().splitlines()[-1] if out.stderr.strip() else 'kubectl failed'
    try:
        return json.loads(out.stdout), None
    except ValueError:
        return None, 'kubectl returned something that is not JSON'


def pod_stage(pods, application, image, argo_error=None):
    """What dev's pods run: `pods` and `application` are kubectl's JSON (application may be None), `image` the
    deploy PR's pinned ref. ok when every live app pod is ready on that digest and every building-block pod
    (postgres, redis: they run their own images) is ready (and Argo reports synced and healthy), failed when
    a pod can't start, otherwise a rollout in progress. Without the
    application (`argo_error`: why) pods that are ready are 'unknown', never a verified rollout."""
    digest = image.rsplit('@', 1)[-1]
    # A CronJob's run (a pod owned by a Job) is not the app's rollout: it may run the previous image until it ends.
    live = [p for p in (pods or {}).get('items') or []
            if not (p.get('metadata') or {}).get('deletionTimestamp')
            and (p.get('status') or {}).get('phase') not in ('Succeeded', 'Failed')
            and not any(o.get('kind') == 'Job' for o in (p.get('metadata') or {}).get('ownerReferences') or [])]
    if not any(BLOCK_LABEL not in ((p.get('metadata') or {}).get('labels') or {}) for p in live):
        return {'stage': 'dev', 'state': 'pending', 'text': f'no pods yet for {digest[:19]}'}
    ready = stuck = 0
    for pod in live:
        block = BLOCK_LABEL in ((pod.get('metadata') or {}).get('labels') or {})
        statuses = (pod.get('status') or {}).get('containerStatuses') or []
        declared = {c.get('name'): c.get('image') or '' for c in (pod.get('spec') or {}).get('containers') or []}

        def ident(c):
            """The image a container runs, or (not pulled yet: no imageID) is declared to run."""
            return c.get('imageID') or c.get('image') or declared.get(c.get('name'), '')
        on_new = bool(statuses) and (block or all(digest in ident(c) for c in statuses))
        if on_new and all(c.get('ready') for c in statuses):
            ready += 1
        for c in statuses:
            reason = ((c.get('state') or {}).get('waiting') or {}).get('reason')
            if reason in STUCK_PODS and (block or digest in ident(c)):
                stuck += 1
                why = f'{pod["metadata"]["name"]}: {reason}'
    if stuck:
        return {'stage': 'dev', 'state': 'failed',
                'text': f'{why}; `kubectl logs` and `kubectl describe pod` in the app\'s dev namespace say why'}
    argo = ((application or {}).get('status') or {})
    sync, health = (argo.get('sync') or {}).get('status'), (argo.get('health') or {}).get('status')
    if ready == len(live) and application is None:
        return {'stage': 'dev', 'state': 'unknown',
                'text': f'{ready}/{len(live)} pods ready on {digest[:19]}; Argo not checked '
                        f'({argo_error or "no application"})'}
    if ready == len(live) and (sync, health) == ('Synced', 'Healthy'):
        return {'stage': 'dev', 'state': 'ok',
                'text': f'running {digest[:19]} ({ready}/{len(live)} pods ready, Argo synced/healthy)'}
    return {'stage': 'dev', 'state': 'pending',
            'text': f'rolling out {digest[:19]}: {ready}/{len(live)} pods ready on it'
                    + (f', Argo {sync}/{health}' if application else '')}


ARGO_REPO_ACCESS = re.compile(r'repository not found|authentication (required|failed)|could not read username'
                              r'|remote: not found|invalid username or password', re.IGNORECASE)


def repo_names(url):
    """The spellings Argo's errors use for a repo URL: owner/name and name, without .git."""
    path = re.sub(r'^(https://[^/]+/|git@[^:]+:|ssh://[^/]+/)', '', str(url or '')).removesuffix('.git')
    return {n for n in (path, path.rsplit('/', 1)[-1]) if n}


def argo_problem(application):
    """What Argo CD says stops it rendering or syncing the app (a stage dict without the name), or None. A
    refused Git read of the app's repo (not in the GitHub App installation) is a failure with the fix; it has
    to be a Git transport error that is not Kustomize's and not about another source (the platform repo is
    read over SSH). Any other comparison error may clear by itself, so it is only pending (and shown)."""
    for condition in ((application or {}).get('status') or {}).get('conditions') or []:
        if condition.get('type') not in ('ComparisonError', 'InvalidSpecError'):
            continue
        message = ' '.join(str(condition.get('message') or '').split())
        spec = application.get('spec') or {}
        sources = spec.get('sources') or [spec.get('source') or {}]
        repo = next((str(s.get('repoURL')) for s in sources if 'github.com/' in str(s.get('repoURL')) and
                     str(s.get('repoURL')).startswith('https://')), '')
        shown = repo.rsplit('/', 1)[-1].removesuffix('.git') if repo else 'the app repo'
        lower = message.lower()
        # Which source failed: Argo's "source N of M" when it says, else the app repo must be named in the
        # message when there are several sources (the platform repo is another, read over SSH). Ambiguous
        # errors stay pending.
        numbered = re.search(r'source (\d+) of (\d+)', lower)
        if numbered:
            index = int(numbered.group(1)) - 1
            ours = 0 <= index < len(sources) and str(sources[index].get('repoURL')) == repo
        elif len(sources) > 1:
            ours = any(n.lower() in lower for n in repo_names(repo) if '/' in n)
        else:
            ours = bool(repo)
        about_other = not ours
        if ARGO_REPO_ACCESS.search(message) and 'kustomize' not in lower and not about_other:
            return {'state': 'failed',
                    'text': f'Argo CD cannot read {shown}: add it to the GitHub App installation it reads app repos '
                            f'through (GitHub > Settings > Applications > Configure > Repository access). '
                            f'Argo says: {message[:160]}'}
        return {'state': 'pending', 'text': f'Argo CD cannot render the app yet: {message[:200]}'}
    return None


# kubectl's answers when this machine has no cluster to ask (a laptop): not a problem, so said plainly.
NO_CLUSTER = re.compile(r'kubectl is not installed|localhost:8080|no configuration has been provided|'
                        r'current-context is not set|context was not found|kubectl has no context', re.I)
# Where dev's job runs can be read when neither kubectl nor the platform's status issue has them.
RUNS_ELSEWHERE = ("for dev's job runs, ask OpenClaw on Telegram (it reads dev), or run `magehand job ls` where kubectl "
                  'reaches the cluster; failed and missed runs alert the owner either way')
# The platform's copy of what only the cluster knows (homelab `homelab app publish-status`, every 5 minutes):
# one bot-owned issue per app repo, its JSON (kubectl's CronJobs, Jobs and Pods, trimmed) in an HTML comment.
PLATFORM_STATUS_TITLE = 'Platform status'
PLATFORM_STATUS_MARK = 'homelab-platform-status'
PLATFORM_STATUS_STALE = 2 * 3600  # it is rewritten at least hourly: older than this, the publisher has stopped


def platform_status(repo):
    """(the published status {'as_of', 'envs': {env: items}}, None) from the app repo's Platform status issue,
    or (None, why not). Read through gh, as the rest of magehand reads GitHub."""
    code, out, err = gh_probe('api', f'repos/{repo}/issues?state=all&creator={BOT}&per_page=100')
    if code != 0:
        return None, f'cannot read {repo}\'s issues ({err or "gh failed"})'
    try:
        issue = next((i for i in json.loads(out) if i.get('title') == PLATFORM_STATUS_TITLE and not i.get('pull_request')), None)
    except ValueError:
        return None, 'gh returned something that is not JSON'
    if not issue:
        return None, f'the platform has not published {repo}\'s status yet'
    m = re.search(rf'<!-- {PLATFORM_STATUS_MARK}\n(.*?)\n-->', issue.get('body') or '', re.S)
    try:
        data = json.loads(m.group(1)) if m else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get('schema') != 1:
        return None, f'{repo}#{issue.get("number")} is not a status this magehand reads (`magehand upgrade`?)'
    return data, None


def no_cluster(why):
    """Plain words for a kubectl failure that means this machine has no cluster to ask (NO_CLUSTER), else None."""
    if not why or not NO_CLUSTER.search(why):
        return None
    return 'no cluster access configured on this machine (' + (
        'kubectl is not installed' if 'not installed' in why else 'kubectl has no cluster context') + ')'


def cluster_access():
    """None when kubectl has a current context, else why not. Reads kubectl's config only: no network call."""
    if not shutil.which('kubectl'):
        return 'kubectl is not installed'
    try:
        out = subprocess.run(['kubectl', 'config', 'current-context'], capture_output=True, text=True,  # nosec B603 B607
                             timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return 'kubectl did not answer'
    return None if out.returncode == 0 and (out.stdout or '').strip() else 'kubectl has no context'


def platform_deployment(repo, environment, sha, application, image=None):
    """The newest GitHub Deployment the platform recorded in the app's repo for `sha` in `environment` (dev or
    production: its deploy timer writes one per deploy, homelab#403), as {'state', 'description', 'at'}, or
    None when there is none or it can't be read: callers then do what they did before Deployments existed, so
    an unreadable one is never a failure. Only the platform's records count: a payload object naming the
    Argo CD `application` (and the pinned `image`, when given); anyone else's Deployment is skipped. `state`
    is GitHub's (in_progress, success, failure, inactive, ...)."""
    found, err = gh_query('api', f'repos/{repo}/deployments?environment={environment}&sha={sha}&per_page=5')
    if err or not isinstance(found, list):
        return None
    payload = lambda d: d.get('payload') if isinstance(d, dict) and isinstance(d.get('payload'), dict) else {}  # noqa: E731
    mine = [d for d in found if payload(d).get('application') == application
            and (image is None or payload(d).get('image') == image)]
    if not mine:
        return None
    statuses, err = gh_query('api', f'repos/{repo}/deployments/{mine[0]["id"]}/statuses?per_page=1')
    if err or not isinstance(statuses, list):
        return None
    latest = statuses[0] if statuses and isinstance(statuses[0], dict) else {}
    return {'state': latest.get('state') or 'pending', 'description': latest.get('description') or '',
            'at': latest.get('created_at') or mine[0].get('created_at')}


def dev_stage(name, deploy, cluster=True, repo=None, head=None):
    """The running pods for a merged dev deploy, read through kubectl when this machine can (current, and the
    most specific). When it can't, the platform's GitHub Deployment for `head` says how the rollout ended
    (readable from any machine: running, failed, or still rolling out), else an unknown stage says why the
    pods weren't read."""
    recorded = platform_deployment(repo, 'dev', head, f'{name}-dev', deploy['image']) if repo and head else None
    unknown = lambda why: {'stage': 'dev', 'state': 'unknown',  # noqa: E731
                           'text': f'config deployed ({deploy["digest"]}); ' + (
                               'its pods are not visible from this machine ('
                               + ('kubectl is not installed' if 'not installed' in why else 'no cluster access')
                               + ': normal on a laptop)' if NO_CLUSTER.search(why) else
                               f'the running pod is not checked ({why})')}
    if recorded and recorded['state'] == 'success':
        unknown = lambda why: {'stage': 'dev', 'state': 'ok',  # noqa: E731
                               'text': f'running {deploy["digest"]} (the platform saw every pod Ready, '
                                       f'{clock(recorded["at"])})'}
    elif recorded and recorded['state'] == 'failure':
        unknown = lambda why: {'stage': 'dev', 'state': 'failed',  # noqa: E731
                               'text': f'{recorded["description"] or "the rollout failed"} (the platform, '
                                       f'{clock(recorded["at"])})'}
    elif recorded and recorded['state'] in ('in_progress', 'queued', 'pending'):
        unknown = lambda why: {'stage': 'dev', 'state': 'pending',  # noqa: E731
                               'text': f'rolling out {deploy["digest"]} (the platform reports when every pod is Ready)'}
    if not cluster:
        return unknown('--no-cluster')
    pods, err = kubectl_json('pods', '-n', f'{name}-dev')
    if err:
        return unknown(err)
    application, argo_error = kubectl_json('application.argoproj.io', f'{name}-dev', '-n', 'argocd')
    problem = argo_problem(application)
    stage = pod_stage(pods, application, deploy['image'], argo_error)
    if problem and not (stage['state'] == 'failed' and problem['state'] != 'failed'):
        return {'stage': 'dev', **problem}  # a pod that cannot start is more specific than a render still pending
    return stage


def build_runs(repo, head):
    """(workflow_runs, error) for main's commit and, when the PR that merged it has main's exact tree, for
    that PR's head too (the image of a squash merge is the PR's build)."""
    commit, err = gh_query('api', f'repos/{repo}/commits/{head}')
    if err:
        return None, err
    shas = [head]
    pulls, err = gh_query('api', f'repos/{repo}/commits/{head}/pulls')
    if err:
        return None, err
    merged = next((p for p in pulls if p.get('merge_commit_sha') == head), None)
    if merged:
        pr_head = merged['head']['sha']
        tip, err = gh_query('api', f'repos/{repo}/commits/{pr_head}')
        if err:
            return None, err
        if tip['commit']['tree']['sha'] == commit['commit']['tree']['sha']:
            shas.append(pr_head)
    runs = []
    for sha in shas:
        found, err = gh_query('api', f'repos/{repo}/actions/runs?head_sha={sha}&per_page=50')
        if err:
            return None, err
        runs += found.get('workflow_runs', [])
    return runs, None


def collect_status(repo, name, cluster=True, ref='main'):
    """The stages for the app `name` whose code is `repo` (OWNER/NAME) at `ref` (main, or the one commit
    `release` is about to tag); a failed query is a stage in state 'error' naming what could not be read."""
    commit, err = gh_query('api', f'repos/{repo}/commits/{ref}')
    if err:
        die(f'cannot read {repo} through gh: {err} (is it yours, and are you signed in: `gh auth login`)')
    head = commit['sha']
    subject = commit['commit']['message'].splitlines()[0]
    runs, err = build_runs(repo, head)
    build = query_error('build', f"{repo}'s Build runs", err) if err else build_stage(runs, head)
    pr_fields = 'number,state,mergedAt,title,body,statusCheckRollup'
    dev, err = gh_query('pr', 'list', '-R', APPS_REPO, '--head', f'deploy-dev/{name}', '--state', 'all',
                        '--limit', '10', '--json', pr_fields)
    deploy = query_error('deploy', f'{APPS_REPO} deploy PRs', err) if err else deploy_stage(dev, head)
    prod_prs, err_prs = gh_query('pr', 'list', '-R', APPS_REPO, '--head', f'deploy-prod/{name}', '--state', 'all',
                                 '--limit', '10', '--json', 'number,state,title')
    releases, err = gh_query('api', f'repos/{repo}/releases?per_page=20')
    if err or err_prs:
        prod = query_error('prod', 'the releases or their deploy PRs', err or err_prs)
    else:
        release = eligible_release(releases)
        tag, ahead = (release or {}).get('tag_name'), None
        err = None
        if tag:
            compare, err = gh_query('api', f'repos/{repo}/compare/{tag}...main')
            ahead = None if err else compare.get('ahead_by')
        prod = (query_error('prod', f'the comparison of {tag} with main', err) if err
                else prod_stage(tag, ahead, prod_prs))
        if prod['state'] == 'ok':
            tagged, err = gh_query('api', f'repos/{repo}/commits/{tag}')
            if not err:
                prod = prod_running(prod, platform_deployment(repo, 'production', tagged['sha'], f'{name}-prod'))
    stages = [build, deploy, prod]
    if deploy['state'] == 'ok':
        stages.insert(2, dev_stage(name, deploy, cluster, repo, head))
    return {'app': name, 'repo': repo, 'commit': head, 'subject': subject, 'stages': stages}


def lifecycle_stages(repo, name):
    """Where the app is before main's build and deploy, as status stages: its repo set up for the platform,
    deploy/ on main, and registered. Each that isn't ok says what to do in `next`."""
    short = repo.split('/', 1)[1]
    state = repo_state(repo)
    problems = [text for bad, text in (
        (state['bot'] == 'missing', f'{BOT} is not a collaborator, so its review can\'t be requested'),
        (state['bot'] == 'read', f'{BOT} can only read'),
        (state['ruleset'] == 'missing', f"no ruleset '{RULESET['name']}'"),
        (state['private'] is False, 'it is public')) if bad]
    if state['archived']:
        repo_stage = {'state': 'unknown', 'text': 'archived (a retired app)'}
    elif problems:
        only_public = problems == ['it is public']
        repo_stage = {'state': 'failed', 'text': 'not set up: ' + '; '.join(problems),
                      'next': (f'ask the owner to make it private: `gh repo edit {repo} --visibility private '
                               '--accept-visibility-change-consequences`' if only_public else
                               f'ask the owner to run `magehand new {short}` (it repairs the repo)')}
    elif state['ruleset'] == 'weak':
        repo_stage = {'state': 'failed', 'text': f"ruleset '{RULESET['name']}' would not pause a merge "
                      f"({state['ruleset_detail']})", 'next': "ask the owner to fix the ruleset in the repo's Settings > Rules"}
    elif state['bot'] == 'invited':
        repo_stage = {'state': 'pending', 'text': f'{BOT} is invited; OpenClaw accepts within about 5 minutes',
                      'next': 'wait, then `magehand pr ready N` on open PRs'}
    elif None in (state['bot'], state['ruleset'], state['private']):
        repo_stage = {'state': 'unknown', 'text': 'setup not fully readable from this account (the ruleset and '
                      'invitations need admin)'}
    else:
        repo_stage = {'state': 'ok', 'text': f'set up: private, {BOT} can write, approval ruleset on main'}
    spec, unregistered_why = registration_lookup(name)
    listings = {path: github_listing(repo, path) for path in (DEPLOY, f'{DEPLOY}/base', f'{DEPLOY}/dev')}
    unread = next((f'{path}: {why}' for path, (_, why) in listings.items() if why), None)
    names = {path: names or [] for path, (names, _) in listings.items()}
    missing = [path for path, there in (
        (f'{DEPLOY}/app.yaml', 'app.yaml' in names[DEPLOY]),
        (f'{DEPLOY}/base/', any(n.endswith(('.yaml', '.yml')) for n in names[f'{DEPLOY}/base'])),
        (f'{DEPLOY}/dev/', 'kustomization.yaml' in names[f'{DEPLOY}/dev'])) if not there]
    pinned = bool(pinned_source(spec or {}))
    if unread:  # a failed lookup is not a missing file
        config = {'state': 'error', 'text': f'cannot read {DEPLOY}/ on main ({unread})',
                  'next': f'fix the access problem it names, then `magehand status {name}` again'}
    elif not missing:
        config = {'state': 'ok', 'text': f'{DEPLOY}/ on main (app.yaml, base/, dev/'
                  + (', prod/)' if 'prod' in names[DEPLOY] else ')'), 'prod': 'prod' in names[DEPLOY]}
    elif spec is not None and not pinned and len(missing) == 3:  # an older layout: the deployment is in homelab-apps
        config = {'state': 'unknown', 'text': f'no {DEPLOY}/ on main: the deployment is homelab-apps apps/{name}/'}
    elif spec is not None:
        config = {'state': 'failed', 'text': f'missing on main: {", ".join(missing)} (the next deploy reads them)',
                  'next': 'restore them (`magehand guide search deploy`), `magehand check`, then merge a PR'}
    else:
        config = {'state': 'pending', 'text': f'not on main yet: {", ".join(missing)}',
                  'next': 'write them (`magehand guide search deploy`), `magehand check`, then merge a PR'}
    if unregistered_why:
        register = {'state': 'error', 'text': f'cannot read the registration ({unregistered_why})',
                    'next': f'fix the access problem it names, then `magehand status {name}` again'}
    elif spec is None:
        on_main = config['state'] == 'ok'
        register = {'state': 'pending', 'text': 'not registered yet' + (
            ': the platform registers it within about 5 minutes of the merge' if on_main else
            f': it registers itself once {DEPLOY}/ is on main' if config['state'] == 'pending' else ''),
            'next': f'`magehand status {name} --watch`' if on_main else None}
    else:
        blocks = ', '.join(f'{b} ({(svc or {}).get("type")})' for b, svc in (spec.get('services') or {}).items())
        register = {'state': 'ok', 'text': f'registered; blocks: {blocks or "none"}'}
    return [{'stage': 'repo', **repo_stage}, {'stage': 'config', **config}, {'stage': 'register', **register}]


def status_next(report, failed, done):
    """What to do about a status report: fix the first stage that failed, wait for one in progress, or release
    what dev runs and production doesn't yet. Commands name the app: the report may be about another app than
    the checkout's."""
    app = report['app']
    watch = f'magehand status {app} --watch'
    for st in report['stages']:  # a failure anywhere comes before anything that is only waiting
        if st['state'] in ('failed', 'error'):
            if st.get('next'):
                return st['next']
            if st['state'] == 'error':
                return f'fix the access problem it names, then `magehand status {app}` again'
            return {'build': f'fix the failed build (its run is linked above), push, then `{watch}`',
                    'deploy': "tell the owner: the platform's dev deploy PR failed",
                    'dev': f'fix what the dev line says, push, then `{watch}`',
                    'prod': f'fix what the prod line says on main, then `magehand release {app} --dry-run` '
                            '(a new release redeploys production)'}.get(st['stage'], 'fix what that line says')
    for st in report['stages']:  # then the lifecycle's own steps (config to write, an invitation to accept)
        if st['state'] == 'pending' and st.get('next'):
            return st['next']
    if report.get('timeout') or not done:
        return f'`{watch}` (waits for the build and the dev deploy)'
    prod = next((st for st in report['stages'] if st['stage'] == 'prod'), None)
    if prod and prod.get('ahead'):
        return f'`magehand release {app} --dry-run`: dev runs commits production does not have yet'
    config = next((st for st in report['stages'] if st['stage'] == 'config'), {})
    if prod and prod.get('released') is False and config.get('prod'):  # a dev-only app has no deploy/prod/
        return f'when it is ready for production: `magehand release {app} --dry-run`'
    return None


def render_status(report):
    lines = [f'{report["app"]}  ({report["repo"]} main {report["commit"][:7]}: {report["subject"]})']
    for st in report['stages']:
        lines.append(f'  {st["stage"]:<9}{STATUS_MARK[st["state"]]} {st["text"]}')
    return '\n'.join(lines)


def code_repo(app):
    """OWNER/NAME of the app's code repo, or exits: an app built in homelab-apps has no main of its own."""
    spec = app['spec'].get('source') or {}
    source = spec.get('repo') or (remote_repo(app['root']) if app['root'] else None)
    if not source:
        die(f'{app["name"]} has no code repo (its image is built in homelab-apps), so there is no main to follow')
    return f'{OWNER}/{source}'


def status_target(name=None):
    """(app name, OWNER/REPO) for status, without loading the app's Deployment: a new app's checkout has none yet,
    and status is where it learns what's missing."""
    name = name or current_app()
    root = repo_root(name)
    spec, _ = registration_lookup(name)
    source = ((spec or {}).get('source') or {}).get('repo') or (remote_repo(root) if root else None)
    if not source:
        die(f'{name} is not registered yet and this is not its checkout: run `magehand status` in its repo'
            if spec is None else
            f'{name} has no code repo (its image is built in homelab-apps), so there is no main to follow')
    return name, f'{OWNER}/{source}'


def cmd_status(a):
    name, repo = status_target(a.app_flag or a.app)
    deadline = time.monotonic() + a.timeout * 60
    last = None
    lifecycle, polls, seen = lifecycle_stages(repo, name), 0, None
    while True:
        report = collect_status(repo, name, cluster=not a.no_cluster)
        # Read again when main moved (a merge can add deploy/prod/ or change the blocks) or while a stage can
        # still change (a registration, an invitation or a repair can land mid-watch).
        if polls and (report['commit'] != seen or any(st['state'] in ('pending', 'failed', 'error') for st in lifecycle)):
            lifecycle = lifecycle_stages(repo, name)
        seen = report['commit']
        report['stages'] = lifecycle + report['stages']
        # build and deploy always count; dev's pods only when they could be read (an unknown one doesn't)
        states = [st['state'] for st in report['stages'] if st['stage'] in ('build', 'deploy', 'dev')]
        stopped = 'failed' in states or any(st['state'] == 'error' for st in report['stages'] if st not in lifecycle)
        waiting = [st['stage'] for st in report['stages'] if st['state'] == 'pending'
                   and (st['stage'] in ('build', 'deploy', 'dev') or st in lifecycle)]
        done = all(st == 'ok' for st in states if st != 'unknown') and len(
            [st for st in report['stages'] if st['stage'] in ('build', 'deploy') and st['state'] == 'ok']) == 2 \
            and not any(st['state'] in ('pending', 'error') for st in lifecycle)  # waiting, or not read yet
        if not a.watch or stopped or done:  # a repo-setup problem doesn't stop the watch: the deploy goes on
            break
        if time.monotonic() > deadline:
            report['timeout'] = True
            break
        text = render_status(report)
        if text != last:
            print(text, file=sys.stderr)
            last = text
        time.sleep(a.interval)
        polls += 1
    # production's rollout failing (the platform's Deployment) fails status too; it never holds up a dev watch
    failed = stopped or any(st['state'] in ('failed', 'error') for st in lifecycle) or any(
        st['stage'] == 'prod' and st['state'] == 'failed' for st in report['stages'])
    report['ok'] = done and not failed  # set up, registered, and built and deployed to dev for the newest commit
    report['next'] = status_next(report, failed, done)
    if a.json:
        print(json.dumps(report, indent=1))
    else:
        print(render_status(report))
        print(next_line(report['next']))
        if report.get('timeout'):
            print(f'magehand: still waiting after {a.timeout} min for: {", ".join(waiting) or "the dev deploy"}; '
                  'run it again to keep waiting', file=sys.stderr)
    sys.exit(1 if failed or report.get('timeout') else 0)


# --- release: cut an app's production release ----------------------------------------
# The second write magehand makes, to GitHub as the owner (through gh): a tag and release on the app's
# repo. The platform deploys production from the owner's published release vX.Y.Z (see `status`), so
# this only picks the version, writes the notes and refuses when the release would not deploy.

CONVENTIONAL = re.compile(r'^(\w+)(\([^)]*\))?(!)?:\s*(.*)$')
PR_SUFFIX = re.compile(r'\s*\(#(\d+)\)\s*$')
PR_MERGE = re.compile(r'^Merge pull request #(\d+)')
BREAKING_FOOTER = re.compile(r'^BREAKING[ -]CHANGE:', re.MULTILINE)
KINDS = (('breaking', 'Breaking changes'), ('feature', 'Features'), ('fix', 'Fixes'), ('other', 'Other changes'))
BUMP_OF = {'breaking': 'major', 'feature': 'minor', 'fix': 'patch', 'other': 'patch'}
# A finished run (the REST API's lowercase conclusion) that isn't a pass: the same rule as GREEN_CONCLUSIONS.
FAILED_CHECK = ('failure', 'cancelled', 'timed_out', 'startup_failure', 'action_required', 'stale')


def classify_change(subject, body='', labels=()):
    """(kind, number or None, title) of one commit on main: breaking | feature | fix | other, from a
    Conventional Commit prefix (`!` or a BREAKING CHANGE footer) or the PR's labels."""
    names = {str(label).lower() for label in labels}
    number = None
    merge = PR_MERGE.match(subject)
    if merge:  # a merge commit: the PR's title is the first line of its body
        number = int(merge.group(1))
        subject, body = (body or '').strip().splitlines()[0] if (body or '').strip() else subject, ''
    tail = PR_SUFFIX.search(subject)
    if tail:
        number = int(tail.group(1))
        subject = subject[:tail.start()]
    m = CONVENTIONAL.match(subject)
    kind_word = m.group(1).lower() if m else ''
    if (m and m.group(3)) or BREAKING_FOOTER.search(body or '') or names & {'breaking', 'breaking change'}:
        kind = 'breaking'
    elif kind_word == 'feat' or names & {'enhancement', 'feature'}:
        kind = 'feature'
    elif kind_word in ('fix', 'perf') or 'bug' in names:
        kind = 'fix'
    else:
        kind = 'other'
    return kind, number, subject


def spec_changes(old, new):
    """(removed, added): what a release takes out of or puts into deploy/app.yaml that an app's users or its
    data would notice: building blocks (by name and type) and the llm models they request."""
    def items(spec):
        found = {}
        for block, svc in ((spec or {}).get('services') or {}).items():
            svc = svc if isinstance(svc, dict) else {}
            found[f'block {block} ({svc.get("type")})'] = None
            for model in svc.get('models') or []:
                found[f'model {model} of {block}'] = None
        return found
    before, after = items(old), items(new)
    return sorted(set(before) - set(after)), sorted(set(after) - set(before))


def next_version(tag, bump):
    """The tag after `tag` (None: no release yet, so v1.0.0) for a major | minor | patch bump."""
    m = re.match(r'^v(\d+)(?:\.(\d+))?(?:\.(\d+))?$', tag or '')
    if not m:
        return 'v1.0.0'
    major, minor, patch = (int(g or 0) for g in m.groups())
    if bump == 'major':
        return f'v{major + 1}.0.0'
    if bump == 'minor':
        return f'v{major}.{minor + 1}.0'
    return f'v{major}.{minor}.{patch + 1}'


def choose_bump(changes, removed, added, forced=None):
    """(bump, why) from the classified changes [(kind, number, title)] and the deploy/app.yaml delta. An
    explicit bump wins and is shown next to what was found."""
    def refs(kind):
        return ', '.join(f'#{n}' if n else f'"{t[:40]}"' for k, n, t in changes if k == kind)
    breaking = [c for c in changes if c[0] == 'breaking']
    features = [c for c in changes if c[0] == 'feature']
    if breaking or removed:
        found = 'major', ('breaking: ' + refs('breaking') if breaking else 'deploy/app.yaml removes ' + ', '.join(removed))
    elif features or added:
        found = 'minor', ('features: ' + refs('feature') if features else 'deploy/app.yaml adds ' + ', '.join(added))
    elif any(c[0] == 'fix' for c in changes) or not changes:
        found = 'patch', 'only fixes and other changes; no features or breaking changes found'
    else:  # nothing marks these as fixes or features: say it is a floor, not a finding
        found = 'patch', (f'{len(changes)} change{"" if len(changes) == 1 else "s"} with no feat/fix prefix or '
                          'label to read: a guess; pass --minor if they add features')
    if forced and forced != found[0]:
        return forced, f'{forced} as asked (the changes read as {found[0]}: {found[1]})'
    return found[0], f'{found[0]}: {found[1]}'


def release_notes(repo, previous, version, changes):
    """Markdown release notes grouped by kind, each change linking its PR."""
    lines = []
    for kind, heading in KINDS:
        mine = [c for c in changes if c[0] == kind]
        if mine:
            lines += [f'## {heading}', ''] + [
                f'- {t}' + (f' ([#{n}](https://github.com/{repo}/pull/{n}))' if n else '') for _, n, t in mine] + ['']
    if previous:
        lines.append(f'**Full changelog**: https://github.com/{repo}/compare/{previous}...{version}')
    return '\n'.join(lines).strip() + '\n'


def file_at(repo, path, ref):
    """(text, None) of a file at a ref, (None, None) when it is absent, (None, why) when it can't be read."""
    code, out, err = gh_probe('api', f'repos/{repo}/contents/{path}?ref={ref}')
    if code != 0:
        return (None, None) if '404' in err or 'Not Found' in err else (None, err or 'gh failed')
    try:
        listing = json.loads(out)
        if isinstance(listing, list):
            return json.dumps([f.get('name') for f in listing]), None
        return base64.b64decode(listing.get('content') or '').decode(), None
    except (ValueError, UnicodeDecodeError):
        return None, 'gh returned something unreadable'


def compare_commits(repo, base, head):
    """(commits, ahead_by, None) of base...head across every page of GitHub's comparison (it returns at most
    250 commits, and `ahead_by` is the whole count), or ([], 0, why) when a page can't be read or the
    result is incomplete: a release must not be versioned from part of its changes."""
    commits, ahead, page = [], 0, 1
    while page <= 30:
        found, err = gh_query('api', f'repos/{repo}/compare/{base}...{head}?per_page=100&page={page}')
        if err:
            return [], 0, f'could not compare {base} with main: {err}'
        ahead = found.get('ahead_by') or 0
        got = found.get('commits') or []
        commits += got
        if len(commits) >= ahead or len(got) < 100:
            break
        page += 1
    if len(commits) < ahead:
        return [], ahead, f'only {len(commits)} of the {ahead} commits since {base} could be read'
    return commits, ahead, None


def gh_pages(path, per_page=100, max_pages=10):
    """(items, None) of every page of a GitHub list endpoint, or ([], why): a read error, or a list still
    full after max_pages, is refused rather than used in part (reviews and commits are in date order, so the
    last page can overturn the first)."""
    items = []
    sep = '&' if '?' in path else '?'
    for page in range(1, max_pages + 1):
        found, err = gh_query('api', f'{path}{sep}per_page={per_page}&page={page}')
        if err:
            return [], err
        if not isinstance(found, list):
            return [], 'GitHub answered with something that is not a list'
        items += found
        if len(found) < per_page:
            return items, None
    return [], f'more than {per_page * max_pages} entries'


def production_approval(name):
    """(True | False, None), or (None, why): is `name` in homelab's production register
    (platform/prod-apps/apps.yaml)? A repo that still has a file per app in platform/prod-apps/apps/ is read
    too, so this works on either side of that move."""
    home = f'{OWNER}/homelab'
    text, err = file_at(home, 'platform/prod-apps/apps.yaml', 'main')
    if err:
        return None, err
    if text is not None:
        try:
            registered = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            return None, 'platform/prod-apps/apps.yaml is not valid YAML'
        return isinstance(registered, dict) and name in registered, None
    text, err = file_at(home, f'platform/prod-apps/apps/{name}.yaml', 'main')
    return (None, err) if err else (text is not None, None)


def main_checks_problems(repo, head):
    """Reasons main's own checks don't certify `head`: none ran, one failed or is running, or the Build
    workflow's push run (the security scan; its image job is skipped on a push) is missing or not green."""
    problems = []
    runs, total, page = [], None, 1
    while page <= 10:
        found, err = gh_query('api', f'repos/{repo}/commits/{head}/check-runs?per_page=100&page={page}')
        if err:
            return [f'could not read the checks of main: {err}']
        total = found.get('total_count', 0)
        runs += found.get('check_runs') or []
        if len(runs) >= total or not found.get('check_runs'):
            break
        page += 1
    if total is not None and len(runs) < total:  # a failing or running check may be in the part not read
        return [f'could only read {len(runs)} of the {total} checks on main {head[:7]}']
    if not runs:
        problems.append(f'no checks ran on main {head[:7]}: its security scan has not run')
    bad = [r.get('name') for r in runs if r.get('conclusion') in FAILED_CHECK]
    busy = [r.get('name') for r in runs if r.get('status') != 'completed']
    if bad:
        problems.append(f'main {head[:7]} has failing checks: {", ".join(bad)}')
    if busy:
        problems.append(f'main {head[:7]} still has checks running: {", ".join(busy)}')
    workflows, err = gh_query('api', f'repos/{repo}/actions/runs?head_sha={head}&per_page=100')
    if err:
        problems.append(f'could not read the workflow runs of main: {err}')
    else:
        pushes = [w for w in workflows.get('workflow_runs') or []
                  if w.get('path') == BUILD_WORKFLOW and w.get('event') == 'push']
        if not pushes:
            problems.append(f"the security scan of main {head[:7]} did not run (no push run of {BUILD_WORKFLOW})")
        elif not any(w.get('status') == 'completed' and w.get('conclusion') == 'success' for w in pushes):
            problems.append(f"the security scan of main {head[:7]} has not passed ({pushes[0].get('html_url', '')})")
    return problems


def read_spec(repo, ref, problems, required):
    """deploy/app.yaml at `ref`, parsed: {} when the file is absent and not required. A read or parse error
    is a problem, never an empty spec (an empty one would invent removals or hide them)."""
    text, err = file_at(repo, 'deploy/app.yaml', ref)
    if err:
        problems.append(f'could not read deploy/app.yaml at {ref[:12]}: {err}')
        return {}
    if text is None:
        if required:
            problems.append(f'deploy/app.yaml is missing on main (production needs deploy/app.yaml, base/ and prod/)')
        return {}
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError as error:
        problems.append(f'deploy/app.yaml at {ref[:12]} is not valid YAML: {str(error).splitlines()[0]}')
        return {}


def plan_release(repo, name, forced=None, cluster=True):
    """Everything `release` decides, from reads only: {app, repo, commit, previous, version, bump, why,
    changes, notes, problems, warnings}. A problem is a reason the release would not deploy."""
    problems, warnings = [], []
    commit, err = gh_query('api', f'repos/{repo}/commits/main')
    if err:
        die(f'cannot read {repo} through gh: {err} (is it yours, and are you signed in: `gh auth login`)')
    head = commit['sha']
    who, err = gh_query('api', 'user')
    if err or (who or {}).get('login') != OWNER:
        problems.append(f'gh is signed in as {(who or {}).get("login") or "nobody"}: only {OWNER}\'s release '
                        f'deploys production (`gh auth login`)')
    approved, err = production_approval(name)
    if err:
        problems.append(f'could not read the production approval of {name}: {err}')
    elif not approved:
        problems.append(f'{name} is not approved for production: add it to homelab platform/prod-apps/apps.yaml '
                        f'(the owner approves that PR; see the guide, "Going to production")')
    prod_dir, err = file_at(repo, 'deploy/prod', head)
    if prod_dir is None:
        problems.append(f'deploy/prod/ is missing on main (production needs deploy/app.yaml, base/ and prod/)'
                        if not err else f'could not read deploy/prod: {err}')
    releases, err = gh_query('api', f'repos/{repo}/releases?per_page=20')
    if err:
        die(f'cannot read {repo} releases through gh: {err}')
    last = eligible_release(releases)
    previous = (last or {}).get('tag_name')
    changes, ahead, old_spec = [], 0, None
    if previous:
        commits, ahead, err = compare_commits(repo, previous, head)
        if err:
            problems.append(err)
        old_spec = read_spec(repo, previous, problems, required=False)
    else:
        commits, err = gh_pages(f'repos/{repo}/commits?sha={head}')
        if err:
            problems.append(f'could not read the history of main: {err}')
        ahead = len(commits)
    for c in commits:
        message = (c.get('commit') or {}).get('message') or ''
        subject, _, body = message.partition('\n')
        changes.append(classify_change(subject, body))
    new_spec = read_spec(repo, head, problems, required=True)
    removed, added = spec_changes(old_spec, new_spec) if previous else ([], [])
    bump, why = choose_bump(changes, removed, added, forced) if previous else ('major', 'major: the first release')
    version = next_version(previous, bump)
    if previous and ahead == 0:
        problems.append(f'nothing new on main since {previous}')
    ref, err = gh_query('api', f'repos/{repo}/git/ref/tags/{version}')
    if ref and not err:
        problems.append(f'the tag {version} already exists (a draft or prerelease?): delete it or pick a bump')
    problems += main_checks_problems(repo, head)
    stages = {st['stage']: st for st in collect_status(repo, name, cluster, ref=head)['stages']}
    for stage in ('build', 'deploy'):
        if stages[stage]['state'] != 'ok':
            problems.append(f'{stage} of {head[:7]} is not done: {stages[stage]["text"]}')
    dev = stages.get('dev')
    if dev and dev['state'] in ('failed', 'pending'):
        problems.append(f'dev is not running {head[:7]}: {dev["text"]}')
    elif not dev or dev['state'] == 'unknown':
        warnings.append("dev's pods were not read (no kubectl, or --no-cluster): only its deploy PR merged")
    notes = release_notes(repo, previous, version, changes)
    return {'app': name, 'repo': repo, 'commit': head, 'previous': previous, 'version': version, 'bump': bump,
            'why': why, 'commits': ahead, 'changes': [{'kind': k, 'pr': n, 'title': t} for k, n, t in changes],
            'notes': notes, 'problems': problems, 'warnings': warnings}


def render_release(plan):
    lines = [f'{plan["app"]}  {plan["previous"] or "no release yet"} -> {plan["version"]}'
             f'  ({plan["repo"]} main {plan["commit"][:7]}, {plan["commits"]} commit'
             f'{"" if plan["commits"] == 1 else "s"})', f'  {plan["why"]}']
    lines += [f'  ! {w}' for w in plan['warnings']]
    lines += [f'  ✗ {p}' for p in plan['problems']]
    lines += ['', plan['notes'].rstrip()]
    return '\n'.join(lines)


def release_next(plan, refuse, created=False):
    """The next step after a release plan, naming its app (not whatever app the checkout is)."""
    if created:
        return f'`magehand status {plan["app"]}`: the platform deploys it to production within minutes'
    if refuse:
        return f'fix what blocks it (above), then `magehand release {plan["app"]} --dry-run` again'
    return f'`magehand release {plan["app"]}` (the owner only: it deploys {plan["app"]} to production)'


def cmd_release(a):
    if sum(map(bool, (a.major, a.minor, a.patch))) > 1:
        usage('release', 'choose one of --major, --minor, --patch')
    forced = 'major' if a.major else 'minor' if a.minor else 'patch' if a.patch else None
    app = app_here(a.app_flag or a.app)
    plan = plan_release(code_repo(app), app['name'], forced, cluster=not a.no_cluster)
    plan['created'] = False
    refuse = f'not releasing {plan["version"]}: ' + '; '.join(plan['problems']) if plan['problems'] else None
    plan['next'] = release_next(plan, refuse)
    if a.dry_run or refuse:
        if a.json:
            print(json.dumps(plan, indent=1))
        else:
            print(render_release(plan))
            print(next_line(plan['next']))
        if refuse:
            print(f'magehand: {refuse}', file=sys.stderr)
        sys.exit(1 if refuse else 0)
    if not a.json:
        print(render_release(plan))
    if a.confirm:
        if not sys.stdin.isatty():
            die('not a terminal: drop --confirm (a release that passes every check is created without asking)')
        if input(f'\nCreate release {plan["version"]} of {plan["repo"]} (deploys {plan["app"]} to production)? '
                 '[y/N] ').strip().lower() not in ('y', 'yes'):
            die('cancelled; nothing was created')
    code, out, err = gh_cli('release', 'create', plan['version'], '-R', plan['repo'], '--target', plan['commit'],
                            '--title', plan['version'], '--notes-file', '-', stdin=plan['notes'])
    if code != 0:
        die(f'gh could not create the release: {err}')
    plan['created'], plan['url'] = True, out.strip()
    plan['next'] = release_next(plan, None, created=True)
    if a.json:
        print(json.dumps(plan, indent=1))
    else:
        print(f'released {plan["version"]}: {plan["url"]}')
        print(next_line(plan['next']))


# --- new: start an app's repo --------------------------------------------------------
# A write magehand makes, to GitHub as the owner (never to the homelab): the
# app's private repo from the template, with OpenClaw's bot as a collaborator.

OWNER = APPS_REPO.split('/')[0]
TEMPLATE_REPO = f'{OWNER}/homelab-app-template'
BOT = 'davidlarrimore-bot'
APP_NAME = re.compile(r'^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$')  # homelab-apps validate.yaml
# Platform Applications (homelab-apps validate.yaml) and the platform's own repos.
RESERVED = {'argocd', 'authentik', 'cloudflared', 'external-secrets', 'ingress', 'kyverno', 'litellm', 'monitoring',
            'openbao', 'openclaw', 'agent-app-groups', 'homelab', 'homelab-apps', 'homelab-app-template',
            'homelab-events', 'magehand'}


def gh_cli(*args, stdin=None):
    """`gh ARGS` as the signed-in user: (exit status, stdout, stderr)."""
    if not shutil.which('gh'):
        die('needs the GitHub CLI: `brew install gh`, then `gh auth login`')
    out = subprocess.run(['gh', *args], capture_output=True, text=True, timeout=60, input=stdin)  # nosec B603 B607
    return out.returncode, out.stdout, out.stderr.strip()


# The ruleset every app code repo carries (the same as homelab's "main: owner approves"): a PR needs one
# approval of its latest push, so a merge is a pause for the owner; repo admins (the owner) can bypass it
# through a pull request. OpenClaw's review (as the bot) or the owner's own approval satisfies it.
RULESET = {
    'name': 'main: owner approves', 'target': 'branch', 'enforcement': 'active',
    'conditions': {'ref_name': {'include': ['~DEFAULT_BRANCH'], 'exclude': []}},
    'rules': [{'type': 'pull_request', 'parameters': {
        'required_approving_review_count': 1, 'dismiss_stale_reviews_on_push': True, 'required_reviewers': [],
        'require_code_owner_review': False, 'require_last_push_approval': True,
        'required_review_thread_resolution': False, 'require_extra_approval_for_unattributed_changes': True,
        'allowed_merge_methods': ['squash']}}],
    'bypass_actors': [{'actor_id': 5, 'actor_type': 'RepositoryRole', 'bypass_mode': 'pull_request'}],
}


def ruleset_problems(ruleset):
    """Why a ruleset named like ours would not give the approval pause (a list, empty when it would): not
    active, or no approving review. Whether it applies to the default branch is asked of GitHub
    (ruleset_status), not worked out from its include/exclude patterns."""
    found = []
    if ruleset.get('enforcement') != 'active':
        found.append(f"enforcement is {ruleset.get('enforcement')!r}, not 'active'")
    rules = [r for r in ruleset.get('rules') or [] if r.get('type') == 'pull_request']
    if not rules or int((rules[0].get('parameters') or {}).get('required_approving_review_count') or 0) < 1:
        found.append('it does not require an approving review')
    return found


def gh_json_pages(path, per_page=100, max_pages=20):
    """(items, None) from every page of a GitHub list endpoint, or (None, reason) if any page can't be read
    (no gh, an HTTP error, bad JSON, or more than max_pages pages: never a partial answer)."""
    items = []
    for page in range(1, max_pages + 1):
        code, out, err = gh_probe('api', f'{path}?per_page={per_page}&page={page}')
        if code != 0:
            return None, err or 'gh api failed'
        try:
            got = json.loads(out or '[]')
        except ValueError:
            return None, 'unreadable answer from GitHub'
        items += got
        if len(got) < per_page:
            return items, None
    return None, f'more than {max_pages * per_page} entries'


def ruleset_status(repo):
    """(state, detail) of the repo's approval ruleset: 'ok', 'missing', 'weak' (detail says why) or None when it
    could not be read (no gh, no admin, rate limit, network: detail has the reason)."""
    code, out, err = gh_probe('api', f'repos/{repo}/rulesets')
    if code != 0:
        return None, err or 'gh api failed'
    try:
        listed = [r for r in json.loads(out or '[]') if r.get('name') == RULESET['name']]
    except ValueError:
        return None, 'unreadable answer from GitHub'
    if not listed:
        return 'missing', ''
    code, out, err = gh_probe('api', f"repos/{repo}/rulesets/{listed[0]['id']}")
    if code != 0:
        return None, err or 'gh api failed'
    try:
        problems = ruleset_problems(json.loads(out))
    except ValueError:
        return None, 'unreadable answer from GitHub'
    # The rules GitHub applies to the default branch today (includes, excludes and ref patterns resolved).
    code, out, err = gh_probe('api', f'repos/{repo}', '--jq', '.default_branch')
    branch = out.strip()
    if code == 0 and branch:
        rules, err = gh_json_pages(f'repos/{repo}/rules/branches/{urllib.parse.quote(branch, safe="")}')
        if rules is None:
            return None, err
    if code != 0 or not branch:
        return None, err or 'could not read the default branch'
    applied = [r for r in rules if r.get('ruleset_id') == listed[0]['id']]
    if not any(r.get('type') == 'pull_request' for r in applied):
        problems.append(f'it does not apply to the default branch ({branch})')
    return ('weak', '; '.join(problems)) if problems else ('ok', '')


def bot_access(repo):
    """(state, detail) of OpenClaw's bot on the repo: 'write' (write, maintain or admin), 'read' (a collaborator
    that can't push), 'invited' (an invitation is pending), 'missing', or None when it could not be read (no gh,
    no admin, rate limit, network: detail has the reason). A request for the bot's review only works on 'write':
    on the others `gh pr edit --add-reviewer` exits 0 and adds nobody."""
    code, _, err = gh_probe('api', f'repos/{repo}/collaborators/{BOT}')
    if code == 0:
        # permission is the legacy level (maintain reads as write, triage as read); role_name is the role itself.
        code, out, err = gh_probe('api', f'repos/{repo}/collaborators/{BOT}/permission', '--jq',
                                  '.permission + " " + (.role_name // "")')
        if code != 0:
            return None, err or 'gh api failed'
        levels = set(out.split())
        return ('write', '') if levels & {'admin', 'maintain', 'write', 'push'} else ('read', out.strip())
    if code is None or 'HTTP 404' not in err:
        return None, err or 'gh api failed'
    invitations, err = gh_json_pages(f'repos/{repo}/invitations')
    if invitations is None:
        return None, err
    pending = [i for i in invitations if (i.get('invitee') or {}).get('login', '').lower() == BOT.lower()]
    if pending:
        return 'invited', pending[0].get('permissions') or ''
    return 'missing', ''


def ensure_bot(repo):
    """Make sure OpenClaw's bot can write to the repo: 'collaborator' (it already can), 'invited' (an invitation
    is pending: new, renewed, or raised from read to write). Dies when GitHub refuses the invitation."""
    state, _ = bot_access(repo)
    if state == 'write':
        print(f'{BOT}: already a collaborator with write', file=sys.stderr)
        return 'collaborator'
    # PUT invites a missing bot, raises a read-only one to write and renews a pending invitation.
    code, _, err = gh_cli('api', '-X', 'PUT', f'repos/{repo}/collaborators/{BOT}', '-f', 'permission=push')
    if code != 0:
        die(f'cannot invite {BOT} to {repo}: {err or "gh api failed"}')
    if state == 'read':
        print(f'{BOT}: raised from read to write', file=sys.stderr)
        return 'collaborator'
    print(f'{BOT}: {"invitation renewed" if state == "invited" else "invited"} with write; OpenClaw accepts it '
          'within about 5 minutes', file=sys.stderr)
    return 'invited'


def ensure_ruleset(repo):
    """Make sure the repo has the approval ruleset: 'exists', 'created', 'weak' or 'failed' (a warning, not an
    error: the owner can fix it by hand). An existing ruleset is never changed: if it is weaker than ours
    (disabled, another branch, no approval needed) it says so."""
    state, detail = ruleset_status(repo)
    if state == 'ok':
        print(f"{repo}: ruleset '{RULESET['name']}' already there", file=sys.stderr)
        return 'exists'
    if state == 'weak':
        print(f"{repo}: ruleset '{RULESET['name']}' exists but would not pause a merge ({detail}); fix it in the "
              "repo's Settings > Rules (magehand leaves an existing ruleset alone)", file=sys.stderr)
        return 'weak'
    if state == 'missing':
        code, _, detail = gh_cli('api', '-X', 'POST', f'repos/{repo}/rulesets', '--input', '-', stdin=json.dumps(RULESET))
        if code == 0:
            print(f"{repo}: ruleset '{RULESET['name']}' added (a PR needs one approval; you can bypass it as admin)",
                  file=sys.stderr)
            return 'created'
    print(f"{repo}: could not add the ruleset '{RULESET['name']}' ({detail or 'gh api failed'}); "
          'PRs can merge without a review until it exists', file=sys.stderr)
    return 'failed'


# --- the scaffold: a deployable app on day one -----------------------------------------
# `new` writes these into the repo's checkout when they are missing (never over a file that exists), so the
# first PR can register the app and create its blocks before any feature code. The app is a small Python
# server (stdlib only) with /healthz, laid out like the platform's other apps; replace it freely.

SCAFFOLD_PYTHON = ('python:3.14-alpine@sha256:'
                   '9e9fde4d32eedce0b661d9ab91e826b62dddf28e928c230ec55f1866cac66b01')

SCAFFOLD_FILES = {
    'Dockerfile': """\
FROM {python} AS base
WORKDIR /app
COPY app.py /app/app.py

# The build runs this stage's tests before it builds the image (homelab-apps build-app.yaml).
FROM base AS test
COPY tests/ /app/tests/
RUN python -B -m unittest discover -s tests

FROM base AS runtime
USER 65532
CMD ["python", "-B", "/app/app.py"]
""",
    '.dockerignore': '.git\n.github\n.venv\n__pycache__\n**/__pycache__\n.env\n.env.*\n',
    'app.py': '''\
"""{app}: a starting point written by `magehand new`. Replace it freely; keep /healthz (the pod's probes)."""
import html
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8000"))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            return self.reply(200, "text/plain", "ok")
        if self.path in ("/", "/index.html"):
            # authentik signs the user in and sets X-authentik-* headers (magehand guide search user)
            user = html.escape(self.headers.get("X-authentik-name") or "there")
            return self.reply(200, "text/html; charset=utf-8",
                              "<!doctype html><html lang='en'><meta name='viewport' content='width=device-width'>"
                              f"<title>{app}</title><h1>{app}</h1><p>Hello, {{user}}.</p>")
        return self.reply(404, "text/plain", "not found")

    def reply(self, status, kind, body):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # quiet: the pod's log is for the app's own messages
        pass


if __name__ == "__main__":
    with ThreadingHTTPServer(("0.0.0.0", PORT), Handler) as server:
        print(f"{app} listening on :{{PORT}}", flush=True)
        server.serve_forever()
''',
    'tests/test_app.py': '''\
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

import app


class App(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{{self.server.server_address[1]}}"

    def test_healthz(self):
        with urllib.request.urlopen(self.url + "/healthz") as reply:
            self.assertEqual(reply.read(), b"ok")

    def test_home_greets_the_signed_in_user(self):
        request = urllib.request.Request(self.url + "/", headers={{"X-authentik-name": "Ada"}})
        with urllib.request.urlopen(request) as reply:
            self.assertIn(b"Hello, Ada.", reply.read())


if __name__ == "__main__":
    unittest.main()
''',
    'ui-check.yaml': """\
# The browser check CI runs on every PR (homelab-apps .github/ui-check): each page must render, with these
# elements visible. Add a page for every screen and the main action (magehand guide search ui-check).
port: 8000
headers:
  X-authentik-uid: ui-check
  X-authentik-name: UI Check
pages:
  - name: home
    path: /
    visible: [h1]
    text: [h1]
""",
    'deploy/app.yaml': """\
# The blocks this app asks the platform for (magehand guide catalog): add them here and map their keys in
# deploy/base/deployment.yaml env. Merging this file with base/ and dev/ registers the app.
{app_line}services: {{}}
""",
    'deploy/base/kustomization.yaml': """\
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - deployment.yaml
  - service.yaml
  - ingress.yaml
""",
    'deploy/base/deployment.yaml': """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: "{app}"
spec:
  replicas: 1
  selector:
    matchLabels:
      app: "{app}"
  template:
    metadata:
      labels:
        app: "{app}"
    spec:
      automountServiceAccountToken: false
      securityContext:
        runAsNonRoot: true
        runAsUser: 65532
        runAsGroup: 65532
        fsGroup: 65532
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: app  # the platform pins the commit and image on the container named app
          image: ghcr.io/davidlarrimore/homelab-apps/{app}
          imagePullPolicy: IfNotPresent
          ports:
            - name: http
              containerPort: 8000
          env:
            - name: PORT
              value: "8000"
            - name: SOURCE_COMMIT
              value: unknown
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
            limits:
              cpu: 500m
              memory: 256Mi
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop:
                - ALL
            readOnlyRootFilesystem: true
          readinessProbe:
            httpGet:
              path: /healthz
              port: http
            initialDelaySeconds: 2
            periodSeconds: 10
            timeoutSeconds: 2
          livenessProbe:
            httpGet:
              path: /healthz
              port: http
            initialDelaySeconds: 5
            periodSeconds: 30
            timeoutSeconds: 2
""",
    'deploy/base/service.yaml': """\
apiVersion: v1
kind: Service
metadata:
  name: "{app}"
spec:
  selector:
    app: "{app}"
  ports:
    - name: http
      port: 80
      targetPort: http
""",
    'deploy/base/ingress.yaml': """\
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: "{app}"
  annotations:
    traefik.ingress.kubernetes.io/router.middlewares: authentik-forward-auth@kubernetescrd
spec:
  ingressClassName: traefik
  rules:
    # dev's host: a deploy/prod/ overlay patches it (magehand guide search production)
    - host: {app}-dev.{lab}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: "{app}"
                port:
                  name: http
""",
    'deploy/dev/kustomization.yaml': """\
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: "{app}-dev"
resources:
  - ../base
""",
}
# Every app-name scalar above is quoted: true, null and 123 are valid app names, and not YAML strings bare.
# The code files belong together: written only into a repo with no code of its own yet (another stack keeps its own).
SCAFFOLD_CODE = ('Dockerfile', '.dockerignore', 'app.py', 'tests/test_app.py')
OWN_CODE = ('Dockerfile', 'app.py', 'main.py', 'tests', 'src', 'package.json', 'pyproject.toml', 'requirements.txt',
            'go.mod', 'Cargo.toml', 'Gemfile', 'pom.xml')
SCAFFOLD_BASE = tuple(f for f in SCAFFOLD_FILES if f.startswith('deploy/base/'))


def scaffold(root, app, repo_name, skip=()):
    """Write the scaffold's missing files into the checkout at `root` for app `app`: never over a file that
    exists, the code only into a repo with no code of its own (OWN_CODE), the Kustomize base only when
    deploy/base/ has no manifests, nothing in `skip`. Returns the paths written (relative)."""
    root = pathlib.Path(root)
    skip = set(skip or ())
    if any((root / own).exists() for own in OWN_CODE):
        skip.update(SCAFFOLD_CODE)
    if any((root / 'deploy' / 'base').glob('*.y*ml')):
        skip.update(SCAFFOLD_BASE)
    values = {'app': app, 'lab': LAB, 'python': SCAFFOLD_PYTHON,
              'app_line': f'app: "{app}"  # the repo is {repo_name}; keep this line\n' if app != repo_name else ''}
    written = []
    for rel, text in SCAFFOLD_FILES.items():
        path = root / rel
        if rel in skip or path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.format(**values))
        written.append(rel)
    return written


APP_SEED = ('# This repo is {repo}; the app it deploys is named {app} (its hostnames, namespace and image).\n'
            '# Keep `app:` here: without it the platform names the app after the repo.\n'
            'app: "{app}"\n'  # quoted: true, null and 123 are valid names and not YAML strings bare
            'services: {{}}\n')


def ensure_app_name(repo, app):
    """Make the repo's deploy/app.yaml say `app: APP`: 'seeded' (written, as the owner, on main), 'present'
    (already says it), 'differs' (says another app, left alone) or 'failed' (couldn't write). deploy/ with only
    app.yaml is not deployed: registration waits for deploy/base/."""
    code, out, _ = gh_cli('api', '-H', 'Accept: application/vnd.github.raw', f'repos/{repo}/contents/deploy/app.yaml')
    if code == 0:
        try:
            spec = yaml.safe_load(out) or {}
        except yaml.YAMLError:
            spec = {}
        found = spec.get('app') if isinstance(spec, dict) else None
        return 'present' if found == app else 'differs'
    body = APP_SEED.format(repo=repo.split('/', 1)[1], app=app)
    for attempt in range(6):  # a repo made from a template has its files a moment later
        code, _, err = gh_cli('api', '-X', 'PUT', f'repos/{repo}/contents/deploy/app.yaml',
                              '-f', f'message=Name the app {app} (deploy/app.yaml app:)',
                              '-f', 'content=' + base64.b64encode(body.encode()).decode())
        if code == 0:
            return 'seeded'
        time.sleep(2 if attempt < 5 else 0)
    print(f'{repo}: could not write deploy/app.yaml ({err or "gh api failed"}); add `app: {app}` to it yourself '
          '(first line) before the first deploy', file=sys.stderr)
    return 'failed'


# GitHub copies a template into a new repo asynchronously, as a push to main. Anything that lands on main
# first (our deploy/app.yaml) or a ruleset that requires a pull request makes the copy fail ("Changes must
# be made through a pull request") and leaves the repo without the template's files. So we wait for them.
TEMPLATE_FILES = ('AGENTS.md', '.github/workflows/build.yaml')


def template_state(repo):
    """('ready', ''), ('missing', path) when GitHub says a template file isn't there (HTTP 404), or
    ('unknown', reason) when it can't be told (rate limit, authorization, network): never "missing"."""
    for path in TEMPLATE_FILES:
        code, _, err = gh_cli('api', f'repos/{repo}/contents/{path}')
        if code != 0:
            if re.search(r'HTTP 404|Not Found', err or ''):
                return 'missing', path
            return 'unknown', err or 'gh api failed'
    return 'ready', ''


def wait_for_template(repo, tries=30, delay=2):
    """The last template_state after polling until the template's files are on the repo's main (about
    tries*delay seconds). A copy in progress reads as missing; an unreadable API is retried too."""
    state = ('missing', '')
    for attempt in range(tries):
        state = template_state(repo)
        if state[0] == 'ready':
            return state
        if attempt < tries - 1:
            time.sleep(delay)
    return state


def shell_path(path):
    """`path` as one safe word for a command the user pastes: quoted, and never starting with a dash (it
    would be read as an option)."""
    text = str(path)
    return shlex.quote('./' + text if text.startswith('-') else text)


def clone_repo(repo, dest):
    """'cloned', 'exists' (dest is a non-empty directory: left alone) or 'failed'. A repo made from a
    template has its files a moment later, so a first failure is retried briefly. (cmd_new has already
    refused a destination that is a file.)"""
    try:
        if dest.is_dir() and any(dest.iterdir()):
            return 'exists'
    except OSError:  # unreadable directory: let git say what is wrong
        return 'failed'
    target = './' + str(dest) if str(dest).startswith('-') else str(dest)
    for attempt in range(5):
        code, _, _ = gh_cli('repo', 'clone', repo, target)
        if code == 0:
            return 'cloned'
        time.sleep(2 if attempt < 4 else 0)
    return 'failed'


def cmd_new(a):
    name = a.name
    app = a.app_name or name
    for label, value in (('repo', name), ('app', app)):
        if not APP_NAME.match(value):
            usage('new', f"bad {label} name '{value}': lowercase letters, digits and dashes, at most 40, "
                         'starting and ending with a letter or digit')
        if value in RESERVED or (label == 'app' and (value.endswith(('-dev', '-prod')) or value.startswith('kube-'))):
            usage('new', f"'{value}' is reserved for the platform; choose another")
    repo = f'{OWNER}/{name}'
    dest = pathlib.Path(a.dir or name)
    if not a.no_clone and dest.exists() and not dest.is_dir():  # before anything is created on GitHub
        usage('new', f'{dest} exists and is not a directory: pick another with --dir, or use --no-clone')
    code, out, _ = gh_cli('api', f'repos/{repo}', '--jq', '.private, .archived')
    created = code != 0
    if created:
        code, _, err = gh_cli('repo', 'create', repo, '--private', '--template', TEMPLATE_REPO)
        if code != 0:
            die(f'cannot create {repo}: {err or "gh repo create failed"} (only the owner can; `gh auth status`)')
        print(f'{repo}: created (private, from {TEMPLATE_REPO})', file=sys.stderr)
        private = True
    else:
        flags = out.split() + ['false', 'false']
        private, archived = flags[0] == 'true', flags[1] == 'true'
        if archived:  # how an app is retired: magehand leaves it alone
            die(f'{repo} is archived (a retired app), so magehand changes nothing on it. To work on it again, '
                f'unarchive it (`gh repo unarchive {repo}`) and run `magehand new` again')
        print(f'{repo}: exists' + ('' if private else
              f'; it is PUBLIC, app repos are private: gh repo edit {repo} --visibility private '
              '--accept-visibility-change-consequences'), file=sys.stderr)
    # The template's files first: a commit or a ruleset on main before GitHub's asynchronous copy lands makes
    # the copy fail. Nothing else is touched until they are there.
    ready, detail = wait_for_template(repo, tries=30 if created else 4)
    if ready == 'unknown':
        die(f"could not check whether {repo} has the template's files ({detail}). Nothing else was changed; "
            'fix the access problem (`gh auth status`, rate limits, network) and run `magehand new` again: '
            'it is safe to re-run, and the repo is not known to be broken')
    if ready != 'ready':
        die(f"{repo} has not got the template's files ({', '.join(TEMPLATE_FILES)}) "
            + ("after about a minute: GitHub's copy of " + TEMPLATE_REPO + ' has not finished. '
               'Nothing else was changed (no ruleset yet); run `magehand new` again in a minute'
               if created else
               'on main: its template copy did not complete (a ruleset or an early commit blocks it). '
               'An app repo with nothing in it yet is quickest to recreate: `gh repo delete '
               f'{repo}` and run `magehand new` again; otherwise restore the files through a pull request'))
    bot = ensure_bot(repo)
    # Before the ruleset: the owner's one commit to main, so deploy/app.yaml names the app from the start.
    named = ensure_app_name(repo, app) if app != name else None
    if named == 'seeded':
        print(f'{repo}: deploy/app.yaml says `app: {app}` (the repo keeps its name)', file=sys.stderr)
    elif named == 'differs':
        print(f'{repo}: deploy/app.yaml names another app; make its first line `app: {app}` or run without '
              '--app-name', file=sys.stderr)
    ruleset = ensure_ruleset(repo)
    unnamed = named in ('failed', 'differs')  # the repo does not say `app: APP`: its app is still the repo's name
    here = not a.dir and current_repo() == repo  # repairing the checkout you are in: no second copy inside it
    cloned = None if a.no_clone or here else clone_repo(repo, dest)
    if cloned == 'cloned':
        print(f'{repo}: cloned to {dest}', file=sys.stderr)
    elif cloned == 'exists':
        print(f'{dest}: already exists; not cloned (is it {repo}?)', file=sys.stderr)
    elif cloned == 'failed':
        print(f'{repo}: could not clone it; run `gh repo clone {repo} {shell_path(dest)}`', file=sys.stderr)
    # The checkout's own files: the scaffold's missing ones, and AGENTS.md's platform rules (local writes only).
    candidate = checkout_top() if here else dest if cloned in ('cloned', 'exists') else None
    # Only into the top of a checkout of this very repo (its origin, owner and name, says so): never a directory
    # that merely has the name, another owner's repo of that name, or a plain directory inside some checkout.
    work = candidate if (candidate and pathlib.Path(candidate).is_dir()
                         and pathlib.Path(checkout_top(candidate) or '/nowhere').resolve()
                         == pathlib.Path(candidate).resolve()
                         and (origin_repo(candidate) or '').lower() == repo.lower()) else None
    wrote, pull = [], False
    if work:
        # The app's name: the checkout's own `app:` if it has one, else the one asked for (a failed seed is
        # what the printed step tells you to write, so the manifests already match it).
        live = app_override(work) or app
        # A seed `new` just committed to main isn't in a checkout made before it: pull it, don't write a rival.
        pull = named == 'seeded' and not (pathlib.Path(work) / DEPLOY / 'app.yaml').exists()
        wrote = [] if named == 'differs' else scaffold(work, live, name, skip={f'{DEPLOY}/app.yaml'} if pull else ())
        try:
            current, text, problem = agents_text(pathlib.Path(work) / 'AGENTS.md')
        except SystemExit as error:  # GitHub unreachable and nothing cached: report it, keep what was written
            current, text, problem = None, None, str(error.code).replace('magehand: ', '')
        if problem:
            print(f'magehand: AGENTS.md not refreshed: {problem}', file=sys.stderr)
        elif text != current:
            (pathlib.Path(work) / 'AGENTS.md').write_text(text)
            wrote.append('AGENTS.md')
        if wrote:
            print(f'{work}: wrote {", ".join(wrote)} (nothing existing was changed but AGENTS.md\'s platform rules)'
                  if 'AGENTS.md' in wrote else f'{work}: wrote {", ".join(wrote)}', file=sys.stderr)
    elif cloned == 'exists':
        print(f'{dest}: not touched (it is not a checkout of {repo})', file=sys.stderr)
    if named == 'differs' and work:
        print(f'{work}: no scaffold written: deploy/app.yaml on main names another app', file=sys.stderr)
    steps = (new_steps(repo, name, app, dest, named, cloned, wrote) if created else
             repair_steps(repo, private, bot, ruleset, cloned, dest, app, named, wrote))
    if pull:
        steps.insert(0, f'run:   git -C {shell_path(work)} pull   # new committed deploy/app.yaml (`app: {app}`) to main')
    if a.json:
        print(json.dumps({'repo': repo, 'app': app, 'created': created, 'private': private, 'bot': bot,
                          'ruleset': ruleset, 'app_name': named, 'clone': cloned,
                          'dir': str(work or dest) if work or cloned in ('cloned', 'exists') else None, 'wrote': wrote,
                          'next': step_text(steps[0]) if steps else None}, indent=1))
    elif created:
        print(new_summary(repo, name, app, dest, named, cloned, wrote, steps))
    else:
        print(repair_summary(repo, private, bot, ruleset, cloned, dest, app, named, wrote, steps))
    if unnamed:  # everything else is set up; a script must not carry on as if the app were named
        sys.exit(1)


def commit_step(wrote):
    """The step after `new` wrote files into the checkout, worded for what it wrote."""
    deploy = any(path.startswith('deploy/') for path in wrote)
    if wrote == ['AGENTS.md']:
        return 'do:    review the AGENTS.md diff (its platform rules were refreshed) and commit it'
    return ('do:    commit what new wrote, `magehand check`, `gh pr create --draft`, then `magehand pr ready`'
            + ('; merging it registers the app' if deploy else ''))


def step_text(step):
    """A summary step without its marker ("run:   cd elvis" -> "cd elvis"), for the `next:` line."""
    return step.split(':', 1)[1].strip() if re.match(r'^[a-z]+:\s', step) else step


def new_steps(repo, name, app, dest, named, cloned, wrote=()):
    """The steps `new` leaves you after creating a repo, in order."""
    # run: a command to type, write:/edit: work in the repo, do: a decision or action of yours. A program
    # can't change its parent shell's directory, so `cd` is a step even when it cloned.
    steps = [f'run:   gh repo clone {repo} {shell_path(dest)}'] if cloned not in ('cloned', 'exists') else []
    steps.append(f'run:   cd -- {shell_path(dest)}' if str(dest).startswith('-') else f'run:   cd {shell_path(dest)}')
    if named in ('failed', 'differs'):
        steps.append(f'edit:  make the first line of deploy/app.yaml `app: "{app}"` (the repo still names the app '
                     f'{name})')
    if wrote:  # the scaffold deploys as it is: the first PR registers the app before any feature code
        steps += [commit_step(wrote), 'write: the app' + (' (app.py is a starting point)' if 'app.py' in wrote else '')
                  + '   guide: magehand guide search new app']
    else:
        steps += ['write: the code, and deploy/ (app.yaml, base/ and dev/)   guide: magehand guide search new app',
                  'run:   magehand check          # the platform\'s rules, before you push',
                  'do:    open a PR and merge it  # the platform deploys what is on main']
    return steps


def new_summary(repo, name, app, dest, named, cloned, wrote=(), steps=None):
    """What `new` leaves for you, in two lists: the steps that are yours (commands first, plainly marked) and
    what the platform does by itself afterwards, then the first step as `next:`. Everything already done went
    to stderr as it happened."""
    live = name if named in ('failed', 'differs') else app
    steps = new_steps(repo, name, app, dest, named, cloned, wrote) if steps is None else steps
    lines = ['You:'] + [f'  {i}. {step}' for i, step in enumerate(steps, 1)]
    lines += ['', 'Then, by itself (about 5 minutes after deploy/ with base/ is merged to main):',
              f'  - {live} is registered and deployed to https://{live}-dev.{LAB}/',
              '  - its blocks and its own LLM key are created (until then, mock the LLM)',
              next_line(step_text(steps[0]))]
    return '\n'.join(lines)


def repair_steps(repo, private, bot, ruleset, cloned, dest, app=None, named=None, wrote=()):
    """The steps `new` on an existing repo leaves you: only what it could not do itself."""
    steps = []
    if named in ('failed', 'differs'):
        steps.append(f'edit:  make the first line of deploy/app.yaml `app: "{app}"` (until then the app is named '
                     f"{repo.split('/', 1)[1]})")
    if not private:
        steps.append(f'run:   gh repo edit {repo} --visibility private --accept-visibility-change-consequences')
    if bot == 'invited':
        steps.append('wait:  OpenClaw accepts the invitation within about 5 minutes; then `magehand pr ready N` '
                     'on open PRs (it requests the review and confirms it)')
    if ruleset == 'weak':
        steps.append(f"do:    fix the ruleset '{RULESET['name']}' in the repo's Settings > Rules (magehand leaves an "
                     'existing ruleset alone)')
    elif ruleset == 'failed':
        steps.append(f"do:    add the ruleset '{RULESET['name']}' (only an admin can): run `magehand new "
                     f"{repo.split('/', 1)[1]} --no-clone` again, or add it in Settings > Rules")
    if cloned == 'failed':
        steps.append(f'run:   gh repo clone {repo} {shell_path(dest)}')
    if wrote:
        steps.append(commit_step(list(wrote)))
    return steps


def repair_summary(repo, private, bot, ruleset, cloned, dest, app=None, named=None, wrote=(), steps=None):
    """What `new` on an existing repo leaves for you (its changes went to stderr as it made them)."""
    if steps is None:
        steps = repair_steps(repo, private, bot, ruleset, cloned, dest, app, named, wrote)
    if not steps:
        return (f'{repo}: set up (private, {BOT} can write, the approval ruleset is on main); nothing left for you\n'
                + next_line(None))
    return '\n'.join(['You:'] + [f'  {i}. {step}' for i, step in enumerate(steps, 1)]
                     + [next_line(step_text(steps[0]))])


# --- the container runtime: what runs local blocks and `run --container` -------------
# magehand drives a docker-compatible CLI: `docker` (Docker Desktop, OrbStack,
# Colima, Rancher Desktop with dockerd) or `podman`. Detection only runs
# `<cli> info`, as the user (no sudo). `magehand runtime docker|podman|none` pins the choice;
# `none` means no containers: `magehand run` still works for apps whose blocks
# are llm and secret only.

RUNTIMES = ('docker', 'podman')
CONFIG = pathlib.Path(os.environ.get('XDG_CONFIG_HOME', HOME / '.config')) / 'magehand' / 'config.json'
DOCKER_PRODUCTS = (('orbstack', 'OrbStack'), ('colima', 'Colima'), ('rancher-desktop', 'Rancher Desktop'),
                   ('desktop-linux', 'Docker Desktop'))
SUPPORTED = 'Docker Desktop, OrbStack, Colima, Rancher Desktop (dockerd) or Podman'
_runtime = []  # memo: [cli or None]


def quiet(cmd, timeout=10):
    """A read-only probe; None if the command is missing or hangs."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)  # nosec B603  # fixed probes
    except (OSError, subprocess.TimeoutExpired):
        return None


def docker_product(context, os_name=''):
    for key, label in DOCKER_PRODUCTS:
        if key in context:
            return label
    return 'Docker Desktop' if 'Docker Desktop' in os_name else 'Docker'


def detect_runtimes():
    """[{cli, product, running, hint}] for the container CLIs on PATH."""
    found = []
    if shutil.which('docker'):
        ctx = quiet(['docker', 'context', 'show'])
        context = ctx.stdout.strip() if ctx and ctx.returncode == 0 else ''
        info = quiet(['docker', 'info', '--format', '{{.OperatingSystem}}'])
        running = bool(info and info.returncode == 0)
        product = docker_product(context, info.stdout if running else '')
        hint = {'Colima': '`colima start`', 'Rancher Desktop': 'start Rancher Desktop, with Preferences > '
                'Container Engine set to dockerd (moby)'}.get(product, f'start {product}')
        found.append({'cli': 'docker', 'product': product, 'running': running, 'hint': '' if running else hint})
    if shutil.which('podman'):
        info = quiet(['podman', 'info', '--format', '{{.Version.Version}}'])
        running = bool(info and info.returncode == 0)
        found.append({'cli': 'podman', 'product': 'Podman', 'running': running,
                      'hint': '' if running else '`podman machine start`'})
    if shutil.which('nerdctl') and not shutil.which('docker'):
        found.append({'cli': 'nerdctl', 'product': 'Rancher Desktop (containerd)', 'running': False,
                      'hint': 'not supported: set Rancher Desktop > Preferences > Container Engine to dockerd (moby)'})
    return found


def configured_runtime():
    """The user's choice (MAGEHAND_RUNTIME, else `magehand runtime NAME`), or None for automatic."""
    value = os.environ.get('MAGEHAND_RUNTIME')
    if not value:
        try:
            value = json.loads(CONFIG.read_text()).get('runtime')
        except (OSError, ValueError, AttributeError):
            value = None
    if value and value not in RUNTIMES + ('none',):
        die(f'unknown container runtime {value!r} (MAGEHAND_RUNTIME or `magehand runtime NAME`): '
            f'{", ".join(RUNTIMES)} or none')
    return value


def choose_runtime(configured, found):
    """The CLI to use: the configured one, else the first running supported one; None means no containers."""
    if configured == 'none':
        return None
    if configured:
        return configured
    return next((f['cli'] for f in found if f['running'] and f['cli'] in RUNTIMES), None)


def runtime():
    if not _runtime:
        configured = configured_runtime()
        _runtime.append(choose_runtime(configured, [] if configured else detect_runtimes()))
    return _runtime[0]


def no_runtime(what):
    configured = configured_runtime()
    reason = 'set to none (`magehand runtime`)' if configured == 'none' else 'none is running'
    return (f'{what} needs a container runtime, and {reason}. Supported: {SUPPORTED}; '
            '`magehand runtime` shows what this machine has. Without one, `magehand run -- CMD` works for apps '
            'whose blocks are llm and secret only.')


def engine(*args, check=True, env=None):
    """Run the container CLI. Secret values go in `env` and are named in args as `-e NAME`: docker
    reads them from its own environment, so they never appear in argv."""
    cli = runtime()
    if not cli:
        die(no_runtime('this'))
    if not shutil.which(cli):
        die(f'container runtime {cli} is configured but not installed: `magehand runtime auto` or install it')
    return subprocess.run([cli, *args], capture_output=True, text=True, check=check,  # nosec B603  # docker/podman
                          env={**os.environ, **env} if env else None)


def save_config(**changes):
    try:
        data = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        data = {}
    data.update(changes)
    data = {k: v for k, v in data.items() if v is not None}
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(data, indent=1) + '\n')


def runtime_lines(found, configured, chosen):
    lines = [f"  {f['cli']:7} {f['product']}: {'running' if f['running'] else 'not running -> ' + f['hint']}"
             for f in found] or ['  none found']
    how = f'set with `magehand runtime {configured}`' if configured else 'automatic'
    lines.append(f"Using: {chosen or 'none (no containers)'} ({how})")
    return lines


def cmd_runtime(a):
    choice = a.runtime
    if choice == 'auto':
        save_config(runtime=None)
        print('container runtime: automatic (the first running of docker, podman)')
        return
    if choice:
        if choice != 'none' and not shutil.which(choice):
            die(f'{choice} is not installed here. Install one of {SUPPORTED} first; '
                '`magehand runtime` shows what this machine has')
        save_config(runtime=choice)
        print(f'container runtime: {choice}' + (' (local postgres/redis blocks and run --container are off)'
                                               if choice == 'none' else ''))
        return
    configured = configured_runtime()
    found = detect_runtimes()
    if a.json:
        print(json.dumps({'using': choose_runtime(configured, found), 'configured': configured,
                          'found': found, 'supported': list(RUNTIMES)}, indent=1))
        return
    print('Container runtimes (for local postgres/redis blocks and run --container):')
    print('\n'.join(runtime_lines(found, configured, choose_runtime(configured, found))))
    print(f'Supported: {SUPPORTED}. Change: magehand runtime docker|podman|none|auto')


# What an older magehand wrote into the state file and a newer one never does: secrets belong to the
# containers (and are made per run), not to disk.
STATE_SECRET_KEYS = ('password', 'value', 'secret_access_key', 'admin_token')


def local_state(app_name):
    """The app's local state ({block: {port, container, ...}}). A file written by an older magehand that
    still holds a password, a generated secret value or a key's secret is cleaned on first read, and
    rewritten, so upgrading removes them from disk (a removed block's too)."""
    path = STATE / f'{app_name}.json'
    if not path.is_file():
        return {}
    state = json.loads(path.read_text())
    dirty = False
    for block, entry in list(state.items()):
        if not isinstance(entry, dict):
            continue
        for key in STATE_SECRET_KEYS:
            if key in entry:
                del entry[key]
                dirty = True
        if not entry:   # a generated secret block held nothing else
            del state[block]
        elif 'port' in entry and 'container' not in entry:
            # an older state named no container, and the password now lives in it: its name is fixed
            entry['container'] = f'magehand-{app_name}-{block}'
            dirty = True
    if dirty:
        save_local_state(app_name, state)
    return state


def save_local_state(app_name, data):
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / f'{app_name}.json'
    path.touch(mode=0o600)
    path.chmod(0o600)   # touch never narrows a file that already exists
    path.write_text(json.dumps(data, indent=1))


def block_image(kind, svc):
    values = yaml.safe_load(github_file(HOMELAB_REPO, f'platform/building-blocks/{kind}/values.yaml'))
    if kind == 'postgres' and (svc or {}).get('vector') in (True, 'true'):
        return values['pgvector']['image']
    return values['image']


def garage_admin(port, token, method, path, body=None):
    """(status, JSON or None) from the local Garage's admin API on 127.0.0.1:port."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=data, method=method,
                                 headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310  # 127.0.0.1, a container we started
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as error:
        return error.code, None


def garage_setup(port, token, bucket, key_id=None, wait=30):
    """Layout, bucket, quota and a key limited to it on the local Garage; (access key id, secret).
    Every step is checked and the whole is idempotent, so a half-finished setup is repaired by
    running `dev up` again. A key that already works (key_id) is reused."""
    deadline = time.time() + wait
    while True:
        try:
            status, cluster = garage_admin(port, token, 'GET', '/v2/GetClusterStatus')
        except OSError:
            status, cluster = 0, None
        if status == 200 and cluster and cluster.get('nodes'):
            break
        if time.time() > deadline:
            die('the local object store did not start (check `docker logs`)')
        time.sleep(0.5)

    def need(result, what):
        if result[0] not in (200, 204):
            die(f'local object store: {what} failed (HTTP {result[0]}); run `magehand dev up` again, '
                'or `magehand dev down` to start clean')
        return result[1]
    node = cluster['nodes'][0]
    if not node.get('role'):
        need(garage_admin(port, token, 'POST', '/v2/UpdateClusterLayout',
                          {'roles': [{'id': node['id'], 'zone': 'local', 'capacity': 10 * 1024 ** 3, 'tags': []}]}),
             'staging the layout')
        need(garage_admin(port, token, 'POST', '/v2/ApplyClusterLayout',
                          {'version': (cluster.get('layoutVersion') or 0) + 1}), 'applying the layout')
    status, info = garage_admin(port, token, 'GET', f'/v2/GetBucketInfo?globalAlias={urllib.parse.quote(bucket)}')
    if status == 404:
        info = need(garage_admin(port, token, 'POST', '/v2/CreateBucket', {'globalAlias': bucket}), 'making the bucket')
    elif status != 200:
        die(f'local object store: reading the bucket failed (HTTP {status})')
    need(garage_admin(port, token, 'POST', f'/v2/UpdateBucket?id={info["id"]}',
                      {'quotas': {'maxSize': GARAGE_QUOTA, 'maxObjects': None}}), 'setting the quota')
    name = f'magehand:{bucket}'
    key = None
    if key_id:   # the recorded key, if this Garage still has it (its volume may have been removed)
        status, found = garage_admin(port, token, 'GET',
                                     f'/v2/GetKeyInfo?id={urllib.parse.quote(key_id)}&showSecretKey=true')
        if status == 200 and found and found.get('secretAccessKey'):
            key = found
    if key is None:
        listed = need(garage_admin(port, token, 'GET', '/v2/ListKeys'), 'listing keys') or []
        for old in listed:   # a key from an earlier, half-finished attempt
            if old.get('name') == name:
                need(garage_admin(port, token, 'POST', f'/v2/DeleteKey?id={urllib.parse.quote(old["id"])}'),
                     'removing an old key')
        key = need(garage_admin(port, token, 'POST', '/v2/CreateKey', {'name': name}), 'making a key')
        if not key or not key.get('secretAccessKey'):
            die('local object store: it returned no key secret')
    need(garage_admin(port, token, 'POST', '/v2/AllowBucketKey', {
        'bucketId': info['id'], 'accessKeyId': key['accessKeyId'],
        'permissions': {'read': True, 'write': True, 'owner': False}}), 'allowing the key on the bucket')
    return key['accessKeyId'], key['secretAccessKey']


def cmd_dev(a):
    action = a.action
    app = app_here(a.app_flag or a.app)
    state = local_state(app['name'])
    network = f'magehand-{app["name"]}'
    contained = [b for b, svc in app['services'].items() if (svc or {}).get('type') in ('postgres', 'redis', 'bucket')]
    cli = runtime() if contained else None
    if action == 'up' and contained and not cli:
        die(no_runtime(f'the local {", ".join(contained)} block' + ('s' if len(contained) > 1 else '')))
    if action == 'up' and cli:  # blocks and `run --container` share it, like a namespace
        engine('network', 'create', network, check=False)
    for block, svc in app['services'].items():
        kind = (svc or {}).get('type')
        container = f'magehand-{app["name"]}-{block}'
        if kind not in ('postgres', 'redis', 'bucket'):
            continue
        if not cli:
            print(f'{block} ({kind}): no container runtime')
            continue
        if action == 'down':
            engine('rm', '-f', '-v', container, check=False)
            if kind == 'bucket':
                engine('volume', 'rm', '-f', f'{container}-data', check=False)
            state.pop(block, None)
            print(f'{block}: removed')
            continue
        running = engine('inspect', '-f', '{{.State.Running}}', container, check=False).stdout.strip() == 'true'
        if action == 'status':
            print(f'{block} ({kind}): {"running" if running else "not running"}')
            continue
        if kind == 'bucket':
            local_bucket(app, block, svc, state, container, network, running)
            continue
        # The password is the container's own environment (read back with container_env when a run needs
        # it), never the state file: a stopped container is started again with it, a new one gets a new one.
        if not running and engine('start', container, check=False).returncode != 0:
            password = secrets.token_urlsafe(24)
            engine('rm', '-f', container, check=False)
            if kind == 'postgres':
                engine('run', '-d', '--name', container, '--network', network, '-p', '127.0.0.1::5432',
                       '-e', 'POSTGRES_USER=app', '-e', 'POSTGRES_DB=app', '-e', 'POSTGRES_PASSWORD',
                       block_image(kind, svc), env={'POSTGRES_PASSWORD': password})
            else:   # read by sh from the container's own environment, not argv
                engine('run', '-d', '--name', container, '--network', network, '-p', '127.0.0.1::6379',
                       '-e', 'VALKEY_PASSWORD', block_image(kind, svc),
                       'sh', '-c', 'exec valkey-server --requirepass "$VALKEY_PASSWORD" --maxmemory 200mb '
                       '--maxmemory-policy allkeys-lru --appendonly yes', env={'VALKEY_PASSWORD': password})
        inner = '5432' if kind == 'postgres' else '6379'
        port = engine('port', container, inner).stdout.strip().splitlines()[0].rsplit(':', 1)[-1]
        state[block] = {'port': port, 'container': container}
        print(f'{block} ({kind}): 127.0.0.1:{port}')
    if not contained and action in ('up', 'status'):
        print(f"{app['name']} has no postgres/redis/bucket blocks: nothing runs locally "
              '(llm keys come from OpenBao when the app runs)')
    if action == 'up':
        save_local_state(app['name'], state)
    elif action == 'down':
        (STATE / f'{app["name"]}.json').unlink(missing_ok=True)
        if cli:
            engine('network', 'rm', network, check=False)
    print(next_line('`magehand run -- <your run command>` (the app with these blocks and its pod\'s env)'
                    if action == 'up' else None))


def local_bucket_name(app_name, block):
    """The bucket's name on the local Garage: <app>-<block>, bounded to Garage's 63 characters
    (an app name is up to 40 and a block up to 30) with a stable hash when it must be cut."""
    name = f'{app_name}-{block}'
    if len(name) <= 63:
        return name
    return f'{name[:52].rstrip("-")}-{hashlib.sha256(name.encode()).hexdigest()[:10]}'


def container_env(container):
    """The environment a container was started with ({} if it does not exist). The admin token
    lives only there: it is read back when needed, never written to the state file."""
    out = engine('inspect', '-f', '{{json .Config.Env}}', container, check=False)
    try:
        return dict(item.split('=', 1) for item in json.loads(out.stdout or '[]') if '=' in item)
    except ValueError:
        return {}


def local_bucket(app, block, svc, state, container, network, running):
    """Start (or keep) the app's local Garage for one bucket block and record how to reach it.
    Its data is a named volume that only `dev down` removes, so a stopped container (a reboot) is
    started again with its objects, and the key is reused while Garage still has it. No secret is
    kept in the state file: the admin token is the container's own environment and the key's
    secret is asked of Garage when a run needs it (see local_connection)."""
    mine = state.get(block, {})
    bucket = local_bucket_name(app['name'], block)
    if running and not container_env(container).get('GARAGE_ADMIN_TOKEN'):
        running = False   # not ours (or gone): start it again
    if not running and engine('start', container, check=False).returncode != 0:
        engine('rm', '-f', container, check=False)
        STATE.mkdir(parents=True, exist_ok=True)
        config = STATE / f'{app["name"]}-{block}-garage.toml'
        config.write_text(GARAGE_CONFIG)
        engine('run', '-d', '--name', container, '--network', network,
               '-p', '127.0.0.1::3900', '-p', '127.0.0.1::3903',
               '-v', f'{config}:/etc/garage.toml:ro', '-v', f'{container}-data:/data',
               '-e', 'GARAGE_RPC_SECRET', '-e', 'GARAGE_ADMIN_TOKEN', GARAGE_IMAGE, '/garage', 'server',
               env={'GARAGE_RPC_SECRET': secrets.token_hex(32), 'GARAGE_ADMIN_TOKEN': secrets.token_urlsafe(24)})
    token = container_env(container).get('GARAGE_ADMIN_TOKEN')
    if not token:
        die(f'cannot read the local object store\'s admin token from {container}: `magehand dev down` and up again')
    published = {}
    for inner, label in (('3900', 'port'), ('3903', 'admin_port')):
        published[label] = engine('port', container, inner).stdout.strip().splitlines()[0].rsplit(':', 1)[-1]
    key_id, _secret = garage_setup(published['admin_port'], token, bucket, key_id=mine.get('access_key_id'))
    state[block] = {'access_key_id': key_id, **published, 'bucket': bucket, 'container': container,
                    'access': (svc or {}).get('access') or 'private'}
    print(f'{block} (bucket): http://127.0.0.1:{published["port"]}/{bucket}  (local Garage)')


def local_connection(kind, block, local, app_name=None, app=None):
    """A block's Secret keys for a local run: on the host (published port), or
    with app_name, from a container on the app's Docker network. `app` is the app's name when it is
    needed to find the container (it is fixed: magehand-APP-BLOCK) and the run is on the host."""
    app = app or app_name
    if kind == 'secret':
        # Made for this run and kept nowhere (a secret never touches disk): a session key, say, is new
        # each run. Set the variable in your shell to keep one.
        return {'value': secrets.token_urlsafe(36)[:48]}
    if block not in local:
        die(f'block {block} ({kind}) has no local container: run `magehand dev up`')
    conn = local[block]
    if kind == 'bucket':
        endpoint = f'http://magehand-{app_name}-{block}:3900' if app_name else f'http://127.0.0.1:{conn["port"]}'
        # The key's secret is not in the state file: ask the running Garage (its admin token is the
        # container's environment). A stopped or removed store is a `dev up` away.
        token = container_env(conn.get('container') or f'magehand-{app}-{block}').get('GARAGE_ADMIN_TOKEN')
        found = None
        if token:
            try:
                status, found = garage_admin(conn['admin_port'], token, 'GET',
                                             f'/v2/GetKeyInfo?id={urllib.parse.quote(conn["access_key_id"])}'
                                             '&showSecretKey=true')
            except OSError:
                status, found = 0, None
        if not found or not found.get('secretAccessKey'):
            die(f'block {block} (bucket): the local object store is not running: `magehand dev up`')
        data = {'endpoint': endpoint, 'bucket': conn['bucket'], 'access_key_id': conn['access_key_id'],
                'secret_access_key': found['secretAccessKey'], 'region': 'garage',
                'access': conn.get('access', 'private'), 'path_style': 'true'}
        if data['access'] == 'public':   # the platform's URL is the data Worker's; here it is the S3 path
            data['url'] = f'{endpoint}/{conn["bucket"]}/'
        return data
    var = 'POSTGRES_PASSWORD' if kind == 'postgres' else 'VALKEY_PASSWORD'
    container = conn.get('container') or (f'magehand-{app}-{block}' if app else None)
    pw = container_env(container).get(var) if container else None
    if not pw:
        die(f'block {block} ({kind}): cannot read its password from the local container {container or "(unknown)"}: '
            'run `magehand dev up` (it starts a stopped one and refreshes this state). If it still cannot be '
            'read (a container from a much older magehand, or it is gone), `magehand dev down` then `dev up` '
            'makes a new one and deletes the local data')
    inner = '5432' if kind == 'postgres' else '6379'
    host, port = (f'magehand-{app_name}-{block}', inner) if app_name else ('127.0.0.1', conn['port'])
    if kind == 'postgres':
        return {'host': host, 'port': port, 'username': 'app', 'database': 'app', 'password': pw,
                'uri': f'postgresql://app:{pw}@{host}:{port}/app'}
    return {'host': host, 'port': port, 'password': pw, 'uri': f'redis://:{pw}@{host}:{port}/0'}


# --- run: the secrets adapter ---------------------------------------------------------

def dotenv(path):
    """KEY=VALUE lines from the repo's committed .magehand.env (local, non-secret overrides)."""
    values = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                values[key.strip()] = os.path.expandvars(value.strip().strip('"\''))
    return values


def resolve_env(app, container=False):
    """The pod's env, resolved for this machine. On the host, precedence is:
    manifest values < .magehand.env < the caller's environment < secrets
    (OpenBao, local blocks). In a container it is exactly the pod's: manifest
    values and secrets, with blocks reached over the app's Docker network."""
    plan = env_plan(app)
    env = {e[0]: e[2] for e in plan if e[1] == 'value'}
    if not container:
        env.update(dotenv(pathlib.Path('.magehand.env')))
        env.update({k: v for k, v in os.environ.items() if k in env})
    local, fetched, token = local_state(app['name']), {}, None
    for entry in plan:
        if entry[1] in ('value', 'unknown'):
            continue
        var, block, kind, key = entry
        if kind == 'secret' and not container and var in os.environ:
            continue   # a value you set in your shell wins: it is how to keep a generated secret stable
        if kind == 'bucket' and block not in local:
            # No local object store running: keep a value supplied in .magehand.env or the shell,
            # and say how to get one when there is none (it is optional: a bucket is not needed to start).
            if var not in env and var not in os.environ:
                print(f'magehand: {var}: bucket block {block} has no local object store; run `magehand dev up` '
                      '(or set the value in .magehand.env or your shell)', file=sys.stderr)
            continue
        if block not in fetched:
            if kind == 'llm':
                token = token or valid_token()
                path = f'apps/data/{app["name"]}-dev/llm-{block}'
                status, body = bao('GET', path, token)
                if status == 404:
                    die(no_llm_key(app['name'], block, path, status))
                if status != 200:
                    die(f'cannot read {path} (HTTP {status}); `magehand login` and try again')
                fetched[block] = body['data']['data']
            else:
                fetched[block] = local_connection(kind, block, local, app['name'] if container else None,
                                                  app=app['name'])
        if key not in fetched[block]:
            die(f'{var}: block {block} has no key {key!r} (keys: {", ".join(sorted(fetched[block]))})')
        env[var] = str(fetched[block][key])
    return env


def start_proxy(app_port_, who):
    """127.0.0.1:PROXY_PORT -> the app, adding the X-authentik-* headers Traefik would."""
    headers = {'X-authentik-uid': who.get('uid', 'local'), 'X-authentik-email': who.get('email', ''),
               'X-authentik-name': who.get('name', 'Local developer'),
               'X-authentik-username': who.get('username', 'local'), 'X-authentik-groups': who.get('groups', '')}

    class Proxy(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def forward(self):
            body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
            out = {k: v for k, v in self.headers.items()
                   if not k.lower().startswith('x-authentik-') and k.lower() not in ('connection', 'host')}
            out.update(headers)
            out['Host'] = self.headers.get('Host', f'127.0.0.1:{PROXY_PORT}')
            conn = http.client.HTTPConnection('127.0.0.1', app_port_, timeout=300)
            try:
                conn.request(self.command, self.path, body=body or None, headers=out)
                resp = conn.getresponse()
                data = resp.read()
            except OSError:
                self.send_error(502, 'app not reachable yet')
                return
            finally:
                conn.close()
            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() not in ('transfer-encoding', 'connection', 'content-length'):
                    self.send_header(k, v)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = forward

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(('127.0.0.1', PROXY_PORT), Proxy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


LAB_SUFFIX = '.' + LAB


def lab_hosts(env):
    """--add-host entries for the *.lab names in env values. Inside a container,
    public DNS's 127.0.0.1 would be the container: use what the host resolves
    (the Studio's LAN address on the MacBook) or, on the Studio, the host itself."""
    hosts = set()
    for value in env.values():
        host = urllib.parse.urlparse(value).hostname if '://' in value else None
        if host and host.endswith(LAB_SUFFIX):
            hosts.add(host)
    out = []
    for host in sorted(hosts):
        try:
            address = socket.gethostbyname(host)
        except OSError:
            die(f'cannot resolve {host}: {NOT_SET_UP}')
        out.append(f'{host}:{"host-gateway" if address.startswith("127.") else address}')
    return out


def container_args(app, env, image, port, hosts, extra):
    """docker run arguments for the app as its pod runs: read-only, non-root,
    no capabilities. Secrets are passed by name only (-e NAME): docker reads
    the values from its own environment, so they are never in argv or on disk."""
    pod_sc = app['pod'].get('securityContext') or {}
    user = f'{pod_sc.get("runAsUser", 65532)}:{pod_sc.get("runAsGroup", 65532)}'
    args = ['run', '--rm', '--name', f'magehand-{app["name"]}-app', '--network', f'magehand-{app["name"]}',
            '-p', f'127.0.0.1:{port}:{port}', '--read-only', '--tmpfs', '/tmp', '--user', user,  # nosec B108  # the container's own tmpfs
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges']
    if sys.stdin.isatty():
        args.append('-it')
    for host in hosts:
        args += ['--add-host', host]
    for name in sorted(env):
        args += ['-e', name]
    command = extra or (app['container'].get('command') or []) + (app['container'].get('args') or [])
    if command and not extra:
        args += ['--entrypoint', command[0], image, *command[1:]]
    else:
        args += [image, *command]
    return args


def cmd_run(a):
    proxy, container, build = not a.no_proxy, a.container, not a.no_build
    args = a.cmd[1:] if a.cmd[:1] == ['--'] else a.cmd
    if not args and not container:
        usage('run', 'give the command after --, e.g. magehand run -- python app.py (or --container)')
    here = app_repo_here()
    if a.app_flag and here and a.app_flag != here:
        die(f'this checkout is {here}\'s code: running it as {a.app_flag} would use {a.app_flag}\'s keys and budget. '
            f'Run it as itself (`magehand run -- CMD`); `magehand app` shows what {here} has')
    app = app_here(a.app_flag)
    env = resolve_env(app, container=container)
    print(f'magehand: {app["name"]} env: {", ".join(sorted(env))}', file=sys.stderr)
    if proxy:
        port = int(env.get('PORT') or app_port(app))
        start_proxy(port, identity())
        print(f'magehand: open http://127.0.0.1:{PROXY_PORT} (signed in as '
              f'{identity().get("email") or "local"}; the app itself listens on {port})', file=sys.stderr)
    if container:
        cli = runtime()
        if not cli:
            die(no_runtime('run --container'))
        image = f'magehand/{app["name"]}:local'
        if build:
            print(f'magehand: {cli} build -t {image} .', file=sys.stderr)
            if subprocess.run([cli, 'build', '-t', image, '.']).returncode:  # nosec B603  # docker/podman
                die(f'{cli} build failed')
        engine('network', 'create', f'magehand-{app["name"]}', check=False)
        engine('rm', '-f', f'magehand-{app["name"]}-app', check=False)
        port = int(env.get('PORT') or app_port(app))
        args = [cli, *container_args(app, env, image, port, lab_hosts(env), args)]
    child = subprocess.Popen(args, env={**os.environ, **env})  # nosec B603  # the user's own command
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda s, _f: child.send_signal(s))
    sys.exit(child.wait())


# --- doctor and update ---------------------------------------------------------------

def checkout_root(path='.'):
    """The top of the Git checkout containing `path`, or None outside one."""
    out = quiet(['git', '-C', str(path), 'rev-parse', '--show-toplevel'])
    return pathlib.Path(out.stdout.strip()) if out and out.returncode == 0 and out.stdout.strip() else None


def current_repo(path='.'):
    """OWNER/NAME of the app repo whose checkout contains `path` (from its origin remote; works from any
    subdirectory), or None outside one."""
    root = checkout_root(path)
    if not root or not is_app_checkout(root):
        return None
    out = quiet(['git', '-C', str(root), 'remote', 'get-url', 'origin'])
    m = re.search(rf'github\.com[:/]{OWNER}/([\w.-]+?)(?:\.git)?\s*$', out.stdout) if out and out.returncode == 0 else None
    return f'{OWNER}/{m.group(1)}' if m else None


def gh_probe(*args):
    """gh_cli for a diagnostic: (None, '', reason) when gh is missing or hangs, instead of exiting, so doctor
    still reports (a machine that signs in with GITHUB_TOKEN has no gh)."""
    try:
        return gh_cli(*args)
    except SystemExit:
        return None, '', 'the GitHub CLI (gh) is not installed'
    except (OSError, subprocess.TimeoutExpired):
        return None, '', 'gh did not answer'


def repo_state(repo):
    """{'ruleset': 'ok'|'missing'|'weak'|None, 'ruleset_detail': str, 'bot': 'write'|'read'|'invited'|'missing'|None,
    'private': True|False|None, 'archived': bool}: the repo's approval ruleset, whether OpenClaw's bot can write
    to it, and whether it is private (an archived repo, a retired app, is not checked further). None means unknown (no gh, no admin, rate limit, a server or network error), which is not
    the same as a confirmed 'missing' (HTTP 404)."""
    code, out, _ = gh_probe('api', f'repos/{repo}', '--jq', '.private, .archived')
    flags = out.split() if code == 0 else []
    private = {'true': True, 'false': False}.get(flags[0]) if flags else None
    if flags[1:2] == ['true']:  # archived: a retired app, nothing to check or repair
        return {'ruleset': None, 'ruleset_detail': '', 'bot': None, 'private': private, 'archived': True}
    ruleset, detail = ruleset_status(repo)
    bot, _ = bot_access(repo)
    return {'ruleset': ruleset, 'ruleset_detail': detail, 'bot': bot, 'private': private, 'archived': False}


def cmd_doctor(a):
    ok = True
    results = []

    def check(label, passed, hint=''):
        nonlocal ok
        ok = ok and passed
        results.append({'check': label, 'ok': bool(passed), 'hint': '' if passed else hint})
        if not a.json:
            print(f'{"ok  " if passed else "FAIL"} {label}{"" if passed else "  -> " + hint}')

    try:
        github_token()
        check('GitHub access', github_file(APPS_REPO, 'catalog.yaml', required=False) is not None)
    except SystemExit:
        check('GitHub access', False, '`gh auth login`')
    managed = install_method() == 'managed'
    if managed:  # OpenClaw's VM: no OpenBao and no passkey, and nothing it does needs them
        note = 'OpenBao and sign-in: not used in OpenClaw\'s VM (the platform gives apps their keys)'
        results.append({'check': 'OpenBao', 'ok': None, 'hint': note})
        if not a.json:
            print(f'--   {note}')
        left = None
    else:
        blocked = False
        try:
            reach = urllib.request.urlopen(f'{OPENBAO}/v1/sys/health', timeout=5).status == 200  # nosec B310
        except urllib.error.HTTPError:
            reach = False
        except OSError as error:
            reach, blocked = False, local_network_blocked(error)
        check(f'reach {OPENBAO}', reach, f'{LOCAL_NETWORK}: {local_network_step()}' if blocked else NOT_SET_UP)
        left = signin_left() if reach else None
        check('signed in to OpenBao' + (f', {minutes(left)} left' if left is not None else ''), left is not None,
              '`magehand login`')
    if left is not None and left < SIGNIN_WARN:
        note = f'sign-in has {minutes(left)} left; `magehand login` renews it before a long task'
        results.append({'check': 'sign-in time left', 'ok': None, 'hint': note})
        if not a.json:
            print(f'--   {note}')
    missing = [] if managed else approvals_missing()
    if missing:
        note = (f'pre-approved commands for {" and ".join(missing)}: not installed (`magehand setup` installs them, '
                'so routine magehand and gh commands need no prompt)')
        results.append({'check': 'pre-approved commands', 'ok': None, 'hint': note})
        if not a.json:
            print(f'--   {note}')
    repo = current_repo()
    state = repo_state(repo) if repo else None
    if state and state['archived']:
        note = 'archived (a retired app): its setup is not checked'
        results.append({'check': repo, 'ok': None, 'hint': note})
        if not a.json:
            print(f'--   {repo}: {note}')
    elif state:
        # Run in the checkout, `new` repairs the repo it is in and clones nothing.
        fix = f'`magehand new {repo.split("/", 1)[1]}` (safe to re-run; it repairs an existing repo)'
        weak = f'{state["ruleset_detail"]}; fix it in the repo\'s Settings > Rules (magehand new leaves it alone)'
        unread = 'could not be read (needs gh, and admin for the ruleset and invitations)'
        bot = state['bot']
        for label, value, hint, note in (
                (f"{repo}: OpenClaw's bot can write", None if bot in (None, 'invited') else bot == 'write',
                 f'{BOT} can only read: {fix}' if bot == 'read' else
                 f'not a collaborator and not invited, so a review request adds nobody: {fix}',
                 'invited; OpenClaw accepts it within about 5 minutes, then request its review again'
                 if bot == 'invited' else unread),
                (f"{repo}: ruleset '{RULESET['name']}'", None if state['ruleset'] is None else state['ruleset'] == 'ok',
                 weak if state['ruleset'] == 'weak' else fix, unread),
                (f'{repo}: private', state['private'],
                 f'app repos are private: gh repo edit {repo} --visibility private '
                 '--accept-visibility-change-consequences', unread)):
            if value is None:  # pending, or unknown (no gh, no admin, rate limit, network): a note, never a failure
                results.append({'check': label, 'ok': None, 'hint': note})
                if not a.json:
                    print(f'--   {label}: {note}')
            else:
                check(label, value, hint)
    # The owner's other app repos: their AGENTS.md changes only through a PR in each, so doctor lists the ones
    # behind the template (not a failure of this machine; the managed copy works on whatever repo it was given).
    stale, unread = (None, None) if managed else stale_agents()
    agents_step = None
    if stale:
        agents_step = (f'in each checkout: `magehand update-agents`, commit and open a PR ({", ".join(stale)})')
        note = f'AGENTS.md behind the template in {len(stale)} app repo(s): {", ".join(stale)}'
        results.append({'check': 'app repos\' AGENTS.md', 'ok': None, 'hint': note, 'stale': stale})
        if not a.json:
            print(f'--   {note}')
    elif unread:
        note = f"app repos' AGENTS.md: not checked ({unread})"
        results.append({'check': 'app repos\' AGENTS.md', 'ok': None, 'hint': note})
        if not a.json:
            print(f'--   {note}')
    elif stale == []:
        check("app repos' AGENTS.md match the template", True)
    # Cluster access is a note, never a failure: only `job ls` and status's per-pod detail use it, and a laptop
    # normally has none (dev's state is in GitHub Deployments; OpenClaw reads the cluster).
    absent = no_cluster(cluster_access() or '')
    note = (f'cluster access: none, {absent.split("(", 1)[1].rstrip(")")}: `magehand job ls` and status\'s pod detail '
            "can't read dev here (normal on a laptop; OpenClaw can)" if absent else 'cluster access: kubectl has a context')
    results.append({'check': 'cluster access', 'ok': None, 'hint': note})
    if not a.json:
        print(f'--   {note}')
    configured = configured_runtime()
    found = [] if configured else detect_runtimes()
    chosen = choose_runtime(configured, found)
    # Not a failure without one: only postgres/redis blocks and run --container need it.
    step = next((r['hint'] for r in results if r['ok'] is False), agents_step)
    if a.json:
        print(json.dumps({'ok': ok, 'checks': results, 'runtime': chosen, 'next': step}, indent=1))
    elif managed:
        print('--   container runtime: not used in OpenClaw\'s VM (CI builds and tests the app)')
        print(next_line(step))
    else:
        print(f'{"ok  " if chosen else "--  "} container runtime: {chosen or "none"}'
              + ('' if chosen else f' (needed only for local postgres/redis blocks and run --container: '
                                   f'{SUPPORTED}; `magehand runtime`)'))
        print(next_line(step))
    sys.exit(0 if ok else 1)


# --- check: the platform's rules for an app's code and deployment -------------------
# Run before every push (the homelab-app skill says so) and in CI (homelab-apps
# build-app.yaml). Each rule is a mistake an app can make about the platform;
# every finding names the guide section that explains it. Errors fail (exit 1),
# warnings are for the author to fix or explain in the PR.

# The keys each block's Secret holds, when catalog.yaml doesn't list them.
BLOCK_KEYS = {
    'postgres': {'keys': ['host', 'port', 'username', 'password', 'database', 'uri']},
    'redis': {'keys': ['host', 'port', 'password', 'uri']},
    'secret': {'keys': ['value']},
    'llm': {'keys': ['api_key', 'base_url', 'models', 'search_url'], 'key_options': {'search_url': 'search'}},
    'bucket': {'keys': ['endpoint', 'bucket', 'access_key_id', 'secret_access_key', 'region', 'access',
                        'path_style', 'url'],
               'key_options': {'url': 'access'}},   # url exists only for access: public (ENUM_OPTION_VALUES)
}
# key_options naming an enumerated option: the value that makes the key exist (a boolean option needs true).
ENUM_OPTION_VALUES = {'access': 'public'}
CODE_SUFFIXES = ('.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.go', '.rb', '.php', '.java', '.kt', '.rs')
SKIP_DIRS = {'.git', 'node_modules', '.venv', 'venv', '__pycache__', 'dist', 'build', '.next', 'vendor', 'target',
             'tests', 'test', '__tests__', 'spec', 'e2e', 'ui-fixtures', 'fixtures'}
ENV_READ = re.compile(r'''(?:os\.environ\.get|os\.getenv|environ\.get|os\.Getenv|Deno\.env\.get|ENV\.fetch)\(\s*["']([A-Z][A-Z0-9_]*)["']'''
                      r'''|(?:os\.environ|process\.env|ENV)\[\s*["']([A-Z][A-Z0-9_]*)["']\s*\]'''
                      r'''|process\.env\.([A-Z][A-Z0-9_]*)''')
# Set by the runtime or the image, not the pod spec.
RUNTIME_ENV = {'HOME', 'PATH', 'HOSTNAME', 'USER', 'PWD', 'SHELL', 'LANG', 'LC_ALL', 'TZ', 'TERM', 'TMPDIR',
               'NODE_ENV', 'PYTHONPATH', 'PYTHONUNBUFFERED', 'CI', 'DEBUG'}
PROVIDER_HOSTS = ('api.openai.com', 'api.anthropic.com', 'generativelanguage.googleapis.com', 'api.mistral.ai',
                  'api.groq.com', 'openrouter.ai', 'api.cohere.com', 'api.cohere.ai', 'api.together.xyz',
                  'api.perplexity.ai', 'api.deepseek.com', 'api.x.ai', 'api.exa.ai', 'api.tavily.com', 'serpapi.com',
                  'api.search.brave.com', 'api.bing.microsoft.com', 'customsearch.googleapis.com')
PROVIDER_KEY = re.compile(r'^(OPENAI|ANTHROPIC|GEMINI|GOOGLE_AI|MISTRAL|GROQ|OPENROUTER|COHERE|TOGETHER|PERPLEXITY|'
                          r'DEEPSEEK|XAI|EXA|TAVILY|SERPAPI|BRAVE)_[A-Z_]*KEY$')
OWN_AUTH = re.compile(r'''^\s*(?:import|from)\s+(passlib|bcrypt|argon2|flask_login|flask_security|authlib|jose)\b'''
                      r'''|require\(\s*["'](bcrypt|bcryptjs|passport[\w-]*|next-auth|jsonwebtoken|express-session)["']\s*\)'''
                      r'''|from\s+["'](bcrypt|bcryptjs|passport[\w-]*|next-auth|@auth/[\w-]+|jsonwebtoken|express-session)["']''',
                      re.MULTILINE)
LOCAL_DB = re.compile(r'''sqlite3\.connect\(\s*(?!["']:memory:)|better-sqlite3|new\s+sqlite3\.Database\(|gorm\.Open\(\s*sqlite''')
# A scheduler inside the app's process: it runs once per replica and stops with the pod.
IN_PROCESS_SCHEDULER = re.compile(r'''^\s*(?:import|from)\s+(apscheduler|schedule|crontab|rq_scheduler)\b'''
                                  r'''|require\(\s*["'](node-cron|node-schedule|cron|croner)["']\s*\)'''
                                  r'''|from\s+["'](node-cron|node-schedule|cron|croner)["']''', re.MULTILINE)
SECRET_NAME_RE = re.compile(r'(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)$')
SECRET_VALUE_RE = re.compile(r'^(sk-|ghp_|github_pat_|xox[bp]-|hvs\.)[A-Za-z0-9_-]{8,}')
LAB_URL = re.compile(r'https?://[a-z0-9.-]+\.lab\.davidlarrimore\.com')


def finding(level, where, text, see):
    return {'level': level, 'where': where, 'text': text, 'see': see}


def block_spec(catalog, kind):
    """{'keys': [...], 'key_options': {key: option}} for a block type: catalog.yaml's, else built in."""
    spec = ((catalog or {}).get('blocks') or {}).get(kind) or {}
    return {'keys': spec.get('keys') or BLOCK_KEYS.get(kind, {}).get('keys') or [],
            'key_options': spec.get('key_options') or BLOCK_KEYS.get(kind, {}).get('key_options') or {}}


def check_app_name(app):
    """The app's name against its repo and homelab-apps (only for a checkout's deploy/): `app:` is a valid
    name, no other repo registered the name, and a repo registered under another name still says so."""
    found = []
    root = app.get('root')
    if app.get('where') != DEPLOY or not root:
        return found
    at, guide = f'{DEPLOY}/app.yaml', 'README "How an app is put together"'
    name, repo = app['name'], remote_repo(root)
    if 'app' in app['spec']:
        value = app['spec']['app']
        if not isinstance(value, str) or not APP_NAME.match(value) or value.endswith(('-dev', '-prod')) \
                or value.startswith('kube-'):
            return [finding('error', at, f'app: {value!r} is not an app name (lowercase letters, digits and dashes, '
                            'at most 40, not ending in -dev or -prod)', guide)]
        if value in RESERVED:
            return [finding('error', at, f'app: {value} is reserved for the platform', guide)]
    if not repo:
        return found
    registered = registration(name)
    if registered is not None:
        owner = (registered.get('source') or {}).get('repo')
        if owner != repo:
            found.append(finding('error', at, f'the app name {name} is registered in homelab-apps to '
                                 f'{"davidlarrimore/" + owner if owner else "code kept there"}, not to this repo '
                                 f'({repo}): set another `app:`', guide))
    if APPS_DIR:  # CI: homelab-apps' apps/ is on disk, so a repo registered under another name is found
        for f in sorted(pathlib.Path(APPS_DIR, 'apps').glob('*/app.yaml')):
            try:
                other = (yaml.safe_load(f.read_text()) or {}).get('source') or {}
            except (OSError, yaml.YAMLError, AttributeError):
                continue
            if f.parent.name != name and isinstance(other, dict) and other.get('repo') == repo:
                found.append(finding('error', at, f'this repo is registered as the app {f.parent.name}, but '
                                     f'{at} names {name}: restore `app: {f.parent.name}` (the image is named '
                                     'after the app)', guide))
    return found


def seeded_name_findings(name, root):
    """The app-name rules for a checkout whose deploy/ has only app.yaml (no Deployment yet, so no pod to
    check): `app:` is a valid, unreserved name that homelab-apps hasn't registered to another repo."""
    local = deploy_dir(root)
    if not local:
        return []
    try:
        spec = yaml.safe_load((local / 'app.yaml').read_text()) or {}
    except (OSError, yaml.YAMLError) as error:
        return [finding('error', f'{DEPLOY}/app.yaml', f'not valid YAML: {str(error).splitlines()[0]}',
                        'README "How an app is put together"')]
    if not isinstance(spec, dict):
        return [finding('error', f'{DEPLOY}/app.yaml', 'must be a mapping (app:, services:)',
                        'README "How an app is put together"')]
    return check_app_name({'name': name, 'spec': spec, 'where': DEPLOY, 'root': local.parent})


def check_manifests(app, catalog):
    """Rules for homelab-apps apps/<app>/: the blocks requested and the pod's env."""
    found = check_app_name(app)
    where = app.get('where') or f"homelab-apps apps/{app['name']}"
    deployment_file = app.get('deployment_file') or f'{where}/base/deployment.yaml'
    if where == DEPLOY and 'source' in app['spec']:
        found.append(finding('error', f'{where}/app.yaml', 'source: is added by the copy in homelab-apps; '
                             'remove it here', 'README "How an app is put together"'))
    blocks = (catalog or {}).get('blocks') or {}
    models = {m.get('id') for m in (catalog or {}).get('models') or [] if isinstance(m, dict)}
    for name, svc in app['services'].items():
        svc = svc or {}
        kind = svc.get('type')
        if blocks and kind not in blocks:
            found.append(finding('error', f'{where}/app.yaml', f'block {name}: unknown type {kind!r}; types: '
                                 f'{", ".join(sorted(blocks))}', 'blocks'))
            continue
        options = blocks.get(kind) or {}
        if svc.get('size') and options.get('sizes') and svc['size'] not in options['sizes']:
            found.append(finding('error', f'{where}/app.yaml', f'block {name}: size {svc["size"]!r} not offered '
                                 f'({", ".join(options["sizes"])})', 'blocks'))
        if kind == 'llm':
            for model in svc.get('models') or []:
                if models and model not in models:
                    found.append(finding('error', f'{where}/app.yaml', f'block {name}: model {model!r} is not in '
                                         'the catalog (magehand guide catalog)', 'blocks "Models"'))
            if 'search' in svc and not isinstance(svc['search'], bool):
                found.append(finding('error', f'{where}/app.yaml', f'block {name}: search must be true or false',
                                     'blocks "Web search"'))
        if kind == 'bucket':
            if not re.fullmatch(r'[a-z]([a-z0-9-]{0,28}[a-z0-9])?', str(name)):
                found.append(finding('error', f'{where}/app.yaml', f'block {name}: a bucket name is lowercase '
                                     'letters, digits and dashes, starting with a letter (at most 30)',
                                     'blocks "Object storage"'))
            offered = options.get('access_values') or []
            if 'access' in svc and offered and svc['access'] not in offered:
                found.append(finding('error', f'{where}/app.yaml', f'block {name}: access {svc["access"]!r} not '
                                     f'offered ({", ".join(offered)})', 'blocks "Object storage"'))
            if 'writers' in svc:
                allowed = options.get('writer_values') or []
                writers = svc['writers']
                if not isinstance(writers, list) or (allowed and not set(map(str, writers)) <= set(allowed)):
                    found.append(finding('error', f'{where}/app.yaml', f'block {name}: writers must be a list '
                                         f'within [{", ".join(allowed)}]', 'blocks "Object storage"'))
                elif writers and svc.get('access') == 'internal':
                    found.append(finding('error', f'{where}/app.yaml', f'block {name}: writers are not available '
                                         'on an internal bucket (it is in-cluster only)', 'blocks "Object storage"'))
    container = app['container']
    image = container.get('image') or ''
    own = r'ghcr\.io/davidlarrimore/homelab-apps/' + re.escape(app['name'])
    pin = app.get('pin')
    if pin and not re.fullmatch(own + r'@sha256:[0-9a-f]{64}', str(pin.get('image') or '')):
        found.append(finding('error', f"homelab-apps apps/{app['name']}/app.yaml",
                             f'the pinned image {pin.get("image") or "(none)"} is not the app\'s own '
                             f'ghcr.io/davidlarrimore/homelab-apps/{app["name"]}@sha256:...', 'README "How an app is put together"'))
    # An app repo's deploy/base (the checkout's, or the one a pin points at) names the image without a digest:
    # the pin's `image` is applied on top of it by the ApplicationSet.
    if (where == DEPLOY or pin) and not re.fullmatch(own + r'(@sha256:[0-9a-f]{64})?', image):
        found.append(finding('error', deployment_file, f'image {image or "(none)"}: write the app\'s own image '
                             f'without a digest (ghcr.io/davidlarrimore/homelab-apps/{app["name"]}); merging pins '
                             'the build of that commit', 'README "How an app is put together"'))
    elif where != DEPLOY and not pin and not re.fullmatch(own + r'(:[\w.-]+)?@sha256:[0-9a-f]{64}', image):
        found.append(finding('error', deployment_file, f'image {image or "(none)"}: an app runs only '
                             f'its own image, pinned by digest (ghcr.io/davidlarrimore/homelab-apps/{app["name"]}@sha256:...)',
                             'README "How an app is put together"'))
    return found + env_findings(app, container, deployment_file, catalog)


def env_findings(app, container, deployment_file, catalog):
    """Rules for a container's env (the Deployment's, or a CronJob's): each Secret it maps is a block's or the
    app's own, a block key exists with the block's options, and nothing is optional or bulk-mapped."""
    found = []
    for item in container.get('env') or []:
        ref = ((item.get('valueFrom') or {}).get('secretKeyRef')) or {}
        if not ref:
            continue
        var, secret, key = item.get('name'), ref.get('name'), ref.get('key')
        block, kind = block_of(app, secret)
        if not block and secret in app.get('provided', ()):
            continue  # the app's own ExternalSecret (homelab-apps AGENTS.md "Secrets")
        if not block:
            found.append(finding('error', deployment_file, f'env {var}: Secret {secret!r} is neither '
                                 'a block\'s nor made by an ExternalSecret in the app\'s manifests, so the pod would '
                                 'wait for it forever. Request a block in app.yaml (type secret for a generated value)',
                                 'blocks'))
            continue
        if ref.get('optional'):
            found.append(finding('error', deployment_file, f'env {var}: optional: true on a block key. '
                                 'The pod would start with an empty value and keep it for its whole life; use a plain '
                                 'secretKeyRef (the pod waits until the block is ready)', 'blocks'))
        spec = block_spec(catalog, kind)
        if spec['keys'] and key not in spec['keys']:
            found.append(finding('error', deployment_file, f'env {var}: block {block} ({kind}) has no '
                                 f'key {key!r}; keys: {", ".join(spec["keys"])}', 'blocks'))
        option = spec['key_options'].get(key)
        wanted = ENUM_OPTION_VALUES.get(option, True)
        if option and (app['services'].get(block) or {}).get(option) != wanted:
            shown = f'{option}: {"true" if wanted is True else wanted}'
            found.append(finding('error', deployment_file, f'env {var}: key {key} exists only with '
                                 f'`{shown}` on block {block}; without it the pod waits forever',
                                 'blocks "Web search"' if option == 'search' else 'blocks'))
    for source in container.get('envFrom') or []:
        found.append(finding('warning', deployment_file, f'envFrom {source}: map each variable with '
                             'env + secretKeyRef instead, so `magehand app` and `magehand run` can see it', 'blocks'))
    return found


# What the platform lets an app deploy (the only copy of this list: app CI runs `check --strict`; Argo CD's
# agent-app-bundles project and Kyverno enforce it in the cluster): namespace, quota, limits and network policy come from the platform.
ALLOWED_KINDS = {
    ('', 'ConfigMap'), ('', 'Secret'), ('', 'Service'), ('', 'ServiceAccount'), ('', 'PersistentVolumeClaim'),
    ('apps', 'Deployment'), ('apps', 'StatefulSet'), ('batch', 'Job'), ('batch', 'CronJob'),
    ('networking.k8s.io', 'Ingress'), ('policy', 'PodDisruptionBudget'), ('autoscaling', 'HorizontalPodAutoscaler'),
    ('external-secrets.io', 'ExternalSecret'),
}
# Ingress middlewares (Kyverno enforces the same in the cluster).
LAB_AUTH = 'authentik-forward-auth@kubernetescrd'
STRIP_AUTH = 'kube-system-strip-authentik-headers@kubernetescrd'
PUBLIC_AUTH = 'authentik-public-forward-auth@kubernetescrd'
# The cluster's admission policies, as homelab-apps keeps a copy for CI (the homelab's sync-catalog.py --apps).
POLICY_DIR = '.github/kyverno/policies'
POLICY_DIGEST = 'sha256:' + '0' * 64  # any digest passes the same rules: Argo CD applies the pinned one
# kubeconform's schemas: Kubernetes' own, and the CRD catalog for ExternalSecret (as build-app.yaml runs it).
CRD_SCHEMAS = 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
TOOL_INSTALL = {'kyverno': '`brew install kyverno`', 'kubeconform': '`brew install kubeconform`'}


def render_overlay(local, overlay):
    """(docs, None) from `kubectl kustomize deploy/<overlay>`, as CI renders it; (None, why) when it can't be
    rendered on this machine (no kubectl: skipped), or ('failed', message) when kustomize refuses it."""
    if not shutil.which('kubectl'):
        return None, 'kubectl is not installed'
    try:
        out = subprocess.run(['kubectl', 'kustomize', str(local / overlay)], capture_output=True, text=True,  # nosec B603 B607
                             timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None, 'kubectl kustomize did not answer'
    if out.returncode != 0:
        return 'failed', (out.stderr.strip().splitlines() or ['kubectl kustomize failed'])[-1]
    try:
        return [d for d in yaml.safe_load_all(out.stdout) if isinstance(d, dict)], None
    except yaml.YAMLError as error:
        return 'failed', str(error).splitlines()[0]


def check_rendered(root, name):
    """The rules CI applies to deploy/ rendered by kustomize (build-app.yaml "Render and check deploy/"): each
    overlay's namespace, the kinds an app may deploy, Ingress hosts and middlewares, and the `app` container the
    platform pins the commit on. Returns (findings, skipped): skipped names what couldn't be rendered here."""
    local = deploy_dir(root)
    if not local or not name or not any((local / 'base').glob('*.y*ml')):
        return [], []
    found, skipped, rendered = [], [], []
    domain = LAB.split('.', 1)[1]
    for overlay, namespace in (('dev', f'{name}-dev'), ('prod', name)):
        where = f'{DEPLOY}/{overlay}'
        if overlay == 'dev' and not (local / overlay / 'kustomization.yaml').is_file():
            continue  # check_deploy_files reports a missing dev overlay
        if overlay == 'prod' and not (local / overlay).is_dir():
            continue  # production is optional, but CI renders deploy/prod whenever it exists
        if not (local / overlay / 'kustomization.yaml').is_file():
            found.append(finding('error', f'{where}/kustomization.yaml', f'missing: CI renders {where} because it '
                                 f'exists (namespace: {namespace}, resources: [../base]), or remove {where}',
                                 'README "How an app is put together"'))
            continue
        docs, why = render_overlay(local, overlay)
        if docs == 'failed':
            found.append(finding('error', where, f'kustomize cannot render it: {why}', 'README "How an app is put together"'))
            continue
        if docs is None:
            skipped.append(f'{where}: not rendered ({why}); CI renders and checks it')
            continue
        rendered.append((overlay, docs))
        lab, public = f'{namespace}.{LAB}', f'{namespace}.{domain}'
        for doc in docs:
            meta = doc.get('metadata') or {}
            kind, label = doc.get('kind', ''), f'{where}: {doc.get("kind", "?")} {meta.get("name", "?")}'
            group = str(doc.get('apiVersion', '')).rpartition('/')[0]
            if meta.get('namespace') not in (None, namespace):
                found.append(finding('error', label, f'namespace must be {namespace} (found {meta["namespace"]})',
                                     'README "How an app is put together"'))
            if (group, kind) not in ALLOWED_KINDS:
                found.append(finding('error', label, f'{kind} ({group or "core"}) is not allowed in an app',
                                     'homelab-apps AGENTS.md "Not allowed"'))
            if kind == 'Ingress':
                hosts = {r.get('host') for r in (doc.get('spec') or {}).get('rules') or []}
                middlewares = [m.strip() for m in (meta.get('annotations') or {}).get(
                    'traefik.ingress.kubernetes.io/router.middlewares', '').split(',') if m.strip()]
                allowed = {lab, public} if overlay == 'prod' else {lab}
                see = 'homelab-apps AGENTS.md "Networking and access"'
                if len(hosts) != 1 or not hosts <= allowed:
                    found.append(finding('error', label, f'one host per Ingress, one of {", ".join(sorted(allowed))} '
                                         f'(found {", ".join(sorted(h or "none" for h in hosts)) or "none"})', see))
                elif hosts == {lab} and LAB_AUTH not in middlewares:
                    found.append(finding('error', label, f'an internal host needs the middleware {LAB_AUTH}', see))
                elif hosts == {public} and (not middlewares or middlewares[0] != STRIP_AUTH or LAB_AUTH in middlewares):
                    found.append(finding('error', label, f'a public host needs {STRIP_AUTH} first (then {PUBLIC_AUTH} '
                                         f'on signed-in paths), never {LAB_AUTH}', see))
            if overlay == 'dev' and kind == 'Deployment' and meta.get('name') == name:
                containers = [c.get('name') for c in ((doc.get('spec') or {}).get('template') or {}).get('spec', {})
                              .get('containers') or []]
                if 'app' not in containers:
                    found.append(finding('error', label, f'needs a container named `app` (it has {containers}): the '
                                         'platform pins the commit and image on it',
                                         'homelab-apps AGENTS.md "Apps with their own repo"'))
    if rendered:
        more, missed = check_admission(name, rendered)
        found, skipped = found + more, skipped + missed
        more, missed = check_schemas(rendered)
        found, skipped = found + more, skipped + missed
    return found, skipped


def tool_missing(tool, what):
    """The `skipped` line for a check whose tool isn't installed here."""
    return f'{DEPLOY}: {what} not checked ({tool} is not installed: {TOOL_INSTALL[tool]}); CI checks them'


def run_tool(argv, timeout, **kwargs):
    """(result, None) from running a check's tool, or (None, why) when it couldn't start or didn't finish: the
    caller reports that as a skipped check (an error with --strict), never a crash that loses the other findings."""
    tool = pathlib.Path(argv[0]).name
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, **kwargs), None  # nosec B603
    except subprocess.TimeoutExpired:
        return None, f'{tool} did not finish within {timeout}s'
    except OSError as error:
        return None, f'{tool} could not run: {error.strerror or error}'


def check_admission(name, rendered):
    """The cluster's admission policies (Kyverno) on each rendered overlay, as homelab-apps CI runs them: the
    render made to look as Argo CD applies it (the app's own image pinned by a digest, and the Application's
    tracking id, which the production policies match on). (findings, skipped)."""
    kyverno = shutil.which('kyverno')
    if not kyverno:
        return [], [tool_missing('kyverno', 'admission policies')]
    names = [n for n in github_dir(APPS_REPO, POLICY_DIR) if n.endswith(('.yaml', '.yml'))]
    if not names:
        return [], [f'{DEPLOY}: admission policies not checked (cannot read {APPS_REPO} {POLICY_DIR}); CI checks them']
    found, skipped, see = [], [], 'homelab-apps AGENTS.md "Every workload must"'
    with tempfile.TemporaryDirectory() as tmp:
        policies = pathlib.Path(tmp) / 'policies'
        policies.mkdir()
        for n in names:
            (policies / n).write_text(github_file(APPS_REPO, f'{POLICY_DIR}/{n}'))
        for overlay, docs in rendered:
            where, env = f'{DEPLOY}/{overlay}', 'prod' if overlay == 'prod' else 'dev'
            kdir = pathlib.Path(tmp) / overlay
            kdir.mkdir()
            (kdir / 'app.yaml').write_text(yaml.safe_dump_all(docs, sort_keys=False))
            (kdir / 'kustomization.yaml').write_text(json.dumps({
                'resources': ['app.yaml'],
                'images': [{'name': f'ghcr.io/davidlarrimore/homelab-apps/{name}', 'digest': POLICY_DIGEST}],
                'commonAnnotations': {'argocd.argoproj.io/tracking-id': f'{name}-{env}:policy-check'}}))
            applied, why = run_tool(['kubectl', 'kustomize', str(kdir)], 60)
            if why:
                skipped.append(f'{where}: admission policies not checked ({why}); CI checks them')
                continue
            if applied.returncode:
                found.append(finding('error', where, 'cannot prepare it for the admission policies: '
                                     + (applied.stderr.strip().splitlines() or ['kubectl kustomize failed'])[-1], see))
                continue
            (kdir / 'applied.yaml').write_text(applied.stdout)
            out, why = run_tool([kyverno, 'apply', str(policies), '--resource', str(kdir / 'applied.yaml')], 120)
            if why:
                skipped.append(f'{where}: admission policies not checked ({why}); CI checks them')
                continue
            text = out.stdout + out.stderr
            m = re.search(r'pass: (\d+), fail: (\d+), warn: \d+, error: (\d+)', text)
            if not m:
                found.append(finding('error', where, 'Kyverno gave no result: '
                                     + (text.strip().splitlines() or ['no output'])[-1], see))
            elif int(m.group(2)) or int(m.group(3)):
                found += [finding('error', where, f'the cluster would refuse it: {refusal}', see)
                          for refusal in policy_refusals(text)]
    return found, skipped


def policy_refusals(text):
    """Each refusal in `kyverno apply`'s output ("policy P -> resource R failed:" and its numbered reasons,
    "1 -  why" in 1.19, "1. rule: why" before), one string each; the output's own lines when it has none."""
    refusals, current = [], None
    for line in text.splitlines():
        line = line.strip()
        head = re.match(r'policy (\S+) -> resource (\S+) (?:failed|error)', line)
        if head:
            current = [f'{head.group(1)} on {head.group(2).strip("/").rsplit("/", 1)[-1]}']
            refusals.append(current)
        elif current is not None and re.match(r'\d+(?:\.|\s+-)\s', line):
            current.append(re.sub(r'^\d+(?:\.|\s+-)\s+', '', line))
        elif not line:
            current = None
    if refusals:
        return [': '.join([r[0], '; '.join(r[1:])]) if len(r) > 1 else r[0] for r in refusals]
    lines = [line for line in text.strip().splitlines() if line.strip() and not line.startswith('Applying')]
    return ['\n          '.join(lines[-15:]) or 'Kyverno reported a failure']


def check_schemas(rendered):
    """Each rendered overlay against the Kubernetes and CRD schemas (kubeconform -strict, as CI runs it): an
    unknown or misspelled field fails here instead of at deploy. (findings, skipped)."""
    kubeconform = shutil.which('kubeconform')
    if not kubeconform:
        return [], [tool_missing('kubeconform', 'schemas')]
    found, skipped, see = [], [], 'README "How an app is put together"'
    for overlay, docs in rendered:
        where = f'{DEPLOY}/{overlay}'
        out, why = run_tool([kubeconform, '-strict', '-summary', '-schema-location', 'default', '-schema-location',
                             CRD_SCHEMAS], 120, input=yaml.safe_dump_all(docs, sort_keys=False))
        if why:
            skipped.append(f'{where}: schemas not checked ({why}); CI checks them')
            continue
        if out.returncode == 0:
            continue
        lines = [line.strip() for line in (out.stdout + out.stderr).splitlines()
                 if line.strip() and not line.startswith('Summary:')]
        if lines and all(re.search(r'failed downloading schema|could not find schema', line) for line in lines):
            skipped.append(f'{where}: schemas not checked (cannot download them here); CI checks them')
            continue
        found += [finding('error', where, re.sub(r'^stdin - ', '', line), see) for line in lines] or \
            [finding('error', where, f'kubeconform failed (exit {out.returncode})', see)]
    return found, skipped


def check_deploy_files(root, name=None):
    """deploy/ holds app.yaml, base/ (the Kustomize base), the dev/ overlay (namespace <app>-dev: Argo CD deploys
    it, and registration and CI need it once base/ exists) and the optional prod/ (namespace <app>, needed for
    production releases): merging copies exactly those."""
    local = deploy_dir(root)
    if not local:
        return []
    found = []
    for path in sorted(local.rglob('*')):
        rel = path.relative_to(local)
        if path.is_file() and not (rel.as_posix() == 'app.yaml' or (len(rel.parts) == 2 and rel.parts[0] in DEPLOY_DIRS
                                                                    and rel.suffix in ('.yaml', '.yml'))):
            found.append(finding('error', f'{DEPLOY}/{rel.as_posix()}', 'not deployed: deploy/ holds app.yaml and '
                                 'base/, dev/, prod/ *.yaml only, and a merge with anything else is not deployed',
                                 'README "How an app is put together"'))
    if not any((local / 'base').glob('*.y*ml')):
        try:
            requested = (yaml.safe_load((local / 'app.yaml').read_text()) or {}).get('services')
        except (OSError, yaml.YAMLError, AttributeError):
            requested = True
        # `new --app-name` commits deploy/app.yaml alone (naming the app): that is not yet a deployment, and
        # nothing registers or deploys until base/ exists; asking for blocks without a base is a mistake.
        found.append(finding('error' if requested else 'warning', f'{DEPLOY}/base',
                             'missing: the Kustomize base (Deployment, Service, ...) goes here'
                             + ('' if requested else '; until it exists nothing is deployed or registered'),
                             'README "How an app is put together"'))
    elif not (local / 'dev' / 'kustomization.yaml').is_file():
        # build-app.yaml fails on it and the platform can't register or deploy the app without it.
        found.append(finding('error', f'{DEPLOY}/dev/kustomization.yaml',
                             f'missing: the dev overlay (namespace: {name or "<app>"}-dev, resources: [../base]); '
                             'nothing registers or deploys without it', 'README "How an app is put together"'))
    for overlay, namespace in (('dev', f'{name}-dev'), ('prod', name)):
        path = local / overlay / 'kustomization.yaml'
        if not name or not path.is_file():
            continue
        try:
            spec = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as error:
            spec = f'not YAML: {error}'
        if not isinstance(spec, dict):
            found.append(finding('error', f'{DEPLOY}/{overlay}/kustomization.yaml', 'not a Kustomization (a YAML '
                                 'mapping with namespace and resources)', 'README "How an app is put together"'))
            continue
        if spec.get('namespace') != namespace:
            found.append(finding('error', f'{DEPLOY}/{overlay}/kustomization.yaml', f'namespace must be {namespace} '
                                 f'(found {spec.get("namespace")!r})', 'README "How an app is put together"'))
        if '../base' not in (spec.get('resources') or []):
            found.append(finding('error', f'{DEPLOY}/{overlay}/kustomization.yaml', 'resources must include ../base',
                                 'README "How an app is put together"'))
    return found


def requested_models(app, catalog):
    """Models the app's llm blocks request (a block without `models` gets the catalog's default)."""
    default = [m.get('id') for m in (catalog or {}).get('models') or [] if isinstance(m, dict) and m.get('default')]
    out = set()
    for svc in app['services'].values():
        if (svc or {}).get('type') == 'llm':
            out.update((svc or {}).get('models') or default)
    return out


def code_files(root):
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS or part.startswith('.') for part in rel.parts[:-1]):
            continue
        name = rel.name
        if (path.is_file() and name.endswith(CODE_SUFFIXES) and not name.startswith('test_')
                and not re.search(r'(_test\.py|\.(test|spec)\.[jt]sx?|_test\.go)$', name)):
            yield rel, path


def line_of(text, index):
    return text.count('\n', 0, index) + 1


COMMENT_LINE = re.compile(r'^\s*(#|//|/?\*|--)')


# A dependency install must come from a lockfile (app CI's only copy of this rule): a floating one
# can pull a different, possibly malicious, version on every build. (match, what it must also contain, advice)
LOCKED_INSTALLS = [
    (re.compile(r'\b(?:python\d*(?:\.\d+)?\s+-m\s+)?pip3?\s+install\b'), re.compile(r'--require-hashes\b'),
     'pip install needs --require-hashes -r requirements.txt (lock it with pip-compile --generate-hashes)'),
    (re.compile(r'\buv\s+pip\s+(?:install|sync)\b'), re.compile(r'--require-hashes\b'),
     'uv pip install/sync needs --require-hashes -r requirements.txt'),
    (re.compile(r'\buv\s+sync\b'), re.compile(r'--(?:frozen|locked)\b'), 'uv sync needs --frozen or --locked (commit uv.lock)'),
    (re.compile(r'\bnpm\s+(?:install|i|add)\b'), re.compile(r'(?!)'),
     'use npm ci (with a committed package-lock.json), not npm install'),
    (re.compile(r'\byarn(?:\s+install)?\s*(?:$|&&|;|\|)'), re.compile(r'(?!)'),
     'yarn needs install --frozen-lockfile (v1) or --immutable (berry)'),
    (re.compile(r'\byarn\s+install\b'), re.compile(r'--(?:frozen-lockfile|immutable)\b'),
     'yarn install needs --frozen-lockfile (v1) or --immutable (berry)'),
    (re.compile(r'\byarn\s+add\b'), re.compile(r'(?!)'), 'yarn add changes the lockfile; add dependencies locally'),
    (re.compile(r'\bpnpm\s+(?:install|i)\b'), re.compile(r'--frozen-lockfile\b'), 'pnpm install needs --frozen-lockfile'),
    (re.compile(r'\bpnpm\s+add\b'), re.compile(r'(?!)'), 'pnpm add changes the lockfile; add dependencies locally'),
]


def dockerfile_runs(text):
    """(line number, command) of each RUN instruction, its continuation lines joined."""
    out, buf, start = [], None, 0
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if buf is None:
            if not re.match(r'^\s*RUN\s', line, re.I):
                continue
            buf, start = re.sub(r'^\s*RUN\s+', '', line, flags=re.I), n
        elif line.lstrip().startswith('#'):
            continue
        else:
            buf += ' ' + line.strip()
        if buf.endswith('\\'):
            buf = buf[:-1]
            continue
        out.append((start, buf))
        buf = None
    if buf is not None:
        out.append((start, buf))
    return out


def check_dockerfile(root):
    """The build's Dockerfile rules (homelab-apps build-app.yaml "Check the Dockerfile follows the rules"): base
    images pinned by digest, a `test` stage before the final one, dependencies only from a lockfile, and a
    non-root USER."""
    path = root / 'Dockerfile'
    if not path.is_file():
        return []  # check_code reports it missing
    text = path.read_text(errors='replace')
    see = 'homelab-apps AGENTS.md "Your own code (container images)"'
    found = []
    froms = [(n, line.split()) for n, line in enumerate(text.splitlines(), 1) if re.match(r'^\s*FROM\s', line, re.I)]
    stages = {words[-1] for _, words in froms if len(words) >= 4 and words[-2].upper() == 'AS'}
    for n, words in froms:
        image = next((w for w in words[1:] if not w.startswith('--')), '')
        if '@sha256:' not in image and image != 'scratch' and image not in stages:
            found.append(finding('error', f'Dockerfile:{n}', f"base image '{image}' must be pinned by digest "
                                 '(image@sha256:...)', see))
    tests = [n for n, words in froms if len(words) >= 4 and words[-2].upper() == 'AS' and words[-1].lower() == 'test']
    if not tests:
        found.append(finding('error', 'Dockerfile', "add a stage named test (FROM ... AS test) that runs the app's "
                             'tests', see))
    elif froms and froms[-1][0] in tests:
        found.append(finding('error', f'Dockerfile:{froms[-1][0]}', 'the test stage must come before the final '
                             '(runtime) stage', see))
    for n, command in dockerfile_runs(text):
        for part in re.split(r'&&|\|\||;', command):  # one locked install doesn't excuse another
            for match, required, advice in LOCKED_INSTALLS:
                if match.search(part) and not required.search(part):
                    found.append(finding('error', f'Dockerfile:{n}', f'{advice}: {part.strip()[:120]}', see))
                    break
    user = final_user(text)
    if not user or user == 'root' or user.split(':')[0] == '0':
        found.append(finding('error', 'Dockerfile', 'the final stage needs USER <non-root uid>, e.g. USER 65532 '
                             '(after its FROM, or in the stage it builds on)', see))
    return found


def final_user(text):
    """The user the final stage runs as: its last USER, else the user of the earlier stage it is built FROM;
    None when nothing sets one (an outside base image's own user isn't known here, so it counts as root)."""
    stages = []  # [name, base, user] in order
    for line in text.splitlines():
        words = line.split()
        if not words:
            continue
        if words[0].upper() == 'FROM':
            base = next((w for w in words[1:] if not w.startswith('--')), '')
            stages.append([words[-1] if len(words) >= 4 and words[-2].upper() == 'AS' else None, base, None])
        elif words[0].upper() == 'USER' and len(words) > 1 and stages:
            stages[-1][2] = words[1]
    seen = set()
    stage = stages[-1] if stages else None
    while stage and not stage[2] and stage[1] not in seen:  # inherit from an earlier stage, never loop
        seen.add(stage[1])
        stage = next((s for s in stages if s[0] == stage[1]), None)
    return stage[2] if stage else None


def check_code(root, app, catalog=None):
    """Rules for the app's code (the repo checkout); `app` is None before it has a deployment."""
    found = []
    models = {m.get('id') for m in (catalog or {}).get('models') or [] if isinstance(m, dict) and m.get('id')}
    model_re = re.compile(r'["\'](' + '|'.join(re.escape(m) for m in sorted(models, key=len, reverse=True)) + r')["\']') \
        if models else None
    requested = requested_models(app, catalog) if app else set()
    pod_env = {item.get('name') for item in (app['container'].get('env') or [])} if app else set()
    for name in ('Dockerfile', 'ui-check.yaml'):
        if not (root / name).is_file():
            found.append(finding('error', name, 'missing: the build needs it at the repo root',
                                 'homelab-apps AGENTS.md "Apps with their own repo"'))
    found += check_dockerfile(root) + check_dotenv(root, app)
    reported = set()
    for rel, path in code_files(root):
        try:
            text = path.read_text(errors='replace')
        except OSError:
            continue
        defaults = []  # spans of env reads, whose fallback values may name a lab URL
        for match in ENV_READ.finditer(text):
            end = text.find(')', match.end())
            defaults.append((match.start(), end if end != -1 else match.end()))
            var = next(g for g in match.groups() if g)
            where = f'{rel}:{line_of(text, match.start())}'
            if PROVIDER_KEY.match(var):
                found.append(finding('error', where, f'reads {var}: apps never hold provider keys; AI and web search '
                                     'go through the llm block (base_url + api_key, search_url)', 'blocks'))
            elif app and var not in pod_env and var not in RUNTIME_ENV and var not in reported:
                reported.add(var)
                found.append(finding('warning', where, f'reads {var}, which the pod doesn\'t set, so the code\'s '
                                     f'default applies in the cluster. Set it in {app.get("deployment_file") or "homelab-apps apps/" + app["name"] + "/base/deployment.yaml"} '
                                     'env (or drop it); local-only values go in .magehand.env',
                                     'README "What the platform provides"'))
        for match in model_re.finditer(text) if model_re and app else ():
            model = match.group(1)
            line_start = text.rfind('\n', 0, match.start()) + 1
            before = text[line_start:match.start()]
            if model in requested or COMMENT_LINE.match(before):
                continue
            fallback = any(start <= match.start() <= end for start, end in defaults)
            # A warning: text can't prove the code calls the model (a docstring, a display list, a trailing
            # comment look the same), and errors are only for what certainly breaks.
            found.append(finding('warning', f'{rel}:{line_of(text, match.start())}',
                                 f'model {model} is not requested by an llm block in {app.get("where") or "app.yaml"}'
                                 f'/app.yaml (requested: {", ".join(sorted(requested)) or "none"}): LiteLLM refuses '
                                 'it (403) if the code calls it. Add it to the block\'s models'
                                 + (', or drop this fallback' if fallback else ''), 'blocks "Models"'))
        for host in PROVIDER_HOSTS:
            for match in re.finditer(re.escape(host), text):
                found.append(finding('error', f'{rel}:{line_of(text, match.start())}', f'calls {host} directly: '
                                     'AI and web search go through the llm block (LiteLLM: base_url, search_url)',
                                     'blocks'))
        for match in LAB_URL.finditer(text):
            if any(start <= match.start() <= end for start, end in defaults):
                continue
            found.append(finding('warning', f'{rel}:{line_of(text, match.start())}', f'hardcoded {match.group(0)}: '
                                 'read URLs from the env the pod gets (a block\'s base_url/search_url); they differ '
                                 'locally and in production', 'blocks'))
        for match in OWN_AUTH.finditer(text):
            lib = next(g for g in match.groups() if g)
            found.append(finding('warning', f'{rel}:{line_of(text, match.start())}', f'{lib}: authentik signs users '
                                 'in before a request reaches the app; read the X-authentik-* headers instead of '
                                 'building login, sessions or password storage',
                                 'homelab-apps AGENTS.md "Knowing who the user is"'))
        for match in LOCAL_DB.finditer(text):
            found.append(finding('warning', f'{rel}:{line_of(text, match.start())}', 'a database file: the pod\'s '
                                 'root filesystem is read-only and replaced on every deploy; keep data in a postgres '
                                 'block, or on a mounted volume (PVC) if it must be a file',
                                 'homelab-apps AGENTS.md "Storage and resources"'))
        for match in IN_PROCESS_SCHEDULER.finditer(text):
            lib = next(g for g in match.groups() if g)
            found.append(finding('warning', f'{rel}:{line_of(text, match.start())}', f'{lib}: a scheduler in the '
                                 "app's process runs once per replica and stops when the pod restarts; run scheduled "
                                 'work as a CronJob (`magehand job add`)', JOB_SEE))
    return found


def check_dotenv(root, app):
    """.magehand.env is committed and holds only non-secret, container-only values (paths)."""
    path = root / '.magehand.env'
    if not path.is_file():
        return []
    from_secrets = {e[0] for e in env_plan(app) if e[1] != 'value'} if app else set()
    found = []
    for number, line in enumerate(path.read_text(errors='replace').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        var, value = (part.strip() for part in line.split('=', 1))
        value = value.strip('"\'')
        where = f'.magehand.env:{number}'
        if var in from_secrets:
            found.append(finding('error', where, f'sets {var}, which the pod gets from a Secret: magehand run fills '
                                 'it from OpenBao or a local block. This file is committed and holds no secrets',
                                 'local-dev'))
        elif SECRET_VALUE_RE.match(value):
            found.append(finding('error', where, f'{var} looks like a key or token: this file is committed and holds '
                                 'no secrets; an app\'s keys come from its own blocks (never another app\'s)',
                                 'local-dev'))
        elif value and SECRET_NAME_RE.search(var):
            found.append(finding('warning', where, f'{var} looks like a secret: this file is committed and holds only '
                                 'container-only values such as paths; secrets come from blocks', 'local-dev'))
    return found


def provided_secrets(name, root=None, pin=None):
    """Secrets the app's own manifests make (ExternalSecrets from OpenBao, plain Secrets): in base/ and dev/ of
    the checkout's deploy/ when `root` has one, else of the app repo at the commit homelab-apps pins (`pin`),
    else (an app whose manifests are in homelab-apps) of homelab-apps apps/<app>/."""
    local = deploy_dir(root) if root else None
    texts = []
    if local:
        for part in ('base', 'dev'):
            if (local / part).is_dir():
                texts += [f.read_text() for f in sorted((local / part).glob('*.y*ml'))]
    elif pin:
        repo, ref = f"{OWNER}/{pin.get('repo') or name}", str(pin['ref'])
        for part in ('base', 'dev'):
            for file in github_dir(repo, f'deploy/{part}', ref):
                if file.endswith(('.yaml', '.yml')):
                    texts.append(github_file(repo, f'deploy/{part}/{file}', ref=ref) or '')
    else:
        for part in ('base', 'dev'):
            for file in github_dir(APPS_REPO, f'apps/{name}/{part}'):
                if file.endswith(('.yaml', '.yml')):
                    texts.append(github_file(APPS_REPO, f'apps/{name}/{part}/{file}') or '')
    names = set()
    for text in texts:
        try:
            docs = list(yaml.safe_load_all(text))
        except yaml.YAMLError:
            continue
        for doc in docs:
            if isinstance(doc, dict) and doc.get('kind') == 'ExternalSecret':
                names.add(((doc.get('spec') or {}).get('target') or {}).get('name')
                          or (doc.get('metadata') or {}).get('name'))
            elif isinstance(doc, dict) and doc.get('kind') == 'Secret':
                names.add((doc.get('metadata') or {}).get('name'))
    return names - {None}


def try_load_app(name, root=None):
    """load_app (with the Secrets its manifests make): from `root`'s deploy/ when it has one, else homelab-apps';
    None when neither has a deployment for it yet."""
    local = deploy_dir(root) if root else None
    if local and not any((local / 'base').glob('*.y*ml')):
        # deploy/app.yaml alone (`new --app-name` seeds it): no deployment to check yet. A base that has
        # manifests but no (valid) Deployment is a mistake, and load_app says so.
        return None
    if not local and github_file(APPS_REPO, f'apps/{name}/app.yaml', required=False) is None:
        return None
    app = load_app(name, root)
    app['provided'] = provided_secrets(name, root, pin=app.get('pin'))
    return app


def is_app_checkout(root):
    """True for an app's own repo (not a homelab-apps checkout): it has the template's ui-check.yaml or a deploy/app.yaml."""
    root = pathlib.Path(root)
    return (root / 'ui-check.yaml').is_file() or (root / DEPLOY / 'app.yaml').is_file()


def check_agents_drift(root):
    """A warning when the repo's AGENTS.md platform rules (above "## This app") differ from the template's:
    the platform moved on and `magehand update-agents` brings them in. Skipped without a readable template
    (offline, or CI without access to it) and outside an app's own repo."""
    path = pathlib.Path(root) / 'AGENTS.md'
    if not is_app_checkout(root) or not path.is_file():
        return []
    try:
        template = github_file(TEMPLATE_REPO, 'AGENTS.md', required=False)
    except SystemExit:  # no GitHub token: nothing to compare with
        return []
    platform, app = split_agents(template or '')
    if not template or app is None:
        return []
    why = agents_behind(path.read_text(), platform)
    if not why:
        return []
    return [finding('warning', 'AGENTS.md', f'{why}: run `magehand update-agents` and commit the diff',
                    'homelab-apps AGENTS.md')]


def agents_behind(text, platform):
    """Why an AGENTS.md (None: there is none) isn't up to date with the template's platform rules, or None."""
    if text is None:
        return 'no AGENTS.md'
    current, own = split_agents(text)
    if own is None:
        return f'has no "{APP_HEADING}" heading'
    return None if current == platform else f'the platform rules above "{APP_HEADING}" are out of date'


# Every repo the owner owns, with its template and its AGENTS.md on the default branch, and the template's own
# AGENTS.md, a page at a time: one query per 100 repos.
APP_REPOS_QUERY = """query($owner: String!, $template: String!, $cursor: String) {
  template: repository(owner: $owner, name: $template) { agents: object(expression: "HEAD:AGENTS.md") { ... on Blob { text } } }
  repositoryOwner(login: $owner) { repositories(first: 100, after: $cursor, isFork: false, ownerAffiliations: [OWNER],
    orderBy: {field: NAME, direction: ASC}) { pageInfo { hasNextPage endCursor } nodes { name isArchived
    templateRepository { nameWithOwner } agents: object(expression: "HEAD:AGENTS.md") { ... on Blob { text } } } } } }"""


def stale_agents(max_pages=10):
    """(['repo (why)', ...] of the owner's app repos (made from the template, not archived) whose AGENTS.md isn't
    up to date with the template's platform rules, None), or (None, why) when they couldn't all be read."""
    platform, nodes, cursor = None, [], None
    for _ in range(max_pages):
        args = ['api', 'graphql', '-f', f'query={APP_REPOS_QUERY}', '-f', f'owner={OWNER}',
                '-f', f'template={TEMPLATE_REPO.split("/", 1)[1]}'] + (['-f', f'cursor={cursor}'] if cursor else [])
        found, err = gh_query(*args)
        if err:
            return None, (err.strip().splitlines() or ['gh failed'])[0]
        data = (found or {}).get('data') or {}
        if platform is None:
            text = ((data.get('template') or {}).get('agents') or {}).get('text')
            platform, app = split_agents(text or '')
            if not text or app is None:
                return None, f'cannot read {TEMPLATE_REPO} AGENTS.md (or it has no "{APP_HEADING}" heading)'
        repos = (data.get('repositoryOwner') or {}).get('repositories')
        if repos is None:
            return None, f'cannot list the repos of {OWNER}'
        nodes += repos.get('nodes') or []
        cursor = (repos.get('pageInfo') or {}).get('endCursor')
        if not (repos.get('pageInfo') or {}).get('hasNextPage'):
            break
    else:
        return None, f'more than {100 * max_pages} repos'
    apps = [r for r in nodes if ((r.get('templateRepository') or {}).get('nameWithOwner') or '').lower()
            == TEMPLATE_REPO.lower() and not r.get('isArchived')]
    if not apps:
        return None, f'no repo made from {TEMPLATE_REPO}'
    stale = []
    for repo in apps:
        why = agents_behind((repo.get('agents') or {}).get('text'), platform)
        if why:
            stale.append(repo['name'] if why.startswith('the platform rules') else f'{repo["name"]} ({why})')
    return stale, None


def check_merged_branch(root):
    """A warning when the checked-out branch already had a pull request merged (squash merges keep other
    hashes, so `git log main..` still lists its commits) and has gone on since: new work on a merged branch
    is a new PR from a fresh branch off origin/main. Skipped outside an app's own repo, on a detached HEAD
    (CI), without gh or a merged PR for this branch's history, and while the branch has an open PR."""
    if not is_app_checkout(root):
        return []
    branch, tip = git_out(['branch', '--show-current'], root), git_out(['rev-parse', 'HEAD'], root)
    repo = current_repo(root)
    if not branch or not tip or not repo or branch in ('main', 'master'):
        return []
    code, out, _ = gh_probe('pr', 'list', '-R', repo, '--head', branch, '--state', 'all', '--limit', '20',
                            '--json', 'number,state,headRefOid')
    try:
        prs = json.loads(out) if code == 0 else []
    except ValueError:
        return []
    if any(p.get('state') == 'OPEN' for p in prs):
        return []
    covered = []  # merged PRs whose head is in this branch's history (HEAD itself included)
    for pr in prs:
        old = pr.get('headRefOid') or ''
        if pr.get('state') != 'MERGED' or not old:
            continue
        if old == tip:
            return []  # a merged PR already covers HEAD: nothing new to move
        ancestor = subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', old, tip],  # nosec B603 B607
                                  capture_output=True)
        if ancestor.returncode == 0:
            covered.append(pr)
    if not covered:
        return []
    pr = max(covered, key=lambda p: p.get('number') or 0)  # the latest merge the new commits follow
    return [finding('warning', f'branch {branch}',
                    f'its PR #{pr["number"]} was already merged (a squash merge keeps other hashes, so '
                    f'`git log main..` still lists these commits) and the branch has new commits since: '
                    f'start a fresh branch from origin/main (`git fetch && git switch -c NAME '
                    f'origin/main`) and cherry-pick the new commits', 'recipes')]


# --- check --ui: CI's browser check, here ---------------------------------------------

UI_SCRIPT = '.github/ui-check/ui_check.py'
APPS_BUILD_WORKFLOW = '.github/workflows/build-app.yaml'  # homelab-apps' shared build (its pins)
UI_PINS = ('PLAYWRIGHT_VERSION', 'AXE_VERSION', 'AXE_SHA256', 'POSTGRES_IMAGE')
UI_POSTGRES_URL = 'postgresql://ui:ui@ui-postgres:5432/ui'  # the throwaway database, named as CI names it
UI_CACHE_VOLUME = 'magehand-ui-check-cache'  # pip's wheels and axe-core between runs (a volume: no host file owners)
UI_NO_ANSWER = 'no-answer'  # the runner's marker in /out: the app never answered
# Runs in the pinned Playwright image: build-app.yaml's "Browser check" installs and checks, then its script.
UI_RUNNER = r'''import hashlib, os, subprocess, sys, tarfile, time, urllib.error, urllib.request
e = os.environ
subprocess.run([sys.executable, '-m', 'pip', 'install', '--quiet', '--disable-pip-version-check',
                '--root-user-action=ignore', '--cache-dir', '/cache/pip', 'playwright==' + e['PLAYWRIGHT_VERSION'],
                'pyyaml==' + e['PYYAML_VERSION']], check=True)
tgz = '/cache/axe-core-%s.tgz' % e['AXE_VERSION']
data = open(tgz, 'rb').read() if os.path.isfile(tgz) else b''
if hashlib.sha256(data).hexdigest() != e['AXE_SHA256']:
    data = urllib.request.urlopen('https://registry.npmjs.org/axe-core/-/axe-core-%s.tgz' % e['AXE_VERSION'],
                                  timeout=60).read()
    if hashlib.sha256(data).hexdigest() != e['AXE_SHA256']:
        sys.exit('axe-core %s does not match the SHA-256 build-app.yaml pins' % e['AXE_VERSION'])
    open(tgz, 'wb').write(data)
with tarfile.open(tgz) as t, open('/tmp/axe.min.js', 'wb') as f:
    f.write(t.extractfile('package/axe.min.js').read())
os.makedirs('/out', exist_ok=True)
url = 'http://127.0.0.1:' + e['PORT']
deadline = time.monotonic() + 60
while True:
    try:
        urllib.request.urlopen(url + '/', timeout=2)
        break
    except urllib.error.HTTPError:  # it answered
        break
    except OSError:
        if time.monotonic() > deadline:
            open('/out/no-answer', 'w').close()
            sys.exit(1)
        time.sleep(1)
sys.exit(subprocess.run([sys.executable, '/check/ui_check.py', '--app-dir', '/app', '--base-url', url,
                         '--axe', '/tmp/axe.min.js', '--out', '/out'],
                        stdout=subprocess.DEVNULL).returncode)  # magehand prints report.md
'''


def ui_pins():
    """The "Browser check" step's pins in homelab-apps build-app.yaml (and its PyYAML), so this run is CI's."""
    flow = yaml.safe_load(github_file(APPS_REPO, APPS_BUILD_WORKFLOW)) or {}
    for job in (flow.get('jobs') or {}).values():
        for step in (job or {}).get('steps') or []:
            if step.get('name') != 'Browser check':
                continue
            pins = {k: str((step.get('env') or {}).get(k) or '') for k in UI_PINS}
            m = re.search(r'pyyaml==([\w.]+)', step.get('run') or '')
            pins['PYYAML_VERSION'] = m.group(1) if m else ''
            if all(re.fullmatch(r'[\w.:@/-]+', v) for v in pins.values()):
                return pins
    die(f'{APPS_REPO} {APPS_BUILD_WORKFLOW} has no "Browser check" step with its pins; tell the owner')


def ui_env(cfg):
    """ui-check.yaml's env for the app, with ${POSTGRES_URL} the throwaway database (as CI expands it)."""
    env = {}
    for k, v in (cfg.get('env') or {}).items():
        if not re.fullmatch(r'[A-Za-z_]\w*', str(k)):
            die(f'ui-check.yaml env: {k!r} is not a variable name')
        env[str(k)] = re.sub(r'\$\{POSTGRES_URL\}|\$POSTGRES_URL\b', UI_POSTGRES_URL, str(v))
    return env


def ui_config(path):
    """ui-check.yaml as a mapping with a valid port, or exit saying which field is wrong."""
    try:
        cfg = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as error:
        die(f'{path} is not valid YAML: {error}')
    if not isinstance(cfg, dict):
        die(f'{path}: the top level must be a mapping (port:, env:, pages:)')
    port = cfg.get('port', 8000)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        die(f'{path}: port: {port!r} is not a port number (the one the app listens on)')
    if not isinstance(cfg.get('env') or {}, dict):
        die(f'{path}: env: must be a mapping of NAME: value')
    return cfg


def ui_engine(what, *args, env=None):
    """engine() for a step of the browser check: exit saying what failed and docker's own words."""
    result = engine(*args, check=False, env=env)
    if result.returncode:
        die(f'{what} failed: {(result.stderr or result.stdout).strip() or "no output"}')
    return result


def cmd_check_ui(a):
    refuse_if_managed('check --ui')
    root = pathlib.Path(a.code).resolve()
    asked, own = a.app_flag or a.app, checkout_app(root)
    if asked and own and asked != own:
        die(f'{root} is {own}\'s code: checking it as {asked} would report {asked} on code it isn\'t. '
            f'Run it in {asked}\'s checkout, or as itself (`magehand check --ui`)')
    name = asked or own
    if not name:
        usage('check', 'name the app: magehand check --ui -a APP --code DIR')
    again = shlex.join(['magehand', 'check', '--ui', '-a', name] + (['--code', a.code] if a.code != '.' else []))
    path = root / 'ui-check.yaml'
    if not path.is_file():
        die(f'{path} is missing, and CI requires it: add it (magehand guide search ui-check; '
            '`web_ui: false` in it if the app has no web UI)')
    cfg = ui_config(path)
    if cfg.get('web_ui') is False:
        step = 'push (CI skips the browser check too)'
        if a.json:
            print(json.dumps({'app': name, 'ok': True, 'skipped': 'web_ui: false', 'next': step}, indent=1))
        else:
            print(f'magehand check --ui {name}: no web UI (web_ui: false), nothing to check')
            print(next_line(step))
        sys.exit(0)
    cli = runtime()
    if not cli:
        die(no_runtime('check --ui'))
    if not shutil.which(cli):
        die(f'container runtime {cli} is configured but not installed: `magehand runtime auto` or install it')
    pins, script, env = ui_pins(), github_file(APPS_REPO, UI_SCRIPT), ui_env(cfg)
    port = cfg.get('port', 8000)
    tag = re.sub(r'[^a-z0-9-]', '-', name.lower())
    work = CACHE / 'ui-check' / tag
    out = work / 'out'
    # One run per app at a time, from any checkout: runs share this directory, the image tag, the network and
    # the container names. Held until magehand exits (the kernel drops it then, even on a crash).
    work.parent.mkdir(parents=True, exist_ok=True)
    lock = open(work.parent / f'{tag}.lock', 'w')  # held for the whole run
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        die(f'another `magehand check --ui` for {name} is running (perhaps in another worktree): '
            f'wait for it to finish, then `{again}`')
    try:
        shutil.rmtree(work)
    except FileNotFoundError:
        pass
    except OSError as error:
        die(f'cannot clear the last run ({error}): remove {shell_path(work)}, then `{again}` again')
    (work / 'check').mkdir(parents=True)
    (work / 'check' / 'ui_check.py').write_text(script)
    (work / 'check' / 'run.py').write_text(UI_RUNNER)
    image, net = f'magehand/{tag}:ui-check', f'magehand-ui-{tag}'
    app_box, db_box, check_box = f'{net}-app', f'{net}-postgres', f'{net}-browser'
    print(f'magehand: {cli} build -t {image} {shell_path(root)}', file=sys.stderr)
    if subprocess.run([cli, 'build', '-t', image, str(root)], stdout=sys.stderr).returncode:  # nosec B603  # docker/podman
        die(f'{cli} build failed (CI builds the same Dockerfile): fix it, then `{again}`')

    def clean():
        engine('rm', '-f', app_box, db_box, check_box, check=False)
        engine('network', 'rm', net, check=False)

    clean()
    try:
        ui_engine(f'{cli} network create', 'network', 'create', net)
        if any('ui-postgres' in v for v in env.values()):
            print(f'magehand: a throwaway Postgres ({pins["POSTGRES_IMAGE"].split("@")[0]})', file=sys.stderr)
            ui_engine('starting Postgres', 'run', '-d', '--name', db_box, '--network', net, '--network-alias',
                      'ui-postgres', '-e', 'POSTGRES_USER=ui', '-e', 'POSTGRES_PASSWORD=ui', '-e', 'POSTGRES_DB=ui',
                      pins['POSTGRES_IMAGE'])
            # Over TCP: during initdb the image's temporary server answers only on its socket.
            for _ in range(30):
                if engine('exec', db_box, 'pg_isready', '-h', '127.0.0.1', '-U', 'ui', '-q', check=False).returncode == 0:
                    break
                time.sleep(1)
            else:
                logs = engine('logs', '--tail', '20', db_box, check=False)
                die('the throwaway Postgres was not ready within 30s:\n' + (logs.stdout + logs.stderr).rstrip())
        # As CI starts it: the cluster's read-only root and dropped capabilities.
        ui_engine('starting the app', 'run', '-d', '--name', app_box, '--network', net,
                  *[x for k in env for x in ('-e', k)], '--read-only', '--tmpfs', '/tmp',  # nosec B108  # the container's own tmpfs
                  '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', image, env=env)
        print(f'magehand: {UI_SCRIPT} from {APPS_REPO}, Playwright {pins["PLAYWRIGHT_VERSION"]}, '
              f'axe-core {pins["AXE_VERSION"]}', file=sys.stderr)
        values = {**pins, 'PORT': str(port)}
        code = subprocess.run([  # nosec B603  # docker/podman
            cli, 'run', '--name', check_box, '--network', f'container:{app_box}', '-v', f'{work / "check"}:/check:ro',
            '-v', f'{root}:/app:ro', '-v', f'{UI_CACHE_VOLUME}:/cache',
            *[x for k, v in values.items() for x in ('-e', f'{k}={v}')],
            f'mcr.microsoft.com/playwright/python:v{pins["PLAYWRIGHT_VERSION"]}-noble', 'python', '/check/run.py'],
            stdout=sys.stderr).returncode
        # Copied out, not mounted: the files are then yours, whoever the container's root maps to.
        engine('cp', f'{check_box}:/out', str(out), check=False)
        report = out / 'report.md'
        if code or not report.is_file():
            logs = engine('logs', '--tail', '50', app_box, check=False)
            print('--- app logs\n' + (logs.stdout + logs.stderr).rstrip(), file=sys.stderr)
        if (out / UI_NO_ANSWER).exists():
            die(f'the app did not answer on port {port} within 60s (its logs are above): is ui-check.yaml\'s '
                '`port` the one it listens on?')
        if not report.is_file():
            die('the browser check could not run (see above)')
    finally:
        clean()
    ok = code == 0
    step = (f'look at the screenshots in {shell_path(out)}, then push' if ok else
            f'fix what the report lists (screenshots in {shell_path(out)}), then `{again}`')
    if a.json:
        print(json.dumps({'app': name, 'ok': ok, 'report': str(report), 'screenshots': str(out), 'next': step}, indent=1))
    else:
        print(report.read_text().rstrip())
        print(f'screenshots: {out}')
        print(next_line(step))
    sys.exit(0 if ok else 1)


def cmd_check(a):
    global APPS_DIR
    if a.ui:
        if a.manifests_only or a.apps_dir or a.strict:
            usage('check', '--ui runs the browser check only: leave out --manifests-only, --apps-dir and --strict')
        cmd_check_ui(a)
    manifests_only = a.manifests_only
    if a.apps_dir:
        APPS_DIR = a.apps_dir
    root = pathlib.Path(a.code)
    name = a.app_flag or a.app or current_app(root)
    catalog = yaml.safe_load(github_file(APPS_REPO, 'catalog.yaml', required=False) or '') or {}
    # The deployment is the checkout's deploy/ when it has one (else homelab-apps'), with or without the code rules.
    pinned_here = []  # a pinned app checked with --apps-dir from a checkout without deploy/app.yaml
    try:
        app = try_load_app(name, root)
    except PinnedApp as pinned:
        repo, ref = pinned.pin.get('repo') or name, str(pinned.pin['ref'])
        if manifests_only:  # homelab-apps' own CI: only the pin is there, and the app repo's build checks the rest
            note = (f'{name} is pinned to {OWNER}/{repo} at {ref[:7]}: its manifests are in that repo and are checked by '
                    'its build (`magehand check` there); nothing to check in this homelab-apps checkout')
            if a.json:
                print(json.dumps({'app': name, 'pinned': {'repo': repo, 'ref': ref}, 'errors': 0, 'findings': [],
                                  'next': None}, indent=1))
            else:
                print(f'magehand check {name}: {note}')
                print(next_line(None))
            sys.exit(0)
        # An app repo's build (or any --code): the code rules still apply. In the pinned repo's own checkout, a
        # missing deploy/app.yaml is an error: the platform deploys this repo's deploy/ at the pinned commit.
        app = None
        if remote_repo(root) == repo:
            pinned_here.append(finding('error', f'{DEPLOY}/app.yaml', f'missing: {name} is deployed from this repo\'s '
                                       f'{DEPLOY}/ (pinned at {ref[:7]}), so every commit needs it',
                                       'README "How an app is put together"'))
    found = pinned_here + (check_manifests(app, catalog) + check_jobs(app, catalog) if app
                           else seeded_name_findings(name, root)) + check_deploy_files(root, name)
    rendered, skipped = check_rendered(root, name)
    found += rendered
    # CI must check everything a merge deploys: there a render that was skipped is a failure, and with --strict
    # (CI, once it installs kyverno and kubeconform) so is any rule that couldn't run.
    hard = skipped if a.strict else [s for s in skipped if ': not rendered (' in s] if os.environ.get('CI') else []
    for s in hard:
        where, why = s.split(': ', 1)
        found.append(finding('error', where, why.replace('; CI renders and checks it', ': CI needs kubectl to render '
                                                         'deploy/').replace('; CI checks them', ': --strict runs every '
                                                                            "rule CI's checks run"),
                             'README "How an app is put together"'))
    skipped = [s for s in skipped if s not in hard]
    if not manifests_only:
        found += check_code(root, app, catalog) + check_agents_drift(root) + check_merged_branch(root)
    errors = sum(f['level'] == 'error' for f in found)
    step = ('fix the errors, then `magehand check` again' if errors else
            'fix each warning or say in the PR why it is fine, then push' if found else
            'push (CI runs this check too)')
    if a.json:
        print(json.dumps({'app': name, 'code': None if manifests_only else str(root.resolve()),
                          'deployment': app['where'] if app else None, 'errors': errors,
                          'findings': sorted(found, key=lambda f: (f['level'] != 'error', f['where'])),
                          'skipped': skipped, 'next': step}, indent=1))
        sys.exit(1 if errors else 0)
    print(f'magehand check {name}: code {"(skipped)" if manifests_only else root.resolve()}, deployment '
          + (app['where'] if app else 'none yet (code rules only)'))
    for f in sorted(found, key=lambda f: (f['level'] != 'error', f['where'])):
        see = f['see'] if f['see'].startswith('homelab-apps') else f"magehand guide {f['see']}"
        print(f"{f['level']:7} {f['where']}: {f['text']}\n        -> {see}")
    for line in skipped:
        print(f'skipped {line}')
    print(f'{len(found)} finding(s), {errors} error(s)' if found else 'no findings')
    print(next_line(step))
    sys.exit(1 if errors else 0)


# --- agents: keep a repo's AGENTS.md current -----------------------------------------

APP_HEADING = '## This app'  # the template's AGENTS.md: platform rules above it, the app's own notes from it down


def split_agents(text):
    """(platform part, app part) of an AGENTS.md, split at its "## This app" line; (text, None) without one."""
    m = re.search(r'^## This app[ \t]*$', text, re.MULTILINE)
    return (text[:m.start()], text[m.start():]) if m else (text, None)


def agents_text(path):
    """(current text or None, the text AGENTS.md should have, problem or None): the template's platform rules
    above "## This app", this repo's own notes from it down."""
    template = github_file(TEMPLATE_REPO, 'AGENTS.md')
    platform, app = split_agents(template)
    if app is None:
        return None, None, f'{TEMPLATE_REPO} AGENTS.md has no "{APP_HEADING}" heading; tell the owner'
    current = path.read_text() if path.is_file() else None
    if current is None:
        return None, template, None
    _, own = split_agents(current)
    if own is None:
        return current, None, (f'{path} has no "{APP_HEADING}" heading, so magehand can\'t tell the platform rules '
                               f'from this app\'s notes. Put the heading above the app\'s own notes (see '
                               f'{TEMPLATE_REPO}), then run it again')
    return current, platform + own, None


def cmd_agents(a):
    path = pathlib.Path(a.code) / 'AGENTS.md'
    current, new, problem = agents_text(path)
    if problem:
        die(problem)
    changed = new != current
    step = (('`magehand update-agents`' if a.check else f'review the diff (`git diff {path}`) and commit it')
            if changed else None)
    if a.json:
        print(json.dumps({'path': str(path), 'changed': changed, 'written': changed and not a.check, 'next': step}))
    elif not changed:
        print(f'{path}: up to date', file=sys.stderr)
    elif a.check:
        print(f'{path}: the platform rules are out of date; run `magehand update-agents`', file=sys.stderr)
    if changed and not a.check:
        path.write_text(new)
        if not a.json:
            print(f'{path}: updated from {TEMPLATE_REPO}', file=sys.stderr)
    if not a.json:
        print(next_line(step))
    sys.exit(1 if changed and a.check else 0)


# --- the skill: when coding agents should ask the platform ------------------------

SKILL_NAME = 'homelab-app'
SKILL = """---
name: homelab-app
description: Use when writing, changing, reviewing or debugging the code or deployment of a homelab app (a repo made from homelab-app-template, with ui-check.yaml and an AGENTS.md pointing to homelab-apps, or homelab-apps apps/<app>/), or when unsure how the homelab gives an app something (database, cache, AI models, web search, secrets, sign-in, env vars, local runs, deploys). Not for tasks that don't touch app code.
---
# Building a homelab app

The homelab decides how an app gets databases, AI, web search, secrets,
sign-in and deploys. Don't rely on habits from other platforms or guess:
ask the platform, with magehand (installed with `uv tool install magehand`).
Its commands end with `next: ...`: do that next, or say why not. When one
says a newer magehand is available, tell the user (`magehand upgrade`).

1. Before designing or changing behavior: `magehand app` (in the app's repo).
   It lists the app's blocks, every env var its pod gets and where each comes
   from, and its URLs. Code reads exactly those variables. If the repo has
   `deploy/`, that is the app's deployment: `deploy/app.yaml` requests blocks
   and models, `deploy/base/` is its Kubernetes manifests, `deploy/dev/` and
   `deploy/prod/` its overlays; edit them there. Merging deploys dev; the
   owner's GitHub Release `vX.Y.Z` deploys production.
2. Any "how do I ... on the homelab" question, before answering or coding:
   `magehand guide search <words>` (e.g. `web search`, `database`,
   `who is the user`, `env var`, `production`). Read the sections it prints
   and follow them; `magehand guide` lists the topics.
3. Before every push: `magehand check`. Fix every error. Fix each warning, or
   say in the PR why it's fine. Each finding names the guide section to read.
   CI runs the same check. After changing a page: `magehand check --ui` runs
   CI's browser check here (about a minute) and saves its screenshots: look
   at them.
4. Run it as the cluster does: `magehand dev up`, then
   `magehand run -- <command>` (or `magehand run --container`). A new app
   has no LLM key until it is registered and deployed (`magehand app` says
   so): mock the LLM until then. Never use another app's key.
5. If the guide doesn't answer, say so in the PR or issue (what you searched
   for) instead of inventing a mechanism; the owner adds it to the guide.
6. Opening a PR: open it as a draft (`gh pr create --draft`); CI runs on
   drafts and OpenClaw ignores them. Push until `magehand check` and CI are
   green, then `magehand pr ready`: it refuses until the checks are green,
   marks the PR ready, requests OpenClaw's review and confirms the request
   stuck (or says why not). Each review costs a model run. A push starts no
   review and dismisses the approval: to change a reviewed PR,
   `gh pr ready N --undo`, push until green, then `magehand pr ready` again.
   Don't call the PR ready to merge until its review has posted; the owner
   merges. Base every PR on `main` (the repo's ruleset only guards the
   default branch).
7. When `magehand check` warns that AGENTS.md is behind the template, run
   `magehand update-agents`, review the diff and commit it. Never edit
   AGENTS.md above its `## This app` heading: that edit is lost.

Facts that override habits: no direct calls to AI or search providers (the
llm block's base_url and search_url); no own login or passwords (authentik
adds X-authentik-* headers); the root filesystem is read-only (state goes in
a block); a block key is never `optional: true`; merging code deploys dev by
itself, `deploy/` included (never pin images by hand); the code calls only
models its llm block requests; scheduled work is a CronJob that
`magehand job add` writes (never a scheduler inside the app's process).
"""


def claude_dir():
    return pathlib.Path(os.environ.get('CLAUDE_CONFIG_DIR') or HOME / '.claude')


def codex_dir():
    return pathlib.Path(os.environ.get('CODEX_HOME') or HOME / '.codex')


def skill_dirs():
    """Where coding agents on this machine read skills: Claude Code always, Codex if it is installed."""
    dirs = [claude_dir() / 'skills']
    if codex_dir().is_dir():
        dirs.append(codex_dir() / 'skills')
    return dirs


def skill_installed():
    """Whether setup installed the skill here (upgrade refreshes it only then)."""
    return any((base / SKILL_NAME / 'SKILL.md').is_file() for base in skill_dirs())


def install_skill():
    """Writes the skill (refreshed on every run, so an upgrade of magehand updates it); returns the paths."""
    paths = []
    for base in skill_dirs():
        path = base / SKILL_NAME / 'SKILL.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SKILL)
        paths.append(path)
    return paths


def cmd_skill(a):
    if a.install:
        for path in install_skill():
            print(f'installed {path}')
        return
    print(SKILL)


# --- pre-approved commands: one list, for Claude Code and Codex ------------------------
# `setup` installs it (never `skill --install`, which OpenClaw's VM runs): Codex reads every
# $CODEX_HOME/rules/*.rules, and Claude Code loads a plugin from its skills directory with no install step,
# whose PreToolUse hook applies the same list. A command the list doesn't name prompts as before; forbidden
# beats allow in both. Not a boundary (main's ruleset is: only the owner's approval merges), only fewer prompts.
APPROVALS = (
    (('magehand', ('status', 'app', 'check', 'doctor', 'guide', 'version', 'help')), 'allow',
     "magehand's read-only commands"),
    (('magehand', 'job', 'ls'), 'allow', "lists the app's scheduled jobs"),
    (('magehand', 'pr', 'ready'), 'allow', "readies a green PR and requests OpenClaw's review (the PR routine)"),
    (('gh', 'pr', ('view', 'checks', 'list', 'diff', 'status')), 'allow', 'reads PRs'),
    (('gh', 'run', ('view', 'list', 'watch')), 'allow', 'reads CI runs'),
    (('gh', 'issue', ('view', 'list')), 'allow', 'reads issues'),
    (('gh', 'repo', 'view'), 'allow', 'reads a repo'),
    (('cd',), 'allow', 'changes directory (so `cd app && magehand check` needs no prompt)'),
    (('gh', 'pr', 'merge'), 'forbidden', 'the owner merges'),
    (('gh', 'release', ('create', 'edit', 'delete', 'upload')), 'forbidden',
     'the owner releases (`magehand release`, in a terminal)'),
    (('gh', 'repo', 'delete'), 'forbidden', 'the owner deletes repos'),
    (('git', 'push', '--tags'), 'forbidden', 'the owner tags releases'),
    (('git', 'push', 'origin', '--tags'), 'forbidden', 'the owner tags releases'),
)
APPROVALS_PLUGIN = 'magehand-approvals'
CODEX_RULES = 'magehand.rules'
GENERATED = 'Written by `magehand setup` from its approvals list; `magehand upgrade` rewrites it. Remove: magehand setup --undo'

# The Claude Code session hook: at the start of a session, tells the agent when magehand is out of date (its
# commands' output isn't a terminal, so the update notice alone may never be seen). Stdlib only; quiet on any error.
SESSION_HOOK = '''\
# {generated}
import json
import pathlib
import subprocess
import sys


def uses_magehand(cwd):
    """True in a checkout whose AGENTS.md or CLAUDE.md names magehand (app repos and the platform's)."""
    for d in [cwd, *cwd.parents]:
        if (d / '.git').exists():
            return any('magehand' in (d / n).read_text(errors='replace') for n in ('AGENTS.md', 'CLAUDE.md')
                       if (d / n).is_file())
    return False


try:
    if not uses_magehand(pathlib.Path(json.load(sys.stdin).get('cwd') or '.')):
        sys.exit(0)
except (OSError, ValueError, AttributeError):
    sys.exit(0)
try:
    out = subprocess.run({magehand} + ['version', '--json'], capture_output=True, text=True, timeout=8)
    found = json.loads(out.stdout)
except (OSError, ValueError, subprocess.SubprocessError):
    found = {{}}
if isinstance(found, dict) and found.get('newer'):
    print(json.dumps({{'hookSpecificOutput': {{'hookEventName': 'SessionStart', 'additionalContext': (
        'magehand ' + str(found.get('latest')) + ' is out; this machine has ' + str(found.get('version')) + '. Tell '
        'the user before any platform work: `magehand upgrade` updates it, and with it the homelab-app skill and '
        'the pre-approved commands.')}}}}))
'''

# The Claude Code hook: stdlib only (it runs before every Bash call), the list baked in as JSON.
APPROVE_HOOK = '''\
# {generated}
import json
import re
import shlex
import sys

RULES = json.loads({rules!r})
SEPARATOR = set(';&|')


def matches(words, pattern):
    return len(words) >= len(pattern) and all(w == p or (isinstance(p, list) and w in p)
                                              for w, p in zip(words, pattern))


def decide(command):
    """('deny', why) when any part is forbidden; ('allow', why) when the command is a chain of plain
    commands (no redirect, substitution, subshell or assignment) and the list allows every part; else None."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|')
        lexer.whitespace_split = True
        lexer.commenters = ''  # bash starts a comment only at a word's start: `x#;rm` is two commands
        tokens = list(lexer)
    except ValueError:
        return None
    parts, current = [], []
    for token in tokens:
        if token and set(token) <= SEPARATOR:
            parts.append(current)
            current = []
        else:
            current.append(token)
    parts = [part for part in parts + [current] if part]
    allowed = []
    for part in parts:
        hits = [(decision, why) for pattern, decision, why in RULES if matches(part, pattern)]
        for decision, why in hits:
            if decision == 'forbidden':
                return 'deny', why
        allowed.append(next((why for decision, why in hits if decision == 'allow'), None))
    # Only characters with no meaning to bash beyond words, quotes and the separators: no redirect, substitution,
    # subshell, glob, comment, escape or newline; and no assignment (`FOO=1 cmd`).
    plain = (re.fullmatch(r'[A-Za-z0-9_ ./:@%+,=\\x27"&|;-]*', command)
             and not re.search(r'(^|[\\s;&|])[A-Za-z_][A-Za-z0-9_]*=', command))
    if plain and parts and all(allowed):
        return 'allow', allowed[-1]
    return None


def main():
    event = json.load(sys.stdin)
    command = (event.get('tool_input') or {{}}).get('command')
    found = decide(command) if event.get('tool_name') == 'Bash' and isinstance(command, str) else None
    if found:
        print(json.dumps({{'hookSpecificOutput': {{'hookEventName': 'PreToolUse', 'permissionDecision': found[0],
                                                  'permissionDecisionReason': 'magehand: ' + found[1]}}}}))


if __name__ == '__main__':
    main()
'''


def approval_rules():
    """APPROVALS as JSON-able lists (alternatives as lists), for both harnesses."""
    return [[[list(word) if isinstance(word, tuple) else word for word in pattern], decision, why]
            for pattern, decision, why in APPROVALS]


def codex_rules_text():
    lines = [f'# {GENERATED}']
    for pattern, decision, why in approval_rules():
        lines.append(f'prefix_rule(pattern = {json.dumps(pattern)}, decision = "{decision}", '
                     f'justification = {json.dumps(why)})')
    return '\n'.join(lines) + '\n'


def approvals_installed():
    """Whether setup installed the approvals here, for either harness (refresh rewrites them only then)."""
    return (claude_dir() / 'skills' / APPROVALS_PLUGIN).is_dir() or (codex_dir() / 'rules' / CODEX_RULES).is_file()


def approvals_missing():
    """The harnesses on this machine that lack their part of the approvals: Claude Code always applies, Codex
    when it is installed. Each is checked on its own: a Claude plugin says nothing about Codex's rules."""
    missing = [] if (claude_dir() / 'skills' / APPROVALS_PLUGIN).is_dir() else ['Claude Code']
    if codex_dir().is_dir() and not (codex_dir() / 'rules' / CODEX_RULES).is_file():
        missing.append('Codex')
    return missing


def rejected_rules_step(problem):
    return f'Codex rejected magehand\'s rules ({problem}): update Codex (`codex --version`), then `magehand setup`'


def codex_rules_problem(path):
    """Why Codex rejects the rules file at `path`, or None (also None when the codex CLI isn't on PATH)."""
    if not shutil.which('codex'):
        return None
    try:
        out = subprocess.run(['codex', 'execpolicy', 'check', f'--rules={path}', '--', 'magehand', 'status'],  # nosec B603 B607
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f'codex execpolicy check failed ({error})'
    try:
        decision = json.loads(out.stdout).get('decision') if out.returncode == 0 else None
    except ValueError:
        decision = None
    return None if decision == 'allow' else (out.stderr.strip() or out.stdout.strip() or 'codex rejected it')[:300]


def install_approvals():
    """Writes the approvals for Claude Code and (if installed) Codex; returns [(path, problem or None)]."""
    results = []
    plugin = claude_dir() / 'skills' / APPROVALS_PLUGIN
    (plugin / '.claude-plugin').mkdir(parents=True, exist_ok=True)
    (plugin / 'hooks').mkdir(exist_ok=True)
    (plugin / '.claude-plugin' / 'plugin.json').write_text(json.dumps({
        'name': APPROVALS_PLUGIN, 'version': installed_version() or '0.0.0', 'author': {'name': 'magehand'},
        'license': 'MIT', 'homepage': f'https://github.com/{MAGEHAND_REPO}',
        'description': "magehand's pre-approved commands: read-only magehand and gh run without a prompt; "
                       'merges, releases and tag pushes are refused. A new session hears when magehand is out of '
                       'date. ' + GENERATED}, indent=2) + '\n')
    (plugin / 'hooks' / 'approve.py').write_text(APPROVE_HOOK.format(generated=GENERATED,
                                                                     rules=json.dumps(approval_rules())))
    # uv's and pipx's own Python stays put across upgrades; any other (a uvx run, a venv) may not: python3. -I keeps
    # the user's PYTHONPATH and the project's files out of the hook that decides what runs.
    python = shlex.quote(sys.executable) if install_method() in ('uv', 'pipx') else '/usr/bin/env python3'
    command = f'{python} -I "${{CLAUDE_PLUGIN_ROOT}}/hooks/approve.py"'
    # This install's own magehand (uv and pipx keep it put), else the one on PATH.
    exe = pathlib.Path(sys.executable).with_name('magehand')
    magehand = [str(exe)] if install_method() in ('uv', 'pipx') and exe.is_file() else ['magehand']
    (plugin / 'hooks' / 'session.py').write_text(SESSION_HOOK.format(generated=GENERATED, magehand=json.dumps(magehand)))
    session = f'{python} -I "${{CLAUDE_PLUGIN_ROOT}}/hooks/session.py"'
    (plugin / 'hooks' / 'hooks.json').write_text(json.dumps({'hooks': {
        'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': command, 'timeout': 10}]}],
        'SessionStart': [{'matcher': 'startup', 'hooks': [{'type': 'command', 'command': session, 'timeout': 10}]}]}},
        indent=2) + '\n')
    results.append((plugin, None))
    if codex_dir().is_dir():
        rules = codex_dir() / 'rules' / CODEX_RULES
        rules.parent.mkdir(parents=True, exist_ok=True)
        # Checked outside rules/ (Codex reads every file there), then moved in: a rules file Codex can't load
        # would break Codex itself, so a rejected one never replaces the file there.
        draft = codex_dir() / f'{CODEX_RULES}.new'
        draft.write_text(codex_rules_text())
        problem = codex_rules_problem(draft)
        if problem:
            draft.unlink()
        else:
            os.replace(draft, rules)
        results.append((rules, problem))
    return results


def remove_approvals():
    """Removes what install_approvals wrote; returns the paths removed."""
    removed = []
    plugin = claude_dir() / 'skills' / APPROVALS_PLUGIN
    if plugin.is_dir():
        shutil.rmtree(plugin)
        removed.append(plugin)
    rules = codex_dir() / 'rules' / CODEX_RULES
    if rules.is_file():
        rules.unlink()
        removed.append(rules)
    return removed


# --- setup: once per machine ---------------------------------------------------------

def lab_address(host):
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None


def dns_answer(server, host):
    """What `server` answers for `host` (A record), or None. Uses dig (macOS)."""
    if not shutil.which('dig'):
        return None
    out = subprocess.run(['dig', '+short', '+time=3', '+tries=1', f'@{server}', host],  # nosec B603 B607
                         capture_output=True, text=True)
    lines = [line for line in out.stdout.split() if line and line[0].isdigit()]
    return lines[0] if out.returncode == 0 and lines else None


def reachable():
    try:
        return urllib.request.urlopen(f'{OPENBAO}/v1/sys/health', timeout=5).status == 200  # nosec B310
    except (OSError, urllib.error.HTTPError):
        return False


def local_network_blocked(error=None):
    """Whether macOS Local Network privacy keeps this Python off the homelab: a Mac, "No route to host" from
    here (`error`, the failed request's, or a new request's), and Apple's own curl (exempt from the permission)
    gets an answer from the same URL."""
    if sys.platform != 'darwin':
        return False
    if error is None:
        try:
            urllib.request.urlopen(f'{OPENBAO}/v1/sys/health', timeout=5)  # nosec B310
            return False
        except urllib.error.HTTPError:  # it answered
            return False
        except OSError as failed:
            error = failed
    if getattr(getattr(error, 'reason', error), 'errno', None) != errno.EHOSTUNREACH:  # urlopen wraps it
        return False
    curl = subprocess.run(['/usr/bin/curl', '-sS', '-o', '/dev/null', '--max-time', '5',  # nosec B603
                           f'{OPENBAO}/v1/sys/health'], capture_output=True)
    return curl.returncode == 0


def on_apple_python():
    """Whether this magehand runs on Apple's own Python, which Local Network privacy doesn't apply to."""
    return os.path.realpath(sys.executable).startswith(APPLE_PREFIXES)


def can_move_to_apple_python():
    """Whether setup can reinstall magehand on Apple's Python: a uv install on another Python, uv on PATH, and the
    Command Line Tools present (without them /usr/bin/python3 is a stub that opens their installer)."""
    return (sys.platform == 'darwin' and not on_apple_python() and install_method() == 'uv'
            and bool(shutil.which('uv')) and os.path.exists(APPLE_PYTHON)
            and subprocess.run(['xcode-select', '-p'], capture_output=True).returncode == 0)  # nosec B603 B607


def local_network_step():
    """What fixes a Local Network block: setup moving magehand onto Apple's Python, else the permission."""
    return ("`magehand setup` (it moves magehand onto Apple's Python, which macOS exempts)"
            if can_move_to_apple_python() else LOCAL_NETWORK_PERMISSION)


def move_to_apple_python():
    """Reinstall magehand on Apple's Python, then run its setup again; exits with that setup's code. Unpinned (the
    latest): `magehand==X` would pin it and stop `upgrade`. uv records the Python, so upgrades stay on it."""
    cmd = ['uv', 'tool', 'install', '--reinstall', '--python', APPLE_PYTHON, 'magehand']
    print(f"    Apple's own Python is exempt: {' '.join(cmd)}", flush=True)
    if subprocess.run(cmd).returncode != 0:  # nosec B603  # a fixed command
        die(f"could not reinstall magehand on Apple's Python (see above): {LOCAL_NETWORK_PERMISSION}; "
            'then `magehand setup` again')
    exe = pathlib.Path(sys.executable).with_name('magehand')  # the same tool directory, now on Apple's Python
    sys.exit(subprocess.run([str(exe), 'setup']).returncode)  # nosec B603  # magehand's own executable


def sudo_write_resolver(server):
    """/etc/resolver/<lab domain>: macOS asks `server` for *.lab names only."""
    print(f'Pointing *.{LAB} lookups (only those) at {server}; macOS asks for your password once.')
    subprocess.run(['sudo', 'mkdir', '-p', str(RESOLVER.parent)], check=True)  # nosec B603 B607
    subprocess.run(['sudo', 'tee', str(RESOLVER)], input=f'nameserver {server}\n', text=True,  # nosec B603 B607
                   stdout=subprocess.DEVNULL, check=True)
    subprocess.run(['sudo', 'killall', '-HUP', 'mDNSResponder'], check=False)  # nosec B603 B607


def cmd_setup(a):
    host = urllib.parse.urlparse(OPENBAO).hostname
    if a.undo:
        if RESOLVER.exists():
            subprocess.run(['sudo', 'rm', '-f', str(RESOLVER)], check=True)  # nosec B603 B607
            print(f'removed {RESOLVER}')
        for path in remove_approvals():
            print(f'removed {path}')
        print(next_line(None))
        return
    if a.refresh:
        return setup_refresh(host)
    print('1/6 GitHub')
    try:
        github_token()
        print('    ok')
    except SystemExit:
        die('sign in to GitHub first: `gh auth login`, then run `magehand setup` again')
    print('2/6 container runtime (only for local postgres/redis blocks and run --container)')
    configured = configured_runtime()
    found = detect_runtimes()
    for line in runtime_lines(found, configured, choose_runtime(configured, found)):
        print('  ' + line)
    print(f'    Supported: {SUPPORTED}. Change: magehand runtime docker|podman|none')
    print(f'3/6 the homelab network (*.{LAB})')
    if reachable():
        print(f'    ok: {host} -> {lab_address(host)}')
    elif sys.platform != 'darwin':
        die(f'{host} is not reachable and only macOS is set up automatically')
    elif local_network_blocked():  # DNS and the network are fine: --dns would change nothing
        print(f'    {host} resolves to {lab_address(host)}, the homelab, but {LOCAL_NETWORK}.')
        if not can_move_to_apple_python():
            die(f'{LOCAL_NETWORK_PERMISSION}; then `magehand setup` again')
        move_to_apple_python()
    else:
        current = lab_address(host)
        print(f'    {host} resolves to {current or "nothing"} here, which is not the homelab.')
        if not a.dns and not sys.stdin.isatty():  # prompt only in a terminal
            die(f'{host} does not resolve to the homelab here: run `magehand setup --dns ADDRESS` '
                '(the homelab\'s DNS, your home gateway)')
        server = a.dns or input('    Homelab DNS address (your home gateway, e.g. 192.168.1.1): ').strip()
        answer = dns_answer(server, host)
        if not answer or answer.startswith('127.'):
            die(f'{server} did not answer for {host} with a homelab address ({answer or "no answer"}); '
                'is it the right address, and can this Mac reach it right now?')
        sudo_write_resolver(server)
        for _ in range(10):
            if reachable():
                break
            time.sleep(1)
        else:
            die(f'{host} now resolves via {server} but https still fails; is this Mac on a network that reaches it?')
        print(f'    ok: {host} -> {lab_address(host)} (undo: magehand setup --undo)')
    print('4/6 sign in')
    if signin_left() is not None:
        print('    ok (already signed in)')
    else:
        cmd_login(None)
    print('5/6 the homelab-app skill for coding agents (Claude Code, Codex)')
    for path in install_skill():
        print(f'    ok: {path}')
    print('6/6 pre-approved commands for Claude Code and Codex (read-only magehand and gh; merges, releases '
          'and tag pushes refused)')
    rejected = None
    for path, problem in install_approvals():
        print(f'    not installed: {path}: {problem}' if problem else f'    ok: {path}')
        rejected = rejected or problem
    if rejected:  # the one step that can fail without stopping setup: say it failed, and how to fix it
        die('setup is done except for Codex: ' + rejected_rules_step(rejected))
    print('Done: this machine is set up.')
    print(next_line('in an app repo, `magehand status` (or `magehand new REPO` to start one)'))


def setup_refresh(host):
    """`setup --refresh`, which `upgrade` runs from the new version: rewrite the skill and the approvals where
    setup installed them, check what can break without asking anything, and say whether setup is needed."""
    if skill_installed():
        install_skill()
        print('refreshed the homelab-app skill')
    rejected = None
    if approvals_installed():
        rejected = next((problem for _, problem in install_approvals() if problem), None)
        print('refreshed the pre-approved commands' + (' (not for Codex: it rejected them)' if rejected else ''))
    needs = []
    try:
        github_token()
    except SystemExit:
        needs.append('GitHub sign-in (`gh auth login`)')
    if not reachable():
        needs.append(f'{LOCAL_NETWORK}: {local_network_step()}' if local_network_blocked()
                     else f'{host} is not reachable')
    missing = approvals_missing()
    if rejected:
        needs.append(rejected_rules_step(rejected))
    elif missing:
        needs.append(f'the pre-approved commands for {" and ".join(missing)}')
    for need in needs:
        print(f'needs setup: {need}')
    print(next_line(('update Codex (`codex --version`), then `magehand setup`' if rejected else '`magehand setup`')
                    if needs else None))


# --- staying current -------------------------------------------------------------
# Releases are the repo's vX.Y.Z tags (the release workflow publishes each to
# PyPI), so the version check asks GitHub, like every other call here. Upgrading
# is one command the user runs, never automatic: a release runs on their Mac.

MAGEHAND_REPO = 'davidlarrimore/magehand'
LATEST_CACHE = STATE / 'latest.json'


def installed_version():
    try:
        return package_version('magehand')
    except PackageNotFoundError:
        return None


def version_tuple(text):
    parts = (text or '').lstrip('v').split('.')
    return tuple(int(p) for p in parts) if len(parts) == 3 and all(p.isdigit() for p in parts) else None


def newest_tag(names):
    """The highest vX.Y.Z among tag names, without the v; None if there is none."""
    versions = [v for v in (version_tuple(n) for n in names if n.startswith('v')) if v]
    return '.'.join(map(str, max(versions))) if versions else None


def latest_version(max_age=86400):
    """The newest released version (cached for a day); None when GitHub can't be asked."""
    try:
        cached = json.loads(LATEST_CACHE.read_text())
        # A failed lookup (offline) is retried after an hour, not on every command and session start.
        if time.time() - cached['checked'] < (min(max_age, 3600) if cached.get('failed') else max_age):
            return cached['version']
    except (OSError, ValueError, KeyError, TypeError):
        cached = {}
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'magehand'}
    token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
    if token:
        headers['Authorization'] = f'Bearer {token}'
    req = urllib.request.Request(f'https://api.github.com/repos/{MAGEHAND_REPO}/tags?per_page=100', headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:  # nosec B310  # fixed https URL
            latest = newest_tag([t.get('name', '') for t in json.load(resp) if isinstance(t, dict)])
    except (OSError, ValueError):
        latest = cached.get('version') if isinstance(cached, dict) else None
        try:
            LATEST_CACHE.parent.mkdir(parents=True, exist_ok=True)
            LATEST_CACHE.write_text(json.dumps({'checked': time.time(), 'version': latest, 'failed': True}))
        except OSError:
            pass
        return latest
    try:
        LATEST_CACHE.parent.mkdir(parents=True, exist_ok=True)
        LATEST_CACHE.write_text(json.dumps({'checked': time.time(), 'version': latest}))
    except OSError:
        pass
    return latest


def install_method(prefix=None, base_prefix=None):
    """How this copy was installed: uv, pipx, venv, managed (OpenClaw's VM pins it) or unknown."""
    prefix = str(prefix or sys.prefix)
    base_prefix = str(base_prefix or getattr(sys, 'base_prefix', sys.prefix))
    if prefix.startswith('/opt/magehand'):
        return 'managed'
    if '/uv/tools/' in prefix + '/':
        return 'uv'
    if '/pipx/venvs/' in prefix + '/':
        return 'pipx'
    return 'venv' if prefix != base_prefix else 'unknown'


# OpenClaw's VM (the managed copy) has GitHub as the bot and read-only kubectl, but no container runtime, no
# route to OpenBao and no passkey: these commands can't work there. Each says why and what does the job instead.
NOT_IN_VM = 'OpenClaw\'s VM has no container runtime and no OpenBao sign-in'
MANAGED_REFUSALS = {
    'run': (f"can't run the app here: {NOT_IN_VM}. CI runs its tests (the Dockerfile's test stage) and the "
            'browser check on every push', '`magehand check`, then push'),
    'dev': (f"no local blocks here: {NOT_IN_VM}. The app's dev deploy has real ones", '`magehand check`, then push'),
    'bucket': (f"can't reach the app's bucket here: {NOT_IN_VM} for its keys", 'ask the owner to run it'),
    'login': ("nothing to sign in to here: only the owner's passkey signs in, and the platform gives the app its "
              'keys itself', None),
    'check --ui': (f"can't run the browser check here: {NOT_IN_VM}. CI runs it on every push",
                   '`magehand check`, then push'),
    'setup': ('this machine is set up by the platform (homelab platform/openclaw/vm/provision.sh)', None),
}


def refuse_if_managed(command):
    """Exit 1 with why and what to do instead when `command` can't work in the managed copy."""
    if install_method() == 'managed' and command in MANAGED_REFUSALS:
        why, step = MANAGED_REFUSALS[command]
        print(f'magehand: {why}\n{next_line(step)}', file=sys.stderr)  # why first, then what to do
        sys.exit(1)


# Set by coding agents in the commands they run (Claude Code: AI_AGENT, CLAUDECODE; Codex: CODEX_SANDBOX when it
# sandboxes one): their output isn't a terminal, but an agent reads stderr and can tell the user.
AGENT_VARS = ('AI_AGENT', 'CLAUDECODE', 'CODEX_SANDBOX')


def run_by_agent():
    return any(os.environ.get(k) for k in AGENT_VARS)


def newer_available():
    """(installed, latest, whether latest is newer): latest is None when GitHub can't be asked."""
    current, latest = installed_version(), latest_version()
    now, then = version_tuple(current), version_tuple(latest)
    return current, latest, bool(now and then and then > now)


def update_notice():
    """One line on stderr when a newer magehand exists, in a terminal or for a coding agent; quiet in CI, other
    scripts and the managed copy."""
    if os.environ.get('CI') or os.environ.get('MAGEHAND_NO_UPDATE_CHECK') \
            or not (sys.stderr.isatty() or run_by_agent()) or install_method() == 'managed':
        return
    current, latest, newer = newer_available()
    if newer:
        print(f'magehand {latest} is available (you have {current}): magehand upgrade'
              + (' (tell the user: it updates the skill and pre-approved commands too)' if run_by_agent() else ''),
              file=sys.stderr)


UPGRADE_COMMANDS = {'uv': ['uv', 'tool', 'upgrade', 'magehand'], 'pipx': ['pipx', 'upgrade', 'magehand'],
                    'venv': [sys.executable, '-m', 'pip', 'install', '--upgrade', 'magehand']}


def cmd_upgrade(_a):
    method = install_method()
    if method == 'managed':
        die('this copy is pinned by the platform (homelab platform/openclaw/vm/versions.env); '
            'a PR there upgrades it')
    if method not in UPGRADE_COMMANDS:
        die('not installed with uv, pipx or a venv; reinstall with `uv tool install magehand`')
    current, latest = installed_version(), latest_version(max_age=0)
    if current and latest and version_tuple(latest) <= (version_tuple(current) or ()):
        print(f'magehand {current} is the latest')
    else:
        cmd = UPGRADE_COMMANDS[method]
        print(f'magehand {current or "?"} -> {latest or "latest"}: {" ".join(cmd)}', flush=True)
        if subprocess.run(cmd).returncode != 0:  # nosec B603  # fixed commands above
            die('the upgrade failed (see above)')
    # The new version, from this install's own bin (not whatever is first on PATH).
    exe = pathlib.Path(sys.executable).with_name('magehand')
    if not exe.is_file():
        print('upgraded')
        print(next_line('`magehand skill --install`, if you use the skill'))
        return
    out = subprocess.run([str(exe), 'version'], capture_output=True, text=True)  # nosec B603
    print(f'now: magehand {out.stdout.splitlines()[0] if out.returncode == 0 and out.stdout else "?"}', flush=True)
    # The new version's own setup, quietly: it refreshes the skill and the approvals where setup put them, and
    # says (with the `next:` line) whether `magehand setup` needs you. A version without --refresh: its skill.
    code = subprocess.run([str(exe), 'setup', '--refresh']).returncode  # nosec B603
    if code == 2:
        if skill_installed():
            subprocess.run([str(exe), 'skill', '--install'], stdout=subprocess.DEVNULL)  # nosec B603
        print(next_line(None))
    elif code != 0:  # its own `next:` may not have been printed
        print("the new version's quiet setup failed (see above)")
        print(next_line('`magehand setup`'))


def cmd_version(a):
    managed = install_method() == 'managed'  # no network call there: provisioning runs this on every sync
    current, latest, newer = (installed_version(), None, False) if managed else newer_available()
    if getattr(a, 'json', False):
        print(json.dumps({'version': current, 'latest': latest, 'managed': managed, 'newer': newer,
                          'next': '`magehand upgrade`' if newer else None}))
        return
    print(current or 'unknown (not installed as a package)')
    if managed:
        print('pinned by the platform (homelab platform/openclaw/vm/versions.env)')
    elif newer:
        print(f'{latest} is available: magehand upgrade')
    else:
        print('upgrade: magehand upgrade')


# --- the command line ----------------------------------------------------------------
# Conventions (clig.dev, POSIX, the Heroku CLI style guide; AGENTS.md "CLI
# conventions"): -h/--help on every command, -V/--version; usage errors on stderr
# with exit 2 and a suggestion; failures exit 1; data on stdout, messages on
# stderr; --json where a script or agent reads the result; one flag name per
# idea (-a/--app); flag > environment > config file; prompts only in a terminal.

class Parser(argparse.ArgumentParser):
    def error(self, message):
        match = re.search(r"argument (\S+): invalid choice: '([^']*)' \(choose from (.*)\)", message)
        if match:
            choices = [c.strip().strip("'") for c in match.group(3).split(', ')]  # quoted before Python 3.14
            close = difflib.get_close_matches(match.group(2), choices, n=1)
            hint = f"; did you mean '{close[0]}'?" if close else ''
            if match.group(1) == 'COMMAND':  # 13 choices: point to the list instead of printing it
                message = f"unknown command '{match.group(2)}'{hint} (magehand --help lists them)"
            else:
                message += hint
        self.print_usage(sys.stderr)
        self.exit(2, f'{self.prog}: error: {message}\n')


# --- bucket: ls, cp, sync, url ---------------------------------------------------------
# Read and write the app's buckets with the keys the platform hands the app (the same
# <service>-connection values): the local Garage after `dev up`, or OpenBao's copy for dev and prod.
# One small SigV4 client (stdlib), path-style, so R2 and Garage are the same to it.

S3_MAX_BODY = 256 * 1024 * 1024   # one request, held in memory: a bigger file is `sync`'s to split later
BUCKET_ENVS = ('local', 'dev', 'prod')
BUCKET_CAP_BYTES = 10**9   # the platform's per-bucket cap; it alerts at 80% and 100%
FILES_HOST = 'files.davidlarrimore.com'


def s3_sign(method, host, path, query, payload_hash, key_id, secret, region, extra=None, when=None):
    """SigV4 request headers for S3 (`path` and `query` already encoded; `extra` are more headers
    to sign, lowercase names). Values are trimmed and runs of spaces collapse, as SigV4 requires."""
    when = when or datetime.datetime.now(datetime.timezone.utc)
    stamp = when.strftime('%Y%m%dT%H%M%SZ')
    headers = {'host': host, 'x-amz-date': stamp, 'x-amz-content-sha256': payload_hash, **(extra or {})}
    names = sorted(headers)
    canonical = '\n'.join([method, path, query,
                           ''.join(f"{n}:{' '.join(str(headers[n]).split())}\n" for n in names),
                           ';'.join(names), payload_hash])
    day = stamp[:8]
    scope = f'{day}/{region}/s3/aws4_request'
    to_sign = '\n'.join(['AWS4-HMAC-SHA256', stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = f'AWS4{secret}'.encode()
    for part in (day, region, 's3', 'aws4_request'):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    headers['authorization'] = (f'AWS4-HMAC-SHA256 Credential={key_id}/{scope}, '
                                f'SignedHeaders={";".join(names)}, '
                                f'Signature={hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()}')
    return headers


def s3_call(conn, method, key='', params=None, body=b'', headers=None):
    """(status, response headers, body bytes). `key` is the object key ('' for the bucket)."""
    endpoint = conn['endpoint'].rstrip('/')
    host = urllib.parse.urlsplit(endpoint).netloc
    path = '/' + urllib.parse.quote(f'{conn["bucket"]}/{key}' if key else conn['bucket'], safe='/~')
    query = '&'.join(f'{urllib.parse.quote(k, safe="~")}={urllib.parse.quote(str(v), safe="~")}'
                     for k, v in sorted((params or {}).items()))
    signed = s3_sign(method, host, path, query, hashlib.sha256(body).hexdigest(), conn['access_key_id'],
                     conn['secret_access_key'], conn.get('region') or 'auto', {k.lower(): v for k, v in (headers or {}).items()})
    req = urllib.request.Request(f'{endpoint}{path}' + (f'?{query}' if query else ''), data=body or None, method=method,
                                 headers={k.title() if k != 'authorization' else 'Authorization': v
                                          for k, v in signed.items() if k != 'host'})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:  # nosec B310  # the bucket's own S3 endpoint
            return resp.status, dict(resp.getheaders()), resp.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers.items()), error.read()
    except OSError as error:
        die(f'cannot reach {endpoint}: {error}')


def s3_error(status, body):
    """A short reason for a failed S3 call (its <Code> and <Message>)."""
    try:
        root = ET.fromstring(body)  # nosec B314  # the bucket endpoint's own error document
        found = {child.tag.split('}')[-1]: (child.text or '') for child in root}
        return f'HTTP {status} {found.get("Code", "")}: {found.get("Message", "")}'.strip()
    except ET.ParseError:
        return f'HTTP {status}'


def bucket_summary(app_name, block, svc):
    """Lines for `magehand app`: where a bucket lives and how it is reached."""
    access = (svc or {}).get('access') or 'private'
    backend = {'public': 'Cloudflare R2, served read-only over HTTP', 'private': 'Cloudflare R2, S3 API only',
               'internal': 'Garage in the cluster, never on the internet, 1 GB quota'}.get(access, access)
    lines = [f'backend: {backend}']
    if access == 'public':
        lines.append(f'url: dev https://{FILES_HOST}/{app_name}-dev/{block}/   prod https://{FILES_HOST}/{app_name}/{block}/')
    lines.append(f'use: magehand bucket ls {block}   (local: magehand dev up, then --env local)')
    return lines


def bucket_blocks(app):
    return {block: svc for block, svc in app['services'].items() if (svc or {}).get('type') == 'bucket'}


def bucket_target(app, text):
    """(block, key) from 'BLOCK' or 'BLOCK/KEY'; dies unless BLOCK is one of the app's bucket blocks."""
    block, _, key = text.partition('/')
    blocks = bucket_blocks(app)
    if block not in blocks:
        have = ', '.join(sorted(blocks)) or 'none: request one with `type: bucket` in app.yaml'
        die(f'{block!r} is not a bucket block of {app["name"]} (buckets: {have})')
    return block, key


def bucket_connection(app, block, env):
    """The keys for one bucket block in one environment, as the app's pod would get them."""
    access = (bucket_blocks(app)[block] or {}).get('access') or 'private'
    if env == 'local':
        local = local_state(app['name'])
        if block not in local:
            die(f'block {block} has no local object store: run `magehand dev up` (or use --env dev)')
        return local_connection('bucket', block, local, app=app['name'])
    namespace = f'{app["name"]}-dev' if env == 'dev' else app['name']
    if access == 'internal':
        die(f'{block} is an internal bucket: it exists only inside the cluster, so it cannot be reached from '
            f'here in {env}. Use --env local (magehand dev up), or code running in the app')
    status, body = bao('GET', f'apps/data/{namespace}/bucket-{block}', valid_token())
    if status == 404:
        die(f'{block} is not provisioned in {env} yet (the platform makes it within about 2 minutes of the '
            f'merge; production needs a release)')
    if status != 200:
        die(f'cannot read the {env} keys for {block} (HTTP {status}); `magehand login` and try again')
    return body['data']['data']


def bucket_url(app, block, env, conn, key):
    """The public URL for an object, or None (only a public bucket has one)."""
    if conn.get('url'):
        return conn['url'].rstrip('/') + '/' + urllib.parse.quote(key, safe='/~') if key else conn['url']
    if (bucket_blocks(app)[block] or {}).get('access') == 'public' and env != 'local':
        ns = f'{app["name"]}-dev' if env == 'dev' else app['name']
        return f'https://{FILES_HOST}/{ns}/{block}/' + urllib.parse.quote(key, safe='/~')
    return None


def bucket_list(conn, prefix='', delimiter='/'):
    """([(key, size, etag, modified)], [common prefixes]) for everything under `prefix`."""
    objects, prefixes, token = [], [], None
    while True:
        params = {'list-type': 2, 'prefix': prefix}
        if delimiter:
            params['delimiter'] = delimiter
        if token:
            params['continuation-token'] = token
        status, _, body = s3_call(conn, 'GET', '', params)
        if status != 200:
            die(f'listing failed: {s3_error(status, body)}')
        root = ET.fromstring(body)  # nosec B314  # the bucket endpoint's own listing
        token = None
        for node in root:
            tag = node.tag.split('}')[-1]
            fields = {c.tag.split('}')[-1]: (c.text or '') for c in node}
            if tag == 'Contents':
                objects.append((fields.get('Key', ''), int(fields.get('Size') or 0),
                                fields.get('ETag', '').strip('"'), fields.get('LastModified', '')))
            elif tag == 'CommonPrefixes':
                prefixes.append(fields.get('Prefix', ''))
            elif tag == 'NextContinuationToken':
                token = node.text
        if not token:
            return objects, prefixes


def put_object(conn, key, path, content_type=None, cache_control=None):
    size = path.stat().st_size
    if size > S3_MAX_BODY:
        die(f'{path}: {size // (1024 * 1024)} MiB is over the {S3_MAX_BODY // (1024 * 1024)} MiB a single upload takes')
    ctype = content_type or mimetypes.guess_type(key)[0] or mimetypes.guess_type(str(path))[0] or 'application/octet-stream'
    extra = {'content-type': ctype}
    if cache_control:
        extra['cache-control'] = cache_control
    status, _, body = s3_call(conn, 'PUT', key, body=path.read_bytes(), headers=extra)
    if status not in (200, 204):
        die(f'upload of {key} failed: {s3_error(status, body)}')
    return size, ctype


def bucket_env(a):
    return a.env or 'dev'


def bucket_app(named=None):
    """The app of the checkout you are in: `magehand bucket` works on that app's buckets and nothing else.
    `--app` stays for scripts that name their own app; another app is refused, as `run` refuses its keys."""
    here = app_repo_here()
    if not here:
        die("run `magehand bucket` inside the app's repo: it works on the buckets of the app whose code this is "
            "(`magehand app` shows them)")
    if named and named != here:
        die(f"this checkout is {here}'s code: `magehand bucket` works only on {here}'s buckets, not {named}'s. "
            f'Run it from {named}\'s own repo')
    return app_here(here)


def bucket_usage(app, blocks, env):
    """Per bucket block: objects, bytes and the share of the 1 GB cap. An internal bucket is only
    reachable locally; asked for by name it is an error, found by sweeping it is reported as unreachable."""
    rows = []
    for block, explicit in blocks:
        row = {'bucket': block, 'env': env, 'access': (bucket_blocks(app)[block] or {}).get('access') or 'private',
               'cap_bytes': BUCKET_CAP_BYTES}
        if env != 'local' and row['access'] == 'internal' and not explicit:
            row['error'] = 'internal: reachable only with --env local'
        else:
            objects, _ = bucket_list(bucket_connection(app, block, env), '', delimiter='')
            row['objects'] = len(objects)
            row['bytes'] = sum(size for _, size, _, _ in objects)
            row['percent'] = round(100 * row['bytes'] / BUCKET_CAP_BYTES, 1)
        rows.append(row)
    return rows


def cmd_bucket(a):
    app = bucket_app(a.app_flag)
    env = bucket_env(a)
    action = a.bucket_action
    if action == 'usage':
        names = sorted(bucket_blocks(app))
        if a.target:
            block, _ = bucket_target(app, a.target)
            blocks = [(block, True)]
        elif names:
            blocks = [(n, False) for n in names]
        else:
            die(f'{app["name"]} has no bucket blocks: request one with `type: bucket` in app.yaml')
        rows = bucket_usage(app, blocks, env)
        if a.json:
            print(json.dumps({'app': app['name'], 'env': env, 'buckets': rows}, indent=1))
            return
        for row in rows:
            if 'error' in row:
                print(f'{row["bucket"]}: {row["error"]}')
                continue
            print(f'{row["bucket"]} ({env}, {row["access"]}): {row["objects"]} objects, {row["bytes"]} bytes '
                  f'({row["percent"]}% of the {BUCKET_CAP_BYTES // 10**6} MB cap)')
        return
    if action == 'ls':
        block, prefix = bucket_target(app, a.target)
        conn = bucket_connection(app, block, env)
        objects, prefixes = bucket_list(conn, prefix, delimiter='' if a.recursive else '/')
        if a.json:
            print(json.dumps({'bucket': block, 'env': env, 'prefix': prefix,
                              'prefixes': prefixes, 'objects': [{'key': k, 'size': n, 'etag': e, 'modified': m}
                                                                for k, n, e, m in objects]}, indent=1))
            return
        for name in prefixes:
            print(f'{"PRE":>10}  {name}')
        for key, size, _etag, modified in objects:
            print(f'{size:>10}  {modified[:19].replace("T", " ")}  {key}')
        if not objects and not prefixes:
            print(f'(nothing under {block}/{prefix})')
        return
    if action == 'url':
        block, key = bucket_target(app, a.target)
        conn = bucket_connection(app, block, env)
        url = bucket_url(app, block, env, conn, key)
        if url:
            print(json.dumps({'action': 'url', 'bucket': block, 'key': key, 'env': env, 'url': url})
                  if a.json else url)
            return
        access = (bucket_blocks(app)[block] or {}).get('access') or 'private'
        die(f'{block} is {access}: it has no public URL (only access: public does). Read it through the S3 API '
            f'with its keys, or serve it from the app')
    if action == 'cp':
        src, dst = a.source, a.dest
        names = set(bucket_blocks(app))

        def remote(text):
            # Deterministic, whatever is on disk: BUCKET/KEY is remote when BUCKET is one of the app's
            # bucket blocks. A local file or directory that has that name is written ./BUCKET/... or
            # with an absolute path.
            if text.startswith(('./', '../', '/', '~')):
                return False
            return text.split('/', 1)[0] in names
        if remote(src) == remote(dst):
            die('one side is a file and the other BUCKET/KEY (a bucket block of the app; write a local path '
                f'that starts with a bucket name as ./PATH): buckets: {", ".join(sorted(names)) or "none"}')
        if remote(dst):   # upload
            block, key = bucket_target(app, dst)
            path = pathlib.Path(src)
            if not path.is_file():
                die(f'{src}: not a file')
            if not key or key.endswith('/'):
                key = key + path.name
            conn = bucket_connection(app, block, env)
            size, ctype = put_object(conn, key, path, a.content_type, a.cache_control)
            url = bucket_url(app, block, env, conn, key)
            if a.json:
                print(json.dumps({'action': 'cp', 'direction': 'upload', 'bucket': block, 'key': key, 'env': env,
                                  'file': str(path), 'bytes': size, 'content_type': ctype, 'url': url}))
                return
            print(f'uploaded {size} bytes to {block}/{key} ({ctype}, {env})')
            if url:
                print(f'public: {url}')
            return
        block, key = bucket_target(app, src)   # download
        if not key:
            die(f'give the object key: {block}/KEY')
        conn = bucket_connection(app, block, env)
        status, _, body = s3_call(conn, 'GET', key)
        if status != 200:
            die(f'download of {key} failed: {s3_error(status, body)}')
        out = pathlib.Path(dst).expanduser()
        if out.is_dir():
            out = out / pathlib.PurePosixPath(key).name
        # A unique file made exclusively in the destination's directory, written through its
        # descriptor and then renamed over the destination: never an existing `NAME.part`, a symlink's
        # target, or another download's temporary file.
        fd, tmp_name = tempfile.mkstemp(dir=str(out.parent), prefix=f'.{out.name}.', suffix='.part')
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(body)
            os.replace(tmp_name, out)
        except BaseException:
            pathlib.Path(tmp_name).unlink(missing_ok=True)
            raise
        if a.json:
            print(json.dumps({'action': 'cp', 'direction': 'download', 'bucket': block, 'key': key, 'env': env,
                              'file': str(out), 'bytes': len(body)}))
        else:
            print(f'downloaded {len(body)} bytes to {out}')
        return
    if action == 'sync':
        block, prefix = bucket_target(app, a.dest)
        root = pathlib.Path(a.source)
        if not root.is_dir():
            die(f'{a.source}: not a directory')
        if prefix and not prefix.endswith('/'):
            prefix += '/'
        conn = bucket_connection(app, block, env)
        have = {k: e for k, _n, e, _m in bucket_list(conn, prefix, delimiter='')[0]}
        files = [f for f in sorted(root.rglob('*')) if f.is_file() and '.git' not in f.relative_to(root).parts]
        sent, skipped, keys = 0, 0, []
        progress = sys.stderr if a.json else sys.stdout    # a JSON object is all that goes to stdout
        for path in files:
            key = prefix + path.relative_to(root).as_posix()
            if have.get(key) == hashlib.md5(path.read_bytes()).hexdigest():  # nosec B324  # S3's ETag is an MD5, not a secret
                skipped += 1
                continue
            if a.dry_run:
                print(f'would upload {key}', file=progress)
            else:
                put_object(conn, key, path, None, a.cache_control)
                print(f'uploaded {key}', file=progress)
            keys.append(key)
            sent += 1
        if a.json:
            print(json.dumps({'action': 'sync', 'bucket': block, 'prefix': prefix, 'env': env,
                              'dry_run': bool(a.dry_run), 'uploaded': keys, 'unchanged': skipped}))
            return
        verb = 'would upload' if a.dry_run else 'uploaded'
        print(f'{verb} {sent} file(s), {skipped} unchanged, in {block}/{prefix} ({env})')
        return


# --- job: scheduled work, a CronJob in the app's own deploy/base ---------------------------
# The hosted-platform model (homelab docs/app-jobs-plan.md): a job is the app's image run with another command
# on a schedule, with the app's env. `job add` writes the CronJob; merging deploys it like the rest of deploy/.

JOB_NAME = re.compile(r'^[a-z]([a-z0-9-]{0,50}[a-z0-9])?$')  # a CronJob name: its Jobs add 11 characters (63 at most)
# The schedule grammar of Kubernetes' CronJob controller (robfig/cron v3 ParseStandard): five fields, each a
# list of `*`, `?`, a value or a low-high range, optionally /step (a whole number above 0); months and days of
# the week also by name; days of the week 0-6 (Sunday is 0). Or a descriptor, spelled exactly (a schedule is
# one only when it starts with @). `@every DURATION` is refused: its duration is Go's, which we can't check.
CRON_MACROS = ('@yearly', '@annually', '@monthly', '@weekly', '@daily', '@midnight', '@hourly')
CRON_MONTHS = {m: i + 1 for i, m in enumerate(('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct',
                                               'nov', 'dec'))}
CRON_DAYS = {d: i for i, d in enumerate(('sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'))}
CRON_FIELDS = (('minute', 0, 59, {}), ('hour', 0, 23, {}), ('day of month', 1, 31, {}), ('month', 1, 12, CRON_MONTHS),
               ('day of week', 0, 6, CRON_DAYS))
JOB_LABEL = 'homelab.davidlarrimore.com/job'  # on a job's pods; never app.kubernetes.io/name, the app's selector
JOB_TIMEOUT, JOB_MEMORY, JOB_TIMEZONE = '10m', '256Mi', 'Etc/UTC'
JOB_MAX_MEMORY_MI = 1024   # a container's memory limit at most (homelab platform/agent-apps-baseline limit-range)
JOB_START_DEADLINE = 3600  # a run that can't start within an hour is skipped, and the controller never gives up
JOB_HISTORY = 3            # finished runs kept per CronJob, for `job ls`
JOB_MIN_INTERVAL = 5       # minutes: more often than this is a warning
JOB_SEE = 'recipes "Run background or scheduled work"'


def cron_problem(schedule):
    """Why `schedule` is not a CronJob schedule, or None. Five fields or a macro; the time zone goes in
    timeZone, not the schedule."""
    text = str(schedule or '')
    if text.startswith('@'):
        if text in CRON_MACROS:
            return None
        if text.startswith('@every'):
            return '@every is not supported: write five cron fields (*/30 * * * * for every 30 minutes)'
        return f'{text!r} is not a descriptor: {", ".join(CRON_MACROS)}, spelled exactly'
    if text.startswith(('TZ=', 'CRON_TZ=')):
        return 'set the time zone with timeZone (--timezone), not in the schedule'
    fields = text.split()
    if len(fields) != 5:
        return (f'{text!r} is not a cron schedule: five fields (minute hour day-of-month month day-of-week), '
                f'or one of {", ".join(CRON_MACROS)}')
    for field, (what, low, high, names) in zip(fields, CRON_FIELDS):
        problem = cron_field_problem(field, low, high, names)
        if problem:
            return f'{what} {field!r}: {problem}'
    return None


def cron_field_problem(field, low, high, names):
    """Why one cron field is not valid for the controller's parser, or None."""
    def value(text):
        number = int(text) if text.isdigit() else names.get(text.lower())
        return number if number is not None and low <= number <= high else None
    named = f' or {", ".join(n.upper() for n in names)}' if names else ''
    for part in field.split(','):
        span, slash, step = part.partition('/')
        if slash and not (step.isdigit() and int(step) > 0):
            return 'a step (after /) is a whole number above 0'
        if span in ('*', '?'):
            continue
        start, dash, end = span.partition('-')
        first, last = value(start), (value(end) if dash else value(start))
        if first is None or last is None:
            return f'values are {low} to {high}{named}; parts are *, a value or a range (1-5), with an optional /step'
        if first > last:
            return f'the range {span} goes from high to low'
    return None


def cron_too_frequent(schedule):
    """True when the schedule's minute field runs more often than every JOB_MIN_INTERVAL minutes."""
    fields = str(schedule or '').split()
    if len(fields) != 5:
        return False
    minutes = set()
    for part in fields[0].split(','):
        base, slash, step = part.partition('/')
        step = int(step) if step.isdigit() and int(step) > 0 else 1
        if base == '*':
            low, high = 0, 59
        elif '-' in base and all(n.isdigit() for n in base.split('-', 1)):
            low, high = (int(n) for n in base.split('-', 1))
        elif base.isdigit():
            low, high = int(base), (59 if slash else int(base))  # "5/10": from minute 5, every 10
        else:
            return False
        minutes.update(range(low, high + 1, step))
    ordered = sorted(minutes)
    if len(ordered) < 2:
        return False
    gaps = [b - a for a, b in zip(ordered, ordered[1:])] + [60 - ordered[-1] + ordered[0]]
    return min(gaps) < JOB_MIN_INTERVAL


def duration_seconds(text):
    """Seconds in '90s', '10m' or '2h', or None."""
    match = re.fullmatch(r'(\d+)([smh])', str(text or ''))
    if not match or int(match.group(1)) == 0:
        return None
    return int(match.group(1)) * {'s': 1, 'm': 60, 'h': 3600}[match.group(2)]


def memory_mi(text):
    """MiB in '256Mi' or '1Gi', or None."""
    match = re.fullmatch(r'(\d+)(Mi|Gi)', str(text or ''))
    if not match or int(match.group(1)) == 0:
        return None
    return int(match.group(1)) * (1024 if match.group(2) == 'Gi' else 1)


def job_manifest(app, name, schedule, command, timeout, memory, timezone):
    """The CronJob for job `name`: the Deployment's image, env and security settings with `command`, and the
    settings the platform needs (no overlapping runs, deadlines, one retry, kept history). Its pods have their
    own labels, without app.kubernetes.io/name (the app's selector), so the app's Service never sends them
    requests whatever the job is called."""
    container, pod = app['container'], app['pod']
    labels = {JOB_LABEL: name, 'app.kubernetes.io/part-of': app['name'], 'app.kubernetes.io/component': 'job'}
    job = {'name': 'app', 'image': container.get('image') or f'ghcr.io/davidlarrimore/homelab-apps/{app["name"]}',
           'command': list(command)}
    for key in ('env', 'envFrom'):
        if container.get(key):
            job[key] = json.loads(json.dumps(container[key]))
    job['resources'] = {'requests': {'cpu': '10m', 'memory': f'{min(memory_mi(memory), 64)}Mi'},
                        'limits': {'memory': memory}}
    job['securityContext'] = json.loads(json.dumps(container.get('securityContext') or {
        'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}}))
    job['volumeMounts'] = [{'name': 'tmp', 'mountPath': '/tmp'}]  # nosec B108  # an emptyDir: the root is read-only
    spec = {'restartPolicy': 'Never', 'automountServiceAccountToken': False,
            'securityContext': json.loads(json.dumps(pod.get('securityContext') or {
                'runAsNonRoot': True, 'seccompProfile': {'type': 'RuntimeDefault'}})),
            'containers': [job], 'volumes': [{'name': 'tmp', 'emptyDir': {'sizeLimit': '256Mi'}}]}
    return {'apiVersion': 'batch/v1', 'kind': 'CronJob', 'metadata': {'name': name, 'labels': dict(labels)},
            'spec': {'schedule': schedule, 'timeZone': timezone, 'concurrencyPolicy': 'Forbid',
                     'startingDeadlineSeconds': JOB_START_DEADLINE, 'successfulJobsHistoryLimit': JOB_HISTORY,
                     'failedJobsHistoryLimit': JOB_HISTORY,
                     'jobTemplate': {'spec': {'backoffLimit': 1, 'activeDeadlineSeconds': timeout,
                                              'template': {'metadata': {'labels': labels}, 'spec': spec}}}}}


def kustomization_resources(text):
    """The resources of a kustomization.yaml's text (a list), or None when it is not a Kustomization."""
    try:
        spec = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(spec, dict) or not isinstance(spec.get('resources') or [], list):
        return None
    return [str(r) for r in spec.get('resources') or []]


# resources as a flow list, on one line or several: `resources: [a.yaml, b.yaml]`.
FLOW_RESOURCES = re.compile(r'^resources:[ \t]*\[(?P<items>[^\]]*)\]', re.MULTILINE)


def flow_items(match):
    try:
        items = yaml.safe_load('[' + match.group('items') + ']')
    except yaml.YAMLError:
        return None
    return [str(r) for r in items] if isinstance(items, list) else None


def add_resource(text, entry):
    """kustomization.yaml's text with `entry` added to resources, keeping its comments and layout (a flow list
    is rewritten on one line). edit_kustomization checks the result."""
    flow = FLOW_RESOURCES.search(text)
    if flow:
        items = flow_items(flow)
        if items is None:
            return text
        return text[:flow.start()] + 'resources: [' + ', '.join(items + [entry]) + ']' + text[flow.end():]
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if not re.match(r'^resources:\s*(#.*)?$', line.rstrip('\n')):
            continue
        last, indent = i, '  '
        for j in range(i + 1, len(lines)):
            stripped = lines[j].strip()
            if stripped.startswith('-') and lines[j][:1] in (' ', '-'):
                last, indent = j, lines[j][:len(lines[j]) - len(lines[j].lstrip())]
            elif stripped and not stripped.startswith('#'):
                break
        if not lines[last].endswith('\n'):
            lines[last] += '\n'
        lines.insert(last + 1, f'{indent}- {entry}\n')
        return ''.join(lines)
    return text + ('\n' if text and not text.endswith('\n') else '') + f'resources:\n  - {entry}\n'


def remove_resource(text, entry):
    """kustomization.yaml's text without `entry` in resources. edit_kustomization checks the result."""
    flow = FLOW_RESOURCES.search(text)
    if flow:
        items = flow_items(flow)
        if items is None:
            return text
        return text[:flow.start()] + 'resources: [' + ', '.join(i for i in items if i != entry) + ']' + text[flow.end():]
    return ''.join(line for line in text.splitlines(keepends=True)
                   if not re.match(r'^\s*-\s*["\']?' + re.escape(entry) + r'["\']?\s*(#.*)?$', line.rstrip('\n')))


def edit_kustomization(text, entry, add=True):
    """kustomization.yaml's text with `entry` added to (or removed from) resources, or None when the edit
    would change anything else: every other resource and key must come out the same."""
    try:
        before = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return None
    old = kustomization_resources(text)
    if not isinstance(before, dict) or old is None or (entry in old) == add:
        return None
    new = (add_resource if add else remove_resource)(text, entry)
    try:
        after = yaml.safe_load(new) or {}
    except yaml.YAMLError:
        return None
    wanted = old + [entry] if add else [r for r in old if r != entry]
    rest = lambda spec: {k: v for k, v in spec.items() if k != 'resources'}  # noqa: E731
    if not isinstance(after, dict) or kustomization_resources(new) != wanted or rest(after) != rest(before):
        return None
    return new


def deploy_documents(local):
    """(file, document) for every YAML document in deploy/base, dev and prod."""
    for part in DEPLOY_DIRS:
        for path in sorted((local / part).glob('*.y*ml')) if (local / part).is_dir() else ():
            try:
                docs = list(yaml.safe_load_all(path.read_text()))
            except (OSError, yaml.YAMLError):
                continue  # load_app and check_deploy_files report a file that isn't YAML
            for doc in docs:
                if isinstance(doc, dict):
                    yield f'{DEPLOY}/{part}/{path.name}', doc


def cronjob_parts(doc):
    """(CronJob spec, Job spec, pod spec, first container) of a CronJob document, each {} when missing."""
    spec = doc.get('spec') or {}
    job = (spec.get('jobTemplate') or {}).get('spec') or {}
    pod = (job.get('template') or {}).get('spec') or {}
    return spec, job, pod, ((pod.get('containers') or [{}])[0] or {})


def check_jobs(app, catalog):
    """Rules for the CronJobs (and bare Jobs) in a checkout's deploy/: the settings the platform needs, the
    app's own image, Secrets that exist, pods the app's Service doesn't select, and listed in the base."""
    if app.get('where') != DEPLOY or not app.get('root'):
        return []
    local = pathlib.Path(app['root']) / DEPLOY
    found = []
    listed = None
    kustomization = local / 'base' / 'kustomization.yaml'
    if kustomization.is_file():
        listed = kustomization_resources(kustomization.read_text())
    own = r'ghcr\.io/davidlarrimore/homelab-apps/' + re.escape(app['name']) + r'(@sha256:[0-9a-f]{64})?'
    for where, doc in deploy_documents(local):
        kind, name = doc.get('kind'), str((doc.get('metadata') or {}).get('name') or '')
        if kind == 'Job':
            found.append(finding('warning', where, f'Job {name}: one-off Jobs are not supported yet (it runs once, '
                                 'when first synced, and Argo CD keeps re-creating it); scheduled work is a CronJob '
                                 '(`magehand job add`)', JOB_SEE))
            continue
        if kind != 'CronJob':
            continue
        spec, job, pod, container = cronjob_parts(doc)
        label = f'CronJob {name}'
        if not JOB_NAME.match(name):
            found.append(finding('error', where, f'{label}: a job name is lowercase letters, digits and dashes, '
                                 'starting with a letter (at most 52)', JOB_SEE))
        problem = cron_problem(spec.get('schedule'))
        if problem:
            found.append(finding('error', where, f'{label}: schedule {problem}', JOB_SEE))
        elif cron_too_frequent(spec.get('schedule')):
            found.append(finding('warning', where, f'{label}: runs more often than every {JOB_MIN_INTERVAL} '
                                 'minutes; a loop in a worker suits that better than a new pod each time', JOB_SEE))
        if spec.get('concurrencyPolicy') != 'Forbid':
            found.append(finding('error', where, f'{label}: concurrencyPolicy: Forbid is required: without it a '
                                 'slow run and the next one run at the same time', JOB_SEE))
        if not spec.get('startingDeadlineSeconds'):
            found.append(finding('error', where, f'{label}: startingDeadlineSeconds is required: without it the '
                                 'controller stops scheduling the job for good after 100 missed runs', JOB_SEE))
        if not job.get('activeDeadlineSeconds'):
            found.append(finding('error', where, f'{label}: jobTemplate.spec.activeDeadlineSeconds (the timeout) is '
                                 'required: with Forbid, a run that hangs blocks every later run', JOB_SEE))
        if job.get('backoffLimit') is None or int(job.get('backoffLimit') or 0) > 2:
            found.append(finding('error', where, f'{label}: jobTemplate.spec.backoffLimit 0 to 2 is required (the '
                                 'default, 6, retries a failing run for hours)', JOB_SEE))
        image = str(container.get('image') or '')
        if not re.fullmatch(own, image):
            found.append(finding('error', where, f'{label}: image {image or "(none)"}: a job runs the app\'s own image '
                                 f'(ghcr.io/davidlarrimore/homelab-apps/{app["name"]}, without a digest)', JOB_SEE))
        labels = ((job.get('template') or {}).get('metadata') or {}).get('labels') or {}
        selector = app.get('selector') or {}
        if selector and all(labels.get(k) == v for k, v in selector.items()):
            found.append(finding('error', where, f'{label}: its pods carry the Deployment\'s labels '
                                 f'({", ".join(f"{k}={v}" for k, v in selector.items())}), so the app\'s Service would '
                                 'send requests to a job; give them their own (`magehand job add` does)', JOB_SEE))
        found += env_findings(app, container, where, catalog)
        if where.startswith(f'{DEPLOY}/base/') and listed is not None and where.rsplit('/', 1)[1] not in listed:
            found.append(finding('error', where, f'{label}: not in deploy/base/kustomization.yaml resources, so it '
                                 'is never deployed', JOB_SEE))
    return found


def job_app(named=None):
    """The app of this checkout, from its deploy/: `magehand job` edits and lists that app's jobs only."""
    here = app_repo_here()
    if not here:
        die("run `magehand job` inside the app's repo: its jobs are files in that repo's deploy/base")
    if named and named != here:
        die(f"this checkout is {here}'s code: `magehand job` works only on {here}'s jobs, not {named}'s")
    app = app_here(here)
    if app.get('where') != DEPLOY or not app.get('root'):
        die(f'{here} has no deploy/ with a base in this checkout: its jobs go in deploy/base next to the Deployment')
    return app


def local_jobs(app):
    """The CronJobs in the checkout's deploy/ as rows: name, file, schedule, time zone, command."""
    rows = []
    for where, doc in deploy_documents(pathlib.Path(app['root']) / DEPLOY):
        if doc.get('kind') == 'CronJob':
            spec, _, _, container = cronjob_parts(doc)
            rows.append({'name': str((doc.get('metadata') or {}).get('name') or ''), 'file': where,
                         'schedule': spec.get('schedule'), 'timezone': spec.get('timeZone') or 'UTC',
                         'command': container.get('command') or container.get('args') or []})
    return rows


def job_runs(jobs, cronjob):
    """The finished and running Jobs of `cronjob`, newest first: name, state, start, seconds."""
    runs = []
    for job in jobs:
        meta = job.get('metadata') or {}
        if not any(o.get('kind') == 'CronJob' and o.get('name') == cronjob for o in meta.get('ownerReferences') or []):
            continue
        status = job.get('status') or {}
        failed = next((c for c in status.get('conditions') or []
                       if c.get('type') == 'Failed' and c.get('status') == 'True'), None)
        state = 'ok' if status.get('succeeded') else 'failed' if failed else 'pending'
        end = status.get('completionTime') or (failed or {}).get('lastTransitionTime')
        runs.append({'job': meta.get('name'), 'state': state, 'started': status.get('startTime'),
                     'seconds': seconds_between(status.get('startTime'), end) if end else None})
    return sorted(runs, key=lambda r: r['started'] or '', reverse=True)


def job_rows(local, cluster):
    """The checkout's jobs with what dev has (cluster: kubectl's JSON of cronjobs and jobs, or None)."""
    rows = {r['name']: dict(r, dev=None) for r in local}
    items = (cluster or {}).get('items') or []
    jobs = [i for i in items if i.get('kind') == 'Job']
    for cron in (i for i in items if i.get('kind') == 'CronJob'):
        name = (cron.get('metadata') or {}).get('name')
        status, spec = cron.get('status') or {}, cron.get('spec') or {}
        row = rows.setdefault(name, {'name': name, 'file': None, 'schedule': spec.get('schedule'),
                                     'timezone': spec.get('timeZone') or 'UTC', 'command': [], 'dev': None})
        row['dev'] = {'suspended': bool(spec.get('suspend')), 'last_schedule': status.get('lastScheduleTime'),
                      'last_success': status.get('lastSuccessfulTime'), 'active': len(status.get('active') or []),
                      'runs': job_runs(jobs, name)}
    return sorted(rows.values(), key=lambda r: r['name'])


def when(stamp):
    """YYYY-MM-DD HH:MMZ of a Kubernetes timestamp."""
    return f'{stamp[:10]} {clock(stamp)}' if stamp else 'never'


def job_add(a, app, base):
    command = list(getattr(a, 'cmd', None) or [])
    if not command:
        usage('job', 'give the command after --, e.g. magehand job add collect --schedule "15 6 * * *" '
                     '-- python -m jobs.collect')
    if not JOB_NAME.match(a.name):
        usage('job', f'job name {a.name!r}: lowercase letters, digits and dashes, starting with a letter (at most 52)')
    problem = cron_problem(a.schedule)
    if problem:
        usage('job', f'--schedule {problem}')
    timeout = duration_seconds(a.timeout)
    if not timeout:
        usage('job', f'--timeout {a.timeout!r}: a number with s, m or h (90s, 10m, 2h)')
    mi = memory_mi(a.memory)
    if not mi or mi > JOB_MAX_MEMORY_MI:
        usage('job', f'--memory {a.memory!r}: Mi or Gi, at most {JOB_MAX_MEMORY_MI}Mi (a container\'s limit)')
    try:
        import zoneinfo
        zoneinfo.ZoneInfo(a.timezone)
    except (ImportError, ValueError, LookupError, OSError):
        usage('job', f'--timezone {a.timezone!r}: an IANA zone such as Etc/UTC or America/New_York')
    if any(r['name'] == a.name for r in local_jobs(app)):
        die(f'job {a.name} exists: edit its file, or `magehand job rm {a.name}` first')
    path = base / f'job-{a.name}.yaml'
    if path.exists():
        die(f'{DEPLOY}/base/{path.name} exists: pick another name or remove the file')
    if cron_too_frequent(a.schedule):
        print(f'warning: {a.schedule!r} runs more often than every {JOB_MIN_INTERVAL} minutes', file=sys.stderr)
    manifest = job_manifest(app, a.name, a.schedule, command, timeout, a.memory, a.timezone)
    pod_labels = manifest['spec']['jobTemplate']['spec']['template']['metadata']['labels']
    selector = app.get('selector') or {}
    if selector and all(pod_labels.get(k) == v for k, v in selector.items()):
        die(f'the Deployment selects pods by {", ".join(f"{k}={v}" for k, v in selector.items())}, which a job\'s '
            "pods would match, so the app's Service would send them requests: select the app's pods by "
            'app.kubernetes.io/name')
    # Every check before any write: the kustomization edit is made (and verified) first, written last.
    kustomization = base / 'kustomization.yaml'
    listed = new = None
    if kustomization.is_file():
        new = edit_kustomization(kustomization.read_text(), path.name)
        if new is None:
            die(f'could not add {path.name} to {DEPLOY}/base/kustomization.yaml without changing its other '
                'resources (an unusual layout): nothing written. Add the job by hand, or write resources as a '
                'block list (`resources:` then `  - deployment.yaml` lines)')
        listed = f'{DEPLOY}/base/kustomization.yaml'
    header = (f'# Job {a.name}: written by `magehand job add`. Edit it freely; `magehand check` checks it.\n'
              f'# The app\'s image and env with another command; `magehand job ls` shows its runs in dev.\n')
    path.write_text(header + yaml.safe_dump(manifest, sort_keys=False, default_flow_style=False))
    if new is not None:
        kustomization.write_text(new)
    step = '`magehand check`, then commit and merge: the platform deploys it to dev with the rest of deploy/'
    if a.json:
        print(json.dumps({'action': 'add', 'job': a.name, 'file': f'{DEPLOY}/base/{path.name}', 'kustomization': listed,
                          'schedule': a.schedule, 'timezone': a.timezone, 'command': command,
                          'timeout_seconds': timeout, 'memory': a.memory, 'next': step}, indent=1))
        return
    print(f'wrote {DEPLOY}/base/{path.name}: {a.schedule} ({a.timezone}), {shlex.join(command)}, '
          f'timeout {a.timeout}, memory {a.memory}')
    print(f'added it to {listed}' if listed else
          f'warning: no {DEPLOY}/base/kustomization.yaml: list {path.name} in the base\'s resources', file=sys.stderr)
    print(next_line(step))


def job_rm(a, app, base):
    path = base / f'job-{a.name}.yaml'
    if not path.is_file():
        names = [r['name'] for r in local_jobs(app)]
        close = difflib.get_close_matches(a.name, names, n=1)
        die(f'no {DEPLOY}/base/{path.name}' + (f"; did you mean '{close[0]}'?" if close else '')
            + (f' (jobs: {", ".join(names)}; a job not written by `job add` is removed by hand)' if names else ''))
    kustomization = base / 'kustomization.yaml'
    listed = new = None
    if kustomization.is_file():
        text = kustomization.read_text()
        if path.name in (kustomization_resources(text) or []):
            new = edit_kustomization(text, path.name, add=False)
            if new is None:
                die(f'could not take {path.name} out of {DEPLOY}/base/kustomization.yaml without changing its other '
                    'resources: nothing removed. Remove the file and its entry by hand')
            listed = f'{DEPLOY}/base/kustomization.yaml'
    path.unlink()
    if new is not None:
        kustomization.write_text(new)
    step = 'commit and merge: that removes the CronJob from dev (its finished runs go with it)'
    if a.json:
        print(json.dumps({'action': 'rm', 'job': a.name, 'file': f'{DEPLOY}/base/{path.name}', 'kustomization': listed,
                          'next': step}))
        return
    print(f'removed {DEPLOY}/base/{path.name}' + (f' and its line in {listed}' if listed else ''))
    print(next_line(step))


def job_ls(a, app):
    namespace = f'{app["name"]}-dev'
    cluster, why = (None, '--no-cluster') if a.no_cluster else kubectl_json('cronjobs,jobs', '-n', namespace)
    absent = no_cluster(why)  # a laptop: the platform's published copy instead, else say where the runs are
    source, as_of = 'cluster', None
    if absent:
        repo = origin_repo(app['root'])
        published, missing = platform_status(repo) if repo else (None, 'this checkout has no GitHub origin')
        if published:
            cluster, source, as_of = {'items': (published.get('envs') or {}).get('dev') or []}, 'platform', published.get('as_of')
        else:
            absent = f'{absent}; {missing}'
    rows = job_rows(local_jobs(app), cluster)
    failed = [run['job'] for row in rows for run in ((row['dev'] or {}).get('runs') or [])[:1] if run['state'] == 'failed']
    stale = bool(as_of) and (seconds_between(as_of, time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())) or 0) > PLATFORM_STATUS_STALE
    logs = (f'`kubectl -n {namespace} logs job/{failed[0]}`' if source == 'cluster' else
            'ask OpenClaw on Telegram for its logs (this machine has no cluster access)') if failed else None
    step = ('`magehand job add NAME --schedule CRON -- COMMAND`' if not rows else
            f'read why its last run failed: {logs}' if failed else
            "the platform's status is over 2 hours old: tell the owner (OpenClaw's publish-status timer stopped?)"
            if stale else RUNS_ELSEWHERE if cluster is None and absent else None)
    if a.json:
        print(json.dumps({'app': app['name'], 'namespace': namespace,
                          'cluster': f'published by the platform as of {as_of}' if source == 'platform' else
                          absent or why or 'read', 'source': source, 'as_of': as_of, 'stale': stale, 'jobs': rows,
                          'next': step}, indent=1))
        return
    if not rows:
        print(f'{app["name"]} has no jobs')
        print(next_line(step))
        return
    if source == 'platform':
        print(f"dev as the platform published it at {clock(as_of)} (this machine has no cluster access)"
              + (': OVER 2 HOURS OLD' if stale else ''))
    for row in rows:
        where = row['file'] or 'in dev only: not in this checkout\'s deploy/ (removed on this branch?)'
        command = shlex.join(map(str, row['command'])) if row['command'] else '-'
        print(f'{row["name"]}: {row["schedule"]} ({row["timezone"]}), {command} [{where}]')
        dev = row['dev']
        if cluster is None:
            print(f'  dev: not checked ({absent or why})')
        elif dev is None:
            print('  dev: not deployed yet (it deploys when merged)')
        else:
            state = 'suspended' if dev['suspended'] else f'{dev["active"]} running' if dev['active'] else 'idle'
            print(f'  dev: {state}; last run {when(dev["last_schedule"])}, last success {when(dev["last_success"])}')
            for run in dev['runs']:
                took = f', {run["seconds"]}s' if run['seconds'] is not None else ''
                print(f'    {STATUS_MARK[run["state"]]} {run["job"]} {when(run["started"])}{took}')
            if dev['runs'] and source == 'cluster':
                print(f'  logs: kubectl -n {namespace} logs job/{dev["runs"][0]["job"]}')
    print(next_line(step))


# --- pr ready: the one step of the review routine that can fail silently ---------------------------
# A write magehand makes as the user, through gh: it marks a green PR ready for review and requests
# OpenClaw's bot, then reads the PR back, because a request can leave GitHub recording nobody (a bot that
# isn't a collaborator: #44; `gh pr edit --add-reviewer` even exits 0 then). The request goes through REST:
# `gh pr edit`'s GraphQL query fails now and then ("Something went wrong while executing your query").

PR_FIELDS = 'number,url,state,isDraft,headRefOid,author,statusCheckRollup,reviewRequests'


def requested(pr):
    """Whether OpenClaw's bot is among the PR's requested reviewers."""
    return any((r.get('login') or '').lower() == BOT.lower() for r in pr.get('reviewRequests') or [])


def cmd_pr(a):
    target = [str(a.number)] if a.number else []
    pr, err = gh_query('pr', 'view', *target, '--json', PR_FIELDS)
    if err:
        die(f'cannot read {"PR #" + str(a.number) if a.number else "the PR for this branch"}: {err}'
            + (' (open one with `gh pr create --draft`, or pass its number)' if 'no pull requests found' in err
               else ' (`gh auth status`; is the PR in this repo?)'))
    number, repo, head = pr['number'], '/'.join(pr['url'].split('/')[3:5]), pr.get('headRefOid')
    changed = []

    def finish(ok, why, step, recorded=None):
        """The result: what changed, why it stopped (if it did) and the next step; exit 1 unless ok."""
        if a.json:
            print(json.dumps({'pr': number, 'url': pr['url'], 'ok': ok, 'changed': changed,
                              'requested': requested(pr) if recorded is None else recorded, 'why': why, 'next': step}))
        else:
            print(f'PR #{number}: ' + '; '.join(changed + ([why] if why else [])) if changed or why else
                  f'PR #{number}: nothing to do')
            print(next_line(step))
        sys.exit(0 if ok else 1)

    if pr['state'] != 'OPEN':
        die(f'PR #{number} is {pr["state"].lower()}: only an open PR can be readied')
    rollup = pr.get('statusCheckRollup')
    failing, running = not_green_checks(rollup), pending_checks(rollup)
    if failing or running or not rollup:
        finish(False, f'not readied: its checks are not green: {", ".join(failing)}' if failing else
               f'not readied: its checks are still running: {", ".join(running)}' if running else
               'not readied: no checks have reported yet',
               f'fix them and push, then `magehand pr ready {number}` again' if failing else
               f'`gh pr checks {number} --watch`, then `magehand pr ready {number}` again')
    own = (pr.get('author') or {}).get('login', '').lower() == BOT.lower()  # OpenClaw's own PR: the owner reviews it
    # The request first, on the draft (OpenClaw ignores drafts), confirmed; only then mark it ready, so a request
    # that didn't stick never leaves a ready PR without its reviewer.
    if not own and not requested(pr):
        # a refusal (HTTP 422 for a bot that isn't a collaborator) is diagnosed from the read-back like a silent one
        code, _, sent = gh_cli('api', '-X', 'POST', f'repos/{repo}/pulls/{number}/requested_reviewers',
                               '-f', f'reviewers[]={BOT}')
        back, err = gh_query('pr', 'view', str(number), '-R', repo, '--json', PR_FIELDS)
        if err:
            finish(False, f'requested {BOT}, but could not read the PR back to confirm it ({err})' if code == 0 else
                   f'could not request {BOT}: {sent or "gh failed"}', f'`magehand pr ready {number}` again',
                   recorded=None)
        pr = back
        if not requested(pr):  # the silent failure: say why, from the bot's access to the repo
            access, unread = bot_access(repo)
            mine = repo.split('/', 1)[0].lower() == OWNER.lower()
            fix = (f'ask the owner to run `magehand new {repo.split("/", 1)[1]}` (it repairs the repo)' if mine else
                   f"ask {repo.split('/', 1)[0]} to add {BOT} with write")
            why, step = {
                'missing': (f'{BOT} is not a collaborator on {repo}', fix),
                'read': (f'{BOT} can only read {repo}', fix),
                'invited': (f'{BOT} has not accepted its invitation to {repo} yet', 'wait about 5 minutes'),
                'write': ('GitHub did not record the request, though the bot can write', 'request it on the PR page')
                if code == 0 else (f'GitHub refused it, though the bot can write ({sent or "gh failed"})',
                                   'wait a minute'),
                None: (f"could not check {BOT}'s access to {repo} ({unread or 'gh failed'})",
                       f"ask the owner to check {BOT} has write on {repo} (`magehand doctor` in its checkout)")}[access]
            finish(False, f'review request NOT recorded: {why}; the PR stays as it was',
                   f'{step}, then `magehand pr ready {number}` again', recorded=False)
        changed.append(f'requested {BOT} (confirmed)')
    if pr.get('headRefOid') != head:  # a push since the checks were read: those checks are for another commit
        finish(False, 'a new commit arrived: its checks have not run', f'`gh pr checks {number} --watch`, then '
               f'`magehand pr ready {number}` again')
    if pr.get('isDraft'):
        code, _, err = gh_cli('pr', 'ready', str(number), '-R', repo)
        if code != 0:
            finish(False, f'could not mark it ready: {err or "gh failed"}', f'`magehand pr ready {number}` again')
        changed.append('marked ready for review')
    if own:
        finish(True, None if changed else "already ready; OpenClaw's own PR, so no review is requested",
               'the owner reviews and merges it (OpenClaw does not review its own PRs)')
    finish(True, None if changed else 'already ready, and its review requested',
           "wait for OpenClaw's review (it starts within about 5 minutes); the owner merges")

def cmd_job(a):
    app = job_app(a.app_flag)
    base = pathlib.Path(app['root']) / DEPLOY / 'base'
    if a.job_action == 'add':
        job_add(a, app, base)
    elif a.job_action == 'rm':
        job_rm(a, app, base)
    else:
        job_ls(a, app)


# The root help groups commands by task, most used first (each line from the
# command's short help).
GROUPS = (('start an app', ('new',)),
          ('learn the platform and check an app', ('guide', 'app', 'status', 'check')),
          ('ship an app', ('pr', 'release', 'job')),
          ('run an app locally', ('run', 'dev', 'bucket', 'runtime', 'login')),
          ('this machine', ('setup', 'doctor', 'upgrade')))
# Working, but not what a person needs to learn: -V and -h do version's and help's jobs, setup and upgrade
# install the skill (OpenClaw's provisioning runs `skill --install`), and check says when to update-agents.
PLUMBING = ('update-agents', 'skill', 'version', 'help')
# Commands whose output is the result itself (data, or your own command's output): no `next:` line. Every
# other command ends its human output with next_line() and puts "next" in its --json.
NO_NEXT = ('guide', 'run', 'bucket', 'runtime', 'login', 'skill', 'version', 'help')
SHORT = {}


class RootParser(Parser):
    def format_help(self):
        lines = [self.format_usage().rstrip(), '', self.description, '']
        for title, names in GROUPS:
            lines.append(f'{title}:')
            lines += [f'  {name:14} {SHORT[name]}' for name in names]
            lines.append('')
        lines += [f'also: {", ".join(PLUMBING)} (rarely needed: `magehand help COMMAND`)', '', 'options:', '  -h, --help     show this help and exit', '  -V, --version  print the version and exit',
                  '', self.epilog, '']
        return '\n'.join(lines)


SUBPARSERS = {}
ROOT = []


def usage(command, message):
    """A usage error found inside a command: reported like argparse's (stderr, exit 2)."""
    SUBPARSERS[command].error(message)


def build_parser():
    root = RootParser(prog='magehand', formatter_class=argparse.RawDescriptionHelpFormatter,
                  description="build apps on the homelab: learn the platform, check an app, run it locally",
                  epilog='Run `magehand COMMAND --help` for its options and examples. '
                         'Docs: https://github.com/davidlarrimore/magehand')
    root.add_argument('-V', '--version', action='store_true', help='print the version and exit')
    sub = root.add_subparsers(dest='command', metavar='COMMAND', title='commands', parser_class=Parser)

    def command(name, func, short, text, epilog=''):
        SHORT[name] = short
        p = sub.add_parser(name, help=short, description=text, epilog=epilog,
                           formatter_class=argparse.RawDescriptionHelpFormatter)
        p.set_defaults(func=func)
        SUBPARSERS[name] = p
        return p

    def app_option(p, positional=True):
        if positional:
            p.add_argument('app', nargs='?', metavar='APP', help='the app (default: the app repo you are in)')
        p.add_argument('-a', '--app', dest='app_flag', metavar='APP',
                       help='the app (default: the app repo you are in)')

    def json_option(p):
        p.add_argument('--json', action='store_true', help='print JSON (for scripts and agents)')

    def runtime_option(p):
        p.add_argument('--runtime', dest='runtime_flag', choices=RUNTIMES + ('none',),
                       help='container runtime for this run (else MAGEHAND_RUNTIME, then `magehand runtime`)')

    # start an app
    p = command('new', cmd_new, "create an app's repo for the platform, or repair one",
                "create an app's private repo from homelab-app-template, invite OpenClaw's bot (write) and add\n"
                "the approval ruleset 'main: owner approves'; on an existing repo, add whichever is missing",
                'Uses gh as you (only the owner can create the repo).\n\n'
                'Repair: run it again on a repo made without it (`magehand doctor` says when one is). It\n'
                'invites a missing bot, gives a read-only one write, renews a pending invitation and adds a\n'
                'missing ruleset. It never removes anything, edits an existing ruleset or changes visibility\n'
                '(a public repo gets a warning and the command), leaves an archived repo (a retired app)\n'
                'alone, clones nothing when run in the repo\'s checkout, and lists only what is left for you.\n\n'
                'In the repo\'s checkout (the clone it makes, or the one you run it in) it writes the files a\n'
                'deployable app needs that are missing: deploy/ (app.yaml, base/, dev/), ui-check.yaml and, when\n'
                'there is no Dockerfile yet, a small Python app with /healthz, its tests and a Dockerfile with a\n'
                'test stage. It never overwrites a file, and it refreshes AGENTS.md\'s platform rules (what\n'
                '`update-agents` does). Commit them and open a PR: merging it registers the app and creates its\n'
                'blocks before any feature code. OpenClaw accepts the invitation by itself.\n\n'
                'The app is named after the repo unless --app-name says otherwise (the repo lab-website\n'
                'for the app lab: lab-dev.lab.davidlarrimore.com). It clones the repo into ./REPO (--dir\n'
                'DIR, or --no-clone), then lists what is left for you and what the platform does by itself.\n\n'
                'examples:\n  magehand new elvis\n  magehand new lab-website --app-name lab\n'
                '  magehand new elvis                 # in elvis\'s checkout: repair it, clone nothing\n'
                '  magehand new elvis --no-clone --json')
    p.add_argument('name', metavar='REPO', help="the repo's name (also the app's, unless --app-name)")
    p.add_argument('--no-clone', action='store_true', help="don't clone the new repo into ./REPO")
    p.add_argument('--dir', metavar='DIR', help='clone into DIR (default: ./REPO)')
    p.add_argument('--app-name', metavar='APP', dest='app_name',
                   help="name the app APP when the repo isn't named after it (hostnames, namespace and image "
                        'use APP); written to deploy/app.yaml as app:')
    json_option(p)
    # learn the platform and check an app
    p = command('guide', cmd_guide, 'read the platform guide: search it, recipes, a topic', 'read the platform guide for apps (live from homelab-apps docs/platform)',
                'examples:\n'
                '  magehand guide                       the overview and its topics\n'
                '  magehand guide search web search     the sections that answer a question\n'
                '  magehand guide recipes               steps for common changes\n'
                '  magehand guide blocks "Web search"   one section of a topic\n'
                '  magehand guide catalog               block types and models')
    p.add_argument('topic', nargs='?', metavar='TOPIC', help='a topic, `search` or `catalog`')
    p.add_argument('rest', nargs='*', metavar='WORDS', help='a section of the topic, or what to search for')
    p = command('app', cmd_app, "an app's blocks, env vars and URLs", "show an app's blocks, the env vars its pod gets (and from where), its URLs",
                'examples:\n  magehand app                   in an app repo\n  magehand app scavenger-hunt\n'
                '  magehand app --json')
    app_option(p)
    json_option(p)
    p = command('status', cmd_status, 'where the app is: set up, registered, built, on dev and prod',
                "show where an app is: its repo's setup, deploy/ on main, registration, then its latest main "
                "commit's build, dev deploy and prod release",
                'One line per stage, each ok or saying what blocks it, then `next:`. First the lifecycle, from GitHub:\n'
                "repo (private, OpenClaw's bot can write, the approval ruleset), config (deploy/ on main) and\n"
                "register (the platform registered it). Then main's build, dev deploy and prod, and, when kubectl has\n"
                'a context, the dev pods and Argo application (read-only `kubectl get`; --no-cluster skips it).\n'
                '--watch waits until the dev deploy (and, when readable, the pods) is done and nothing in the\n'
                'lifecycle is pending (an invitation, config, registration). Exit 1 on a failed stage, a repo\n'
                'that is not set up, or a timeout.\n\n'
                'examples:\n  magehand status\n  magehand status elvis --json\n'
                '  magehand status --watch            wait for the merge to reach dev')
    app_option(p)
    p.add_argument('--watch', action='store_true', help='wait for build and the dev deploy to finish')
    p.add_argument('--timeout', type=int, default=20, metavar='MIN', help='with --watch: give up after MIN (default: 20)')
    p.add_argument('--no-cluster', action='store_true', dest='no_cluster',
                   help="don't read the dev pods with kubectl (GitHub only)")
    p.add_argument('--interval', type=int, default=15, metavar='SEC', help=argparse.SUPPRESS)
    json_option(p)
    p = command('release', cmd_release, "cut an app's production release (the next vX.Y.Z)", "create an app's GitHub release: the next semver version, release notes, and the checks that it will deploy",
                "Picks the next version from what merged to main since the last release (Conventional Commit\n"
                "types, `!` or BREAKING CHANGE, blocks and models added or removed in deploy/app.yaml), drafts\n"
                "grouped notes and, when every check passes, creates the tag and release as you through gh,\n"
                "without asking (--confirm asks). Refuses unless main's checks and security scan are green, its\n"
                "image built and scanned, dev runs the commit, the app is approved for production and deploy/prod/\n"
                "exists. The platform deploys the release; follow it with `status`.\n\n"
                'examples:\n  magehand release --dry-run           the version, notes and checks only\n'
                '  magehand release                     create it when every check passes\n'
                '  magehand release --minor --confirm   force the bump, ask first')
    app_option(p)
    bump = p.add_mutually_exclusive_group()
    bump.add_argument('--major', action='store_true', help='release the next major version whatever the changes')
    bump.add_argument('--minor', action='store_true', help='release the next minor version whatever the changes')
    bump.add_argument('--patch', action='store_true', help='release the next patch version whatever the changes')
    p.add_argument('--dry-run', action='store_true', dest='dry_run', help='print the version, notes and checks; create nothing')
    p.add_argument('--confirm', action='store_true', help='ask before creating (default: a release that passes every check is created)')
    p.add_argument('--yes', action='store_true', help=argparse.SUPPRESS)  # released form: now the default
    p.add_argument('--no-cluster', action='store_true', dest='no_cluster',
                   help="don't read the dev pods with kubectl (GitHub only)")
    json_option(p)
    p = command('pr', cmd_pr, "mark a green PR ready and request OpenClaw's review",
                "mark a PR ready for review once its checks are green, request OpenClaw's review, and confirm it",
                'The step of the review routine that can fail silently: `gh pr edit --add-reviewer` exits 0 even\n'
                'when GitHub records nobody. `pr ready` refuses while checks are running or failed (say which),\n'
                f'marks a draft ready, requests {BOT} and reads the PR back: if the request did not stick it\n'
                'says why (the bot is not a collaborator, can only read, or has not accepted yet) and exits 1.\n'
                "On OpenClaw's own PRs it requests nobody: the owner reviews those. Open the PR with\n"
                '`gh pr create --draft`; after a push to a reviewed PR, run it again.\n\n'
                'examples:\n  magehand pr ready          # the PR of this branch\n  magehand pr ready 12\n'
                '  magehand pr ready 12 --json')
    psub = p.add_subparsers(dest='pr_action', metavar='ACTION', required=True, parser_class=Parser)
    q = psub.add_parser('ready', help='ready a green PR and request the review, confirmed')
    q.add_argument('number', nargs='?', type=int, metavar='N', help='the PR (default: the PR of this branch)')
    q.add_argument('--json', action='store_true', help='print a JSON object (for scripts and agents)')
    p = command('job', cmd_job, 'add, remove and list scheduled jobs (CronJobs)', "add, remove or list the app's scheduled jobs: CronJobs in its deploy/base that run its image with another command",
                'A job is the app\'s own image run on a schedule with another command and the Deployment\'s env (its\n'
                'blocks, keys and settings), in a fresh pod that exits when the command does. `add` writes\n'
                'deploy/base/job-NAME.yaml with the settings the platform needs (no overlapping runs, a start\n'
                'deadline, the timeout, one retry, three runs kept) and lists it in the base\'s kustomization;\n'
                '`rm` removes both. Commit and merge: the platform deploys it to dev with the rest of deploy/.\n'
                'Failed and missed runs alert the owner by themselves. `ls` reads dev\'s CronJobs and runs with\n'
                'read-only `kubectl get` (--no-cluster skips it) and prints the command for the last run\'s logs.\n'
                'Make the command safe to run twice, exit non-zero on failure and write nothing then.\n\n'
                'examples:\n'
                '  magehand job add collect --schedule "15 6 * * *" -- python -m jobs.collect\n'
                '  magehand job add cleanup --schedule @hourly --timeout 2m --memory 128Mi -- python -m jobs.cleanup\n'
                '  magehand job add report --schedule "0 8 * * 1" --timezone America/New_York -- python -m jobs.report\n'
                '  magehand job ls\n'
                '  magehand job rm cleanup')
    p.add_argument('-a', '--app', dest='app_flag', metavar='APP',
                   help='the app of this checkout (kept for scripts; another app is refused)')
    jsub = p.add_subparsers(dest='job_action', metavar='ACTION', required=True, parser_class=Parser)
    q = jsub.add_parser('add', help='write a job (a CronJob) into deploy/base',
                        usage='magehand job add NAME --schedule CRON [--timezone ZONE] [--timeout DURATION] '
                              '[--memory SIZE] [--json] -- COMMAND...')
    q.add_argument('name', metavar='NAME', help='the job: lowercase letters, digits and dashes')
    q.add_argument('--schedule', required=True, metavar='CRON',
                   help='five cron fields ("15 6 * * *") or @hourly, @daily, ...; in --timezone')
    q.add_argument('--timezone', default=JOB_TIMEZONE, metavar='ZONE',
                   help='an IANA time zone for the schedule (default: %(default)s); avoid 01:00-03:00 where '
                        'clocks change')
    q.add_argument('--timeout', default=JOB_TIMEOUT, metavar='DURATION',
                   help='stop a run after this long: 90s, 10m, 2h (default: %(default)s)')
    q.add_argument('--memory', default=JOB_MEMORY, metavar='SIZE',
                   help=f'the memory limit, at most {JOB_MAX_MEMORY_MI}Mi (default: %(default)s)')
    q.add_argument('--json', action='store_true', help='print a JSON object (for scripts and agents)')
    q.set_defaults(cmd=[])  # the words after `--`: main() takes them off before parsing
    q = jsub.add_parser('rm', help='remove a job that `job add` wrote')
    q.add_argument('name', metavar='NAME')
    q.add_argument('--json', action='store_true', help='print a JSON object')
    q = jsub.add_parser('ls', help="the app's jobs and their runs in dev")
    q.add_argument('--no-cluster', action='store_true', dest='no_cluster',
                   help="don't read dev with kubectl (this checkout's files only)")
    q.add_argument('--json', action='store_true', help='print JSON')
    p = command('check', cmd_check, "check an app against the platform's rules (before every push)", "check an app's code and deployment against the platform's rules",
                'Run it before every push; CI runs the same check. Each finding names the guide section\n'
                'that explains it. Exit status 1 when there are errors; fix warnings or explain them in the PR.\n'
                "deploy/ is rendered with kubectl and run through the cluster's admission policies (kyverno) and\n"
                'schemas (kubeconform); a rule whose tool is missing here is skipped with a note (--strict: an error).\n\n'
                'examples:\n  magehand check                          in an app repo\n'
                '  magehand check --json\n'
                '  magehand check --ui                     CI\'s browser check, here (about a minute)\n'
                '  magehand check -a demo --code apps/demo/image --apps-dir .   in a homelab-apps checkout')
    app_option(p)
    p.add_argument('--code', default='.', metavar='DIR', help="the app's code (default: .)")
    p.add_argument('--apps-dir', metavar='DIR', help='read this homelab-apps checkout instead of GitHub (CI)')
    p.add_argument('--manifests-only', action='store_true', help='check the deployment only, not the code')
    p.add_argument('--strict', action='store_true',
                   help="a rule that can't run here (kubectl, kyverno or kubeconform missing) is an error (CI)")
    p.add_argument('--ui', action='store_true',
                   help="run CI's browser check here instead (builds the image; needs a container runtime)")
    json_option(p)
    p = command('update-agents', cmd_agents, "update this repo's AGENTS.md from the template", "update the platform rules in this repo's AGENTS.md from homelab-app-template",
                'Replaces everything above the "## This app" heading with the template\'s current text and keeps\n'
                "the heading and everything below it (the app's own notes). Writes only AGENTS.md; commit it yourself.\n\n"
                'examples:\n  magehand update-agents\n  magehand update-agents --check          exit 1 if out of date, writes nothing')
    p.add_argument('--code', default='.', metavar='DIR', help="the app's repo (default: .)")
    p.add_argument('--check', action='store_true', help='only report whether it is out of date (exit 1 if so)')
    json_option(p)
    # run it locally
    p = command('login', cmd_login, 'sign in to OpenBao with your passkey', 'sign in to OpenBao through authentik with your passkey (1h, up to 8h)',
                'The session lasts about an hour; commands that need it show the time left and, in a terminal,\n'
                'offer to sign in again when it has expired. An agent cannot tap the passkey: check first.\n\n'
                'examples:\n  magehand login\n  magehand login --check            exit 1 unless signed in, 10+ min left\n'
                '  magehand login --check --min 30 --json')
    p.add_argument('--check', action='store_true', help='only report the time left (exit 1 when signed out or low)')
    p.add_argument('--min', type=int, default=SIGNIN_WARN // 60, metavar='N',
                   help='with --check: minutes that must remain (default: %(default)s)')
    json_option(p)
    p = command('dev', cmd_dev, 'local postgres/redis/bucket blocks: up, down, status', "start, stop or show local containers for the app's postgres/redis/bucket blocks",
                'Also makes local values for secret blocks. llm keys need nothing here: they come from\n'
                'OpenBao when the app runs.\n\n'
                'A bucket block runs the platform\'s own Garage (one container, its data in a volume that\n'
                'survives a restart and goes only with `down`), with one bucket capped at 1 GB and a key limited\n'
                'to it. A public bucket\'s local `url` is the S3 path: it needs the key (no anonymous reads).\n\n'
                'examples:\n  magehand dev up\n  magehand dev            (status)\n'
                '  magehand dev down       (deletes the local data)')
    p.add_argument('action', nargs='?', default='status', choices=('up', 'down', 'status'),
                   help='up, down or status (default)')
    app_option(p)
    runtime_option(p)
    p = command('bucket', cmd_bucket, "list, copy, sync and link an app's object storage", "read and write the app's `bucket` blocks with the keys the platform hands the app",
                'Works only on the app whose repo you are in (--app may name it, never another). BUCKET is one of its\n'
                'bucket blocks\n'
                '(a service of type bucket in app.yaml). --env picks where:\n'
                'dev (default) and prod read the keys from OpenBao (`magehand login`); local uses the Garage\n'
                '`magehand dev up` started. An internal bucket exists only in the cluster, so use --env local.\n'
                'Prod writes are yours to decide: --env prod uploads to real data.\n\n'
                'examples:\n'
                '  magehand bucket ls files                 top level of the bucket\n'
                '  magehand bucket ls files/reports/        under a prefix (-r for everything)\n'
                '  magehand bucket cp latest.json files/data/latest.json\n'
                '  magehand bucket cp files/data/latest.json ./latest.json\n'
                '  magehand bucket sync ./site files/site   upload what changed (no deletes)\n'
                '  magehand bucket url files/data/latest.json   the public URL (access: public)\n'
                '  magehand bucket usage                    objects and bytes of each bucket against the cap\n'
                '  magehand bucket cp ./files/x files/x     a local path that starts with a bucket name: ./\n'
                '  magehand bucket --env local ls files     against the local store')
    p.add_argument('-a', '--app', dest='app_flag', metavar='APP',
                   help="the app of this checkout (kept for scripts; another app is refused)")
    p.add_argument('--env', choices=BUCKET_ENVS, help='local, dev (default) or prod')
    bsub = p.add_subparsers(dest='bucket_action', metavar='ACTION', required=True, parser_class=Parser)
    q = bsub.add_parser('ls', help='list objects')
    q.add_argument('target', metavar='BUCKET[/PREFIX]')
    q.add_argument('-r', '--recursive', action='store_true', help='everything under the prefix, not one level')
    q.add_argument('--json', action='store_true', help='print JSON')
    q = bsub.add_parser('cp', help='copy one file up or down')
    q.add_argument('source', metavar='SOURCE', help='a file, or BUCKET/KEY to download')
    q.add_argument('dest', metavar='DEST', help='BUCKET/KEY to upload to (a trailing / keeps the file name), or a file')
    q.add_argument('--content-type', help='Content-Type (default: guessed from the name)')
    q.add_argument('--cache-control', help='Cache-Control to store with the object')
    q.add_argument('--json', action='store_true', help='print a JSON object (for scripts and agents)')
    q = bsub.add_parser('sync', help='upload the files that changed (never deletes)')
    q.add_argument('source', metavar='DIR')
    q.add_argument('dest', metavar='BUCKET[/PREFIX]')
    q.add_argument('--dry-run', action='store_true', help='say what would be uploaded')
    q.add_argument('--cache-control', help='Cache-Control to store with each object')
    q.add_argument('--json', action='store_true', help='print a JSON object (progress goes to stderr)')
    q = bsub.add_parser('usage', help='objects and bytes against the 1 GB cap')
    q.add_argument('target', nargs='?', metavar='BUCKET', help='one bucket block (default: all of the app\'s)')
    q.add_argument('--json', action='store_true', help='print JSON')
    q = bsub.add_parser('url', help='the public URL of an object')
    q.add_argument('target', metavar='BUCKET/KEY')
    q.add_argument('--json', action='store_true', help='print a JSON object')
    p = command('run', cmd_run, "run a command or the app's image with its pod's env", "run a command, or the app's image, with the env the app's pod gets",
                'Secrets come from OpenBao and local containers, never from disk. A proxy on 127.0.0.1:8080\n'
                'adds the X-authentik-* headers Traefik would, as you.\n\n'
                'Keys are read from OpenBao once, when it starts (the time left on your sign-in is shown), so\n'
                'a run keeps working after the session expires; sign in again before starting a new one.\n\n'
                'examples:\n  magehand run -- python -B app.py\n  magehand run --no-proxy -- npm run dev\n'
                '  magehand run --container        build the Dockerfile and run the image as its pod runs')
    app_option(p, positional=False)
    p.add_argument('--no-proxy', action='store_true', help='no identity proxy; use the app port directly')
    p.add_argument('--container', action='store_true',
                   help="build the repo's Dockerfile and run the image as its pod runs")
    p.add_argument('--no-build', action='store_true', help='with --container: reuse the last build')
    runtime_option(p)
    p.add_argument('cmd', nargs=argparse.REMAINDER, metavar='-- CMD', help='the command to run')
    p = command('runtime', cmd_runtime, 'show or set the container runtime', 'show or set the container runtime for local blocks and run --container',
                f'Supported: {SUPPORTED}. Detection runs `<cli> info` as you (no sudo).\n'
                'Precedence: --runtime > MAGEHAND_RUNTIME > this setting > automatic.\n\n'
                'examples:\n  magehand runtime           what this machine has, and which one is used\n'
                '  magehand runtime docker    always use docker (or podman)\n'
                '  magehand runtime none      no containers\n'
                '  magehand runtime auto      the first running one (the default)')
    p.add_argument('runtime', nargs='?', choices=RUNTIMES + ('none', 'auto'), metavar='RUNTIME',
                   help=f'{", ".join(RUNTIMES)}, none or auto; omit it to show')
    json_option(p)
    # this machine
    p = command('setup', cmd_setup, 'set this machine up (once)', 'set this machine up once: GitHub, container runtime, *.lab names, '
                'sign-in, the skill, pre-approved commands',
                'Step 6 lets Claude Code and Codex run read-only magehand and gh commands without asking, and\n'
                'refuses merges, releases and tag pushes: one list, written as a Claude Code plugin\n'
                f'($CLAUDE_CONFIG_DIR or ~/.claude, skills/{APPROVALS_PLUGIN}/) and a Codex rules file ($CODEX_HOME\n'
                f'or ~/.codex, rules/{CODEX_RULES}). `magehand upgrade` refreshes them; --undo removes them.\n\n'
                'examples:\n  magehand setup\n  magehand setup --dns 192.0.2.1\n  magehand setup --undo')
    p.add_argument('--dns', metavar='ADDRESS', help="the homelab's DNS address, if *.lab doesn't resolve "
                   '(else it asks, in a terminal)')
    p.add_argument('--undo', action='store_true', help='remove the *.lab name resolution and the pre-approved commands '
                   'setup added')
    p.add_argument('--refresh', action='store_true', help=argparse.SUPPRESS)  # upgrade's quiet mode
    p = command('doctor', cmd_doctor, "check this machine, sign-in and this app repo's setup",
                "check GitHub access, homelab reach, sign-in, the app repo's setup and the container runtime",
                'In an app repo it also checks that OpenClaw\'s bot can write to it (a pending invitation is\n'
                "a note), that it is private, and that the ruleset 'main: owner approves' is active on the\n"
                'default branch. Each failure names its fix (`magehand new REPO` repairs the repo). What\n'
                'needs admin to read (the ruleset, invitations) is a note when unreadable, not a failure;\n'
                'an archived repo is not checked. Run it at the start of a session. Exit 1 on a failure.\n\n'
                'examples:\n  magehand doctor\n  magehand doctor --json')
    json_option(p)
    p = command('skill', cmd_skill, 'print or install the homelab-app skill for coding agents', 'print the homelab-app skill for coding agents, or install it')
    p.add_argument('--install', action='store_true', help='install it for Claude Code (and Codex, if installed)')
    command('upgrade', cmd_upgrade, 'upgrade magehand, its skill and approvals',
            'upgrade magehand the way it was installed, then refresh what setup installed (the skill, the '
            'pre-approved commands)')
    p = command('version', cmd_version, 'print the version', 'print the version, and whether a newer one exists')
    json_option(p)
    p = command('help', cmd_help, 'help for magehand or a command', 'show help for magehand or a command')
    p.add_argument('topic', nargs='?', metavar='COMMAND')
    ROOT[:] = [root]
    return root


def cmd_help(a):
    if a.topic and a.topic not in SUBPARSERS:
        close = difflib.get_close_matches(a.topic, list(SUBPARSERS), n=1)
        usage('help', f"no command '{a.topic}'" + (f"; did you mean '{close[0]}'?" if close else ''))
    (SUBPARSERS[a.topic] if a.topic else ROOT[0]).print_help()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:2] == ['runtime', 'use']:  # the 0.4.0 form keeps working
        del argv[1]
    command = None
    if argv[:1] == ['job'] and '--' in argv:  # `job add NAME --schedule ... -- COMMAND`: argparse can't take a
        cut = argv.index('--')                 # positional, then options, then everything after --
        argv, command = argv[:cut], argv[cut + 1:]
    parser = build_parser()
    a = parser.parse_args(argv)
    if command is not None:
        a.cmd = command
    if a.version:
        a.command, a.func = 'version', cmd_version
    if not a.command:
        parser.print_help()
        return
    if getattr(a, 'runtime_flag', None):  # flag > MAGEHAND_RUNTIME > config file
        os.environ['MAGEHAND_RUNTIME'] = a.runtime_flag
    if a.command not in ('upgrade', 'version', 'help'):
        update_notice()
    refuse_if_managed(a.command)
    try:
        a.func(a)
    except KeyboardInterrupt:
        sys.exit(130)
    except PinnedApp as pinned:  # a homelab-apps checkout (--apps-dir) has no manifests for a pinned app
        die(f"{pinned.name} is pinned to {pinned.pin.get('repo') or pinned.name} at {str(pinned.pin['ref'])[:7]}: "
            'its deployment is in that repo; run this in its checkout, or without --apps-dir')


if __name__ == '__main__':
    main()
