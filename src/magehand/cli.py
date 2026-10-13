"""magehand: build homelab apps from anywhere, the same way everywhere.

The developer CLI for apps on the homelab, used by the owner (Claude Code on
the MacBook) and by OpenClaw's coding workers. `magehand --help` lists the
commands; `magehand COMMAND --help` explains each, with examples.

Its writes are `magehand new` (an app's repo on GitHub) and `magehand release` (a tag and release on an
app's repo), both as the owner.

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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
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
RESOLVER = pathlib.Path('/etc/resolver') / LAB
SECRET_NAME = {'postgres': '{}-connection', 'redis': '{}-connection', 'llm': '{}-connection', 'secret': '{}'}
# A homelab-apps checkout to read instead of GitHub (CI has one; `magehand check --apps-dir`).
APPS_DIR = os.environ.get('MAGEHAND_APPS_DIR')


def die(msg):
    sys.exit(f'magehand: {msg}')


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
            'pin': pinned}


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
    except OSError:
        if soft:  # an optional lookup: the caller reports it as unknown
            return 0, None
        die(f'cannot reach {OPENBAO}: {NOT_SET_UP}')


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
    left = signin_left(soft=True)
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
                          'signin_seconds_left': left if left != UNREACHABLE else None}, indent=1))
        return
    deployment = ('deploy/ in this repo (merging copies it to homelab-apps apps/%s)' % name if app['root']
                  else app['where'])
    print(f'{name}  (code: {"davidlarrimore/" + source if source else "homelab-apps apps/" + name + "/image"};'
          f' deployment: {deployment})')
    print(f'  dev:      https://{name}-dev.{LAB}')
    if left == UNREACHABLE:
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
    print(f'Env (the pod spec; locally: magehand run, app port {app_port(app)}):')
    for entry in env_plan(app):
        if entry[1] == 'value':
            print(f'  {entry[0]} = {entry[2]}')
        elif entry[1] == 'unknown':
            print(f'  {entry[0]} <- Secret {entry[2]} (not a block; magehand run leaves it unset)')
        else:
            print(f'  {entry[0]} <- block {entry[1]} ({entry[2]}) key {entry[3]}')


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
    """Names of the failed entries of a PR's statusCheckRollup: CheckRuns (conclusion, name) and
    StatusContexts (state, context) both appear in it."""
    names = []
    for c in rollup or []:
        if c.get('conclusion') in ('FAILURE', 'CANCELLED', 'TIMED_OUT', 'STARTUP_FAILURE') \
                or c.get('state') in ('FAILURE', 'ERROR'):
            names.append(c.get('name') or c.get('context') or '?')
    return names


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


def prod_stage(tag, ahead, prs):
    """Prod: the latest eligible release, whether its deploy PR merged, and the commits on main since."""
    if not tag:
        return {'stage': 'prod', 'state': 'unknown',
                'text': "no release yet (the owner's GitHub Release vX.Y.Z deploys to prod)"}
    since = f'  ({ahead} commit{"" if ahead == 1 else "s"} on main since {tag})' if ahead else ''
    mine = [p for p in prs or [] if (PROD_TITLE.search(p.get('title') or '') or [None, None])[1] == tag]
    if mine and mine[0].get('state') == 'MERGED':
        return {'stage': 'prod', 'state': 'ok', 'text': f'{tag} deployed{since}', 'tag': tag, 'ahead': ahead}
    if mine and mine[0].get('state') == 'OPEN':
        return {'stage': 'prod', 'state': 'pending', 'text': f'{tag}: homelab-apps#{mine[0]["number"]} is open{since}',
                'tag': tag, 'ahead': ahead}
    return {'stage': 'prod', 'state': 'unknown', 'text': f'{tag} released; its deploy PR was not found{since}',
            'tag': tag, 'ahead': ahead}


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
    live = [p for p in (pods or {}).get('items') or []
            if not (p.get('metadata') or {}).get('deletionTimestamp')
            and (p.get('status') or {}).get('phase') not in ('Succeeded', 'Failed')]
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


def dev_stage(name, deploy, cluster=True):
    """The running pods for a merged dev deploy, or an unknown stage saying why they weren't read."""
    unknown = lambda why: {'stage': 'dev', 'state': 'unknown',  # noqa: E731
                           'text': f'config deployed ({deploy["digest"]}); the running pod is not checked ({why})'}
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
    stages = [build, deploy, prod]
    if deploy['state'] == 'ok':
        stages.insert(2, dev_stage(name, deploy, cluster))
    return {'app': name, 'repo': repo, 'commit': head, 'subject': subject, 'stages': stages}


def render_status(report):
    lines = [f'{report["app"]}  ({report["repo"]} main {report["commit"][:7]}: {report["subject"]})']
    for st in report['stages']:
        lines.append(f'  {st["stage"]:<7}{STATUS_MARK[st["state"]]} {st["text"]}')
    return '\n'.join(lines)


