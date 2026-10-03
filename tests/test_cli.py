"""Unit tests for magehand (no network, no Docker)."""
import http.server
import json
import os
import pathlib
import socket
import tempfile
import threading
import unittest
import urllib.request

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
                               {'deploy/prod/kustomization.yaml': 'x', 'deploy/base/README.md': 'x'})
            (root / 'deploy/base/deployment.yaml').write_text(DEPLOYMENT.replace('homelab-apps/demo', 'homelab-apps/other'))
            app = magehand.try_load_app('demo', root)
            errors = texts(magehand.check_manifests(app, CATALOG_MODELS), 'error')
            self.assertTrue(any('source: is added by the copy' in e for e in errors))
            self.assertTrue(any('without a digest' in e for e in errors))
            self.assertEqual(sorted(f['where'] for f in magehand.check_deploy_files(root)),
                             ['deploy/base/README.md', 'deploy/prod/kustomization.yaml'])


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
