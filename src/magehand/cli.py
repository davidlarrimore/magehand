"""magehand: build homelab apps from anywhere, the same way everywhere.

The developer CLI for apps on the homelab, used by the owner (Claude Code on
the MacBook) and by OpenClaw's coding workers:

  magehand guide [topic]        the platform guide for apps (live from homelab-apps)
  magehand app [name]           this app's blocks, env vars and URLs
  magehand login                sign in to OpenBao through authentik (passkey)
  magehand dev up|down|status   local containers for the app's postgres/redis blocks
  magehand run [--no-proxy] -- CMD...
                                run CMD with the env the app's pod gets; secrets
                                come from OpenBao and local containers, never disk
  magehand run --container [--no-build] [-- CMD...]
                                build the repo's Dockerfile and run the image as
                                its pod runs (read-only, non-root, no capabilities)
  magehand setup [--undo]       once per machine: GitHub, Docker, *.lab names, sign-in
  magehand doctor               check GitHub access, homelab reach, login, Docker
  magehand version              this version (upgrade: uv tool upgrade magehand)

Private by design: it talks only to GitHub and the private *.lab network,
never to a public homelab endpoint. Everyday commands assume they can
connect; `magehand setup` checks that once per machine and, if needed, points
this Mac's *.lab lookups at the homelab's DNS. Its OpenBao role `dev` can
only read the LiteLLM keys of agent apps (homelab
platform/openbao/policies/dev-read.hcl).
"""
import base64
import http.client
import http.server
import json
import os
import pathlib
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


def load_app(name):
    spec = yaml.safe_load(github_file(APPS_REPO, f'apps/{name}/app.yaml')) or {}
    services = spec.get('services') or {}
    docs = list(yaml.safe_load_all(github_file(APPS_REPO, f'apps/{name}/base/deployment.yaml')))
    deployment = next((d for d in docs if isinstance(d, dict) and d.get('kind') == 'Deployment'), None)
    if not deployment:
        die(f'apps/{name}/base/deployment.yaml has no Deployment')
    pod = deployment['spec']['template']['spec']
    return {'name': name, 'spec': spec, 'services': services, 'container': pod['containers'][0], 'pod': pod}


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

def cmd_guide(args):
    topics = [n[:-3] for n in github_dir(APPS_REPO, 'docs/platform') if n.endswith('.md')]
    if not topics:  # before docs/platform exists
        print(github_file(APPS_REPO, 'AGENTS.md'))
        return
    topic = args[0] if args else 'README'
    if topic == 'catalog':
        print(github_file(APPS_REPO, 'catalog.yaml'))
        return
    if topic not in topics:
        die(f'no topic {topic!r}; topics: {", ".join(topics)}, catalog')
    print(github_file(APPS_REPO, f'docs/platform/{topic}.md'))
    if not args:
        print(f'\nMore: magehand guide <topic>  ({", ".join(t for t in topics if t != "README")}, catalog)')


def cmd_app(args):
    app = load_app(args[0] if args else current_app())
    name = app['name']
    source = (app['spec'].get('source') or {}).get('repo')
    print(f'{name}  (code: {"davidlarrimore/" + source if source else "homelab-apps apps/" + name + "/image"};'
          f' deployment: homelab-apps apps/{name})')
    print(f'  dev:      https://{name}-dev.{LAB}')
    for preview in github_dir(APPS_REPO, f'previews/{name}'):
        if preview.endswith('.yaml'):
            print(f'  preview:  https://{name}-dev-{preview[:-5]}.{LAB}')
    print('Blocks (app.yaml services:):')
    if not app['services']:
        print('  none; request one with a PR to homelab-apps apps/%s/app.yaml (`magehand guide blocks`)' % name)
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


# --- local containers -------------------------------------------------------------

def docker(*args, check=True):
    if not shutil.which('docker'):
        die('docker is required for local blocks (Docker Desktop, OrbStack or colima)')
    return subprocess.run(['docker', *args], capture_output=True, text=True, check=check)  # nosec B603 B607


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


def cmd_dev(args):
    action = args[0] if args else 'status'
    app = load_app(args[1] if len(args) > 1 else current_app())
    state = local_state(app['name'])
    network = f'magehand-{app["name"]}'
    if action == 'up':  # blocks and `run --container` share it, like a namespace
        docker('network', 'create', network, check=False)
    for block, svc in app['services'].items():
        kind = (svc or {}).get('type')
        container = f'magehand-{app["name"]}-{block}'
        if kind == 'secret' and action == 'up':
            state.setdefault(block, {'value': secrets.token_urlsafe(36)[:48]})
        if kind not in ('postgres', 'redis'):
            continue
        if action == 'down':
            docker('rm', '-f', '-v', container, check=False)
            state.pop(block, None)
            print(f'{block}: removed')
            continue
        running = docker('inspect', '-f', '{{.State.Running}}', container, check=False).stdout.strip() == 'true'
        if action == 'status':
            print(f'{block} ({kind}): {"running" if running else "not running"}')
            continue
        if not running:
            password = state.get(block, {}).get('password') or secrets.token_urlsafe(24)
            docker('rm', '-f', container, check=False)
            if kind == 'postgres':
                docker('run', '-d', '--name', container, '--network', network, '-p', '127.0.0.1::5432',
                       '-e', 'POSTGRES_USER=app',
                       '-e', 'POSTGRES_DB=app', '-e', f'POSTGRES_PASSWORD={password}', block_image(kind, svc))
            else:
                docker('run', '-d', '--name', container, '--network', network, '-p', '127.0.0.1::6379',
                       block_image(kind, svc),
                       'valkey-server', '--requirepass', password, '--maxmemory', '200mb',
                       '--maxmemory-policy', 'allkeys-lru', '--appendonly', 'yes')
            state[block] = {'password': password}
        inner = '5432' if kind == 'postgres' else '6379'
        port = docker('port', container, inner).stdout.strip().splitlines()[0].rsplit(':', 1)[-1]
        state[block]['port'] = port
        print(f'{block} ({kind}): 127.0.0.1:{port}')
    if action == 'up':
        save_local_state(app['name'], state)
    elif action == 'down':
        (STATE / f'{app["name"]}.json').unlink(missing_ok=True)
        docker('network', 'rm', network, check=False)
    elif action != 'status':
        die('usage: magehand dev up|down|status [app]')


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