def code_repo(app):
    """OWNER/NAME of the app's code repo, or exits: an app built in homelab-apps has no main of its own."""
    spec = app['spec'].get('source') or {}
    source = spec.get('repo') or (remote_repo(app['root']) if app['root'] else None)
    if not source:
        die(f'{app["name"]} has no code repo (its image is built in homelab-apps), so there is no main to follow')
    return f'{OWNER}/{source}'


def cmd_status(a):
    app = app_here(a.app_flag or a.app)
    repo = code_repo(app)
    deadline = time.monotonic() + a.timeout * 60
    last = None
    while True:
        report = collect_status(repo, app['name'], cluster=not a.no_cluster)
        # build and deploy always count; dev's pods only when they could be read (an unknown one doesn't)
        states = [st['state'] for st in report['stages'] if st['stage'] in ('build', 'deploy', 'dev')]
        failed = 'failed' in states or any(st['state'] == 'error' for st in report['stages'])
        done = all(st == 'ok' for st in states if st != 'unknown') and len(
            [st for st in report['stages'] if st['stage'] in ('build', 'deploy') and st['state'] == 'ok']) == 2
        if not a.watch or failed or done:
            break
        if time.monotonic() > deadline:
            report['timeout'] = True
            break
        text = render_status(report)
        if text != last:
            print(text, file=sys.stderr)
            last = text
        time.sleep(a.interval)
    report['ok'] = done and not failed  # build and the dev deploy both finished for the newest commit
    if a.json:
        print(json.dumps(report, indent=1))
    else:
        print(render_status(report))
        if report.get('timeout'):
            print(f'magehand: still not deployed to dev after {a.timeout} min; run it again to keep waiting',
                  file=sys.stderr)
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
MAX_PR_LOOKUPS = 100  # PRs read per release (labels and reviews): past it the release is refused
FAILED_CHECK = ('failure', 'cancelled', 'timed_out', 'startup_failure', 'action_required')


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


def review_verdict(number, head, reviews, reviewers):
    """None when PR `number` was approved at its final push `head` by one of `reviewers` and nobody's latest
    word is a change request, else why not. `reviews` is GitHub's list, oldest first."""
    latest = {}
    for r in reviews or []:
        if r.get('state') in ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'):
            latest[(r.get('user') or {}).get('login')] = r
    asked = sorted(u for u, r in latest.items() if r['state'] == 'CHANGES_REQUESTED')
    if asked:
        return f'PR #{number} has changes requested by {", ".join(asked)}'
    if not any(r['state'] == 'APPROVED' and u in reviewers and r.get('commit_id') == head
               for u, r in latest.items()):
        return (f'PR #{number} has no approving review of its final push from {" or ".join(sorted(reviewers))} '
                '(approve it on GitHub: a review of a merged PR counts)')
    return None


def pr_review(repo, number):
    """{'labels': [...], 'problem': None | why} for a merged PR: its labels, and whether its final push was
    approved. A read error is a problem, never an assumption."""
    pr, err = gh_query('api', f'repos/{repo}/pulls/{number}')
    if err:
        return {'labels': [], 'problem': f'could not read PR #{number}: {err}'}
    reviews, err = gh_pages(f'repos/{repo}/pulls/{number}/reviews')
    if err:
        return {'labels': [], 'problem': f'could not read the reviews of PR #{number}: {err}'}
    labels = [l.get('name') for l in pr.get('labels') or []]
    head = (pr.get('head') or {}).get('sha')
    return {'labels': labels, 'problem': review_verdict(number, head, reviews, {OWNER, BOT})}


def merged_prs(repo, sha):
    """([PR numbers], None): the PRs merged into main that GitHub itself says brought in `sha` (never the
    number in a commit message, which anyone can write), or (None, why)."""
    found, err = gh_query('api', f'repos/{repo}/commits/{sha}/pulls')
    if err:
        return None, err
    return [p['number'] for p in found or []
            if p.get('merged_at') and (p.get('base') or {}).get('ref') == 'main'], None


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
    prs, covered, direct, limit_hit = {}, [], [], False
    for c in commits:  # newest first
        sha = c.get('sha') or ''
        message = (c.get('commit') or {}).get('message') or ''
        subject, _, body = message.partition('\n')
        numbers, err = merged_prs(repo, sha)
        labels = []
        if err:
            problems.append(f'could not tell which PR brought in {sha[:7]}: {err}')
        elif not numbers:
            direct.append((sha, subject))
        for n in numbers or []:
            if n not in prs and len(prs) >= MAX_PR_LOOKUPS:
                limit_hit = True
                continue
            prs.setdefault(n, None)
            if prs[n] is None:
                prs[n] = pr_review(repo, n)
                if prs[n]['problem']:
                    problems.append(prs[n]['problem'])
            labels += prs[n]['labels']
        covered.append(bool(numbers))
        changes.append(classify_change(subject, body, labels))
    if limit_hit:
        problems.append(f'more than {MAX_PR_LOOKUPS} PRs since {previous or "the start"}: release more often, '
                        'so every one can be checked for its review')
    if previous is None and any(covered):
        # the repo's own bootstrap (the template copy, magehand new's deploy/app.yaml) predates any PR
        oldest = max(i for i, hit in enumerate(covered) if hit)
        shas_before = {commits[i].get('sha') for i in range(oldest + 1, len(commits))}
        bootstrap = [d for d in direct if d[0] in shas_before]
        direct = [d for d in direct if d[0] not in shas_before]
        if bootstrap:
            warnings.append(f'{len(bootstrap)} commit{"" if len(bootstrap) == 1 else "s"} before the first PR '
                            "(the repo's creation) were pushed straight to main: not reviewed")
    for sha, subject in direct[:5]:
        problems.append(f'{sha[:7]} "{subject[:50]}" was pushed straight to main: no merged PR, no review')
    if len(direct) > 5:
        problems.append(f'and {len(direct) - 5} more commits pushed straight to main')
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
    return {'app': name, 'repo': repo, 'commit': head, 'previous': previous, 'version': version, 'bump': bump,
            'why': why, 'commits': ahead, 'changes': [{'kind': k, 'pr': n, 'title': t} for k, n, t in changes],
            'notes': release_notes(repo, previous, version, changes), 'problems': problems, 'warnings': warnings}


