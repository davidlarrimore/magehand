"""magehand: build homelab apps from anywhere, the same way everywhere.

The developer CLI for apps on the homelab, used by the owner (Claude Code on
the MacBook) and by OpenClaw's coding workers. `magehand --help` lists the
commands; `magehand COMMAND --help` explains each, with examples.

Private by design: it talks only to GitHub and the private *.lab network,
never to a public homelab endpoint. Everyday commands assume they can
connect; `magehand setup` checks that once per machine and, if needed, points
this Mac's *.lab lookups at the homelab's DNS. Its OpenBao role `dev` can
only read the LiteLLM keys of agent apps (homelab
platform/openbao/policies/dev-read.hcl).
"""
import argparse
import base64
import difflib
import http.client
import http.server
import json
import os
import pathlib
import re
import secrets
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


def github_file(repo, path, required=True):
    """A file on main, cached for offline use; None if it doesn't exist."""
    if repo == APPS_REPO and APPS_DIR:
        local = pathlib.Path(APPS_DIR) / path
        if local.is_file():
            return local.read_text()
        if required:
            die(f'{local} not found')
        return None
    cached = CACHE / repo / path
    req = urllib.request.Request(
        f'https://api.github.com/repos/{repo}/contents/{urllib.parse.quote(path)}?ref=main',
        headers={'Authorization': f'Bearer {github_token()}', 'Accept': 'application/vnd.github.raw',
                 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'magehand'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310  # fixed https URL
            text = resp.read().decode()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            if required:
                die(f'{repo}: {path} not found on main')
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


def github_dir(repo, path):
    """Names in a directory on main ([] if it doesn't exist)."""
    if repo == APPS_REPO and APPS_DIR:
        local = pathlib.Path(APPS_DIR) / path
        return sorted(child.name for child in local.iterdir()) if local.is_dir() else []
    req = urllib.request.Request(
        f'https://api.github.com/repos/{repo}/contents/{urllib.parse.quote(path)}?ref=main',
        headers={'Authorization': f'Bearer {github_token()}', 'Accept': 'application/vnd.github+json',
                 'User-Agent': 'magehand'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310  # fixed https URL
            return sorted(item['name'] for item in json.load(resp) if isinstance(item, dict))
    except (urllib.error.HTTPError, OSError, ValueError):
        return []


# --- The app ------------------------------------------------------------------

def current_app():
    """The app this checkout is: an app repo is named after its app."""
    out = subprocess.run(['git', 'remote', 'get-url', 'origin'], capture_output=True, text=True)  # nosec B603 B607
    if out.returncode != 0:
        die('not in a git checkout; pass the app name')
    name = out.stdout.strip().rstrip('/').rsplit('/', 1)[-1].rsplit(':', 1)[-1]
    name = name[:-4] if name.endswith('.git') else name
    if name in ('homelab', 'homelab-apps'):
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
    out = subprocess.run(['git', 'rev-parse', '--show-toplevel'], capture_output=True, text=True)  # nosec B603 B607
    remote = subprocess.run(['git', 'remote', 'get-url', 'origin'], capture_output=True, text=True)  # nosec B603 B607
    if out.returncode != 0 or remote.returncode != 0:
        return None
    repo = remote.stdout.strip().rstrip('/').rsplit('/', 1)[-1].rsplit(':', 1)[-1]
    return pathlib.Path(out.stdout.strip()) if repo in (name, name + '.git') else None


def load_app(name, root=None):
    """The app's block requests and pod spec: from `root`/deploy/ when that checkout has one, else from
    homelab-apps apps/<name>/. `where` says which."""
    local = deploy_dir(root) if root else None
    if local:
        where = DEPLOY
        spec = yaml.safe_load((local / 'app.yaml').read_text()) or {}
        files = [(f'{DEPLOY}/base/{f.name}', f.read_text()) for f in sorted((local / 'base').glob('*.y*ml'))]
    else:
        where = f'homelab-apps apps/{name}'
        files = [(f'{where}/base/deployment.yaml', github_file(APPS_REPO, f'apps/{name}/base/deployment.yaml'))]
        spec = yaml.safe_load(github_file(APPS_REPO, f'apps/{name}/app.yaml')) or {}
    found = [(path, d) for path, text in files for d in yaml.safe_load_all(text)
             if isinstance(d, dict) and d.get('kind') == 'Deployment']
    if not found:
        die(f'{where}/base has no Deployment')
    path, deployment = found[0]
    pod = deployment['spec']['template']['spec']
    return {'name': name, 'spec': spec, 'services': spec.get('services') or {}, 'container': pod['containers'][0],
            'pod': pod, 'where': where, 'deployment_file': path, 'root': local.parent if local else None}


def app_here(name=None):
    """load_app for `name` (default: this checkout's app), from this checkout's deploy/ when it is that app's repo."""
    name = name or current_app()
    return load_app(name, repo_root(name))


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

def bao(method, path, token=None, body=None):
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
        die(f'cannot reach {OPENBAO}: {NOT_SET_UP}')


def store_token(token, identity):
    if sys.platform == 'darwin':
        subprocess.run(['security', 'add-generic-password', '-U', '-s', KEYCHAIN_SERVICE,  # nosec B603 B607
                        '-a', 'openbao-token', '-w', token], check=True, capture_output=True)
    else:
        STATE.mkdir(parents=True, exist_ok=True)
        path = STATE / 'openbao-token'
        path.touch(mode=0o600)
        path.write_text(token)
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / 'identity.json').write_text(json.dumps(identity))


def saved_token():
    if os.environ.get('MAGEHAND_BAO_TOKEN'):
        return os.environ['MAGEHAND_BAO_TOKEN']
    if sys.platform == 'darwin':
        out = subprocess.run(['security', 'find-generic-password', '-s', KEYCHAIN_SERVICE,  # nosec B603 B607
                              '-a', 'openbao-token', '-w'], capture_output=True, text=True)
        return out.stdout.strip() if out.returncode == 0 else None
    path = STATE / 'openbao-token'
    return path.read_text().strip() if path.is_file() else None


def valid_token():
    token = saved_token()
    if token and bao('GET', 'auth/token/lookup-self', token)[0] == 200:
        return token
    die('not signed in, or the token expired: run `magehand login`')


def identity():
    path = STATE / 'identity.json'
    return json.loads(path.read_text()) if path.is_file() else {}


def cmd_login(_args):
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
        print('Sign in through authentik in your browser (passkey)...')
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
    hours = auth.get('lease_duration', 3600) // 3600
    print(f'Signed in as {email or "?"} (policy {", ".join(auth.get("policies", []))}, {hours}h).')


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
    source = (app['spec'].get('source') or {}).get('repo') or (name if app['root'] else None)
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
                          'port': app_port(app), 'blocks': app['services'], 'env': env}, indent=1))
        return
    deployment = ('deploy/ in this repo (merging copies it to homelab-apps apps/%s)' % name if app['root']
                  else app['where'])
    print(f'{name}  (code: {"davidlarrimore/" + source if source else "homelab-apps apps/" + name + "/image"};'
          f' deployment: {deployment})')
    print(f'  dev:      https://{name}-dev.{LAB}')
    for preview in github_dir(APPS_REPO, f'previews/{name}'):
        if preview.endswith('.yaml'):
            print(f'  preview:  https://{name}-dev-{preview[:-5]}.{LAB}')
    print('Blocks (app.yaml services:):')
    if not app['services']:
        print('  none; request one in ' + ('deploy/app.yaml' if app['root'] else 'homelab-apps apps/%s/app.yaml' % name)
              + ' (`magehand guide blocks`)')
    for block, svc in app['services'].items():
        opts = ', '.join(f'{k}={v}' for k, v in (svc or {}).items() if k != 'type')
        print(f'  {block}: {(svc or {}).get("type")}{"  (" + opts + ")" if opts else ""}')
    print(f'Env (the pod spec; locally: magehand run, app port {app_port(app)}):')
    for entry in env_plan(app):
        if entry[1] == 'value':
            print(f'  {entry[0]} = {entry[2]}')
        elif entry[1] == 'unknown':
            print(f'  {entry[0]} <- Secret {entry[2]} (not a block; magehand run leaves it unset)')
        else:
            print(f'  {entry[0]} <- block {entry[1]} ({entry[2]}) key {entry[3]}')


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
                if status != 200:
                    die(f'cannot read {path} (HTTP {status}); has the broker created the key? (`magehand app`)')
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
    token = saved_token()
    check('signed in to OpenBao', bool(reach and token and bao('GET', 'auth/token/lookup-self', token)[0] == 200),
          '`magehand login`')
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
LAB_URL = re.compile(r'https?://[a-z0-9.-]+\.lab\.davidlarrimore\.com')


