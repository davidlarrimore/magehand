"""Unit tests for magehand (no network, no Docker)."""
import http.server
import json
import os
import pathlib
import re
import socket
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

from magehand import cli as magehand

os.environ['MAGEHAND_NO_UPDATE_CHECK'] = '1'  # tests never ask GitHub for the latest version

APP = {
    'name': 'demo',
    'pod': {'securityContext': {'runAsUser': 1000, 'runAsGroup': 1000}},
    'spec': {},
    'services': {'ai': {'type': 'llm'}, 'db': {'type': 'postgres'}, 'cache': {'type': 'redis'},
                 'session': {'type': 'secret'}},
    'container': {'command': ['python', '-B', '/app/app.py'], 'ports': [{'containerPort': 9000}], 'env': [
        {'name': 'PORT', 'value': '8000'},
        {'name': 'WEB_DIR', 'value': '/app/web'},
        {'name': 'LLM_KEY', 'valueFrom': {'secretKeyRef': {'name': 'ai-connection', 'key': 'api_key'}}},
        {'name': 'DATABASE_URL', 'valueFrom': {'secretKeyRef': {'name': 'db-connection', 'key': 'uri'}}},
        {'name': 'REDIS_URL', 'valueFrom': {'secretKeyRef': {'name': 'cache-connection', 'key': 'uri'}}},
        {'name': 'SESSION_KEY', 'valueFrom': {'secretKeyRef': {'name': 'session', 'key': 'value'}}},
        {'name': 'OTHER', 'valueFrom': {'secretKeyRef': {'name': 'handmade', 'key': 'x'}}},
    ]},
}
LOCAL = {'db': {'password': 'pw', 'port': '5555'}, 'cache': {'password': 'rpw', 'port': '6666'},
         'session': {'value': 'v' * 48}}


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Plan(unittest.TestCase):
    def test_env_plan(self):
        plan = {e[0]: e[1:] for e in magehand.env_plan(APP)}
        self.assertEqual(plan['PORT'], ('value', '8000'))
        self.assertEqual(plan['LLM_KEY'], ('ai', 'llm', 'api_key'))
        self.assertEqual(plan['DATABASE_URL'], ('db', 'postgres', 'uri'))
        self.assertEqual(plan['SESSION_KEY'], ('session', 'secret', 'value'))
        self.assertEqual(plan['OTHER'], ('unknown', 'handmade'))

    def test_app_port(self):
        self.assertEqual(magehand.app_port(APP), 8000)
        no_port_env = {**APP, 'container': {'ports': [{'containerPort': 9000}], 'env': []}}
        self.assertEqual(magehand.app_port(no_port_env), 9000)

    def test_local_connection(self):
        pg = magehand.local_connection('postgres', 'db', LOCAL)
        self.assertEqual(pg['uri'], 'postgresql://app:pw@127.0.0.1:5555/app')
        self.assertEqual(magehand.local_connection('redis', 'cache', LOCAL)['uri'], 'redis://:rpw@127.0.0.1:6666/0')
        with self.assertRaises(SystemExit):
            magehand.local_connection('postgres', 'db', {})


class Resolve(unittest.TestCase):
    def setUp(self):
        self.saved = (magehand.bao, magehand.valid_token, magehand.local_state)
        self.paths = []
        magehand.valid_token = lambda: 'tok'
        magehand.local_state = lambda _name: LOCAL

        def fake_bao(method, path, token=None, body=None):
            self.paths.append(path)
            return 200, {'data': {'data': {'api_key': 'sk-test', 'base_url': 'https://llm.lab/v1'}}}
        magehand.bao = fake_bao
        self.cwd = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        os.chdir(self.tmp.name)

    def tearDown(self):
        magehand.bao, magehand.valid_token, magehand.local_state = self.saved
        os.chdir(self.cwd)
        self.tmp.cleanup()
        os.environ.pop('PORT', None)

    def test_resolves_every_source(self):
        env = magehand.resolve_env(APP)
        self.assertEqual(env['LLM_KEY'], 'sk-test')
        self.assertEqual(env['DATABASE_URL'], 'postgresql://app:pw@127.0.0.1:5555/app')
        self.assertEqual(env['SESSION_KEY'], 'v' * 48)
        self.assertNotIn('OTHER', env)
        self.assertEqual(self.paths, ['apps/data/demo-dev/llm-ai'])

    def test_precedence(self):
        pathlib.Path('.magehand.env').write_text('# local\nWEB_DIR=./web\nPORT=7000\n')
        os.environ['PORT'] = '7100'
        env = magehand.resolve_env(APP)
        self.assertEqual(env['WEB_DIR'], './web')     # .magehand.env over the manifest
        self.assertEqual(env['PORT'], '7100')         # the caller's environment over both

    def test_container_mode_is_exactly_the_pod(self):
        pathlib.Path('.magehand.env').write_text('WEB_DIR=./web\n')
        os.environ['PORT'] = '7100'
        env = magehand.resolve_env(APP, container=True)
        self.assertEqual(env['WEB_DIR'], '/app/web')  # no host overrides
        self.assertEqual(env['PORT'], '8000')
        self.assertEqual(env['DATABASE_URL'], 'postgresql://app:pw@magehand-demo-db:5432/app')
        self.assertEqual(env['REDIS_URL'], 'redis://:rpw@magehand-demo-cache:6379/0')

    def test_missing_key_fails(self):
        bad = {**APP, 'container': {'env': [
            {'name': 'X', 'valueFrom': {'secretKeyRef': {'name': 'ai-connection', 'key': 'nope'}}}]}}
        with self.assertRaises(SystemExit):
            magehand.resolve_env(bad)


class MissingKey(unittest.TestCase):
    """No dev key in OpenBao: say why (not registered, not deployed, being created) instead of a bare 404."""

    def setUp(self):
        self.saved = magehand.APPS_DIR, magehand.bao, magehand.valid_token, magehand.local_state
        self.apps = tempfile.TemporaryDirectory()
        magehand.APPS_DIR = self.apps.name
        magehand.bao = lambda *_a, **_k: (404, None)
        magehand.valid_token = lambda: 'tok'
        magehand.local_state = lambda _name: LOCAL

    def tearDown(self):
        magehand.APPS_DIR, magehand.bao, magehand.valid_token, magehand.local_state = self.saved
        self.apps.cleanup()

    def register(self, app_yaml):
        (pathlib.Path(self.apps.name) / 'apps/demo').mkdir(parents=True)
        (pathlib.Path(self.apps.name) / 'apps/demo/app.yaml').write_text(app_yaml)

    def message(self):
        with self.assertRaises(SystemExit) as ctx:
            magehand.resolve_env(APP, container=True)
        return str(ctx.exception.code)

    def test_not_registered(self):
        text = self.message()
        self.assertIn("isn't registered", text)
        self.assertIn('registers by itself', text)
        self.assertIn("Never use another app's key", text)

    def test_block_not_deployed_yet(self):
        self.register('services:\n  db: {type: postgres}\n')
        self.assertIn('not deployed yet', self.message())

    def test_deployed_key_being_created(self):
        self.register('services:\n  ai: {type: llm}\n')
        self.assertIn("hasn't created its key yet", self.message())

    def test_key_states(self):
        self.assertEqual(magehand.llm_key_state('demo', 'ai', None), 'unregistered')
        self.assertEqual(magehand.llm_key_state('demo', 'ai', {'services': {}}), 'undeployed')
        self.assertEqual(magehand.llm_key_state('demo', 'ai', {'services': {'ai': {'type': 'llm'}}}), 'deployed')


class RunAsAnotherApp(unittest.TestCase):
    def test_refuses_another_apps_env_in_an_app_checkout(self):
        saved = magehand.app_repo_here, magehand.load_app
        magehand.app_repo_here = lambda: 'elvis'
        magehand.load_app = lambda name, root=None: magehand.die('loaded ' + name)
        try:
            parser = magehand.build_parser()
            with self.assertRaises(SystemExit) as ctx:
                magehand.cmd_run(parser.parse_args(['run', '-a', 'demo', '--', 'true']))
            self.assertIn("would use demo's keys", str(ctx.exception.code))
            with self.assertRaises(SystemExit) as ctx:  # its own name is fine
                magehand.cmd_run(parser.parse_args(['run', '-a', 'elvis', '--', 'true']))
            self.assertIn('loaded elvis', str(ctx.exception.code))
        finally:
            magehand.app_repo_here, magehand.load_app = saved


class Container(unittest.TestCase):
    def test_hardened_and_secrets_by_name_only(self):
        env = {'LLM_KEY': 'sk-secret', 'PORT': '8000'}
        args = magehand.container_args(APP, env, 'magehand/demo:local', 8000, ['llm.lab.davidlarrimore.com:192.0.2.5'], [])
        joined = ' '.join(args)
        self.assertNotIn('sk-secret', joined)
        self.assertIn('-e LLM_KEY', joined)
        for flag in ('--read-only', '--cap-drop ALL', '--security-opt no-new-privileges', '--user 1000:1000',
                     '-p 127.0.0.1:8000:8000', '--network magehand-demo',
                     '--add-host llm.lab.davidlarrimore.com:192.0.2.5'):
            self.assertIn(flag, joined)
        self.assertEqual(args[args.index('--entrypoint') + 1:], ['python', 'magehand/demo:local', '-B', '/app/app.py'])

    def test_explicit_command_replaces_the_pods(self):
        args = magehand.container_args(APP, {}, 'img', 8000, [], ['sh', '-c', 'true'])
        self.assertNotIn('--entrypoint', args)
        self.assertEqual(args[-4:], ['img', 'sh', '-c', 'true'])

    def test_lab_hosts(self):
        saved = magehand.socket.gethostbyname
        magehand.socket.gethostbyname = lambda h: '127.0.0.1' if h.startswith('llm.') else '192.0.2.7'
        try:
            hosts = magehand.lab_hosts({'A': 'https://llm.lab.davidlarrimore.com/v1',
                                        'B': 'https://x.lab.davidlarrimore.com', 'C': 'https://example.com', 'D': 'plain'})
        finally:
            magehand.socket.gethostbyname = saved
        self.assertEqual(hosts, ['llm.lab.davidlarrimore.com:host-gateway', 'x.lab.davidlarrimore.com:192.0.2.7'])


class Doctor(unittest.TestCase):
    def test_unreachable_lab_points_to_setup(self):
        import contextlib
        import io
        saved = (magehand.urllib.request.urlopen, magehand.github_token, magehand.github_file, magehand.saved_token)

        def no_route(*_a, **_k):
            raise OSError('connection refused')
        magehand.urllib.request.urlopen = no_route
        magehand.github_token = lambda: 't'
        magehand.github_file = lambda *_a, **_k: 'x'
        magehand.saved_token = lambda: None
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
                magehand.main(['doctor'])
        finally:
            (magehand.urllib.request.urlopen, magehand.github_token, magehand.github_file,
             magehand.saved_token) = saved
        self.assertIn('magehand setup', out.getvalue())


class Setup(unittest.TestCase):
    def setUp(self):
        self.names = ('reachable', 'lab_address', 'dns_answer', 'sudo_write_resolver', 'github_token',
                      'saved_token', 'cmd_login', 'shutil', 'install_skill')
        self.saved = {n: getattr(magehand, n) for n in self.names}
        self.skills = []  # never the real ~/.claude
        magehand.install_skill = lambda: self.skills.append(1) or []
        self.saved_platform = magehand.sys.platform
        magehand.sys.platform = 'darwin'
        magehand.github_token = lambda: 't'
        magehand.saved_token = lambda: None
        self.logins = []
        magehand.cmd_login = lambda _a: self.logins.append(1)
        magehand.lab_address = lambda _h: '127.0.0.1'
        self.written = []
        magehand.sudo_write_resolver = lambda server: self.written.append(server)
        self.saved_sleep = magehand.time.sleep
        magehand.time.sleep = lambda _s: None

    def tearDown(self):
        for n, v in self.saved.items():
            setattr(magehand, n, v)
        magehand.sys.platform = self.saved_platform
        magehand.time.sleep = self.saved_sleep

    def run_setup(self, args):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            magehand.main(['setup', *args])

    def test_already_reachable_changes_nothing(self):
        magehand.reachable = lambda: True
        self.run_setup([])
        self.assertEqual(self.written, [])
        self.assertEqual(self.logins, [1])
        self.assertEqual(self.skills, [1])

    def test_points_lab_lookups_at_the_given_dns(self):
        magehand.reachable = lambda: bool(self.written)
        magehand.dns_answer = lambda server, host: '192.0.2.10'
        self.run_setup(['--dns', '192.0.2.1'])
        self.assertEqual(self.written, ['192.0.2.1'])

    def test_refuses_a_dns_that_does_not_know_the_lab(self):
        magehand.reachable = lambda: False
        magehand.dns_answer = lambda server, host: '127.0.0.1'
        with self.assertRaises(SystemExit):
            self.run_setup(['--dns', '192.0.2.1'])
        self.assertEqual(self.written, [])