def render_release(plan):
    lines = [f'{plan["app"]}  {plan["previous"] or "no release yet"} -> {plan["version"]}'
             f'  ({plan["repo"]} main {plan["commit"][:7]}, {plan["commits"]} commit'
             f'{"" if plan["commits"] == 1 else "s"})', f'  {plan["why"]}']
    lines += [f'  ! {w}' for w in plan['warnings']]
    lines += [f'  ✗ {p}' for p in plan['problems']]
    lines += ['', plan['notes'].rstrip()]
    return '\n'.join(lines)


def cmd_release(a):
    if sum(map(bool, (a.major, a.minor, a.patch))) > 1:
        usage('release', 'choose one of --major, --minor, --patch')
    forced = 'major' if a.major else 'minor' if a.minor else 'patch' if a.patch else None
    app = app_here(a.app_flag or a.app)
    plan = plan_release(code_repo(app), app['name'], forced, cluster=not a.no_cluster)
    plan['created'] = False
    refuse = f'not releasing {plan["version"]}: ' + '; '.join(plan['problems']) if plan['problems'] else None
    if a.dry_run or refuse:
        if a.json:
            print(json.dumps(plan, indent=1))
        else:
            print(render_release(plan))
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
    if a.json:
        print(json.dumps(plan, indent=1))
    else:
        print(f'released {plan["version"]}: {plan["url"]}')
        print(f'The platform deploys it to production within minutes; follow it with `magehand status {plan["app"]}`.',
              file=sys.stderr)


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
    code, out, _ = gh_cli('api', f'repos/{repo}', '--jq', '.private')
    created = code != 0
    if created:
        code, _, err = gh_cli('repo', 'create', repo, '--private', '--template', TEMPLATE_REPO)
        if code != 0:
            die(f'cannot create {repo}: {err or "gh repo create failed"} (only the owner can; `gh auth status`)')
        print(f'{repo}: created (private, from {TEMPLATE_REPO})', file=sys.stderr)
        private = True
    else:
        private = out.strip() == 'true'
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
    if gh_cli('api', f'repos/{repo}/collaborators/{BOT}')[0] == 0:
        bot = 'collaborator'
        print(f'{BOT}: already a collaborator', file=sys.stderr)
    else:
        code, _, err = gh_cli('api', '-X', 'PUT', f'repos/{repo}/collaborators/{BOT}', '-f', 'permission=push')
        if code != 0:
            die(f'cannot invite {BOT} to {repo}: {err or "gh api failed"}')
        bot = 'invited'
        print(f'{BOT}: invited with write; OpenClaw accepts it within about 5 minutes', file=sys.stderr)
    # Before the ruleset: the owner's one commit to main, so deploy/app.yaml names the app from the start.
    named = ensure_app_name(repo, app) if app != name else None
    if named == 'seeded':
        print(f'{repo}: deploy/app.yaml says `app: {app}` (the repo keeps its name)', file=sys.stderr)
    elif named == 'differs':
        print(f'{repo}: deploy/app.yaml names another app; make its first line `app: {app}` or run without '
              '--app-name', file=sys.stderr)
    ruleset = ensure_ruleset(repo)
    unnamed = named in ('failed', 'differs')  # the repo does not say `app: APP`: its app is still the repo's name
    cloned = None if a.no_clone else clone_repo(repo, dest)
    if cloned == 'cloned':
        print(f'{repo}: cloned to {dest}', file=sys.stderr)
    elif cloned == 'exists':
        print(f'{dest}: already exists; not cloned (is it {repo}?)', file=sys.stderr)
    elif cloned == 'failed':
        print(f'{repo}: could not clone it; run `gh repo clone {repo} {shell_path(dest)}`', file=sys.stderr)
    if a.json:
        print(json.dumps({'repo': repo, 'app': app, 'created': created, 'private': private, 'bot': bot,
                          'ruleset': ruleset, 'app_name': named, 'clone': cloned,
                          'dir': str(dest) if cloned in ('cloned', 'exists') else None}, indent=1))
    else:
        print(new_summary(repo, name, app, dest, named, cloned))
    if unnamed:  # everything else is set up; a script must not carry on as if the app were named
        sys.exit(1)