def cmd_run(args):
    proxy = '--no-proxy' not in args
    container = '--container' in args
    build = '--no-build' not in args
    args = [a for a in args if a not in ('--no-proxy', '--container', '--no-build')]
    name = None
    if args and args[0] == '--app':
        name, args = args[1], args[2:]
    if args and args[0] == '--':
        args = args[1:]
    if not args and not container:
        die('usage: magehand run [--no-proxy] [--app NAME] -- CMD...   or   magehand run --container [--no-build]')
    app = load_app(name or current_app())
    env = resolve_env(app, container=container)
    print(f'magehand: {app["name"]} env: {", ".join(sorted(env))}', file=sys.stderr)
    if proxy:
        port = int(env.get('PORT') or app_port(app))
        start_proxy(port, identity())
        print(f'magehand: open http://127.0.0.1:{PROXY_PORT} (signed in as '
              f'{identity().get("email") or "local"}; the app itself listens on {port})', file=sys.stderr)
    if container:
        image = f'magehand/{app["name"]}:local'
        if build:
            print(f'magehand: docker build -t {image} .', file=sys.stderr)
            if subprocess.run(['docker', 'build', '-t', image, '.']).returncode:  # nosec B603 B607
                die('docker build failed')
        docker('network', 'create', f'magehand-{app["name"]}', check=False)
        docker('rm', '-f', f'magehand-{app["name"]}-app', check=False)
        port = int(env.get('PORT') or app_port(app))
        args = ['docker', *container_args(app, env, image, port, lab_hosts(env), args)]
    child = subprocess.Popen(args, env={**os.environ, **env})  # nosec B603  # the user's own command
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda s, _f: child.send_signal(s))
    sys.exit(child.wait())


# --- doctor and update ---------------------------------------------------------------

def cmd_doctor(_args):
    ok = True

    def check(label, passed, hint=''):
        nonlocal ok
        ok = ok and passed
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
    check('docker (for postgres/redis blocks)', bool(shutil.which('docker')), 'install Docker Desktop or OrbStack')
    sys.exit(0 if ok else 1)


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


def cmd_setup(args):
    host = urllib.parse.urlparse(OPENBAO).hostname
    if '--undo' in args:
        if RESOLVER.exists():
            subprocess.run(['sudo', 'rm', '-f', str(RESOLVER)], check=True)  # nosec B603 B607
            print(f'removed {RESOLVER}')
        return
    print('1/4 GitHub')
    try:
        github_token()
        print('    ok')
    except SystemExit:
        die('sign in to GitHub first: `gh auth login`, then run `magehand setup` again')
    print('2/4 Docker (only for apps with database blocks)')
    print('    ok' if shutil.which('docker') else '    not found: install Docker Desktop or OrbStack when you need it')
    print(f'3/4 the homelab network (*.{LAB})')
    if reachable():
        print(f'    ok: {host} -> {lab_address(host)}')
    elif sys.platform != 'darwin':
        die(f'{host} is not reachable and only macOS is set up automatically')
    else:
        current = lab_address(host)
        print(f'    {host} resolves to {current or "nothing"} here, which is not the homelab.')
        server = args[args.index('--dns') + 1] if '--dns' in args else \
            input('    Homelab DNS address (your home gateway, e.g. 192.168.1.1): ').strip()
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
    print('4/4 sign in')
    token = saved_token()
    if token and bao('GET', 'auth/token/lookup-self', token)[0] == 200:
        print('    ok (already signed in)')
    else:
        cmd_login([])
    print('Done. Try `magehand app <name>` or, in an app repo, `magehand run -- <command>`.')


def cmd_version(_args):
    try:
        print(package_version('magehand'))
    except PackageNotFoundError:
        print('unknown (not installed as a package)')
    print('upgrade: uv tool upgrade magehand')


COMMANDS = {'guide': cmd_guide, 'app': cmd_app, 'login': cmd_login, 'dev': cmd_dev, 'run': cmd_run,
            'doctor': cmd_doctor, 'setup': cmd_setup, 'version': cmd_version}


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ('--version', '-V'):
        sys.argv[1] = 'version'
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__.split('Private by design')[0].strip())
        sys.exit(0 if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help', 'help') else 2)
    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == '__main__':
    main()