def finding(level, where, text, see):
    return {'level': level, 'where': where, 'text': text, 'see': see}


def block_spec(catalog, kind):
    """{'keys': [...], 'key_options': {key: option}} for a block type: catalog.yaml's, else built in."""
    spec = ((catalog or {}).get('blocks') or {}).get(kind) or {}
    return {'keys': spec.get('keys') or BLOCK_KEYS.get(kind, {}).get('keys') or [],
            'key_options': spec.get('key_options') or BLOCK_KEYS.get(kind, {}).get('key_options') or {}}


def check_manifests(app, catalog):
    """Rules for homelab-apps apps/<app>/: the blocks requested and the pod's env."""
    found = []
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
    if where == DEPLOY and not re.fullmatch(own + r'(@sha256:[0-9a-f]{64})?', image):
        found.append(finding('error', deployment_file, f'image {image or "(none)"}: write the app\'s own image '
                             f'without a digest (ghcr.io/davidlarrimore/homelab-apps/{app["name"]}); merging pins '
                             'the build of that commit', 'README "How an app is put together"'))
    elif where != DEPLOY and not re.fullmatch(own + r'(:[\w.-]+)?@sha256:[0-9a-f]{64}', image):
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
        found.append(finding('error', f'{DEPLOY}/base', 'missing: the Kustomize base (Deployment, Service, ...) goes here',
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


def provided_secrets(name, root=None):
    """Secrets the app's own manifests make (ExternalSecrets from OpenBao, plain Secrets): in base/ (the
    checkout's deploy/base/ when `root` has one, else homelab-apps') and in homelab-apps dev/."""
    local = deploy_dir(root) if root else None
    texts = [f.read_text() for f in sorted((local / 'base').glob('*.y*ml'))] if local else []
    for part in ('dev',) if local else ('base', 'dev'):
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
    if not (root and deploy_dir(root)) and github_file(APPS_REPO, f'apps/{name}/app.yaml', required=False) is None:
        return None
    app = load_app(name, root)
    app['provided'] = provided_secrets(name, root)
    return app


def cmd_check(a):
    global APPS_DIR
    manifests_only = a.manifests_only
    if a.apps_dir:
        APPS_DIR = a.apps_dir
    name = a.app_flag or a.app or current_app()
    catalog = yaml.safe_load(github_file(APPS_REPO, 'catalog.yaml', required=False) or '') or {}
    root = pathlib.Path(a.code)
    # The deployment is the checkout's deploy/ when it has one (else homelab-apps'), with or without the code rules.
    app = try_load_app(name, root)
    found = (check_manifests(app, catalog) if app else []) + check_deploy_files(root, name)
    if not manifests_only:
        found += check_code(root, app, catalog)
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
   `magehand run -- <command>` (or `magehand run --container`).
5. If the guide doesn't answer, say so in the PR or issue (what you searched
   for) instead of inventing a mechanism; the owner adds it to the guide.

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
    token = saved_token()
    if token and bao('GET', 'auth/token/lookup-self', token)[0] == 200:
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
            choices = re.findall(r"'([^']*)'", match.group(3))
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
GROUPS = (('learn the platform and check an app', ('guide', 'app', 'check')),
          ('run an app locally', ('run', 'dev', 'runtime', 'login')),
          ('this machine', ('setup', 'doctor', 'upgrade', 'skill', 'version', 'help')))
SHORT = {}


class RootParser(Parser):
    def format_help(self):
        lines = [self.format_usage().rstrip(), '', self.description, '']
        for title, names in GROUPS:
            lines.append(f'{title}:')
            lines += [f'  {name:9} {SHORT[name]}' for name in names]
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
    # run it locally
    command('login', cmd_login, 'sign in to OpenBao with your passkey', 'sign in to OpenBao through authentik with your passkey (1h, up to 8h)')
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


if __name__ == '__main__':
    main()