def new_summary(repo, name, app, dest, named, cloned):
    """What `new` leaves for you, in two lists: the steps that are yours (commands first, plainly marked) and
    what the platform does by itself afterwards. Everything already done went to stderr as it happened."""
    live = name if named in ('failed', 'differs') else app
    # run: a command to type, write:/edit: work in the repo, do: a decision or action of yours. A program
    # can't change its parent shell's directory, so `cd` is a step even when it cloned.
    steps = [f'run:   gh repo clone {repo} {shell_path(dest)}'] if cloned not in ('cloned', 'exists') else []
    steps.append(f'run:   cd -- {shell_path(dest)}' if str(dest).startswith('-') else f'run:   cd {shell_path(dest)}')
    if named in ('failed', 'differs'):
        steps.append(f'edit:  make the first line of deploy/app.yaml `app: "{app}"` (the repo still names the app '
                     f'{name})')
    steps += ['write: the code, and deploy/ (app.yaml and base/)   guide: magehand guide search new app',
              'run:   magehand check          # the platform\'s rules, before you push',
              'do:    open a PR and merge it  # the platform deploys what is on main']
    lines = ['You:'] + [f'  {i}. {step}' for i, step in enumerate(steps, 1)]
    lines += ['', 'Then, by itself (about 5 minutes after deploy/ with base/ is merged to main):',
              f'  - {live} is registered and deployed to https://{live}-dev.{LAB}/',
              '  - its blocks and its own LLM key are created (until then, mock the LLM)']
    return '\n'.join(lines)


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


def engine(*args, check=True):
    cli = runtime()
    if not cli:
        die(no_runtime('this'))
    if not shutil.which(cli):
        die(f'container runtime {cli} is configured but not installed: `magehand runtime auto` or install it')
    return subprocess.run([cli, *args], capture_output=True, text=True, check=check)  # nosec B603  # docker/podman


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


def local_state(app_name):
    path = STATE / f'{app_name}.json'
    return json.loads(path.read_text()) if path.is_file() else {}


def save_local_state(app_name, data):
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / f'{app_name}.json'
    path.touch(mode=0o600)
    path.write_text(json.dumps(data, indent=1))


def block_image(kind, svc):
    values = yaml.safe_load(github_file(HOMELAB_REPO, f'platform/building-blocks/{kind}/values.yaml'))
    if kind == 'postgres' and (svc or {}).get('vector') in (True, 'true'):
        return values['pgvector']['image']
    return values['image']


def cmd_dev(a):
    action = a.action
    app = app_here(a.app_flag or a.app)
    state = local_state(app['name'])
    network = f'magehand-{app["name"]}'
    contained = [b for b, svc in app['services'].items() if (svc or {}).get('type') in ('postgres', 'redis')]
    cli = runtime() if contained else None
    if action == 'up' and contained and not cli:
        die(no_runtime(f'the local {", ".join(contained)} block' + ('s' if len(contained) > 1 else '')))
    if action == 'up' and cli:  # blocks and `run --container` share it, like a namespace
        engine('network', 'create', network, check=False)
    for block, svc in app['services'].items():
        kind = (svc or {}).get('type')
        container = f'magehand-{app["name"]}-{block}'
        if kind == 'secret' and action == 'up':
            state.setdefault(block, {'value': secrets.token_urlsafe(36)[:48]})
        if kind not in ('postgres', 'redis'):
            continue
        if not cli:
            print(f'{block} ({kind}): no container runtime')
            continue
        if action == 'down':
            engine('rm', '-f', '-v', container, check=False)
            state.pop(block, None)
            print(f'{block}: removed')
            continue
        running = engine('inspect', '-f', '{{.State.Running}}', container, check=False).stdout.strip() == 'true'
        if action == 'status':
            print(f'{block} ({kind}): {"running" if running else "not running"}')
            continue
        if not running:
            password = state.get(block, {}).get('password') or secrets.token_urlsafe(24)
            engine('rm', '-f', container, check=False)
            if kind == 'postgres':
                engine('run', '-d', '--name', container, '--network', network, '-p', '127.0.0.1::5432',
                       '-e', 'POSTGRES_USER=app',
                       '-e', 'POSTGRES_DB=app', '-e', f'POSTGRES_PASSWORD={password}', block_image(kind, svc))
            else:
                engine('run', '-d', '--name', container, '--network', network, '-p', '127.0.0.1::6379',
                       block_image(kind, svc),
                       'valkey-server', '--requirepass', password, '--maxmemory', '200mb',
                       '--maxmemory-policy', 'allkeys-lru', '--appendonly', 'yes')
            state[block] = {'password': password}
        inner = '5432' if kind == 'postgres' else '6379'
        port = engine('port', container, inner).stdout.strip().splitlines()[0].rsplit(':', 1)[-1]
        state[block]['port'] = port
        print(f'{block} ({kind}): 127.0.0.1:{port}')
    if not contained and action in ('up', 'status'):
        print(f"{app['name']} has no postgres/redis blocks: nothing runs locally "
              '(llm keys come from OpenBao when the app runs)')
    if action == 'up':
        save_local_state(app['name'], state)
    elif action == 'down':
        (STATE / f'{app["name"]}.json').unlink(missing_ok=True)
        if cli:
            engine('network', 'rm', network, check=False)