class ProxyHeaders(unittest.TestCase):
    def test_injects_identity_and_strips_client_headers(self):
        seen = {}

        class Echo(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.update({k.lower(): v for k, v in self.headers.items()})
                body = b'ok'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        backend = http.server.HTTPServer(('127.0.0.1', 0), Echo)
        threading.Thread(target=backend.serve_forever, daemon=True).start()
        magehand.PROXY_PORT = free_port()
        proxy = magehand.start_proxy(backend.server_address[1], {'email': 'me@example.com', 'uid': 'u1',
                                                                 'username': 'me', 'groups': 'homelab-admins'})
        try:
            req = urllib.request.Request(f'http://127.0.0.1:{magehand.PROXY_PORT}/x',
                                         headers={'X-authentik-email': 'forged@example.com'})
            with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310  # local test server
                self.assertEqual(resp.read(), b'ok')
        finally:
            proxy.shutdown()
            proxy.server_close()
            backend.shutdown()
            backend.server_close()
        self.assertEqual(seen['x-authentik-email'], 'me@example.com')
        self.assertEqual(seen['x-authentik-uid'], 'u1')
        self.assertEqual(seen['x-authentik-groups'], 'homelab-admins')


if __name__ == '__main__':
    unittest.main()


def check_app(env, services=None, image='ghcr.io/davidlarrimore/homelab-apps/demo@sha256:' + 'a' * 64):
    return {'name': 'demo', 'spec': {}, 'pod': {},
            'services': services if services is not None else {'ai': {'type': 'llm', 'models': ['m1']}},
            'container': {'image': image, 'env': env}}


CATALOG = {'blocks': {'postgres': {'sizes': ['1Gi', '2Gi']}, 'redis': {}, 'secret': {}, 'llm': {}},
           'models': [{'id': 'm1'}]}


def ref(var, secret, key, **extra):
    return {'name': var, 'valueFrom': {'secretKeyRef': dict({'name': secret, 'key': key}, **extra)}}


def texts(found, level=None):
    return [f['text'] for f in found if level in (None, f['level'])]


class CheckManifests(unittest.TestCase):
    def test_clean(self):
        app = check_app([ref('LLM_KEY', 'ai-connection', 'api_key'), {'name': 'PORT', 'value': '8000'}])
        self.assertEqual(magehand.check_manifests(app, CATALOG), [])

    def test_search_url_needs_search_true(self):
        app = check_app([ref('SEARCH', 'ai-connection', 'search_url')])
        self.assertIn('search: true', texts(magehand.check_manifests(app, CATALOG), 'error')[0])
        app['services']['ai']['search'] = True
        self.assertEqual(magehand.check_manifests(app, CATALOG), [])

    def test_optional_block_key(self):
        app = check_app([ref('LLM_KEY', 'ai-connection', 'api_key', optional=True)])
        self.assertIn('optional: true', texts(magehand.check_manifests(app, CATALOG), 'error')[0])

    def test_secret_that_is_not_a_block(self):
        app = check_app([ref('X', 'handmade', 'x')])
        self.assertIn("neither a block's", texts(magehand.check_manifests(app, CATALOG), 'error')[0])

    def test_secret_from_the_apps_own_externalsecret(self):
        app = check_app([ref('X', 'handmade', 'x')])
        app['provided'] = {'handmade'}
        self.assertEqual(magehand.check_manifests(app, CATALOG), [])

    def test_provided_secrets_reads_base_and_dev(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / 'apps/demo/base'
            base.mkdir(parents=True)
            (base / 'es.yaml').write_text('kind: ExternalSecret\nmetadata: {name: es}\nspec: {target: {name: api}}\n'
                                          '---\nkind: Deployment\nmetadata: {name: demo}\n')
            (pathlib.Path(tmp) / 'apps/demo/dev').mkdir()
            (pathlib.Path(tmp) / 'apps/demo/dev/s.yaml').write_text('kind: Secret\nmetadata: {name: plain}\n')
            saved, magehand.APPS_DIR = magehand.APPS_DIR, tmp
            try:
                self.assertEqual(magehand.provided_secrets('demo'), {'api', 'plain'})
            finally:
                magehand.APPS_DIR = saved

    def test_unknown_key(self):
        app = check_app([ref('DB', 'db-connection', 'url')], services={'db': {'type': 'postgres'}})
        self.assertIn("no key 'url'", texts(magehand.check_manifests(app, CATALOG), 'error')[0])

    def test_catalog_keys_win(self):
        catalog = dict(CATALOG, blocks=dict(CATALOG['blocks'], postgres={'keys': ['url']}))
        app = check_app([ref('DB', 'db-connection', 'url')], services={'db': {'type': 'postgres'}})
        self.assertEqual(magehand.check_manifests(app, catalog), [])

    def test_model_type_size_and_image(self):
        app = check_app([], services={'ai': {'type': 'llm', 'models': ['nope']}, 'db': {'type': 'postgres', 'size': '9Gi'},
                                      'q': {'type': 'kafka'}}, image='nginx:latest')
        errors = ' | '.join(texts(magehand.check_manifests(app, CATALOG), 'error'))
        for part in ("model 'nope'", "size '9Gi'", "unknown type 'kafka'", 'image nginx:latest'):
            self.assertIn(part, errors)


class CheckCode(unittest.TestCase):
    def run_check(self, files, env=()):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for name in ('Dockerfile', 'ui-check.yaml'):
                (root / name).write_text('x')
            for name, text in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text)
            return magehand.check_code(root, check_app(list(env)))

    def test_dotenv_holds_no_secrets(self):
        found = self.run_check({'.magehand.env': '# local\nWEB_DIR=./web\nKEY=x\nOPENAI_BASE=sk-abcdefgh1234\n'
                                                 'API_TOKEN=abc\nEMPTY_TOKEN=\n'},
                               env=[ref('KEY', 'ai-connection', 'api_key')])
        got = {(f['level'], f['where']) for f in found if f['where'].startswith('.magehand.env')}
        self.assertEqual(got, {('error', '.magehand.env:3'), ('error', '.magehand.env:4'),
                               ('warning', '.magehand.env:5')})

    def test_env_the_pod_does_not_set(self):
        found = self.run_check({'app.py': 'import os\nA = os.environ.get("PORT")\nB = os.environ["FEATURE_X"]\n'},
                               env=[{'name': 'PORT', 'value': '1'}])
        self.assertEqual([f['where'] for f in found], ['app.py:3'])
        self.assertIn('FEATURE_X', found[0]['text'])

    def test_js_env_and_provider_key(self):
        found = self.run_check({'src/x.ts': 'const k = process.env.OPENAI_API_KEY;\n'})
        self.assertEqual(texts(found, 'error')[0][:21], 'reads OPENAI_API_KEY:')

    def test_direct_provider_call(self):
        found = self.run_check({'app.py': 'URL = "https://api.exa.ai/search"\n'})
        self.assertIn('calls api.exa.ai directly', texts(found, 'error')[0])

    def test_lab_url_only_outside_env_defaults(self):
        found = self.run_check({'app.py': 'import os\nB = os.environ.get("BASE", "https://llm.lab.davidlarrimore.com/v1")\n'
                                          'C = "https://llm.lab.davidlarrimore.com/v1"\n'}, env=[{'name': 'BASE', 'value': 'x'}])
        self.assertEqual([f['where'] for f in found], ['app.py:3'])

    def test_own_auth_and_local_db(self):
        found = self.run_check({'app.py': 'import bcrypt\nimport sqlite3\ndb = sqlite3.connect("data.db")\n'
                                          'mem = sqlite3.connect(":memory:")\n'})
        self.assertEqual([f['where'] for f in found], ['app.py:1', 'app.py:3'])

    def test_tests_and_dependencies_are_skipped(self):
        found = self.run_check({'tests/test_app.py': 'URL = "https://api.openai.com"\n',
                                'node_modules/x/index.js': 'fetch("https://api.openai.com")\n',
                                'web/app.test.js': 'process.env.NOPE\n'})
        self.assertEqual(found, [])

    def test_missing_build_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            found = magehand.check_code(pathlib.Path(tmp), None)
        self.assertEqual(sorted(f['where'] for f in found), ['Dockerfile', 'ui-check.yaml'])


CATALOG_MODELS = dict(CATALOG, models=[{'id': 'm1', 'default': True}, {'id': 'paid/m2'}])
DEPLOYMENT = ('apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: demo}\nspec:\n  template:\n    spec:\n'
              '      containers:\n        - name: app\n          image: ghcr.io/davidlarrimore/homelab-apps/demo\n'
              '          env:\n            - {name: PORT, value: "8000"}\n'
              '            - name: KEY\n              valueFrom: {secretKeyRef: {name: ai-connection, key: api_key}}\n')


def deploy_repo(tmp, app_yaml='services:\n  ai: {type: llm, models: [paid/m2]}\n', extra=None):
    root = pathlib.Path(tmp)
    (root / 'deploy/base').mkdir(parents=True)
    (root / 'deploy/app.yaml').write_text(app_yaml)
    (root / 'deploy/base/deployment.yaml').write_text(DEPLOYMENT)
    (root / 'deploy/base/es.yaml').write_text('kind: ExternalSecret\nmetadata: {name: x}\nspec: {target: {name: own}}\n')
    for name, text in (extra or {}).items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return root


class DeployDir(unittest.TestCase):
    """An app repo with deploy/: magehand reads the app from the checkout, not homelab-apps."""

    def setUp(self):
        self.saved = magehand.APPS_DIR
        self.apps = tempfile.TemporaryDirectory()
        magehand.APPS_DIR = self.apps.name  # homelab-apps: nothing for demo
        self.addCleanup(setattr, magehand, 'APPS_DIR', self.saved)
        self.addCleanup(self.apps.cleanup)

    def test_a_seeded_deploy_dir_with_only_app_yaml_checks_without_error(self):
        """`new --app-name` commits deploy/app.yaml alone (`app: ...`, `services: {}`): CI must not fail on it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'deploy').mkdir()
            (root / 'deploy/app.yaml').write_text('app: "lab"\nservices: {}\n')
            self.assertIsNone(magehand.try_load_app('lab', root))  # no Deployment yet: nothing to check, no die
            found = magehand.check_deploy_files(root, 'lab')
            self.assertEqual([(f['level'], f['where']) for f in found], [('warning', 'deploy/base')])
            self.assertIn('nothing is deployed or registered', found[0]['text'])

    def test_blocks_requested_without_a_base_is_still_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'deploy').mkdir()
            (root / 'deploy/app.yaml').write_text('app: "lab"\nservices:\n  ai: {type: llm}\n')
            found = magehand.check_deploy_files(root, 'lab')
            self.assertEqual([(f['level'], f['where']) for f in found], [('error', 'deploy/base')])

    def test_an_existing_base_without_a_deployment_is_still_rejected(self):
        for body, why in (("kind: Service\nmetadata: {name: x}\n", 'has no Deployment'),
                          ("kind: Deployment\n  broken: [", 'not valid YAML')):
            with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as apps:
                root = pathlib.Path(tmp)
                (root / 'deploy/base').mkdir(parents=True)
                (root / 'deploy/app.yaml').write_text('app: "lab"\nservices:\n  ai: {type: llm}\n')
                (root / 'deploy/base/service.yaml').write_text(body)
                with self.assertRaises(SystemExit) as ctx:
                    magehand.try_load_app('lab', root)
                self.assertIn(why, str(ctx.exception.code))
                # and through the command: exit 1, not "no findings"
                code, out, _ = CommandLine.run_main(self, 'check', 'lab', '--code', tmp, '--apps-dir', apps,
                                                    '--manifests-only', '--json')
                self.assertEqual((code, out), (1, ''), why)  # run_main drops the message; real runs print it

    def test_load_app_from_the_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = magehand.try_load_app('demo', deploy_repo(tmp))
        self.assertEqual((app['where'], app['deployment_file']), ('deploy', 'deploy/base/deployment.yaml'))
        self.assertEqual(app['services'], {'ai': {'type': 'llm', 'models': ['paid/m2']}})
        self.assertEqual(app['provided'], {'own'})
        self.assertEqual(magehand.env_plan(app)[1], ('KEY', 'ai', 'llm', 'api_key'))

    def test_no_deploy_dir_and_no_homelab_apps_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(magehand.try_load_app('demo', pathlib.Path(tmp)))

    def test_clean_unpinned_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = magehand.try_load_app('demo', deploy_repo(tmp))
            self.assertEqual(magehand.check_manifests(app, CATALOG_MODELS), [])
            self.assertEqual(magehand.check_deploy_files(pathlib.Path(tmp)), [])

    def test_source_other_image_and_files_deploy_dev_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = deploy_repo(tmp, 'source: {repo: demo}\nservices: {}\n',
                               {'deploy/stage/kustomization.yaml': 'x', 'deploy/base/README.md': 'x'})
            (root / 'deploy/base/deployment.yaml').write_text(DEPLOYMENT.replace('homelab-apps/demo', 'homelab-apps/other'))
            app = magehand.try_load_app('demo', root)
            errors = texts(magehand.check_manifests(app, CATALOG_MODELS), 'error')
            self.assertTrue(any('source: is added by the copy' in e for e in errors))
            self.assertTrue(any('without a digest' in e for e in errors))
            self.assertEqual(sorted(f['where'] for f in magehand.check_deploy_files(root)),
                             ['deploy/base/README.md', 'deploy/stage/kustomization.yaml'])

    def test_overlays_and_their_namespaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = deploy_repo(tmp, extra={'deploy/dev/kustomization.yaml': 'namespace: demo-dev\nresources: [../base]\n',
                                           'deploy/prod/kustomization.yaml': 'namespace: demo\nresources: [../base]\n'})
            self.assertEqual(magehand.check_deploy_files(root, 'demo'), [])
            (root / 'deploy/prod/kustomization.yaml').write_text('namespace: demo-dev\nresources: [../other]\n')
            errors = texts(magehand.check_deploy_files(root, 'demo'), 'error')
            self.assertEqual(len(errors), 2)
            self.assertIn('namespace must be demo', errors[0])


def git_checkout(root, remote):
    import subprocess
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), 'remote', 'add', 'origin', remote], check=True)
    return root


class AppNameOverride(unittest.TestCase):
    """deploy/app.yaml's `app:` names the app when its repo is named differently."""

    REMOTE = 'git@github.com:davidlarrimore/lab-website.git'

    def setUp(self):
        self.saved = magehand.APPS_DIR
        self.apps = tempfile.TemporaryDirectory()
        magehand.APPS_DIR = self.apps.name
        self.addCleanup(setattr, magehand, 'APPS_DIR', self.saved)
        self.addCleanup(self.apps.cleanup)

    def checkout(self, tmp, app_yaml):
        return git_checkout(deploy_repo(tmp, app_yaml), self.REMOTE)

    def register(self, name, text):
        d = pathlib.Path(self.apps.name) / 'apps' / name
        d.mkdir(parents=True)
        (d / 'app.yaml').write_text(text)

    def test_the_app_is_app_or_the_repo_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(magehand.checkout_app(self.checkout(tmp, 'app: lab\nservices: {}\n')), 'lab')
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(magehand.checkout_app(self.checkout(tmp, 'services: {}\n')), 'lab-website')
        with tempfile.TemporaryDirectory() as tmp:  # an invalid app: is reported by check, never used as a name
            self.assertEqual(magehand.checkout_app(self.checkout(tmp, 'app: Not A Name\n')), 'lab-website')
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(magehand.current_app(self.checkout(tmp, 'app: lab\nservices: {}\n')), 'lab')

    def test_platform_repos_are_not_apps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = git_checkout(pathlib.Path(tmp), 'git@github.com:davidlarrimore/homelab-apps.git')
            self.assertIsNone(magehand.checkout_app(root))

    def load(self, tmp, app_yaml, name='lab'):
        return magehand.try_load_app(name, self.checkout(tmp, app_yaml))

    def test_valid_override_and_unregistered_name_are_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(magehand.check_app_name(self.load(tmp, 'app: lab\nservices: {}\n')), [])

    def test_invalid_or_reserved_app(self):
        for bad in ('Lab', 'lab-dev', 'litellm', '123x-', '[x]'):
            with tempfile.TemporaryDirectory() as tmp:
                errors = texts(magehand.check_app_name(self.load(tmp, f'app: {bad}\nservices: {{}}\n', name='x')),
                               'error')
            self.assertEqual(len(errors), 1, bad)

    def test_a_name_registered_to_another_repo_is_an_error(self):
        self.register('lab', 'source: {repo: lab}\nservices: {}\n')
        with tempfile.TemporaryDirectory() as tmp:
            errors = texts(magehand.check_app_name(self.load(tmp, 'app: lab\nservices: {}\n')), 'error')
        self.assertEqual(len(errors), 1)
        self.assertIn('registered in homelab-apps to davidlarrimore/lab', errors[0])

    def test_registered_to_this_repo_is_fine(self):
        self.register('lab', 'source: {repo: lab-website}\nservices: {}\n')
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(magehand.check_app_name(self.load(tmp, 'app: lab\nservices: {}\n')), [])

    def seeded(self, tmp, app_yaml):
        """A repo as `new --app-name` leaves it: deploy/app.yaml alone, origin lab-website."""
        root = pathlib.Path(tmp)
        (root / 'deploy').mkdir()
        (root / 'deploy/app.yaml').write_text(app_yaml)
        return git_checkout(root, self.REMOTE)

    def run_check(self, root, name='lab'):
        code, out, _ = CommandLine.run_main(self, 'check', name, '--code', str(root), '--apps-dir', self.apps.name,
                                            '--manifests-only', '--json')
        return code, json.loads(out)

    def test_seeded_checkout_names_are_still_validated(self):
        """Returning early for "no Deployment yet" must not skip the app-name rules."""
        for app_yaml, why in (('app: Lab\nservices: {}\n', 'not an app name'),
                              ('app: litellm\nservices: {}\n', 'reserved'),
                              ('app: lab-dev\nservices: {}\n', 'not an app name'),
                              ('app: [x\n', 'not valid YAML')):
            with tempfile.TemporaryDirectory() as tmp:
                code, result = self.run_check(self.seeded(tmp, app_yaml), name='lab-website')
            errors = [f['text'] for f in result['findings'] if f['level'] == 'error']
            self.assertEqual(code, 1, app_yaml)
            self.assertTrue(any(why in e for e in errors), (app_yaml, errors))

    def test_seeded_checkout_registered_to_another_repo_fails(self):
        self.register('lab', 'source: {repo: lab}\nservices: {}\n')
        with tempfile.TemporaryDirectory() as tmp:
            code, result = self.run_check(self.seeded(tmp, 'app: "lab"\nservices: {}\n'))
        errors = [f['text'] for f in result['findings'] if f['level'] == 'error']
        self.assertEqual(code, 1)
        self.assertTrue(any('registered in homelab-apps to davidlarrimore/lab' in e for e in errors), errors)

    def test_a_valid_seeded_checkout_only_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, result = self.run_check(self.seeded(tmp, 'app: "lab"\nservices: {}\n'))
        self.assertEqual(code, 0)
        self.assertEqual([(f['level'], f['where']) for f in result['findings']], [('warning', 'deploy/base')])

    def test_a_repo_that_lost_its_app_key_is_an_error(self):
        self.register('lab', 'source: {repo: lab-website}\nservices: {}\n')
        with tempfile.TemporaryDirectory() as tmp:
            errors = texts(magehand.check_app_name(self.load(tmp, 'services: {}\n', name='lab-website')), 'error')
        self.assertTrue(any('registered as the app lab' in e and 'restore `app: lab`' in e for e in errors), errors)


class ModelRule(unittest.TestCase):
    def run_check(self, code, services):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for name in ('Dockerfile', 'ui-check.yaml'):
                (root / name).write_text('x')
            (root / 'app.py').write_text(code)
            return magehand.check_code(root, check_app([{'name': 'M', 'value': 'x'}], services), CATALOG_MODELS)

    def test_requested_model_is_fine_default_counts(self):
        self.assertEqual(self.run_check('MODEL = "paid/m2"\n', {'ai': {'type': 'llm', 'models': ['paid/m2']}}), [])
        self.assertEqual(self.run_check("MODEL = 'm1'\n", {'ai': {'type': 'llm'}}), [])

    def test_unrequested_models_are_warnings_comments_ignored(self):
        # Text can't prove a call (AGENTS.md: anything heuristic is a warning), so none of these fail check;
        # whole-line comments aren't reported at all.
        code = ('import os\nr = client.chat(model="paid/m2")\nDISPLAY = {"model": "paid/m2", "available": False}\n'
                'B = os.environ.get("M", "paid/m2")\n"""client.chat(model="paid/m2")"""\n'
                'x = 1  # Previously model="paid/m2"\n# Previously used "paid/m2"\n    // was "paid/m2"\n')
        found = self.run_check(code, {'ai': {'type': 'llm', 'models': ['m1']}})
        self.assertEqual([(f['level'], f['where']) for f in found],
                         [('warning', f'app.py:{n}') for n in (2, 3, 4, 5, 6)])
        self.assertIn('LiteLLM refuses it (403) if the code calls it', found[0]['text'])
        self.assertIn('drop this fallback', found[2]['text'])

    def test_no_llm_block(self):
        self.assertIn('requested: none', texts(self.run_check('model = "m1"\n', {}), 'warning')[0])


class ManifestsOnly(unittest.TestCase):
    """check --manifests-only: the deployment (the checkout's deploy/ when it has one), not the code."""

    def check(self, root, apps):
        code, out, _ = CommandLine.run_main(self, 'check', 'demo', '--code', str(root), '--apps-dir', apps,
                                            '--manifests-only', '--json')
        return code, json.loads(out)

    def test_local_deploy_without_a_homelab_apps_copy(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as apps:
            root = deploy_repo(tmp, extra={'deploy/prod/kustomization.yaml': 'x', 'app.py': 'model = "nope"\n'})
            code, result = self.check(root, apps)
        self.assertEqual((result['deployment'], result['code']), ('deploy', None))
        self.assertEqual([f['where'] for f in result['findings'] if f['level'] == 'error'],
                         ['deploy/prod/kustomization.yaml'])  # deploy/ files checked; app.py (code) not
        self.assertEqual(code, 1)

    def test_local_deploy_wins_over_the_homelab_apps_copy(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as apps:
            deploy_repo(pathlib.Path(apps) / 'apps/demo')  # homelab-apps' copy: apps/demo/deploy/... is not it
            (pathlib.Path(apps) / 'apps/demo/app.yaml').write_text('services: {}\n')
            (pathlib.Path(apps) / 'apps/demo/base').mkdir()
            (pathlib.Path(apps) / 'apps/demo/base/deployment.yaml').write_text(DEPLOYMENT)
            _, result = self.check(deploy_repo(tmp), apps)
        self.assertEqual(result['deployment'], 'deploy')

    def test_homelab_apps_ci_unchanged(self):
        # homelab-apps CI: run from its checkout (no deploy/ at the top), the deployment is apps/<app>/.
        with tempfile.TemporaryDirectory() as apps:
            (pathlib.Path(apps) / 'apps/demo/base').mkdir(parents=True)
            (pathlib.Path(apps) / 'apps/demo/app.yaml').write_text('services: {}\n')
            (pathlib.Path(apps) / 'apps/demo/base/deployment.yaml').write_text(DEPLOYMENT)
            _, result = self.check(pathlib.Path(apps), apps)
        self.assertEqual(result['deployment'], 'homelab-apps apps/demo')


class GuideSearch(unittest.TestCase):
    PAGES = {'blocks': 'Intro about blocks.\n\n## Web search\n\nAdd search: true.\n\n## Models\n\nThe models.\n',
             'README': '# Guide\n\nApps.\n\n## Who the user is\n\nauthentik headers.\n'}

    def test_sections(self):
        self.assertEqual(magehand.sections(self.PAGES['blocks']),
                         [('', 'Intro about blocks.'), ('Web search', 'Add search: true.'), ('Models', 'The models.')])

    def test_best_section_first(self):
        hits = magehand.search_sections(self.PAGES, 'how do I add web search')
        self.assertEqual((hits[0][1], hits[0][2]), ('blocks', 'Web search'))

    def test_no_match(self):
        self.assertEqual(magehand.search_sections(self.PAGES, 'kubernetes operators'), [])


class Skill(unittest.TestCase):
    def test_install_claude_and_codex_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            claude, codex = pathlib.Path(tmp) / 'claude', pathlib.Path(tmp) / 'codex'
            codex.mkdir()
            old = {k: os.environ.get(k) for k in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME')}
            os.environ.update(CLAUDE_CONFIG_DIR=str(claude), CODEX_HOME=str(codex))
            try:
                paths = magehand.install_skill()
            finally:
                for k, v in old.items():
                    os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
            self.assertEqual(paths, [claude / 'skills/homelab-app/SKILL.md', codex / 'skills/homelab-app/SKILL.md'])
            self.assertTrue(paths[0].read_text().startswith('---\nname: homelab-app\n'))


class New(unittest.TestCase):
    """magehand new: the app's private repo from the template, with OpenClaw's bot invited."""

    def setUp(self):
        self.saved = magehand.gh_cli
        self.calls = []
        self.addCleanup(setattr, magehand, 'gh_cli', self.saved)

    def fake(self, exists=None, collaborator=False, ruleset=False, ruleset_status=0, existing=None):
        """`existing` is the ruleset already on the repo (default: ours when `ruleset`)."""
        existing = existing if existing is not None else (dict(magehand.RULESET, id=7) if ruleset else None)

        def gh(*args, **kw):
            self.calls.append(args)
            if args[:2] == ('api', 'repos/davidlarrimore/elvis/rulesets'):
                listed = [{'id': 7, 'name': existing['name']}] if existing else []
                return ruleset_status, json.dumps(listed), 'HTTP 403' if ruleset_status else ''
            if args[:2] == ('api', 'repos/davidlarrimore/elvis/rulesets/7'):
                return 0, json.dumps(existing), ''
            if args[:2] == ('api', 'repos/davidlarrimore/elvis') and '.default_branch' in args:
                return 0, 'main\n', ''
            if args[0] == 'api' and args[1].startswith('repos/davidlarrimore/elvis/rules/branches/main'):
                covered = existing and existing.get('conditions', {}).get('ref_name', {}).get('include') == ['~DEFAULT_BRANCH']
                return 0, json.dumps([{'type': 'pull_request', 'ruleset_id': 7}] if covered else []), ''
            if args[:3] == ('api', '-X', 'POST'):
                return ruleset_status, '', 'HTTP 403' if ruleset_status else ''
            if args[:2] == ('api', 'repos/davidlarrimore/elvis'):
                return (1, '', 'Not Found') if exists is None else (0, exists + '\n', '')
            if args[:1] == ('api',) and args[1].endswith('/collaborators/davidlarrimore-bot'):
                return (0 if collaborator else 1), '', ''
            return 0, '', ''
        magehand.gh_cli = gh

    def run_new(self, *argv):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            return magehand.cmd_new(magehand.build_parser().parse_args(['new', *argv]))

    def test_creates_private_from_the_template_and_invites_the_bot(self):
        self.fake()
        self.run_new('elvis')
        self.assertIn(('repo', 'create', 'davidlarrimore/elvis', '--private', '--template',
                       'davidlarrimore/homelab-app-template'), self.calls)
        self.assertIn(('api', '-X', 'PUT', 'repos/davidlarrimore/elvis/collaborators/davidlarrimore-bot',
                       '-f', 'permission=push'), self.calls)

    def test_adds_the_approval_ruleset(self):
        self.fake()
        self.run_new('elvis')
        self.assertIn(('api', '-X', 'POST', 'repos/davidlarrimore/elvis/rulesets', '--input', '-'), self.calls)

    def test_a_weakened_ruleset_is_reported_not_called_fine_and_not_changed(self):
        import contextlib
        import io
        for weak, why in ((dict(magehand.RULESET, id=7, enforcement='disabled'), 'enforcement'),
                          (dict(magehand.RULESET, id=7, conditions={'ref_name': {'include': ['refs/heads/x']}}), 'default branch'),
                          (dict(magehand.RULESET, id=7, rules=[]), 'approving review')):
            self.calls.clear()
            self.fake(exists='true', collaborator=True, existing=weak)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.run_new('elvis')
            self.assertIn(why, err.getvalue())
            self.assertNotIn('already there', err.getvalue())
            self.assertFalse([c for c in self.calls if '-X' in c], why)  # nothing is rewritten

    def test_a_refused_ruleset_warns_and_does_not_fail(self):
        import contextlib
        import io
        self.fake(exists='true', collaborator=True, ruleset_status=1)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.run_new('elvis')
        self.assertIn('without a review', err.getvalue())

    def test_ruleset_matches_homelabs_approval_rule(self):
        rule = magehand.RULESET['rules'][0]['parameters']
        self.assertEqual((rule['required_approving_review_count'], rule['dismiss_stale_reviews_on_push'],
                          rule['require_last_push_approval']), (1, True, True))
        self.assertEqual(magehand.RULESET['bypass_actors'][0]['bypass_mode'], 'pull_request')

    def test_rerun_changes_nothing(self):
        self.fake(exists='true', collaborator=True, ruleset=True)
        self.run_new('elvis')
        self.assertFalse([c for c in self.calls if c[:2] == ('repo', 'create') or '-X' in c])

    def test_existing_public_repo_gets_the_bot_and_a_warning(self):
        import contextlib
        import io
        self.fake(exists='false')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.run_new('elvis')
        self.assertIn('PUBLIC', err.getvalue())
        self.assertFalse([c for c in self.calls if c[0] == 'repo' and c[1] != 'clone'])  # visibility is the owner's call
        self.assertTrue([c for c in self.calls if '-X' in c])

    def with_contents(self, existing=None, put_fails=0):
        """Add deploy/app.yaml reads (`existing` text, else 404) and writes (the first `put_fails` fail) to fake()."""
        inner, writes = magehand.gh_cli, []

        def gh(*args, **kw):
            if any(a.endswith('/contents/deploy/app.yaml') for a in args):
                self.calls.append(args)
                if args[:3] == ('api', '-X', 'PUT'):
                    if put_fails > len(writes):
                        writes.append(None)
                        return 1, '', 'HTTP 404'
                    writes.append(args)
                    return 0, '', ''
                return (0, existing, '') if existing is not None else (1, '', 'HTTP 404')
            return inner(*args, **kw)
        magehand.gh_cli = gh
        return writes

    def test_app_name_seeds_deploy_app_yaml_on_the_new_repo(self):
        import base64
        self.fake()
        writes = self.with_contents()
        self.run_new('elvis', '--app-name', 'lab')
        self.assertIn(('repo', 'create', 'davidlarrimore/elvis', '--private', '--template',
                       'davidlarrimore/homelab-app-template'), self.calls)
        self.assertEqual(len(writes), 1)
        body = dict(a.split('=', 1) for a in writes[0][3:] if '=' in a and not a.startswith('repos/'))
        text = base64.b64decode(body['content']).decode()
        self.assertIn('\napp: "lab"\n', text)
        self.assertEqual(magehand.yaml.safe_load(text)['app'], 'lab')
        # the seeding commit comes before the ruleset can ask for a review
        put = next(i for i, c in enumerate(self.calls) if c[:3] == ('api', '-X', 'PUT') and 'deploy/app.yaml' in c[3])
        rules = next(i for i, c in enumerate(self.calls) if c[:3] == ('api', '-X', 'POST'))
        self.assertLess(put, rules)

    def test_app_name_write_is_retried_while_the_template_copy_settles(self):
        self.fake()
        saved, magehand.time.sleep = magehand.time.sleep, lambda _s: None
        self.addCleanup(setattr, magehand.time, 'sleep', saved)
        writes = self.with_contents(put_fails=2)
        self.run_new('elvis', '--app-name', 'lab')
        self.assertEqual(len([w for w in writes if w]), 1)

    def test_app_name_states_on_an_existing_repo(self):
        for existing, state in (('app: lab\nservices: {}\n', 'present'), ('services: {}\n', 'differs'),
                                ('app: other\n', 'differs')):
            self.fake(exists='true', collaborator=True, ruleset=True)
            writes = self.with_contents(existing=existing)
            self.assertEqual(magehand.ensure_app_name('davidlarrimore/elvis', 'lab'), state)
            self.assertEqual(writes, [], state)  # never rewrites a deploy/app.yaml that exists

    def test_names_that_are_not_yaml_strings_bare_round_trip_as_strings(self):
        import base64
        for app in ('true', 'yes', 'null', '123', 'on', '1e3'):
            self.fake()
            writes = self.with_contents()
            self.run_new('elvis', '--app-name', app)
            body = dict(a.split('=', 1) for a in writes[-1][3:] if a.startswith('content='))
            seeded = magehand.yaml.safe_load(base64.b64decode(body['content']).decode())
            self.assertEqual(seeded['app'], app, app)

    def test_a_failed_naming_commit_exits_1_after_the_rest_is_set_up(self):
        import contextlib
        import io
        self.fake()
        self.with_contents(put_fails=99)
        saved, magehand.time.sleep = magehand.time.sleep, lambda _s: None
        self.addCleanup(setattr, magehand.time, 'sleep', saved)
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                magehand.cmd_new(magehand.build_parser().parse_args(['new', 'elvis', '--app-name', 'lab', '--json']))
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn('could not write deploy/app.yaml', err.getvalue())
        self.assertEqual(json.loads(out.getvalue())['app_name'], 'failed')
        self.assertIn(('api', '-X', 'POST', 'repos/davidlarrimore/elvis/rulesets', '--input', '-'), self.calls)
        self.assertIn(('api', '-X', 'PUT', 'repos/davidlarrimore/elvis/collaborators/davidlarrimore-bot',
                       '-f', 'permission=push'), self.calls)

    def test_a_repo_that_names_another_app_exits_1(self):
        self.fake(exists='true', collaborator=True, ruleset=True)
        self.with_contents(existing='app: other\nservices: {}\n')
        with self.assertRaises(SystemExit) as ctx:
            self.run_new('elvis', '--app-name', 'lab')
        self.assertEqual(ctx.exception.code, 1)

    def test_new_clones_the_repo_unless_told_not_to(self):
        self.fake()
        self.run_new('elvis', '--dir', '/nonexistent-parent-x/elvis')
        self.assertIn(('repo', 'clone', 'davidlarrimore/elvis', '/nonexistent-parent-x/elvis'), self.calls)
        self.calls.clear()
        self.fake()
        self.run_new('elvis', '--no-clone')
        self.assertFalse([c for c in self.calls if c[:2] == ('repo', 'clone')])

    def test_an_existing_directory_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / 'x').write_text('x')
            self.fake()
            self.run_new('elvis', '--dir', tmp)
            self.assertFalse([c for c in self.calls if c[:2] == ('repo', 'clone')])

    def test_summary_lists_your_steps_and_the_automatic_ones_apart(self):
        text = magehand.new_summary('davidlarrimore/lab-website', 'lab-website', 'lab', pathlib.Path('lab-website'),
                                    'seeded', 'cloned')
        you, then = text.split('\n\nThen, by itself')
        self.assertNotIn('gh repo clone', you)          # cloned already: not a step
        self.assertIn('1. run:   cd lab-website', you)  # a program can't change your shell's directory
        self.assertIn('2. write: the code', you)
        self.assertIn('run:   magehand check', you)
        self.assertIn('https://lab-dev.', then)          # the app, not the repo
        self.assertNotIn('lab-website-dev', text)

    def test_a_file_where_the_clone_would_go_stops_before_github(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / 'elvis'
            target.write_text('a file')
            self.fake()
            with self.assertRaises(SystemExit) as ctx:
                self.run_new('elvis', '--dir', str(target))
            self.assertEqual(ctx.exception.code, 2)
            self.assertEqual(self.calls, [])  # nothing was created or invited
            self.run_new('elvis', '--dir', str(target), '--no-clone')  # but --no-clone doesn't care

    def test_paths_in_commands_survive_a_shell(self):
        import shlex
        for path in ('/tmp/my apps/elvis', "/tmp/it's here", '/tmp/a;b$(c)', '-x', 'plain'):
            quoted = magehand.shell_path(pathlib.Path(path))
            word = shlex.split(quoted)
            self.assertEqual(len(word), 1, path)
            self.assertEqual(word[0], './-x' if path == '-x' else path)
            text = magehand.new_summary('davidlarrimore/elvis', 'elvis', 'elvis', pathlib.Path(path), 'seeded', 'cloned')
            cd = next(l for l in text.splitlines() if 'run:   cd' in l).split('run:   ', 1)[1]
            self.assertEqual(shlex.split(cd)[0], 'cd')
            self.assertEqual(shlex.split(cd)[-1], './-x' if path == '-x' else path)
            clone = magehand.new_summary('davidlarrimore/elvis', 'elvis', 'elvis', pathlib.Path(path), 'seeded', 'failed')
            line = next(l for l in clone.splitlines() if 'gh repo clone' in l).split('run:   ', 1)[1]
            self.assertEqual(shlex.split(line)[-1], './-x' if path == '-x' else path)

    def test_a_leading_dash_directory_is_not_read_as_an_option(self):
        self.fake()
        self.run_new('elvis', '--dir=-weird')
        self.assertIn(('repo', 'clone', 'davidlarrimore/elvis', './-weird'), self.calls)

    def test_summary_when_not_cloned_or_not_named(self):
        text = magehand.new_summary('davidlarrimore/lab-website', 'lab-website', 'lab', pathlib.Path('lab-website'),
                                    'failed', 'failed')
        self.assertIn('1. run:   gh repo clone davidlarrimore/lab-website lab-website', text)
        self.assertIn('2. run:   cd lab-website', text)
        self.assertIn('3. edit:  make the first line of deploy/app.yaml `app: "lab"`', text)
        self.assertIn('https://lab-website-dev.', text)  # still the repo's name until it is fixed

    def test_nothing_is_touched_until_the_template_copy_has_landed(self):
        """The copy is asynchronous: a commit or a ruleset on main before it lands makes it fail."""
        saved, magehand.time.sleep = magehand.time.sleep, lambda _s: None
        self.addCleanup(setattr, magehand.time, 'sleep', saved)
        self.fake()
        inner = magehand.gh_cli
        polls = []

        def gh(*args, **kw):
            if args[:1] == ('api',) and args[1].endswith('/contents/AGENTS.md'):
                polls.append(args)
                return (1, '', 'HTTP 404') if len(polls) < 3 else (0, '', '')
            return inner(*args, **kw)
        magehand.gh_cli = gh
        self.run_new('elvis')
        order = [c for c in self.calls if c[0] == 'repo' and c[1] == 'create' or '-X' in c]
        self.assertEqual(len(polls), 3)  # polled until the files were there
        self.assertEqual(order[0][:2], ('repo', 'create'))

    def test_a_template_copy_that_never_lands_changes_nothing_more(self):
        saved, magehand.time.sleep = magehand.time.sleep, lambda _s: None
        self.addCleanup(setattr, magehand.time, 'sleep', saved)
        self.fake()
        inner = magehand.gh_cli
        magehand.gh_cli = lambda *a, **k: (1, '', 'HTTP 404') if a[:1] == ('api',) and '/contents/' in a[1] \
            else inner(*a, **k)
        with self.assertRaises(SystemExit) as ctx:
            self.run_new('elvis', '--app-name', 'lab')
        self.assertIn('has not finished', str(ctx.exception.code))
        self.assertFalse([c for c in self.calls if '-X' in c])  # no bot invite, no seed, no ruleset

    def test_an_unreadable_api_is_not_reported_as_a_missing_copy(self):
        """403/401/network on the probe says "could not check", never advice to delete or restore the repo."""
        saved, magehand.time.sleep = magehand.time.sleep, lambda _s: None
        self.addCleanup(setattr, magehand.time, 'sleep', saved)
        for exists, error in (('true', 'HTTP 403: API rate limit exceeded'), (None, 'HTTP 401: Bad credentials'),
                              ('true', 'dial tcp: lookup api.github.com: no such host')):
            self.calls.clear()
            self.fake(exists=exists, collaborator=True, ruleset=True)
            inner = magehand.gh_cli
            magehand.gh_cli = lambda *a, __e=error, __i=inner, **k: (1, '', __e) \
                if a[:1] == ('api',) and '/contents/' in a[1] else __i(*a, **k)
            with self.assertRaises(SystemExit) as ctx:
                self.run_new('elvis')
            message = str(ctx.exception.code)
            self.assertIn('could not check', message, error)
            self.assertIn(error, message)
            self.assertNotIn('gh repo delete', message)
            self.assertNotIn('has not finished', message)
            self.assertFalse([c for c in self.calls if '-X' in c])  # still no writes

    def test_an_existing_repo_without_the_template_files_is_reported(self):
        saved, magehand.time.sleep = magehand.time.sleep, lambda _s: None
        self.addCleanup(setattr, magehand.time, 'sleep', saved)
        self.fake(exists='true', collaborator=True, ruleset=True)
        inner = magehand.gh_cli
        magehand.gh_cli = lambda *a, **k: (1, '', 'HTTP 404') if a[:1] == ('api',) and '/contents/' in a[1] \
            else inner(*a, **k)
        with self.assertRaises(SystemExit) as ctx:
            self.run_new('elvis')
        self.assertIn('gh repo delete davidlarrimore/elvis', str(ctx.exception.code))

    def test_without_app_name_nothing_is_written(self):
        self.fake()
        writes = self.with_contents()
        self.run_new('elvis')
        self.assertEqual(writes, [])
        self.assertFalse([c for c in self.calls if any(a.endswith('/contents/deploy/app.yaml') for a in c)])

    def test_app_name_is_validated_like_a_name(self):
        self.fake()
        for argv in (('elvis', '--app-name', 'Lab'), ('elvis', '--app-name', 'litellm'),
                     ('elvis', '--app-name', 'lab-dev'), ('elvis', '--app-name', 'kube-x')):
            with self.assertRaises(SystemExit) as ctx:
                self.run_new(*argv)
            self.assertEqual(ctx.exception.code, 2, argv)
        self.assertEqual(self.calls, [])

    def test_bad_and_reserved_names(self):
        self.fake()
        for name in ('Elvis', '-x', 'a' * 41, 'litellm', 'homelab-apps'):
            with self.assertRaises(SystemExit) as ctx:
                self.run_new(name)
            self.assertEqual(ctx.exception.code, 2, name)
        self.assertEqual(self.calls, [])


class StayingCurrent(unittest.TestCase):
    def test_newest_tag(self):
        self.assertEqual(magehand.newest_tag(['v0.1.1', 'v0.10.0', 'v0.9.3', 'latest', 'v1.0']), '0.10.0')
        self.assertIsNone(magehand.newest_tag(['nightly']))

    def test_install_method(self):
        home = '/Users/x'
        self.assertEqual(magehand.install_method(f'{home}/.local/share/uv/tools/magehand', '/usr'), 'uv')
        self.assertEqual(magehand.install_method(f'{home}/.local/pipx/venvs/magehand', '/usr'), 'pipx')
        self.assertEqual(magehand.install_method('/opt/magehand', '/usr'), 'managed')
        self.assertEqual(magehand.install_method(f'{home}/src/magehand/.venv', '/usr'), 'venv')
        self.assertEqual(magehand.install_method('/usr', '/usr'), 'unknown')

    def test_latest_version_uses_a_fresh_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved, magehand.LATEST_CACHE = magehand.LATEST_CACHE, pathlib.Path(tmp) / 'latest.json'
            try:
                magehand.LATEST_CACHE.write_text(json.dumps({'checked': magehand.time.time(), 'version': '9.9.9'}))
                self.assertEqual(magehand.latest_version(), '9.9.9')  # no network: the cache is fresh
            finally:
                magehand.LATEST_CACHE = saved

    def test_notice_only_when_newer_and_interactive(self):
        import contextlib
        import io
        saved = (magehand.installed_version, magehand.latest_version, magehand.install_method)
        magehand.installed_version, magehand.latest_version = (lambda: '0.2.0'), (lambda: '0.3.0')
        magehand.install_method = lambda: 'uv'
        env = os.environ.pop('CI', None)
        quiet = os.environ.pop('MAGEHAND_NO_UPDATE_CHECK', None)

        class Tty(io.StringIO):
            def isatty(self):
                return True
        try:
            for stream, expect in ((Tty(), True), (io.StringIO(), False)):
                with contextlib.redirect_stderr(stream):
                    magehand.update_notice()
                self.assertEqual('magehand upgrade' in stream.getvalue(), expect)
            magehand.latest_version = lambda: '0.2.0'
            stream = Tty()
            with contextlib.redirect_stderr(stream):
                magehand.update_notice()
            self.assertEqual(stream.getvalue(), '')
        finally:
            magehand.installed_version, magehand.latest_version, magehand.install_method = saved
            if env is not None:
                os.environ['CI'] = env
            if quiet is not None:
                os.environ['MAGEHAND_NO_UPDATE_CHECK'] = quiet


class Runtimes(unittest.TestCase):
    def setUp(self):
        self.saved = (magehand.shutil.which, magehand.quiet, magehand.CONFIG)
        os.environ.pop('MAGEHAND_RUNTIME', None)
        self.tmp = tempfile.TemporaryDirectory()
        magehand.CONFIG = pathlib.Path(self.tmp.name) / 'config.json'

    def tearDown(self):
        magehand.shutil.which, magehand.quiet, magehand.CONFIG = self.saved
        self.tmp.cleanup()

    def fake(self, installed, answers):
        """installed: CLIs on PATH; answers: {(cli, subcommand): (returncode, stdout)}."""
        import types
        magehand.shutil.which = lambda name: f'/usr/bin/{name}' if name in installed else None

        def quiet(cmd, timeout=10):
            rc, out = answers.get((cmd[0], cmd[1]), (1, ''))
            return types.SimpleNamespace(returncode=rc, stdout=out)
        magehand.quiet = quiet

    def test_products(self):
        self.assertEqual(magehand.docker_product('orbstack'), 'OrbStack')
        self.assertEqual(magehand.docker_product('colima'), 'Colima')
        self.assertEqual(magehand.docker_product('rancher-desktop'), 'Rancher Desktop')
        self.assertEqual(magehand.docker_product('default', 'Docker Desktop'), 'Docker Desktop')
        self.assertEqual(magehand.docker_product('default', 'Ubuntu 24.04'), 'Docker')

    def test_detects_running_and_stopped(self):
        self.fake({'docker', 'podman'}, {('docker', 'context'): (0, 'orbstack\n'), ('docker', 'info'): (1, ''),
                                         ('podman', 'info'): (0, '5.4.0')})
        found = magehand.detect_runtimes()
        self.assertEqual([(f['cli'], f['product'], f['running']) for f in found],
                         [('docker', 'OrbStack', False), ('podman', 'Podman', True)])
        self.assertEqual(found[0]['hint'], 'start OrbStack')
        self.assertEqual(magehand.choose_runtime(None, found), 'podman')

    def test_nothing_installed_means_none(self):
        self.fake(set(), {})
        self.assertEqual(magehand.detect_runtimes(), [])
        self.assertIsNone(magehand.choose_runtime(None, []))

    def test_rancher_containerd_is_explained(self):
        self.fake({'nerdctl'}, {})
        found = magehand.detect_runtimes()
        self.assertIn('dockerd (moby)', found[0]['hint'])
        self.assertIsNone(magehand.choose_runtime(None, found))

    def test_configured_wins_and_none_turns_containers_off(self):
        found = [{'cli': 'docker', 'product': 'Docker', 'running': True, 'hint': ''}]
        self.assertEqual(magehand.choose_runtime('podman', found), 'podman')
        self.assertIsNone(magehand.choose_runtime('none', found))

    def test_use_and_auto(self):
        import contextlib
        import io
        self.fake({'podman'}, {})
        with contextlib.redirect_stdout(io.StringIO()):
            magehand.main(['runtime', 'podman'])  # the way people type it
            self.assertEqual(magehand.configured_runtime(), 'podman')
            magehand.main(['runtime', 'use', 'none'])  # 0.4.0's form still works
            self.assertEqual(magehand.configured_runtime(), 'none')
            with contextlib.redirect_stderr(io.StringIO()):
                for bad, code in ((['docker'], 'magehand: docker is not installed'), (['use', 'docker'], 'magehand: docker'),
                                  (['kubernetes'], 2), (['podman', 'extra'], 2)):
                    with self.assertRaises(SystemExit) as raised:
                        magehand.main(['runtime', *bad])  # docker isn't installed in this fake
                    if isinstance(code, int):
                        self.assertEqual(raised.exception.code, code)
                    else:
                        self.assertTrue(str(raised.exception.code).startswith(code))
            self.assertEqual(magehand.configured_runtime(), 'none')
            magehand.main(['runtime', 'auto'])
        self.assertIsNone(magehand.configured_runtime())

    def test_env_overrides_and_is_validated(self):
        os.environ['MAGEHAND_RUNTIME'] = 'kubernetes'
        try:
            with self.assertRaises(SystemExit):
                magehand.configured_runtime()
            os.environ['MAGEHAND_RUNTIME'] = 'docker'
            self.assertEqual(magehand.configured_runtime(), 'docker')
        finally:
            os.environ.pop('MAGEHAND_RUNTIME', None)


class Agents(unittest.TestCase):
    TEMPLATE = '# Rules\n\nnew platform rules\n\n## This app\n\n(describe it)\n'

    def run_agents(self, current, *argv):
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'AGENTS.md'
            if current is not None:
                path.write_text(current)
            saved = magehand.github_file
            magehand.github_file = lambda repo, name, required=True: self.TEMPLATE
            code = 0
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    try:
                        magehand.main(['update-agents', '--code', tmp, *argv])
                    except SystemExit as exit_:
                        code = exit_.code if isinstance(exit_.code, int) else 1
            finally:
                magehand.github_file = saved
            return code, path.read_text() if path.is_file() else None

    def test_split(self):
        self.assertEqual(magehand.split_agents('a\n## This app\nb\n'), ('a\n', '## This app\nb\n'))
        self.assertEqual(magehand.split_agents('a\n## This application\n'), ('a\n## This application\n', None))

    def test_replaces_the_platform_part_and_keeps_the_apps_notes(self):
        code, text = self.run_agents('# Rules\n\nold rules\n\n## This app\n\nrun pytest\n')
        self.assertEqual((code, text), (0, '# Rules\n\nnew platform rules\n\n## This app\n\nrun pytest\n'))

    def test_up_to_date_changes_nothing(self):
        self.assertEqual(self.run_agents(self.TEMPLATE), (0, self.TEMPLATE))

    def test_check_reports_without_writing(self):
        old = '# Rules\n\nold\n\n## This app\n\nx\n'
        self.assertEqual(self.run_agents(old, '--check'), (1, old))
        self.assertEqual(self.run_agents(self.TEMPLATE, '--check'), (0, self.TEMPLATE))

    def test_no_heading_is_refused_untouched(self):
        self.assertEqual(self.run_agents('# Rules\n\nold\n'), (1, '# Rules\n\nold\n'))

    def test_a_repo_without_the_file_gets_the_template(self):
        self.assertEqual(self.run_agents(None), (0, self.TEMPLATE))


BOT_PATH = f"/collaborators/{magehand.BOT}"


class SkillProcess(unittest.TestCase):
    def test_the_skill_teaches_the_review_and_agents_process(self):
        for text in ('gh pr edit N --add-reviewer davidlarrimore-bot', 'magehand update-agents', 'gh pr create --draft', 'gh pr ready N',
                     'Base every PR on `main`'):
            self.assertIn(text, magehand.SKILL)


class RepoConformance(unittest.TestCase):
    """check warns when AGENTS.md has fallen behind the template; doctor reports a repo's ruleset and bot access."""
    TEMPLATE = '# Rules\n\nnew\n\n## This app\n\n(describe it)\n'

    def repo(self, agents, app_repo=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        if app_repo:
            (root / 'ui-check.yaml').write_text('web_ui: false\n')
        if agents is not None:
            (root / 'AGENTS.md').write_text(agents)
        return root

    def drift(self, root, template=TEMPLATE):
        saved = magehand.github_file
        magehand.github_file = lambda repo, path, required=True: template
        try:
            return magehand.check_agents_drift(root)
        finally:
            magehand.github_file = saved

    def test_up_to_date_is_quiet_whatever_the_apps_notes_say(self):
        self.assertEqual(self.drift(self.repo('# Rules\n\nnew\n\n## This app\n\nrun pytest\n')), [])

    def test_out_of_date_warns_and_names_the_fix(self):
        found = self.drift(self.repo('# Rules\n\nold\n\n## This app\n\nx\n'))
        self.assertEqual([f['level'] for f in found], ['warning'])
        self.assertIn('magehand update-agents', found[0]['text'])

    def test_no_heading_warns(self):
        self.assertIn('no "## This app"', self.drift(self.repo('# Rules\n\nold\n'))[0]['text'])

    def test_skipped_outside_an_app_repo_without_the_file_or_without_the_template(self):
        self.assertEqual(self.drift(self.repo('old', app_repo=False)), [])
        self.assertEqual(self.drift(self.repo(None)), [])
        saved = magehand.github_file
        magehand.github_file = lambda *a, **k: None  # offline, or no access to the template
        try:
            self.assertEqual(magehand.check_agents_drift(self.repo('old')), [])
        finally:
            magehand.github_file = saved

    def test_current_repo_from_a_subdirectory(self):
        root = self.repo(None)
        sub = root / 'src' / 'pkg'
        sub.mkdir(parents=True)
        subprocess = magehand.subprocess
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        subprocess.run(['git', '-C', str(root), 'remote', 'add', 'origin', 'git@github.com:davidlarrimore/elvis.git'], check=True)
        self.assertEqual(magehand.current_repo(root), 'davidlarrimore/elvis')
        self.assertEqual(magehand.current_repo(sub), 'davidlarrimore/elvis')  # not only from the repo root
        self.assertIsNone(magehand.current_repo(tempfile.gettempdir()))

    def with_gh(self, answers):
        """Run repo_state with gh answering from `answers` {path suffix: (code, out, err)}."""
        saved = magehand.gh_cli

        def gh(*args, **kw):
            for suffix, answer in answers.items():
                if args[1].split('?')[0] == 'repos/o/r' + suffix:
                    return answer(args) if callable(answer) else answer
            raise AssertionError(args)
        magehand.gh_cli = gh
        self.addCleanup(setattr, magehand, 'gh_cli', saved)
        return magehand.repo_state('o/r')

    GOOD = dict(magehand.RULESET, id=7)
    LISTED = (0, json.dumps([{'id': 7, 'name': 'main: owner approves'}]), '')
    APPLIED = (0, json.dumps([{'type': 'pull_request', 'ruleset_id': 7}]), '')  # rules GitHub applies to main

    def answers(self, detail=None, applied=APPLIED, bot=(0, '', '')):
        return {'/rulesets': self.LISTED, '/rulesets/7': (0, json.dumps(detail or self.GOOD), ''), '': (0, 'main\n', ''),
                '/rules/branches/main': applied, BOT_PATH: bot}

    def test_repo_state(self):
        ok = self.with_gh(self.answers())
        self.assertEqual((ok['ruleset'], ok['bot']), ('ok', True))
        none = self.with_gh({'/rulesets': (0, '[]', ''), BOT_PATH: (1, '', 'gh: Not Found (HTTP 404)')})
        self.assertEqual((none['ruleset'], none['bot']), ('missing', False))

    def test_a_ruleset_that_would_not_pause_a_merge_is_weak(self):
        for weak in (dict(self.GOOD, enforcement='disabled'), dict(self.GOOD, enforcement='evaluate'),
                     dict(self.GOOD, conditions={'ref_name': {'include': ['refs/heads/dev']}}),
                     dict(self.GOOD, rules=[]),
                     dict(self.GOOD, rules=[{'type': 'pull_request', 'parameters': {'required_approving_review_count': 0}}])):
            on_dev = weak['conditions']['ref_name']['include'] == ['refs/heads/dev']
            state = self.with_gh(self.answers(detail=weak, applied=(0, '[]', '') if on_dev else self.APPLIED))
            self.assertEqual(state['ruleset'], 'weak', weak)
            self.assertTrue(state['ruleset_detail'])

    def test_what_github_applies_to_the_default_branch_decides_coverage(self):
        # ~ALL but excluding main: GitHub applies nothing to main, whatever the patterns look like.
        excluded = dict(self.GOOD, conditions={'ref_name': {'include': ['~ALL'], 'exclude': ['refs/heads/main']}})
        state = self.with_gh(self.answers(detail=excluded, applied=(0, '[]', '')))
        self.assertEqual(state['ruleset'], 'weak')
        self.assertIn('default branch (main)', state['ruleset_detail'])
        # An explicit refs/heads/main include is as good as ~DEFAULT_BRANCH.
        explicit = dict(self.GOOD, conditions={'ref_name': {'include': ['refs/heads/main'], 'exclude': []}})
        self.assertEqual(self.with_gh(self.answers(detail=explicit))['ruleset'], 'ok')
        # A rule from some other ruleset does not count.
        other = (0, json.dumps([{'type': 'pull_request', 'ruleset_id': 99}]), '')
        self.assertEqual(self.with_gh(self.answers(applied=other))['ruleset'], 'weak')
        # Our rule on a later page of GitHub's answer still counts; a page that fails is unknown, not weak.
        others = [{'type': 'pull_request', 'ruleset_id': 99}] * 100

        def pages(last):
            def answer(args):
                return (0, json.dumps(others), '') if args[1].endswith('page=1') else last
            return answer
        self.assertEqual(self.with_gh(self.answers(applied=pages(self.APPLIED)))['ruleset'], 'ok')
        self.assertIsNone(self.with_gh(self.answers(applied=pages((1, '', 'HTTP 502'))))['ruleset'])
        # Unreadable effective rules are unknown, not weak.
        self.assertIsNone(self.with_gh(self.answers(applied=(1, '', 'HTTP 500')))['ruleset'])

    def test_failures_are_unknown_not_missing(self):
        for code, err in ((1, 'HTTP 403: Resource not accessible'), (1, 'HTTP 500'), (1, 'dial tcp: connection refused'),
                          (1, 'HTTP 403: API rate limit exceeded')):
            state = self.with_gh({'/rulesets': (code, '', err), BOT_PATH: (code, '', err)})
            self.assertEqual((state['ruleset'], state['bot']), (None, None), err)

    def test_repo_state_without_gh_is_unknown_not_a_crash(self):
        saved = magehand.gh_cli

        def no_gh(*args, **kw):
            sys.exit('magehand: needs the GitHub CLI')
        magehand.gh_cli = no_gh
        try:
            self.assertEqual(magehand.repo_state('o/r')['bot'], None)
            magehand.gh_cli = lambda *a, **k: (_ for _ in ()).throw(magehand.subprocess.TimeoutExpired('gh', 60))
            self.assertEqual(magehand.repo_state('o/r')['ruleset'], None)
        finally:
            magehand.gh_cli = saved

    def test_doctor_json_survives_a_missing_gh_without_touching_the_machine(self):
        # No OpenBao request, token lookup or container runtime: all mocked.
        import contextlib
        import io
        names = ('gh_cli', 'current_repo', 'github_token', 'github_file', 'saved_token', 'configured_runtime',
                 'choose_runtime')
        saved = {n: getattr(magehand, n) for n in names}
        saved_urlopen = magehand.urllib.request.urlopen
        calls = []

        def no_network(*a, **k):
            calls.append(a)
            raise OSError('no network in tests')
        magehand.current_repo = lambda *a: 'davidlarrimore/elvis'
        magehand.github_token = lambda: 't'
        magehand.github_file = lambda *a, **k: 'x'
        magehand.saved_token = lambda: None
        magehand.configured_runtime = lambda: 'none'
        magehand.choose_runtime = lambda configured, found: None
        magehand.gh_cli = lambda *a, **k: sys.exit('magehand: needs the GitHub CLI')
        magehand.urllib.request.urlopen = no_network
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
                magehand.cmd_doctor(magehand.build_parser().parse_args(['doctor', '--json']))
        finally:
            for n, f in saved.items():
                setattr(magehand, n, f)
            magehand.urllib.request.urlopen = saved_urlopen
        report = json.loads(out.getvalue())
        self.assertEqual([c['ok'] for c in report['checks'] if 'elvis' in c['check']], [None, None])
        self.assertEqual(len(calls), 1)  # only the (mocked, failing) OpenBao health probe was attempted


class CommandLine(unittest.TestCase):
    """The conventions in AGENTS.md "CLI conventions"."""

    def run_main(self, *argv):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                magehand.main(list(argv))
            except SystemExit as exit_:
                code = exit_.code if isinstance(exit_.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def test_every_command_has_help(self):
        magehand.build_parser()
        for name in magehand.SUBPARSERS:
            code, out, _ = self.run_main(name, '--help')
            self.assertEqual(code, 0, name)
            self.assertIn(f'usage: magehand {name}', out)

    def test_no_arguments_shows_help(self):
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        for name in magehand.SUBPARSERS:  # every command is listed in a group
            self.assertIn(f'\n  {name} ', out)

    def test_unknown_command_suggests_on_stderr_with_exit_2(self):
        code, out, err = self.run_main('runtme')
        self.assertEqual((code, out), (2, ''))
        self.assertIn("did you mean 'runtime'?", err)

    def test_bad_choice_suggests(self):
        code, _, err = self.run_main('runtime', 'podmn')
        self.assertEqual(code, 2)
        self.assertIn("did you mean 'podman'?", err)

    def test_usage_error_inside_a_command(self):
        saved = magehand.current_app
        magehand.current_app = lambda: 'demo'
        try:
            code, _, err = self.run_main('run')
        finally:
            magehand.current_app = saved
        self.assertEqual(code, 2)
        self.assertIn('usage: magehand run', err)
        self.assertIn('give the command after --', err)

    def test_help_command(self):
        self.assertEqual(self.run_main('help', 'check')[1].splitlines()[0][:20], 'usage: magehand chec')
        code, _, err = self.run_main('help', 'chek')
        self.assertEqual(code, 2)
        self.assertIn("did you mean 'check'?", err)

    def test_version_flag(self):
        code, out, _ = self.run_main('-V')
        self.assertEqual(code, 0)
        self.assertTrue(out.strip())

    def test_app_flag_and_positional_name_the_same_app(self):
        seen = []
        saved = magehand.load_app
        magehand.load_app = lambda name, root=None: seen.append(name) or magehand.die('stop')
        try:
            self.run_main('app', 'one')
            self.run_main('app', '-a', 'two')
            self.run_main('dev', 'up', '--app', 'three')
            self.run_main('run', '-a', 'four', '--', 'true')
        finally:
            magehand.load_app = saved
        self.assertEqual(seen, ['one', 'two', 'three', 'four'])

    def test_run_passes_the_command_through(self):
        parser = magehand.build_parser()
        a = parser.parse_args(['run', '--no-proxy', '--', 'python', '-m', 'http.server', '--bind', '127.0.0.1'])
        self.assertEqual(a.cmd, ['--', 'python', '-m', 'http.server', '--bind', '127.0.0.1'])
        self.assertTrue(a.no_proxy)

    def test_runtime_flag_wins(self):
        saved = magehand.cmd_dev
        seen = []
        magehand.cmd_dev = lambda a: seen.append(os.environ.get('MAGEHAND_RUNTIME'))
        try:
            self.run_main('dev', 'status', '--runtime', 'podman')
        finally:
            magehand.cmd_dev = saved
            os.environ.pop('MAGEHAND_RUNTIME', None)
        self.assertEqual(seen, ['podman'])

    def test_check_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            pathlib.Path(tmp, 'app.py').write_text('URL = "https://api.openai.com"\n')
            saved = magehand.try_load_app, magehand.github_file
            magehand.try_load_app = lambda name, root=None: None
            magehand.github_file = lambda repo, path, required=True: None
            try:
                code, out, _ = self.run_main('check', 'demo', '--code', tmp, '--json')
            finally:
                magehand.try_load_app, magehand.github_file = saved
        result = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(result['app'], 'demo')
        self.assertEqual(result['errors'], 3)  # the provider call, no Dockerfile, no ui-check.yaml

    def test_ctrl_c_exits_130(self):
        saved = magehand.cmd_version
        magehand.cmd_version = lambda a: (_ for _ in ()).throw(KeyboardInterrupt())
        try:
            self.assertEqual(self.run_main('version')[0], 130)
        finally:
            magehand.cmd_version = saved


class Release(unittest.TestCase):
    """`release` picks the next semver version from what merged, and refuses what would not deploy."""

    def test_classify_change(self):
        c = magehand.classify_change
        self.assertEqual(c('feat: add export (#8)'), ('feature', 8, 'feat: add export'))
        self.assertEqual(c('fix(api): stop the crash (#9)')[0], 'fix')
        self.assertEqual(c('feat!: drop the v1 route (#10)')[0], 'breaking')
        self.assertEqual(c('Rework storage (#11)', 'BREAKING CHANGE: tables renamed')[0], 'breaking')
        self.assertEqual(c('Add a thing (#12)', labels=['enhancement'])[0], 'feature')
        self.assertEqual(c('Tidy (#13)', labels=['Breaking'])[0], 'breaking')
        self.assertEqual(c('docs: wording')[0:2], ('other', None))
        self.assertEqual(c('Merge pull request #14 from x/y', 'fix: thing\n')[0:2], ('fix', 14))

    def test_next_version_and_first_release(self):
        self.assertEqual(magehand.next_version('v1.2.3', 'patch'), 'v1.2.4')
        self.assertEqual(magehand.next_version('v1.2.3', 'minor'), 'v1.3.0')
        self.assertEqual(magehand.next_version('v1.2.3', 'major'), 'v2.0.0')
        self.assertEqual(magehand.next_version('v2', 'minor'), 'v2.1.0')
        self.assertEqual(magehand.next_version(None, 'patch'), 'v1.0.0')

    def test_choose_bump_and_override(self):
        fix, feat, brk = ('fix', 1, 'fix: a'), ('feature', 2, 'feat: b'), ('breaking', 3, 'feat!: c')
        self.assertEqual(magehand.choose_bump([fix], [], [])[0], 'patch')
        self.assertEqual(magehand.choose_bump([fix, feat], [], [])[0], 'minor')
        self.assertEqual(magehand.choose_bump([fix, feat, brk], [], [])[0], 'major')
        self.assertEqual(magehand.choose_bump([fix], [], ['block cache (redis)'])[0], 'minor')
        bump, why = magehand.choose_bump([fix], ['block db (postgres)'], [])
        self.assertEqual(bump, 'major')
        self.assertIn('block db', why)
        bump, why = magehand.choose_bump([feat], [], [], forced='patch')
        self.assertEqual(bump, 'patch')
        self.assertIn('as asked', why)
        # nothing marks the changes: a floor, said so
        bump, why = magehand.choose_bump([('other', 6, 'Design scenarios')], [], [])
        self.assertEqual((bump, 'a guess' in why), ('patch', True))

    def test_spec_changes_reads_blocks_and_models(self):
        old = {'services': {'db': {'type': 'postgres'}, 'ai': {'type': 'llm', 'models': ['small']}}}
        new = {'services': {'ai': {'type': 'llm', 'models': ['small', 'large']}, 'cache': {'type': 'redis'}}}
        removed, added = magehand.spec_changes(old, new)
        self.assertEqual(removed, ['block db (postgres)'])
        self.assertEqual(added, ['block cache (redis)', 'model large of ai'])
        self.assertEqual(magehand.spec_changes(None, {}), ([], []))

    def test_release_notes_group_and_link(self):
        notes = magehand.release_notes('davidlarrimore/app', 'v1.0.0', 'v1.1.0', [
            ('feature', 8, 'feat: add export'), ('fix', None, 'fix: crash'), ('breaking', 10, 'feat!: drop v1')])
        self.assertLess(notes.index('Breaking changes'), notes.index('Features'))
        self.assertLess(notes.index('Features'), notes.index('Fixes'))
        self.assertIn('[#8](https://github.com/davidlarrimore/app/pull/8)', notes)
        self.assertIn('compare/v1.0.0...v1.1.0', notes)

    APP = 'repos/davidlarrimore/app/'
    HEAD = 'a' * 40

    def fake_gh(self, **overrides):
        """gh_query/gh_probe/collect_status for plan_release over one app, two merged PRs (#8, #9) since v1.0.0.
        Commits are c8 and c9; a commit's PRs, a PR's reviews and the checks default to a green, approved state."""
        app, head = self.APP, self.HEAD
        approved = lambda n: [{'user': {'login': magehand.BOT}, 'state': 'APPROVED', 'commit_id': f'h{n}'}]  # noqa: E731
        data = {
            app + 'commits/main': {'sha': head, 'commit': {'message': 'x'}},
            'user': {'login': magehand.OWNER},
            app + 'releases?per_page=20': [
                {'tag_name': 'v1.0.0', 'draft': False, 'prerelease': False, 'author': {'login': magehand.OWNER}}],
            app + f'compare/v1.0.0...{head}?per_page=100&page=1': {'ahead_by': 2, 'commits': [
                {'sha': 'c8', 'commit': {'message': 'feat: add export (#8)\n\nbody'}},
                {'sha': 'c9', 'commit': {'message': 'fix: crash (#9)'}}]},
            app + 'git/ref/tags/v1.1.0': None,
            app + f'commits/{head}/check-runs?per_page=100&page=1': {'total_count': 2, 'check_runs': [
                {'name': 'security / scan', 'status': 'completed', 'conclusion': 'success'},
                {'name': 'image', 'status': 'completed', 'conclusion': 'skipped'}]},
            app + f'actions/runs?head_sha={head}&per_page=100': {'workflow_runs': [
                {'path': magehand.BUILD_WORKFLOW, 'event': 'push', 'status': 'completed', 'conclusion': 'success'}]},
        }
        data.update(overrides)

        def query(*args):
            path = args[1]
            value = data.get(path)
            m = re.match(app + r'pulls/(\d+)(/reviews\?per_page=100&page=1)?$', path)
            if path not in data and m:
                n = m.group(1)
                value = approved(n) if m.group(2) else {'labels': [], 'head': {'sha': f'h{n}'}}
            m = re.match(app + r'commits/c(\d+)/pulls$', path)
            if path not in data and m:  # GitHub's own commit-to-PR association
                value = [{'number': int(m.group(1)), 'merged_at': 'x', 'base': {'ref': 'main'}}]
            return (None, 'HTTP 404') if value is None else (value, None)

        files = {'homelab/contents/platform/prod-apps/apps.yaml': 'lab:\n  exposure: internal\n',
                 'app/contents/deploy/prod': '[]', 'app/contents/deploy/app.yaml': 'services: {}\n'}
        files.update(overrides.get('files', {}))

        def probe(*args):
            path = args[1].split('?')[0].replace('repos/', '', 1).replace('davidlarrimore/', '')
            content = files.get(path)
            if content is None:
                return 1, '', 'HTTP 404: Not Found'
            if content.startswith('['):
                return 0, json.dumps([{'name': 'kustomization.yaml'}]), ''
            import base64
            return 0, json.dumps({'content': base64.b64encode(content.encode()).decode()}), ''
        stages = [{'stage': 'build', 'state': 'ok', 'text': 'built'}, {'stage': 'deploy', 'state': 'ok', 'text': 'merged'},
                  {'stage': 'dev', 'state': 'ok', 'text': 'running'}]
        return mock.patch.multiple(magehand, gh_query=query, gh_probe=probe,
                                   collect_status=lambda *a, **k: {'stages': overrides.get('stages', stages)})

    def plan(self, **overrides):
        with self.fake_gh(**overrides):
            return magehand.plan_release('davidlarrimore/app', 'lab')

    def test_plan_picks_minor_and_has_no_problems_when_everything_is_ready(self):
        plan = self.plan()
        self.assertEqual((plan['version'], plan['bump'], plan['problems']), ('v1.1.0', 'minor', []))
        self.assertIn('feat: add export', plan['notes'])

    def test_plan_is_about_one_commit_even_if_main_moves(self):
        seen = []
        moved = iter([{'sha': self.HEAD, 'commit': {'message': 'x'}}, {'sha': 'b' * 40, 'commit': {'message': 'y'}}])
        with self.fake_gh():
            real = magehand.gh_query

            def query(*args):
                if args[1] == self.APP + 'commits/main':
                    return next(moved), None  # a second read of main would see B
                return real(*args)
            with mock.patch.object(magehand, 'gh_query', query), mock.patch.object(
                    magehand, 'collect_status',
                    lambda repo, name, cluster=True, ref='main': seen.append(ref) or {'stages': [
                        {'stage': 'build', 'state': 'ok', 'text': ''}, {'stage': 'deploy', 'state': 'ok', 'text': ''},
                        {'stage': 'dev', 'state': 'ok', 'text': ''}]}):
                plan = magehand.plan_release('davidlarrimore/app', 'lab')
        self.assertEqual((plan['commit'], seen), (self.HEAD, [self.HEAD]))

    def test_plan_refuses_what_would_not_deploy(self):
        failing = {'total_count': 2, 'check_runs': [{'name': 'scan', 'status': 'completed', 'conclusion': 'failure'},
                                                    {'name': 'build', 'status': 'in_progress', 'conclusion': None}]}
        stages = [{'stage': 'build', 'state': 'pending', 'text': 'building'},
                  {'stage': 'deploy', 'state': 'pending', 'text': 'no deploy PR'},
                  {'stage': 'dev', 'state': 'pending', 'text': 'rolling out'}]
        plan = self.plan(stages=stages, files={'homelab/contents/platform/prod-apps/apps.yaml': 'other:\n  exposure: internal\n',
                                               'app/contents/deploy/prod': None},
                         **{self.APP + f'commits/{self.HEAD}/check-runs?per_page=100&page=1': failing,
                            'user': {'login': 'someone-else'}})
        problems = ' | '.join(plan['problems'])
        for text in ('signed in as someone-else', 'not approved for production', 'deploy/prod/ is missing',
                     'failing checks: scan', 'still has checks running: build', 'build of', 'deploy of',
                     'dev is not running'):
            self.assertIn(text, problems)

    def test_approval_is_read_from_the_register_or_the_old_per_app_file(self):
        self.assertEqual(self.plan()['problems'], [])  # in the register
        old = {'homelab/contents/platform/prod-apps/apps.yaml': None,
               'homelab/contents/platform/prod-apps/apps/lab.yaml': 'exposure: internal\n'}
        self.assertEqual(self.plan(files=old)['problems'], [])
        self.assertTrue(any('not approved' in p for p in self.plan(files={
            'homelab/contents/platform/prod-apps/apps.yaml': 'other: {exposure: internal}\n'})['problems']))

    def test_review_verdict(self):
        v = magehand.review_verdict
        who = {magehand.OWNER, magehand.BOT}
        ok = [{'user': {'login': magehand.BOT}, 'state': 'APPROVED', 'commit_id': 'h'}]
        self.assertIsNone(v(5, 'h', ok, who))
        self.assertIn('no approving review', v(5, 'h', [], who))
        self.assertIn('no approving review', v(5, 'new', ok, who))  # approval of an older push
        stranger = [{'user': {'login': 'someone'}, 'state': 'APPROVED', 'commit_id': 'h'}]
        self.assertIn('no approving review', v(5, 'h', stranger, who))
        asked = ok + [{'user': {'login': magehand.OWNER}, 'state': 'CHANGES_REQUESTED', 'commit_id': 'h'}]
        self.assertIn('changes requested by ' + magehand.OWNER, v(5, 'h', asked, who))
        # a later approval from the same reviewer replaces their change request
        fixed = asked + [{'user': {'login': magehand.OWNER}, 'state': 'APPROVED', 'commit_id': 'h'}]
        self.assertIsNone(v(5, 'h', fixed, who))

    def test_the_prs_of_a_commit_come_from_github_not_its_message(self):
        # a direct push that merely says (#8), and a PR that was closed or aimed at another branch
        subject = 'unreviewed direct push (#8)'
        compare = {'ahead_by': 3, 'commits': [
            {'sha': 'd1', 'commit': {'message': subject}},
            {'sha': 'd2', 'commit': {'message': 'Merge pull request #8 from x/y'}},
            {'sha': 'd3', 'commit': {'message': 'work (#8)'}}]}
        plan = self.plan(**{
            self.APP + f'compare/v1.0.0...{self.HEAD}?per_page=100&page=1': compare,
            self.APP + 'commits/d1/pulls': [],
            self.APP + 'commits/d2/pulls': [{'number': 8, 'merged_at': None, 'base': {'ref': 'main'}}],
            self.APP + 'commits/d3/pulls': [{'number': 8, 'merged_at': 'x', 'base': {'ref': 'staging'}}]})
        straight = [p for p in plan['problems'] if 'pushed straight to main' in p]
        self.assertEqual(len(straight), 3)
        self.assertIn('unreviewed direct push (#8)', straight[0])

    def test_reviews_are_read_across_pages(self):
        key = self.APP + 'pulls/8/reviews?per_page=100&page='
        ok = {'user': {'login': magehand.BOT}, 'state': 'APPROVED', 'commit_id': 'h8'}
        filler = [{'user': {'login': f'u{i}'}, 'state': 'COMMENTED', 'commit_id': 'h8'} for i in range(99)]
        # approval first, a change request on page 2: refused
        asked = {'user': {'login': magehand.OWNER}, 'state': 'CHANGES_REQUESTED', 'commit_id': 'h8'}
        plan = self.plan(**{key + '1': [ok] + filler, key + '2': [asked]})
        self.assertTrue(any('changes requested' in p for p in plan['problems']))
        # the approval only on page 2: accepted
        plan = self.plan(**{key + '1': filler + [filler[0]], key + '2': [ok]})
        self.assertEqual(plan['problems'], [])
        # a page that can't be read refuses
        plan = self.plan(**{key + '1': [ok] + filler, key + '2': None})
        self.assertTrue(any('could not read the reviews of PR #8' in p for p in plan['problems']))

    def first_release(self, commits, **extra):
        """A repo with no release yet whose history (newest first) is `commits`, over two pages."""
        return self.plan(**{self.APP + 'releases?per_page=20': [],
                            self.APP + f'commits?sha={self.HEAD}&per_page=100&page=1': commits[:100],
                            self.APP + f'commits?sha={self.HEAD}&per_page=100&page=2': commits[100:],
                            **extra})

    def test_first_release_reads_the_whole_history_and_forgives_only_the_bootstrap(self):
        def commit(i, pr=True):
            return {'sha': f'c{i}' if pr else f'x{i}', 'commit': {'message': f'fix: n{i} (#{i})'}}
        # the bootstrap: two direct commits older than the first PR (page 2): a warning, not a refusal
        plan = self.first_release([commit(8), commit(9), commit(0, pr=False), commit(-1, pr=False)],
                                  **{self.APP + 'commits/x0/pulls': [], self.APP + 'commits/x-1/pulls': []})
        self.assertEqual([p for p in plan['problems'] if 'straight' in p], [])
        self.assertTrue(any('before the first PR' in w for w in plan['warnings']))
        # a direct push newer than a reviewed PR refuses, even on the second page
        many = [commit(8), commit(9)] + [commit(9)] * 98 + [commit(0, pr=False), commit(8)]
        plan = self.first_release(many, **{self.APP + 'commits/x0/pulls': []})
        self.assertTrue(any('pushed straight to main' in p for p in plan['problems']))
        # nothing was ever reviewed: nothing to forgive
        plan = self.first_release([commit(0, pr=False)], **{self.APP + 'commits/x0/pulls': []})
        self.assertTrue(any('pushed straight to main' in p for p in plan['problems']))

    def test_main_needs_its_security_scan_to_have_run_and_passed(self):
        runs = self.APP + f'actions/runs?head_sha={self.HEAD}&per_page=100'
        checks = self.APP + f'commits/{self.HEAD}/check-runs?per_page=100&page=1'
        self.assertTrue(any('no checks ran' in p for p in self.plan(**{checks: {'total_count': 0, 'check_runs': []}})['problems']))
        self.assertTrue(any('did not run' in p for p in self.plan(**{runs: {'workflow_runs': []}})['problems']))
        failed = {'workflow_runs': [{'path': magehand.BUILD_WORKFLOW, 'event': 'push', 'status': 'completed',
                                     'conclusion': 'failure'}]}
        self.assertTrue(any('has not passed' in p for p in self.plan(**{runs: failed})['problems']))
        # skipped jobs (the image build is skipped on a push) are fine when the scan itself passed
        self.assertEqual(self.plan()['problems'], [])

    def test_an_incomplete_list_of_checks_refuses(self):
        key = self.APP + f'commits/{self.HEAD}/check-runs?per_page=100&page='
        ok = lambda n: {'name': f'c{n}', 'status': 'completed', 'conclusion': 'success'}  # noqa: E731
        # 1,001 checks advertised, 1,000 readable in the ten pages
        pages = {key + str(p): {'total_count': 1001, 'check_runs': [ok(p * 1000 + i) for i in range(100)]}
                 for p in range(1, 11)}
        problems = self.plan(**pages)['problems']
        self.assertTrue(any('could only read 1000 of the 1001 checks' in p for p in problems))
        # an empty page before the advertised total
        problems = self.plan(**{key + '1': {'total_count': 150, 'check_runs': [ok(i) for i in range(100)]},
                                key + '2': {'total_count': 150, 'check_runs': []}})['problems']
        self.assertTrue(any('could only read 100 of the 150 checks' in p for p in problems))
        # two full pages that add up: fine
        both = {key + '1': {'total_count': 101, 'check_runs': [ok(i) for i in range(100)]},
                key + '2': {'total_count': 101, 'check_runs': [ok(100)]}}
        self.assertEqual(self.plan(**both)['problems'], [])

    def test_plan_nothing_new_and_existing_tag(self):
        key = self.APP + f'compare/v1.0.0...{self.HEAD}?per_page=100&page=1'
        self.assertIn('nothing new on main since v1.0.0', self.plan(**{key: {'ahead_by': 0, 'commits': []}})['problems'])
        self.assertTrue(any('already exists' in p
                            for p in self.plan(**{self.APP + 'git/ref/tags/v1.1.0': {'ref': 'x'}})['problems']))

    def test_unreadable_specs_and_labels_refuse_instead_of_guessing(self):
        problems = self.plan(files={'app/contents/deploy/app.yaml': None})['problems']  # absent on main: required
        self.assertTrue(any('deploy/app.yaml is missing' in p for p in problems))
        problems = self.plan(**{self.APP + 'pulls/8': None})['problems']
        self.assertTrue(any('could not read PR #8' in p for p in problems))
        problems = self.plan(files={'app/contents/deploy/app.yaml': 'a: [\n'})['problems']
        self.assertTrue(any('not valid YAML' in p for p in problems))

    def test_comparison_is_read_across_pages_and_refused_when_incomplete(self):
        page = lambda items: {'ahead_by': 101, 'commits': items}  # noqa: E731
        first = [{'sha': 'c8', 'commit': {'message': f'fix: n{i} (#8)'}} for i in range(100)]
        second = [{'sha': 'c9', 'commit': {'message': 'feat!: drop the v1 route (#9)'}}]
        key = self.APP + f'compare/v1.0.0...{self.HEAD}?per_page=100&page='
        plan = self.plan(**{key + '1': page(first), key + '2': page(second)})
        self.assertEqual((plan['bump'], plan['commits'], len(plan['changes'])), ('major', 101, 101))
        self.assertEqual([p for p in plan['problems'] if 'commits' in p], [])
        problems = self.plan(**{key + '1': page(first), key + '2': None})['problems']  # page 2 unreadable
        self.assertTrue(any('could not compare' in p for p in problems))

    def test_unreadable_dev_pods_only_warn(self):
        stages = [{'stage': 'build', 'state': 'ok', 'text': ''}, {'stage': 'deploy', 'state': 'ok', 'text': ''},
                  {'stage': 'dev', 'state': 'unknown', 'text': 'not checked'}]
        with self.fake_gh(stages=stages):
            plan = magehand.plan_release('davidlarrimore/app', 'lab')
        self.assertEqual(plan['problems'], [])
        self.assertEqual(len(plan['warnings']), 1)

    def test_command_creates_when_every_check_passes_and_targets_the_checked_commit(self):
        import contextlib
        import io
        created = []
        app = {'name': 'lab', 'spec': {'source': {'repo': 'app'}}, 'root': None}

        def run(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                try:
                    magehand.main(['release', 'lab', *argv])
                    return 0, out.getvalue()
                except SystemExit as exit_:
                    return exit_.code, out.getvalue()
        with self.fake_gh(), mock.patch.object(magehand, 'app_here', lambda name: app), \
                mock.patch.object(magehand, 'gh_cli',
                                  lambda *args, stdin=None: created.append((args, stdin)) or (0, 'https://x/r\n', '')):
            code, out = run('--dry-run')
            self.assertEqual((code, created), (0, []))
            self.assertIn('v1.0.0 -> v1.1.0', out)
            code, _ = run('--confirm')  # asked to confirm without a terminal: refuse, create nothing
            self.assertEqual((code == 0, created), (False, []))
            code, out = run('--patch', '--json')  # passes every check: created, no prompt, no --yes
            self.assertEqual(code, 0)
        args, notes = created[0]
        self.assertEqual(args[:3], ('release', 'create', 'v1.0.1'))
        self.assertIn('a' * 40, args)
        self.assertIn('feat: add export', notes)
        self.assertTrue(json.loads(out)['created'])


class TokenStorage(unittest.TestCase):
    """The OpenBao token is never an argument, and the file fallback is private to its owner."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(magehand, 'STATE', pathlib.Path(self.tmp.name) / 'state')
        patch.start()
        self.addCleanup(patch.stop)

    def test_keychain_gets_the_token_on_stdin_not_argv(self):
        with mock.patch.object(magehand.sys, 'platform', 'darwin'), \
                mock.patch.object(magehand.subprocess, 'run') as run:
            magehand.store_token('hvs.example-token_1', {'email': 'dev@example.com'})
        argv = run.call_args[0][0]
        self.assertEqual(argv, ['security', '-i'])
        self.assertFalse(any('hvs.' in a for a in argv))
        self.assertIn('-w hvs.example-token_1', run.call_args[1]['input'])

    def test_a_token_that_could_break_out_of_the_command_is_refused(self):
        import contextlib
        import io
        with mock.patch.object(magehand.sys, 'platform', 'darwin'), \
                mock.patch.object(magehand.subprocess, 'run') as run, \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            magehand.store_token('abc\nadd-generic-password -s x', {})
        run.assert_not_called()

    def test_file_fallback_is_owner_only_even_over_a_wider_file(self):
        magehand.STATE.mkdir(parents=True)
        path = magehand.STATE / 'openbao-token'
        path.write_text('old')
        path.chmod(0o644)
        with mock.patch.object(magehand.sys, 'platform', 'linux'):
            magehand.store_token('hvs.new', {})
            self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')
            self.assertEqual(path.read_text(), 'hvs.new')
            self.assertEqual(oct(magehand.STATE.stat().st_mode & 0o777), '0o700')
            self.assertEqual(magehand.saved_token(), 'hvs.new')

    def test_env_token_still_works_with_one_deprecation_note(self):
        import contextlib
        import io
        err = io.StringIO()
        with mock.patch.dict(os.environ, {'MAGEHAND_BAO_TOKEN': 'from-env'}), \
                mock.patch.object(magehand, '_warned_env', []), contextlib.redirect_stderr(err):
            self.assertEqual(magehand.saved_token(), 'from-env')
            self.assertEqual(magehand.saved_token(), 'from-env')
        self.assertEqual(err.getvalue().count('deprecated'), 1)
        self.assertNotIn('from-env', err.getvalue())


class SignIn(unittest.TestCase):
    """Time left on the session: shown, warned about, and renewed in place in a terminal."""

    def setUp(self):
        self.names = ('bao', 'saved_token', 'cmd_login')
        self.saved = {n: getattr(magehand, n) for n in self.names}
        self.ttl = {'tok': 720}
        magehand.saved_token = lambda: 'tok'
        magehand.bao = lambda method, path, token=None, body=None, soft=False: (
            (200, {'data': {'ttl': self.ttl[token]}}) if token in self.ttl else (403, None))

    def tearDown(self):
        for n, v in self.saved.items():
            setattr(magehand, n, v)

    def run_main(self, *argv):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                magehand.main(list(argv))
            except SystemExit as exit_:
                code = exit_.code if isinstance(exit_.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def test_time_left(self):
        self.assertEqual(magehand.signin_left(), 720)
        self.ttl['tok'] = 0  # a token that never expires
        self.assertEqual(magehand.minutes(magehand.signin_left()), 'no expiry')
        magehand.saved_token = lambda: 'gone'
        self.assertIsNone(magehand.signin_left())

    def test_check_passes_and_fails_on_minutes(self):
        code, out, _ = self.run_main('login', '--check')
        self.assertEqual((code, out.strip()), (0, 'signed in, 12 min left'))
        code, out, _ = self.run_main('login', '--check', '--min', '30')
        self.assertEqual(code, 1)
        self.assertIn('less than 30 min', out)

    def test_check_json_and_signed_out(self):
        code, out, _ = self.run_main('login', '--check', '--json')
        self.assertEqual((code, json.loads(out)['seconds_left']), (0, 720))
        magehand.saved_token = lambda: None
        code, out, err = self.run_main('login', '--check', '--json')
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)['signed_in'])

    def test_valid_token_warns_when_low(self):
        import contextlib
        import io
        self.ttl['tok'] = 300
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(magehand.valid_token(), 'tok')
        self.assertIn('5 min left', err.getvalue())
        self.assertIn('magehand login', err.getvalue())

    def test_expired_session_renews_in_a_terminal_and_fails_otherwise(self):
        import contextlib
        import io
        state = {'token': 'old'}
        magehand.saved_token = lambda: state['token']
        magehand.cmd_login = lambda _a: state.update(token='tok') or 'tok'
        tty = type('Tty', (io.StringIO,), {'isatty': lambda self: True})
        with mock.patch('builtins.input', return_value=''), \
                mock.patch.object(magehand.sys, 'stdin', tty()), \
                mock.patch.object(magehand.sys, 'stderr', tty()):
            self.assertEqual(magehand.valid_token(), 'tok')
        # a stale MAGEHAND_BAO_TOKEN still wins in saved_token(): the fresh token is used directly
        state['token'] = 'old'
        magehand.cmd_login = lambda _a: 'tok'
        with mock.patch('builtins.input', return_value='y'), \
                mock.patch.object(magehand.sys, 'stdin', tty()), \
                mock.patch.object(magehand.sys, 'stderr', tty()):
            self.assertEqual(magehand.valid_token(), 'tok')
        state['token'] = 'old'
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            magehand.valid_token()  # not a terminal: exit with the message, no prompt

    def test_app_report_survives_an_unreachable_openbao(self):
        def unreachable(method, path, token=None, body=None, soft=False):
            if soft:
                return 0, None
            raise SystemExit(1)
        magehand.bao = unreachable
        self.assertEqual(magehand.signin_left(soft=True), magehand.UNREACHABLE)
        with self.assertRaises(SystemExit):
            magehand.signin_left()
        app = {'name': 'demo', 'root': None, 'where': 'homelab-apps apps/demo', 'spec': {}, 'services': {},
               'container': {'env': []}}
        names = ('app_here', 'registration', 'github_dir', 'env_plan')
        before = {n: getattr(magehand, n) for n in names}
        magehand.app_here = lambda name=None: app
        magehand.registration = lambda name: {'services': {}}
        magehand.github_dir = lambda repo, path: []
        magehand.env_plan = lambda app: []
        try:
            code, out, _ = self.run_main('app', '-a', 'demo', '--json')
        finally:
            for n, v in before.items():
                setattr(magehand, n, v)
        self.assertEqual(code, 0)
        self.assertIsNone(json.loads(out)['signin_seconds_left'])

    def test_login_json_prints_a_result_without_the_token(self):
        def fake_bao(method, path, token=None, body=None, soft=False):
            if path.endswith('auth_url'):
                return 200, {'data': {'auth_url': 'https://auth.example.com/x'}}
            return 200, {'auth': {'client_token': 'secret-token', 'policies': ['dev-read'], 'lease_duration': 3600,
                                  'metadata': {'email': 'me@example.com'}, 'entity_id': 'e'}}

        class Server:  # delivers the browser's callback to the handler without a socket
            timeout = 0

            def __init__(self, address, handler):
                self.handler = handler

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def handle_request(self):
                request = self.handler.__new__(self.handler)
                request.path = '/oidc/callback?code=c&state=s'
                request.wfile = type('W', (), {'write': lambda self, data: None})()
                request.send_response = request.send_header = request.end_headers = lambda *a: None
                request.do_GET()
        stored = []
        saved = magehand.bao, magehand.store_token, magehand.webbrowser.open, magehand.http.server.HTTPServer
        magehand.bao, magehand.store_token = fake_bao, lambda token, who: stored.append(token)
        magehand.webbrowser.open = lambda url: True
        magehand.http.server.HTTPServer = Server
        try:
            code, out, err = self.run_main('login', '--json')
        finally:
            magehand.bao, magehand.store_token, magehand.webbrowser.open, magehand.http.server.HTTPServer = saved
        self.assertEqual(code, 0)
        result = json.loads(out)  # stdout is the JSON object only; progress went to stderr
        self.assertEqual((result['signed_in'], result['email']), (True, 'me@example.com'))
        self.assertNotIn('secret-token', out + err)
        self.assertEqual(stored, ['secret-token'])
        self.assertIn('Sign in through authentik', err)


class Status(unittest.TestCase):
    """status reads GitHub only: stages from a Build run, the deploy-dev PR and the latest release."""

    HEAD = 'd8b34766a6182b44b83fe9c7faeaee37b3746ba2'
    run_main = CommandLine.run_main
    IMAGE = 'ghcr.io/example/app@sha256:' + 'a' * 64

    def pr(self, number, state, sha=None, checks=(), merged='2026-10-11T02:31:58Z', title='deploy'):
        body = f'x\n<!-- homelab deploy-dev {sha or self.HEAD} {self.IMAGE} -->'
        return {'number': number, 'state': state, 'mergedAt': merged if state == 'MERGED' else None,
                'title': title, 'body': body, 'statusCheckRollup': [{'name': n, 'conclusion': c} for n, c in checks]}

    def build_run(self, event='pull_request', status='completed', conclusion='success', created='2026-10-11T02:29:36Z',
            path=magehand.BUILD_WORKFLOW):
        return {'path': path, 'event': event, 'status': status, 'conclusion': conclusion, 'created_at': created,
                'run_started_at': created, 'updated_at': '2026-10-11T02:30:53Z', 'html_url': 'https://example.com/r'}

    def test_build_stage(self):
        built = magehand.build_stage([self.build_run()], self.HEAD)
        self.assertEqual(built['state'], 'ok')
        self.assertIn('1m17s', built['text'])
        self.assertIn('reused', built['text'])  # a squash merge's image is the PR build's
        self.assertEqual(magehand.build_stage([], self.HEAD)['state'], 'pending')
        self.assertEqual(magehand.build_stage([self.build_run(status='in_progress', conclusion=None)],
                                              self.HEAD)['state'], 'pending')
        self.assertEqual(magehand.build_stage([self.build_run(conclusion='failure')], self.HEAD)['state'], 'failed')

    def test_a_push_run_is_not_the_image_build(self):
        # push to main only runs the security scan: its success says nothing about the image...
        self.assertEqual(magehand.build_stage([self.build_run(event='push')], self.HEAD)['state'], 'pending')
        # ...and a failed dispatched build is reported even beside a successful push run
        runs = [self.build_run(event='push'), self.build_run(event='workflow_dispatch', conclusion='failure')]
        stage = magehand.build_stage(runs, self.HEAD)
        self.assertEqual(stage['state'], 'failed')
        self.assertIn('workflow_dispatch', stage['text'])
        # another workflow's run is ignored
        other = self.build_run(path='.github/workflows/other.yaml')
        self.assertEqual(magehand.build_stage([other], self.HEAD)['state'], 'pending')

    def test_deploy_stage(self):
        merged = magehand.deploy_stage([self.pr(122, 'MERGED')], self.HEAD)
        self.assertEqual((merged['state'], merged['pr']), ('ok', 122))
        self.assertIn('homelab-apps#122 merged 02:31Z', merged['text'])
        old = self.pr(119, 'MERGED', sha='1' * 40)
        self.assertEqual(magehand.deploy_stage([old], self.HEAD)['state'], 'pending')  # not for this commit yet
        open_pr = self.pr(122, 'OPEN', checks=[('scan', 'SUCCESS')])
        self.assertEqual(magehand.deploy_stage([open_pr], self.HEAD)['state'], 'pending')
        bad = self.pr(122, 'OPEN', checks=[('validate', 'FAILURE')])
        stage = magehand.deploy_stage([bad], self.HEAD)
        self.assertEqual(stage['state'], 'failed')
        self.assertIn('validate', stage['text'])
        self.assertEqual(magehand.deploy_stage([self.pr(122, 'CLOSED')], self.HEAD)['state'], 'failed')

    def test_prod_stage(self):
        prs = [{'number': 120, 'state': 'MERGED', 'title': 'elvis: release v1.2.0 to production'}]
        stage = magehand.prod_stage('v1.2.0', 3, prs)
        self.assertEqual(stage['state'], 'ok')
        self.assertIn('3 commits on main since v1.2.0', stage['text'])
        self.assertEqual(magehand.prod_stage(None, None, [])['state'], 'unknown')
        self.assertEqual(magehand.prod_stage('v1.3.0', 0, prs)['state'], 'unknown')

    def test_prod_matches_the_whole_tag_not_a_prefix(self):
        longer = [{'number': 5, 'state': 'MERGED', 'title': 'demo: release v1.2.01 to production'},
                  {'number': 6, 'state': 'MERGED', 'title': 'demo: release v1.2.10 to production'}]
        self.assertEqual(magehand.prod_stage('v1.2.0', 0, longer)['state'], 'unknown')
        self.assertEqual(magehand.prod_stage('v1.2.1', 0, longer)['state'], 'unknown')
        self.assertEqual(magehand.prod_stage('v1.2.10', 0, longer)['state'], 'ok')

    def test_release_eligibility_follows_the_controller(self):
        owner = magehand.OWNER
        mine = {'tag_name': 'v1.2.0', 'author': {'login': owner}}
        newer = [{'tag_name': 'v1.3.0', 'author': {'login': 'someone-else'}},
                 {'tag_name': 'v1.2.9', 'author': {'login': owner}, 'prerelease': True},
                 {'tag_name': 'v1.2.8', 'author': {'login': owner}, 'draft': True},
                 {'tag_name': 'nightly', 'author': {'login': owner}}]
        self.assertEqual(magehand.eligible_release(newer + [mine]), mine)
        self.assertIsNone(magehand.eligible_release(newer))
        self.assertEqual(magehand.eligible_release([{'tag_name': 'v2', 'author': {'login': owner}}])['tag_name'], 'v2')

    def test_failed_queries_are_errors_not_empty_results(self):
        names = ('gh_probe',)
        before = {n: getattr(magehand, n) for n in names}

        def probe(*args):
            if args[:2] == ('api', 'repos/davidlarrimore/demo/commits/main'):
                return 0, json.dumps({'sha': self.HEAD, 'commit': {'message': 'subject\n\nbody'}}), ''
            return 1, '', 'HTTP 403: rate limit exceeded'
        magehand.gh_probe = probe
        try:
            report = magehand.collect_status('davidlarrimore/demo', 'demo')
        finally:
            for n, v in before.items():
                setattr(magehand, n, v)
        self.assertEqual([s['state'] for s in report['stages']], ['error', 'error', 'error'])
        self.assertIn('rate limit', report['stages'][0]['text'])
        self.assertNotIn('no ', report['stages'][2]['text'].split(':')[0])  # not "no release yet"

    IMAGE_ID = 'docker-pullable://' + IMAGE

    def pod(self, name='app-1', image_id=None, ready=True, waiting=None, deleting=False, phase='Running'):
        status = {'ready': ready, 'imageID': self.IMAGE_ID if image_id is None else image_id,
                  'state': {'waiting': {'reason': waiting}} if waiting else {'running': {}}}
        meta = {'name': name}
        if deleting:
            meta['deletionTimestamp'] = '2026-10-12T00:00:00Z'
        return {'metadata': meta, 'status': {'phase': phase, 'containerStatuses': [status]}}

    def argo(self, sync='Synced', health='Healthy'):
        return {'status': {'sync': {'status': sync}, 'health': {'status': health}}}

    def test_pod_stage_ok_only_when_every_live_pod_runs_the_digest(self):
        ok = magehand.pod_stage({'items': [self.pod()]}, self.argo(), self.IMAGE)
        self.assertEqual(ok['state'], 'ok')
        self.assertIn('1/1 pods ready', ok['text'])
        old = self.pod('app-0', image_id='ghcr.io/example/app@sha256:' + 'b' * 64)
        rolling = magehand.pod_stage({'items': [self.pod(), old]}, self.argo(), self.IMAGE)
        self.assertEqual((rolling['state'], 'rolling out' in rolling['text']), ('pending', True))
        # a healthy older rollout is not the deploy
        self.assertEqual(magehand.pod_stage({'items': [old]}, self.argo(), self.IMAGE)['state'], 'pending')
        # a terminating old pod is ignored
        gone = self.pod('app-0', image_id='x', deleting=True)
        self.assertEqual(magehand.pod_stage({'items': [self.pod(), gone]}, self.argo(), self.IMAGE)['state'], 'ok')

    def block(self, name='db-0', ready=True, waiting=None):
        pod = self.pod(name, image_id='docker.io/library/postgres@sha256:' + 'c' * 64, ready=ready, waiting=waiting)
        pod['metadata']['labels'] = {'homelab.davidlarrimore.com/block': 'postgres'}
        return pod

    def test_pod_stage_building_blocks_run_their_own_image(self):
        items = {'items': [self.pod(), self.block()]}
        stage = magehand.pod_stage(items, self.argo(), self.IMAGE)
        self.assertEqual((stage['state'], '2/2 pods ready' in stage['text']), ('ok', True))
        # a block that is not ready yet holds the rollout back, and one that cannot start fails it
        waiting = magehand.pod_stage({'items': [self.pod(), self.block(ready=False)]}, self.argo(), self.IMAGE)
        self.assertEqual((waiting['state'], '1/2 pods ready' in waiting['text']), ('pending', True))
        crash = self.block(ready=False, waiting='CrashLoopBackOff')
        self.assertEqual(magehand.pod_stage({'items': [self.pod(), crash]}, self.argo(), self.IMAGE)['state'],
                         'failed')
        # blocks alone are not the app: wait for its pods
        self.assertEqual(magehand.pod_stage({'items': [self.block()]}, self.argo(), self.IMAGE)['state'], 'pending')

    def test_pod_stage_argo_and_failures(self):
        self.assertEqual(magehand.pod_stage({'items': [self.pod()]}, self.argo(sync='OutOfSync'),
                                            self.IMAGE)['state'], 'pending')
        self.assertEqual(magehand.pod_stage({'items': [self.pod(ready=False)]}, self.argo(), self.IMAGE)['state'],
                         'pending')
        self.assertEqual(magehand.pod_stage({'items': []}, self.argo(), self.IMAGE)['state'], 'pending')
        crash = self.pod(ready=False, waiting='CrashLoopBackOff')
        stage = magehand.pod_stage({'items': [crash]}, self.argo(), self.IMAGE)
        self.assertEqual(stage['state'], 'failed')
        self.assertIn('CrashLoopBackOff', stage['text'])
        pull = self.pod('app-2', image_id='', ready=False, waiting='ImagePullBackOff')
        pull['status']['containerStatuses'][0]['image'] = self.IMAGE  # not pulled yet: the declared image counts
        self.assertEqual(magehand.pod_stage({'items': [pull]}, self.argo(), self.IMAGE)['state'], 'failed')
        # an old pod that can't pull its OLD image has no imageID either: not this deploy's failure
        old_pull = self.pod('app-0', image_id='', ready=False, waiting='ImagePullBackOff')
        old_pull['status']['containerStatuses'][0]['image'] = 'ghcr.io/example/app@sha256:' + 'b' * 64
        self.assertEqual(magehand.pod_stage({'items': [old_pull, self.pod()]}, self.argo(), self.IMAGE)['state'],
                         'pending')
        # the pod spec's image is used when the status has none yet
        spec_only = self.pod('app-3', image_id='', ready=False, waiting='ErrImagePull')
        spec_only['spec'] = {'containers': [{'name': 'c', 'image': self.IMAGE}]}
        spec_only['status']['containerStatuses'][0]['name'] = 'c'
        self.assertEqual(magehand.pod_stage({'items': [spec_only]}, self.argo(), self.IMAGE)['state'], 'failed')
        # an old pod crash-looping while the new one comes up is the old rollout's business
        old = self.pod('app-0', image_id='ghcr.io/example/app@sha256:' + 'b' * 64, ready=False,
                       waiting='CrashLoopBackOff')
        self.assertEqual(magehand.pod_stage({'items': [old]}, self.argo(), self.IMAGE)['state'], 'pending')

    def test_unreadable_argo_is_not_a_verified_rollout(self):
        stage = magehand.pod_stage({'items': [self.pod()]}, None, self.IMAGE, 'Error: forbidden')
        self.assertEqual(stage['state'], 'unknown')
        self.assertIn('Argo not checked (Error: forbidden)', stage['text'])
        # pods that are still rolling out keep --watch waiting, whatever Argo says
        old = self.pod('app-0', image_id='ghcr.io/example/app@sha256:' + 'b' * 64)
        self.assertEqual(magehand.pod_stage({'items': [old]}, None, self.IMAGE, 'forbidden')['state'], 'pending')
        deploy = {'digest': 'sha256:aaaa', 'image': self.IMAGE}
        saved = magehand.kubectl_json
        magehand.kubectl_json = lambda *a: (({'items': [self.pod()]}, None) if a[0] == 'pods'
                                            else (None, 'applications.argoproj.io is forbidden'))
        try:
            stage = magehand.dev_stage('demo', deploy)
        finally:
            magehand.kubectl_json = saved
        self.assertEqual(stage['state'], 'unknown')
        self.assertIn('forbidden', stage['text'])

    def test_dev_stage_without_a_cluster_says_why_and_does_not_block(self):
        deploy = {'digest': 'sha256:aaaa', 'image': self.IMAGE}
        saved = magehand.kubectl_json
        try:
            magehand.kubectl_json = lambda *a: (None, 'kubectl is not installed')
            stage = magehand.dev_stage('demo', deploy)
            self.assertEqual(stage['state'], 'unknown')
            self.assertIn('kubectl is not installed', stage['text'])
            self.assertIn('--no-cluster', magehand.dev_stage('demo', deploy, cluster=False)['text'])
            magehand.kubectl_json = lambda *a: (({'items': [self.pod()]} if a[0] == 'pods' else self.argo()), None)
            self.assertEqual(magehand.dev_stage('demo', deploy)['state'], 'ok')
        finally:
            magehand.kubectl_json = saved

    def test_watch_waits_for_the_pods_and_ignores_unreadable_ones(self):
        names = ('app_here', 'collect_status', 'time')
        before = {n: getattr(magehand, n) for n in names}
        magehand.app_here = lambda name=None: {'name': 'demo', 'root': None, 'spec': {'source': {'repo': 'demo'}}}
        magehand.time = type('T', (), {'monotonic': staticmethod(lambda: 0), 'sleep': staticmethod(lambda s: None)})
        seen = []
        script = [['ok', 'ok', 'pending'], ['ok', 'ok', 'ok']]

        def collect(repo, name, cluster=True):
            build, deploy, dev = script[min(len(seen), len(script) - 1)]
            seen.append(1)
            return {'app': name, 'repo': repo, 'commit': self.HEAD, 'subject': 's',
                    'stages': [{'stage': 'build', 'state': build, 'text': 'b'},
                               {'stage': 'deploy', 'state': deploy, 'text': 'd'},
                               {'stage': 'dev', 'state': dev, 'text': 'p'}]}
        magehand.collect_status = collect
        try:
            code, out, _ = self.run_main('status', '-a', 'demo', '--watch')
            self.assertEqual((code, len(seen)), (0, 2))  # waited one round for the pods
            script[:] = [['ok', 'ok', 'unknown']]  # no kubectl: build and deploy decide
            seen.clear()
            self.assertEqual(self.run_main('status', '-a', 'demo', '--watch')[0], 0)
            script[:] = [['ok', 'ok', 'failed']]
            self.assertEqual(self.run_main('status', '-a', 'demo', '--watch')[0], 1)
        finally:
            for n, v in before.items():
                setattr(magehand, n, v)

    def status_with(self, deploy_checks=(), fail=None):
        """Run `status` through a fake gh: an app whose build and release are fine, a dev deploy PR with
        `deploy_checks`, and every query whose arguments contain `fail` (a string) failing."""
        owner = magehand.OWNER
        run = self.build_run()

        def probe(*args):
            line = ' '.join(args)
            if fail and fail in line:
                return 1, '', 'HTTP 403: forbidden'
            if line.endswith('/commits/main'):
                return 0, json.dumps({'sha': self.HEAD, 'commit': {'message': 'subject', 'tree': {'sha': 't'}}}), ''
            if line.endswith('/pulls'):
                return 0, '[]', ''
            if f'/commits/{self.HEAD}' in line:
                return 0, json.dumps({'sha': self.HEAD, 'commit': {'tree': {'sha': 't'}}}), ''
            if '/actions/runs' in line:
                return 0, json.dumps({'workflow_runs': [run]}), ''
            if 'deploy-dev/' in line:
                pr = self.pr(122, 'OPEN', checks=())
                pr['statusCheckRollup'] = list(deploy_checks)
                return 0, json.dumps([pr]), ''
            if 'deploy-prod/' in line:
                return 0, json.dumps([{'number': 120, 'state': 'MERGED',
                                       'title': 'demo: release v1.2.0 to production'}]), ''
            if '/releases' in line:
                return 0, json.dumps([{'tag_name': 'v1.2.0', 'author': {'login': owner}}]), ''
            if '/compare/' in line:
                return 0, json.dumps({'ahead_by': 2}), ''
            return 1, '', 'unexpected: ' + line
        names = ('app_here', 'gh_probe')
        before = {n: getattr(magehand, n) for n in names}
        magehand.app_here = lambda name=None: {'name': 'demo', 'root': None, 'spec': {'source': {'repo': 'demo'}}}
        magehand.gh_probe = probe
        try:
            code, out, err = self.run_main('status', '-a', 'demo', '--json')
        finally:
            for n, v in before.items():
                setattr(magehand, n, v)
        return code, json.loads(out), err

    def test_a_failed_comparison_is_an_error_not_a_prod_success(self):
        code, report, _ = self.status_with(fail='/compare/')
        self.assertEqual(code, 1)
        prod = report['stages'][-1]
        self.assertEqual(prod['state'], 'error')
        self.assertIn('comparison of v1.2.0 with main', prod['text'])
        self.assertIn('forbidden', prod['text'])

    def test_status_contexts_count_as_failing_checks(self):
        for entry in ({'context': 'policy', 'state': 'FAILURE'}, {'context': 'policy', 'state': 'ERROR'}):
            code, report, _ = self.status_with(deploy_checks=[entry])
            self.assertEqual(code, 1, entry)
            deploy = report['stages'][1]
            self.assertEqual(deploy['state'], 'failed')
            self.assertIn('policy', deploy['text'])
        ok = [{'context': 'policy', 'state': 'SUCCESS'}, {'name': 'scan', 'conclusion': 'SUCCESS'}]
        code, report, _ = self.status_with(deploy_checks=ok)
        self.assertEqual((code, report['stages'][1]['state']), (0, 'pending'))

    def test_status_command_and_watch_exit_codes(self):
        names = ('app_here', 'collect_status')
        before = {n: getattr(magehand, n) for n in names}
        app = {'name': 'demo', 'root': None, 'spec': {'source': {'repo': 'demo'}}}
        magehand.app_here = lambda name=None: app
        stages = {'state': 'ok'}

        def collect(repo, name, cluster=True):
            ok = stages['state']
            return {'app': name, 'repo': repo, 'commit': self.HEAD, 'subject': 's',
                    'stages': [{'stage': 'build', 'state': 'ok', 'text': 'success'},
                               {'stage': 'deploy', 'state': ok, 'text': 'dev'}]}
        magehand.collect_status = collect
        run = self.run_main
        try:
            code, out, _ = run('status', '-a', 'demo', '--watch')
            self.assertEqual(code, 0)
            self.assertIn('demo  (davidlarrimore/demo main d8b3476', out)
            stages['state'] = 'failed'
            self.assertEqual(run('status', '-a', 'demo', '--watch')[0], 1)
            stages['state'] = 'error'
            self.assertEqual(run('status', '-a', 'demo')[0], 1)  # could not read GitHub: not a success
            stages['state'] = 'pending'
            self.assertEqual(run('status', '-a', 'demo')[0], 0)  # no --watch: a snapshot, pending is not a failure
            code, out, _ = run('status', '-a', 'demo', '--json')
            self.assertEqual(code, 0)
            self.assertFalse(json.loads(out)['ok'])  # a snapshot with dev still pending
        finally:
            for n, v in before.items():
                setattr(magehand, n, v)


class MergedBranch(unittest.TestCase):
    """check warns about new commits on a branch whose PR already merged."""

    def setUp(self):
        import subprocess
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.git = lambda *a: subprocess.run(['git', '-C', self.root, *a], check=True, capture_output=True,
                                             text=True).stdout.strip()
        subprocess.run(['git', 'init', '-q', '-b', 'main', self.root], check=True)
        for k, v in (('user.name', 'T'), ('user.email', 't@example.com'), ('commit.gpgsign', 'false')):
            self.git('config', k, v)
        self.git('commit', '-q', '--allow-empty', '-m', 'base')
        self.git('switch', '-q', '-c', 'feature/x')
        self.git('commit', '-q', '--allow-empty', '-m', 'work')
        self.merged_head = self.git('rev-parse', 'HEAD')
        self.names = ('is_app_checkout', 'current_repo', 'gh_probe')
        self.saved = {n: getattr(magehand, n) for n in self.names}
        magehand.is_app_checkout = lambda root: True
        magehand.current_repo = lambda root='.': 'davidlarrimore/demo'
        self.prs = []
        magehand.gh_probe = lambda *a: (0, json.dumps(self.prs), '')

    def tearDown(self):
        for n, v in self.saved.items():
            setattr(magehand, n, v)
        self.tmp.cleanup()

    def test_new_commits_on_a_merged_branch_warn(self):
        self.prs = [{'number': 5, 'state': 'MERGED', 'headRefOid': self.merged_head}]
        self.assertEqual(magehand.check_merged_branch(self.root), [])  # nothing new since the merge
        self.git('commit', '-q', '--allow-empty', '-m', 'more')
        found = magehand.check_merged_branch(self.root)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['level'], 'warning')
        self.assertIn('PR #5', found[0]['text'])
        self.assertIn('origin/main', found[0]['text'])

    def test_no_warning_with_an_open_pr_unrelated_history_main_or_no_gh(self):
        self.git('commit', '-q', '--allow-empty', '-m', 'more')
        self.prs = [{'number': 5, 'state': 'MERGED', 'headRefOid': self.merged_head},
                    {'number': 7, 'state': 'OPEN', 'headRefOid': 'f' * 40}]
        self.assertEqual(magehand.check_merged_branch(self.root), [])  # the branch has an open PR
        self.prs = [{'number': 5, 'state': 'MERGED', 'headRefOid': 'e' * 40}]
        self.assertEqual(magehand.check_merged_branch(self.root), [])  # that head isn't in this history
        self.prs = [{'number': 5, 'state': 'CLOSED', 'headRefOid': self.merged_head}]
        self.assertEqual(magehand.check_merged_branch(self.root), [])  # closed, not merged
        self.prs = [{'number': 5, 'state': 'MERGED', 'headRefOid': self.merged_head}]
        self.git('switch', '-q', 'main')
        self.assertEqual(magehand.check_merged_branch(self.root), [])
        self.git('switch', '-q', '--detach')
        self.assertEqual(magehand.check_merged_branch(self.root), [])  # detached HEAD (CI)
        self.git('switch', '-q', 'feature/x')
        magehand.gh_probe = lambda *a: (None, '', 'gh is not installed')
        self.assertEqual(magehand.check_merged_branch(self.root), [])

    def test_several_merged_prs_use_the_latest_and_stay_quiet_when_head_is_covered(self):
        first = self.merged_head
        self.git('commit', '-q', '--allow-empty', '-m', 'second round')
        second = self.git('rev-parse', 'HEAD')
        self.prs = [{'number': 9, 'state': 'MERGED', 'headRefOid': second},
                    {'number': 5, 'state': 'MERGED', 'headRefOid': first}]
        self.assertEqual(magehand.check_merged_branch(self.root), [])  # everything through HEAD merged in #9
        self.git('commit', '-q', '--allow-empty', '-m', 'third round')
        found = magehand.check_merged_branch(self.root)
        self.assertEqual(len(found), 1)
        self.assertIn('PR #9', found[0]['text'])  # the latest merge, not #5
