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
                magehand.cmd_doctor([])
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
            magehand.cmd_setup(args)

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