def local_connection(kind, block, local, app_name=None):
    """A block's Secret keys for a local run: on the host (published port), or
    with app_name, from a container on the app's Docker network."""
    if block not in local:
        die(f'block {block} ({kind}) has no local container: run `magehand dev up`')
    conn = local[block]
    if kind == 'secret':
        return {'value': conn['value']}
    pw = conn['password']
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
                fetched[block] = local_connection(kind, block, local, app['name'] if container else None)
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
    """{'ruleset': 'ok'|'missing'|'weak'|None, 'ruleset_detail': str, 'bot': True|False|None}: the repo's approval
    ruleset and whether OpenClaw's bot can write to it. None means unknown (no gh, no admin, rate limit, a
    server or network error), which is not the same as a confirmed 'missing' or False (HTTP 404)."""
    ruleset, detail = ruleset_status(repo)
    code, _, err = gh_probe('api', f'repos/{repo}/collaborators/{BOT}')
    bot = True if code == 0 else False if code is not None and 'HTTP 404' in err else None
    return {'ruleset': ruleset, 'ruleset_detail': detail, 'bot': bot}


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
    try:
        reach = urllib.request.urlopen(f'{OPENBAO}/v1/sys/health', timeout=5).status == 200  # nosec B310
    except (OSError, urllib.error.HTTPError):
        reach = False
    check(f'reach {OPENBAO}', reach, NOT_SET_UP)
    left = signin_left() if reach else None
    check('signed in to OpenBao' + (f', {minutes(left)} left' if left is not None else ''), left is not None,
          '`magehand login`')
    if left is not None and left < SIGNIN_WARN:
        note = f'sign-in has {minutes(left)} left; `magehand login` renews it before a long task'
        results.append({'check': 'sign-in time left', 'ok': None, 'hint': note})
        if not a.json:
            print(f'--   {note}')
    repo = current_repo()
    if repo:
        state = repo_state(repo)
        fix = f'`magehand new {repo.split("/", 1)[1]}` (re-running it is safe)'
        weak = f'{state["ruleset_detail"]}; fix it in the repo\'s Settings > Rules (magehand new leaves it alone)'
        for label, value, hint in ((f"{repo}: OpenClaw's bot can write", state['bot'], fix),
                                   (f"{repo}: ruleset '{RULESET['name']}'",
                                    None if state['ruleset'] is None else state['ruleset'] == 'ok',
                                    weak if state['ruleset'] == 'weak' else fix)):
            if value is None:  # unknown (no gh, no admin, rate limit, network): a note, never a failure
                note = 'could not be read (needs gh, and admin for the ruleset)'
                results.append({'check': label, 'ok': None, 'hint': note})
                if not a.json:
                    print(f'--   {label}: {note}')
            else:
                check(label, value, hint)
    configured = configured_runtime()
    found = [] if configured else detect_runtimes()
    chosen = choose_runtime(configured, found)
    # Not a failure without one: only postgres/redis blocks and run --container need it.
    if a.json:
        print(json.dumps({'ok': ok, 'checks': results, 'runtime': chosen}, indent=1))
    else:
        print(f'{"ok  " if chosen else "--  "} container runtime: {chosen or "none"}'
              + ('' if chosen else f' (needed only for local postgres/redis blocks and run --container: '
                                   f'{SUPPORTED}; `magehand runtime`)'))
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
}
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
        if option and (app['services'].get(block) or {}).get(option) is not True:
            found.append(finding('error', deployment_file, f'env {var}: key {key} exists only with '
                                 f'`{option}: true` on block {block}; without it the pod waits forever',
                                 'blocks "Web search"' if option == 'search' else 'blocks'))
    for source in container.get('envFrom') or []:
        found.append(finding('warning', deployment_file, f'envFrom {source}: map each variable with '
                             'env + secretKeyRef instead, so `magehand app` and `magehand run` can see it', 'blocks'))
    return found


def check_deploy_files(root, name=None):
    """deploy/ holds app.yaml, base/ (the Kustomize base) and the optional overlays dev/ (namespace <app>-dev)
    and prod/ (namespace <app>, needed for production releases): merging copies exactly those."""
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
    found += check_dotenv(root, app)
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
    current, own = split_agents(path.read_text())
    if own is not None and current == platform:
        return []
    why = ('has no "## This app" heading' if own is None else 'the platform rules above "## This app" are out of date')
    return [finding('warning', 'AGENTS.md', f'{why}: run `magehand update-agents` and commit the diff',
                    'homelab-apps AGENTS.md')]


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


def cmd_check(a):
    global APPS_DIR
    manifests_only = a.manifests_only
    if a.apps_dir:
        APPS_DIR = a.apps_dir
    root = pathlib.Path(a.code)
    name = a.app_flag or a.app or current_app(root)
    catalog = yaml.safe_load(github_file(APPS_REPO, 'catalog.yaml', required=False) or '') or {}
    # The deployment is the checkout's deploy/ when it has one (else homelab-apps'), with or without the code rules.
    try:
        app = try_load_app(name, root)
    except PinnedApp as pinned:
        repo, ref = pinned.pin.get('repo') or name, str(pinned.pin['ref'])
        note = (f'{name} is pinned to {OWNER}/{repo} at {ref[:7]}: its manifests are in that repo and are checked by its '
                'build (`magehand check` there); nothing to check in this homelab-apps checkout')
        if a.json:
            print(json.dumps({'app': name, 'pinned': {'repo': repo, 'ref': ref}, 'errors': 0, 'findings': []}, indent=1))
        else:
            print(f'magehand check {name}: {note}')
        sys.exit(0)
    found = (check_manifests(app, catalog) if app else seeded_name_findings(name, root)) \
        + check_deploy_files(root, name)
    if not manifests_only:
        found += check_code(root, app, catalog) + check_agents_drift(root) + check_merged_branch(root)
    errors = sum(f['level'] == 'error' for f in found)
    if a.json:
        print(json.dumps({'app': name, 'code': None if manifests_only else str(root.resolve()),
                          'deployment': app['where'] if app else None, 'errors': errors,
                          'findings': sorted(found, key=lambda f: (f['level'] != 'error', f['where']))}, indent=1))
        sys.exit(1 if errors else 0)
    print(f'magehand check {name}: code {"(skipped)" if manifests_only else root.resolve()}, deployment '
          + (app['where'] if app else 'none yet (code rules only)'))
    for f in sorted(found, key=lambda f: (f['level'] != 'error', f['where'])):
        see = f['see'] if f['see'].startswith('homelab-apps') else f"magehand guide {f['see']}"
        print(f"{f['level']:7} {f['where']}: {f['text']}\n        -> {see}")
    print(f'{len(found)} finding(s), {errors} error(s)' if found else 'no findings')
    sys.exit(1 if errors else 0)


# --- agents: keep a repo's AGENTS.md current -----------------------------------------

APP_HEADING = '## This app'  # the template's AGENTS.md: platform rules above it, the app's own notes from it down


def split_agents(text):
    """(platform part, app part) of an AGENTS.md, split at its "## This app" line; (text, None) without one."""
    m = re.search(r'^## This app[ \t]*$', text, re.MULTILINE)
    return (text[:m.start()], text[m.start():]) if m else (text, None)


def cmd_agents(a):
    path = pathlib.Path(a.code) / 'AGENTS.md'
    template = github_file(TEMPLATE_REPO, 'AGENTS.md')
    platform, app = split_agents(template)
    if app is None:
        die(f'{TEMPLATE_REPO} AGENTS.md has no "{APP_HEADING}" heading; tell the owner')
    current = path.read_text() if path.is_file() else None
    if current is None:
        new = template
    else:
        _, own = split_agents(current)
        if own is None:
            die(f'{path} has no "{APP_HEADING}" heading, so magehand can\'t tell the platform rules from this app\'s '
                f'notes. Put the heading above the app\'s own notes (see {TEMPLATE_REPO}), then run it again')
        new = platform + own
    changed = new != current
    if a.json:
        print(json.dumps({'path': str(path), 'changed': changed, 'written': changed and not a.check}))
    elif not changed:
        print(f'{path}: up to date', file=sys.stderr)
    elif a.check:
        print(f'{path}: the platform rules are out of date; run `magehand update-agents`', file=sys.stderr)
    if changed and not a.check:
        path.write_text(new)
        if not a.json:
            print(f'{path}: updated from {TEMPLATE_REPO}; review the diff and commit it', file=sys.stderr)
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
   CI runs the same check.
4. Run it as the cluster does: `magehand dev up`, then
   `magehand run -- <command>` (or `magehand run --container`). A new app
   has no LLM key until it is registered and deployed (`magehand app` says
   so): mock the LLM until then. Never use another app's key.
5. If the guide doesn't answer, say so in the PR or issue (what you searched
   for) instead of inventing a mechanism; the owner adds it to the guide.
6. Opening a PR: open it as a draft (`gh pr create --draft`) and request the
   reviewer at once, `gh pr edit N --add-reviewer davidlarrimore-bot`. CI runs
   on drafts and OpenClaw ignores them: push until `magehand check` and CI are
   green, then `gh pr ready N`, which starts the review (each review costs a
   model run). A push starts no review and dismisses the approval: to change a
   reviewed PR, `gh pr ready N --undo`, push until green, `gh pr ready N` and
   request the reviewer again. Don't call the PR ready to merge until its
   review has posted; the owner merges. Base every PR on `main` (the repo's
   ruleset only guards the default branch).
7. When `magehand check` warns that AGENTS.md is behind the template, run
   `magehand update-agents`, review the diff and commit it. Never edit
   AGENTS.md above its `## This app` heading: that edit is lost.

Facts that override habits: no direct calls to AI or search providers (the
llm block's base_url and search_url); no own login or passwords (authentik
adds X-authentik-* headers); the root filesystem is read-only (state goes in
a block); a block key is never `optional: true`; merging code deploys dev by
itself, `deploy/` included (never pin images by hand); the code calls only
models its llm block requests.
"""


def skill_dirs():
    """Where coding agents on this machine read skills: Claude Code always, Codex if it is installed."""
    dirs = [pathlib.Path(os.environ.get('CLAUDE_CONFIG_DIR') or HOME / '.claude') / 'skills']
    codex = pathlib.Path(os.environ.get('CODEX_HOME') or HOME / '.codex')
    if codex.is_dir():
        dirs.append(codex / 'skills')
    return dirs


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
        return
    print('1/5 GitHub')
    try:
        github_token()
        print('    ok')
    except SystemExit:
        die('sign in to GitHub first: `gh auth login`, then run `magehand setup` again')
    print('2/5 container runtime (only for local postgres/redis blocks and run --container)')
    configured = configured_runtime()
    found = detect_runtimes()
    for line in runtime_lines(found, configured, choose_runtime(configured, found)):
        print('  ' + line)
    print(f'    Supported: {SUPPORTED}. Change: magehand runtime docker|podman|none')
    print(f'3/5 the homelab network (*.{LAB})')
    if reachable():
        print(f'    ok: {host} -> {lab_address(host)}')
    elif sys.platform != 'darwin':
        die(f'{host} is not reachable and only macOS is set up automatically')
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
    print('4/5 sign in')
    if signin_left() is not None:
        print('    ok (already signed in)')
    else:
        cmd_login(None)
    print('5/5 the homelab-app skill for coding agents (Claude Code, Codex)')
    for path in install_skill():
        print(f'    ok: {path}')
    print('Done. Try `magehand app <name>` or, in an app repo, `magehand run -- <command>`.')


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
        if time.time() - cached['checked'] < max_age:
            return cached['version']
    except (OSError, ValueError, KeyError, TypeError):
        pass
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'magehand'}
    token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
    if token:
        headers['Authorization'] = f'Bearer {token}'
    req = urllib.request.Request(f'https://api.github.com/repos/{MAGEHAND_REPO}/tags?per_page=100', headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:  # nosec B310  # fixed https URL
            latest = newest_tag([t.get('name', '') for t in json.load(resp) if isinstance(t, dict)])
    except (OSError, ValueError):
        return None
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


def update_notice():
    """One line on stderr when a newer magehand exists; quiet in CI, scripts and the managed copy."""
    if os.environ.get('CI') or os.environ.get('MAGEHAND_NO_UPDATE_CHECK') or not sys.stderr.isatty() \
            or install_method() == 'managed':
        return
    current, latest = installed_version(), latest_version()
    if current and latest and version_tuple(latest) > (version_tuple(current) or ()):
        print(f'magehand {latest} is available (you have {current}): magehand upgrade', file=sys.stderr)


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
        print('upgraded; run `magehand skill --install` if you use the skill')
        return
    # Its skill text, only where the user installed the skill (magehand setup).
    if any((base / SKILL_NAME / 'SKILL.md').is_file() for base in skill_dirs()):
        if subprocess.run([str(exe), 'skill', '--install'], stdout=subprocess.DEVNULL).returncode == 0:  # nosec B603
            print('refreshed the homelab-app skill')
    out = subprocess.run([str(exe), 'version'], capture_output=True, text=True)  # nosec B603
    print(f'now: magehand {out.stdout.splitlines()[0] if out.returncode == 0 and out.stdout else "?"}')


def cmd_version(_a):
    current = installed_version()
    print(current or 'unknown (not installed as a package)')
    if install_method() == 'managed':  # no network call: provisioning runs this on every sync
        print('pinned by the platform (homelab platform/openclaw/vm/versions.env)')
        return
    latest = latest_version()
    if current and latest and version_tuple(latest) > (version_tuple(current) or ()):
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


# The root help groups commands by task, most used first (each line from the
# command's short help).
GROUPS = (('start an app', ('new',)),
          ('learn the platform and check an app', ('guide', 'app', 'status', 'check', 'update-agents')),
          ('ship an app', ('release',)),
          ('run an app locally', ('run', 'dev', 'runtime', 'login')),
          ('this machine', ('setup', 'doctor', 'upgrade', 'skill', 'version', 'help')))
SHORT = {}


class RootParser(Parser):
    def format_help(self):
        lines = [self.format_usage().rstrip(), '', self.description, '']
        for title, names in GROUPS:
            lines.append(f'{title}:')
            lines += [f'  {name:14} {SHORT[name]}' for name in names]
            lines.append('')
        lines += ['options:', '  -h, --help     show this help and exit', '  -V, --version  print the version and exit',
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
    p = command('new', cmd_new, "create an app's repo (private, from the template) for OpenClaw too",
                "create an app's private repo from homelab-app-template and invite OpenClaw's bot (write)",
                'Uses gh as you (only the owner can create the repo). Safe to re-run: on an existing repo it\n'
                'only invites the bot if needed, and warns if the repo is public. OpenClaw accepts the\n'
                'invitation by itself; the platform registers the app once deploy/ is on main.\n'
                'The app is named after the repo unless --app-name says otherwise (the repo lab-website\n'
                'for the app lab: lab-dev.lab.davidlarrimore.com). It clones the repo into ./REPO (--dir\n'
                'DIR, or --no-clone), then lists what is left for you and what the platform does by itself.\n\n'
                'examples:\n  magehand new elvis\n  magehand new lab-website --app-name lab\n'
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
    p = command('status', cmd_status, 'where a merge to main has got to: build, dev, prod', "show an app's build, dev deploy and prod release for its latest main commit",
                'Reads GitHub through gh and, when kubectl has a context, the dev pods and Argo application (read-only\n'
                '`kubectl get`; --no-cluster skips it). The dev line says whether the pods run the image the deploy\n'
                'PR pinned; without kubectl it only says the deploy merged.\n'
                '--watch waits until build, the dev deploy and (when readable) the pods are done; exit 1 on a\n'
                'failed stage or a timeout.\n\n'
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
                "types, `!` or BREAKING CHANGE, PR labels `breaking` and `enhancement`, blocks and models added or\n"
                "removed in deploy/app.yaml), drafts grouped notes and, when every check passes, creates the tag and\n"
                "release as you through gh, without asking (--confirm asks). Refuses unless main's checks and\n"
                "security scan are green, its image built and scanned, dev runs the commit, every merged PR was\n"
                "approved at its final push, the app is approved for production and deploy/prod/ exists. The\n"
                "platform deploys the release; follow it with `status`.\n\n"
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
    p = command('check', cmd_check, "check an app against the platform's rules (before every push)", "check an app's code and deployment against the platform's rules",
                'Run it before every push; CI runs the same check. Each finding names the guide section\n'
                'that explains it. Exit status 1 when there are errors; fix warnings or explain them in the PR.\n\n'
                'examples:\n  magehand check                          in an app repo\n'
                '  magehand check --json\n'
                '  magehand check -a demo --code apps/demo/image --apps-dir .   in a homelab-apps checkout')
    app_option(p)
    p.add_argument('--code', default='.', metavar='DIR', help="the app's code (default: .)")
    p.add_argument('--apps-dir', metavar='DIR', help='read this homelab-apps checkout instead of GitHub (CI)')
    p.add_argument('--manifests-only', action='store_true', help='check the deployment only, not the code')
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
    p = command('dev', cmd_dev, 'local postgres/redis blocks: up, down, status', "start, stop or show local containers for the app's postgres/redis blocks",
                'Also makes local values for secret blocks. llm keys need nothing here: they come from\n'
                'OpenBao when the app runs.\n\n'
                'examples:\n  magehand dev up\n  magehand dev            (status)\n'
                '  magehand dev down       (deletes the local data)')
    p.add_argument('action', nargs='?', default='status', choices=('up', 'down', 'status'),
                   help='up, down or status (default)')
    app_option(p)
    runtime_option(p)
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
                'sign-in, the skill')
    p.add_argument('--dns', metavar='ADDRESS', help="the homelab's DNS address, if *.lab doesn't resolve "
                   '(else it asks, in a terminal)')
    p.add_argument('--undo', action='store_true', help='remove the *.lab name resolution setup added')
    p = command('doctor', cmd_doctor, 'check access, sign-in and the container runtime', 'check GitHub access, homelab reach, sign-in and the container runtime')
    json_option(p)
    p = command('skill', cmd_skill, 'print or install the homelab-app skill for coding agents', 'print the homelab-app skill for coding agents, or install it')
    p.add_argument('--install', action='store_true', help='install it for Claude Code (and Codex, if installed)')
    command('upgrade', cmd_upgrade, 'upgrade magehand and its skill', 'upgrade magehand the way it was installed, and its skill')
    command('version', cmd_version, 'print the version', 'print the version, and whether a newer one exists')
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
    parser = build_parser()
    a = parser.parse_args(argv)
    if a.version:
        a.command, a.func = 'version', cmd_version
    if not a.command:
        parser.print_help()
        return
    if getattr(a, 'runtime_flag', None):  # flag > MAGEHAND_RUNTIME > config file
        os.environ['MAGEHAND_RUNTIME'] = a.runtime_flag
    if a.command not in ('upgrade', 'version', 'help'):
        update_notice()
    try:
        a.func(a)
    except KeyboardInterrupt:
        sys.exit(130)
    except PinnedApp as pinned:  # a homelab-apps checkout (--apps-dir) has no manifests for a pinned app
        die(f"{pinned.name} is pinned to {pinned.pin.get('repo') or pinned.name} at {str(pinned.pin['ref'])[:7]}: "
            'its deployment is in that repo; run this in its checkout, or without --apps-dir')


if __name__ == '__main__':
    main()
