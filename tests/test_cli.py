"""Unit tests for magehand (no network, no Docker)."""
import contextlib
import datetime
import errno
import fcntl
import http.server
import io
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
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
# The state file holds no secret: a database's password is the container's own environment.
LOCAL = {'db': {'port': '5555', 'container': 'magehand-demo-db'},
         'cache': {'port': '6666', 'container': 'magehand-demo-cache'}}
CONTAINER_ENV = {'magehand-demo-db': {'POSTGRES_PASSWORD': 'pw'}, 'magehand-demo-cache': {'VALKEY_PASSWORD': 'rpw'}}


def container_env_fake(name):
    return CONTAINER_ENV.get(name, {})



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
        with mock.patch.object(magehand, 'container_env', container_env_fake):
            pg = magehand.local_connection('postgres', 'db', LOCAL)
            self.assertEqual(pg['uri'], 'postgresql://app:pw@127.0.0.1:5555/app')
            self.assertEqual(magehand.local_connection('redis', 'cache', LOCAL)['uri'],
                             'redis://:rpw@127.0.0.1:6666/0')
        with self.assertRaises(SystemExit):
            magehand.local_connection('postgres', 'db', {})

    def test_no_password_is_in_the_state_the_connection_comes_from_the_container(self):
        self.assertNotIn('pw', json.dumps(LOCAL))
        with mock.patch.object(magehand, 'container_env', lambda _c: {}):    # the container is gone
            with self.assertRaises(SystemExit) as caught:
                magehand.local_connection('postgres', 'db', LOCAL)
        self.assertIn('magehand dev down', str(caught.exception.code))

    def test_an_older_state_file_loses_its_secrets_on_first_read(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(magehand, 'STATE', pathlib.Path(tmp)):
            path = pathlib.Path(tmp) / 'demo.json'
            path.write_text(json.dumps({
                'db': {'password': 'oldpw', 'port': '5555'},                        # a database, old format
                'session': {'value': 'v' * 48},                                      # a generated secret block
                'removed': {'password': 'forgotten'},                                # a block no longer in app.yaml
                'files': {'access_key_id': 'GK1', 'secret_access_key': 's', 'admin_token': 't', 'port': '1'}}))
            state = magehand.local_state('demo')
            self.assertEqual(state, {'db': {'port': '5555', 'container': 'magehand-demo-db'},
                                     'files': {'access_key_id': 'GK1', 'port': '1',
                                               'container': 'magehand-demo-files'}})
            on_disk = path.read_text()                                               # and it is cleaned on disk too
            for leaked in ('oldpw', 'forgotten', 'v' * 48, '"s"', '"t"'):
                self.assertNotIn(leaked, on_disk)
            self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')

    def test_a_host_run_right_after_upgrading_uses_the_existing_containers_password(self):
        # the legacy state named no container; the password is already in the healthy container's
        # environment, so nothing needs to be reset (and `dev down` would delete the data)
        asked = []

        def env(name):
            asked.append(name)
            return CONTAINER_ENV.get(name, {})
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(magehand, 'STATE', pathlib.Path(tmp)), \
                mock.patch.object(magehand, 'container_env', env):
            (pathlib.Path(tmp) / 'demo.json').write_text(json.dumps({
                'db': {'password': 'legacy-pw', 'port': '5555'}, 'cache': {'password': 'legacy-rpw', 'port': '6666'}}))
            app = {**APP, 'container': {**APP['container'], 'env': [
                {'name': 'DATABASE_URL', 'valueFrom': {'secretKeyRef': {'name': 'db-connection', 'key': 'uri'}}},
                {'name': 'REDIS_URL', 'valueFrom': {'secretKeyRef': {'name': 'cache-connection', 'key': 'uri'}}}]}}
            resolved = magehand.resolve_env(app)
            self.assertEqual(resolved['DATABASE_URL'], 'postgresql://app:pw@127.0.0.1:5555/app')
            self.assertEqual(resolved['REDIS_URL'], 'redis://:rpw@127.0.0.1:6666/0')
            self.assertEqual(sorted(set(asked)), ['magehand-demo-cache', 'magehand-demo-db'])
            self.assertNotIn('legacy', (pathlib.Path(tmp) / 'demo.json').read_text())
            self.assertIn('magehand-demo-db', (pathlib.Path(tmp) / 'demo.json').read_text())   # migrated name

    def test_the_container_is_found_from_the_app_name_when_the_state_names_none(self):
        # independent of the migration: a host run knows the app, and the container name is fixed
        with mock.patch.object(magehand, 'container_env', container_env_fake):
            conn = magehand.local_connection('postgres', 'db', {'db': {'port': '5555'}}, app='demo')
        self.assertEqual((conn['password'], conn['host'], conn['port']), ('pw', '127.0.0.1', '5555'))

    def test_an_unreadable_password_recommends_dev_up_before_a_reset(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(magehand, 'STATE', pathlib.Path(tmp)), \
                mock.patch.object(magehand, 'container_env', lambda _c: {}):
            with self.assertRaises(SystemExit) as caught:
                magehand.local_connection('postgres', 'db', {'db': {'port': '5555'}}, app='demo')
        message = str(caught.exception.code)
        self.assertLess(message.index('magehand dev up'), message.index('magehand dev down'))
        self.assertIn('deletes the local data', message)

    def test_a_secret_set_in_the_shell_is_kept_and_unset_one_is_made_per_run(self):
        app = {**APP, 'container': {**APP['container'], 'env': [
            {'name': 'SESSION_KEY', 'valueFrom': {'secretKeyRef': {'name': 'session', 'key': 'value'}}}]}}
        saved = os.environ.pop('SESSION_KEY', None)
        try:
            first = magehand.resolve_env(app)['SESSION_KEY']
            second = magehand.resolve_env(app)['SESSION_KEY']
            self.assertNotEqual(first, second)                         # unset: made for each run
            os.environ['SESSION_KEY'] = 'shell-stable'
            env = magehand.resolve_env(app)
            self.assertNotIn('SESSION_KEY', env)                       # left to the shell, which the child inherits
            self.assertEqual(os.environ['SESSION_KEY'], 'shell-stable')
        finally:
            os.environ.pop('SESSION_KEY', None)
            if saved is not None:
                os.environ['SESSION_KEY'] = saved

    def test_a_secret_block_is_made_for_the_run_and_kept_nowhere(self):
        one = magehand.local_connection('secret', 'session', {})['value']
        two = magehand.local_connection('secret', 'session', {})['value']
        self.assertEqual((len(one), len(two)), (48, 48))
        self.assertNotEqual(one, two)                              # nothing persists it


class FakeLocalGarage:
    """The local Garage's admin API, as far as magehand uses it."""

    def __init__(self):
        self.calls, self.buckets, self.role, self.keys = [], {}, None, {}
        self.fail = set()      # operation names that answer 500
        self.n = 0

    def __call__(self, port, token, method, path, body=None):
        op = path.split('?')[0].replace('/v2/', '')
        query = dict(p.split('=', 1) for p in path.split('?')[1].split('&')) if '?' in path else {}
        self.calls.append((op, body))
        if op in self.fail:
            return 500, None
        if op == 'GetClusterStatus':
            return 200, {'layoutVersion': 0, 'nodes': [{'id': 'n1', 'role': self.role}]}
        if op == 'UpdateClusterLayout':
            self.role = body['roles'][0]
            return 200, {}
        if op == 'ApplyClusterLayout':
            return 200, {}
        if op == 'GetBucketInfo':
            alias = urllib.parse.unquote(query['globalAlias'])
            return (200, {'id': self.buckets[alias]}) if alias in self.buckets else (404, None)
        if op == 'CreateBucket':
            self.buckets[body['globalAlias']] = 'b1'
            return 200, {'id': 'b1'}
        if op in ('UpdateBucket', 'AllowBucketKey'):
            return 200, {}
        if op == 'GetKeyInfo':
            found = self.keys.get(query['id'])
            return (200, {'accessKeyId': query['id'], 'secretAccessKey': found['secret']}) if found else (404, None)
        if op == 'ListKeys':
            return 200, [{'id': i, 'name': k['name']} for i, k in self.keys.items()]
        if op == 'DeleteKey':
            self.keys.pop(query['id'], None)
            return 200, {}
        if op == 'CreateKey':
            self.n += 1
            self.keys[f'GK{self.n}'] = {'name': body['name'], 'secret': f'secret{self.n}'}
            return 200, {'accessKeyId': f'GK{self.n}', 'secretAccessKey': f'secret{self.n}'}
        return 404, None


class LocalBucket(unittest.TestCase):
    """`magehand dev up` runs a Garage for a bucket block and sets it up through its admin API."""

    def setUp(self):
        self.engine_calls = []
        self.engine_env = []
        self.stopped = False       # a container that exists but is not running
        self.garage = FakeLocalGarage()

        self.containers = {}       # name -> the environment it was started with

        def engine(*args, check=True, env=None):
            self.engine_calls.append(args)
            self.engine_env.append(env or {})
            out = mock.Mock(returncode=0)
            out.stdout = ''
            if args[0] == 'port':
                out.stdout = '127.0.0.1:{}\n'.format({'3900': '41000', '3903': '41003'}.get(args[-1], '0'))
            elif args[0] == 'start':
                out.returncode = 0 if self.stopped and args[1] in self.containers else 1
            elif args[0] == 'run':
                self.containers[args[args.index('--name') + 1]] = dict(env or {})
            elif args[0] == 'rm':
                self.containers.pop(args[-1], None)
            elif args[0] == 'inspect':
                out.stdout = json.dumps([f'{k}={v}' for k, v in self.containers.get(args[-1], {}).items()])
            return out
        self.patches = [mock.patch.object(magehand, 'engine', engine),
                        mock.patch.object(magehand, 'garage_admin', self.garage),
                        mock.patch.object(magehand, 'STATE', pathlib.Path(tempfile.mkdtemp()))]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()

    def up(self, state=None, running=False, svc=None):
        state = {} if state is None else state
        with contextlib.redirect_stdout(io.StringIO()):
            magehand.local_bucket({'name': 'demo'}, 'files', svc or {'type': 'bucket'}, state,
                                  'magehand-demo-files', 'magehand-demo', running)
        return state

    def runs(self):
        return [(a, e) for a, e in zip(self.engine_calls, self.engine_env) if a[0] == 'run']

    def test_it_starts_the_pinned_garage_on_loopback_with_its_data_in_a_volume(self):
        self.up()
        (run, _), = self.runs()
        self.assertIn(magehand.GARAGE_IMAGE, run)
        self.assertIn('127.0.0.1::3900', run)             # published on loopback only
        self.assertIn('127.0.0.1::3903', run)
        self.assertIn('magehand-demo-files-data:/data', run)     # survives a restart, goes with `down`
        self.assertRegex(magehand.GARAGE_IMAGE, r'@sha256:[0-9a-f]{64}$')

    def test_secrets_never_appear_in_argv(self):
        self.up()
        (run, env), = self.runs()
        self.assertEqual(set(env), {'GARAGE_RPC_SECRET', 'GARAGE_ADMIN_TOKEN'})
        self.assertRegex(env['GARAGE_RPC_SECRET'], r'^[0-9a-f]{64}$')   # Garage wants 32 bytes of hex
        for name in env:
            self.assertIn(name, run)                       # named, as `-e NAME`
        for value in env.values():
            self.assertFalse([a for a in run if value in str(a)], 'a secret value is in argv')

    def test_dev_up_records_no_secret_and_restarts_a_stopped_database(self):
        saved, calls = {}, []

        def engine(*args, check=True, env=None):
            calls.append(args)
            out = mock.Mock(returncode=0)
            out.stdout = '0.0.0.0:5432\n' if args[0] == 'port' else ('false' if args[0] == 'inspect' else '')
            if args[0] == 'start':
                out.returncode = 0               # the container exists, stopped: it is started, not replaced
            return out
        app = {'name': 'demo', 'services': {'db': {'type': 'postgres'}, 'session': {'type': 'secret'}}}
        with mock.patch.object(magehand, 'engine', engine), mock.patch.object(magehand, 'runtime', lambda: 'docker'), \
                mock.patch.object(magehand, 'app_here', lambda _a: app), \
                mock.patch.object(magehand, 'local_state', lambda _n: {}), \
                mock.patch.object(magehand, 'save_local_state', lambda name, data: saved.update(data)), \
                contextlib.redirect_stdout(io.StringIO()):
            magehand.cmd_dev(mock.Mock(action='up', app_flag=None, app=None))
        self.assertEqual(saved, {'db': {'port': '5432', 'container': 'magehand-demo-db'}})   # a port, no secret
        self.assertFalse([c for c in calls if c[0] == 'run'])                                 # started, not recreated

    def test_postgres_and_redis_passwords_are_not_in_argv_either(self):
        state, calls, envs = {}, [], []

        def engine(*args, check=True, env=None):
            calls.append(args)
            envs.append(env or {})
            out = mock.Mock(returncode=0)
            out.stdout = '0.0.0.0:5432\n' if args[0] == 'port' else ('false' if args[0] == 'inspect' else '')
            return out
        app = {'name': 'demo', 'services': {'db': {'type': 'postgres'}, 'cache': {'type': 'redis'}}}
        with mock.patch.object(magehand, 'engine', engine), mock.patch.object(magehand, 'runtime', lambda: 'docker'), \
                mock.patch.object(magehand, 'app_here', lambda _a: app), mock.patch.object(magehand, 'local_state',
                                                                                          lambda _n: state), \
                mock.patch.object(magehand, 'save_local_state', lambda *_a: None), \
                mock.patch.object(magehand, 'block_image', lambda *_a: 'img'), contextlib.redirect_stdout(io.StringIO()):
            magehand.cmd_dev(mock.Mock(action='up', app_flag=None, app=None))
        for argv, env in zip(calls, envs):
            if argv[0] == 'run':
                self.assertTrue(env, 'a run with a password passes it through the environment')
                for value in env.values():
                    self.assertFalse([a for a in argv if value in str(a)], f'{argv[:6]} carries a secret in argv')

    def test_it_makes_the_bucket_quota_and_a_key_limited_to_it(self):
        state = self.up()
        ops = [c[0] for c in self.garage.calls]
        for op in ('UpdateClusterLayout', 'ApplyClusterLayout', 'CreateBucket', 'UpdateBucket', 'CreateKey',
                   'AllowBucketKey'):
            self.assertIn(op, ops)
        quota = next(b for o, b in self.garage.calls if o == 'UpdateBucket')
        self.assertEqual(quota['quotas']['maxSize'], magehand.GARAGE_QUOTA)
        allow = next(b for o, b in self.garage.calls if o == 'AllowBucketKey')
        self.assertEqual(allow['permissions'], {'read': True, 'write': True, 'owner': False})
        self.assertEqual((state['files']['bucket'], state['files']['port'], state['files']['access_key_id']),
                         ('demo-files', '41000', 'GK1'))

    def test_a_stopped_container_is_started_again_not_replaced(self):
        state = self.up()
        self.engine_calls.clear()
        self.engine_env.clear()
        self.stopped = True                                # a reboot stopped it; its volume still holds the data
        again = self.up(state=state, running=False)
        self.assertEqual(self.runs(), [])                  # no new container
        self.assertIn(('start', 'magehand-demo-files'), self.engine_calls)
        self.assertNotIn(('rm', '-f', 'magehand-demo-files'), self.engine_calls)
        self.assertEqual(again['files']['access_key_id'], 'GK1')        # the same key, reused

    def test_a_removed_container_keeps_its_data_and_its_key_while_garage_still_has_it(self):
        state = self.up()
        self.containers.clear()                            # the container is gone, its volume is not
        self.engine_calls.clear()
        self.up(state=state, running=False)                # `start` fails: it is started fresh on the volume
        self.assertEqual(len(self.runs()), 1)
        self.assertIn('magehand-demo-files-data:/data', self.runs()[0][0])
        self.assertEqual(state['files']['access_key_id'], 'GK1')    # GetKeyInfo found it: not replaced
        self.assertEqual(len([c for c in self.garage.calls if c[0] == 'CreateKey']), 1)

    def test_a_key_garage_no_longer_has_is_made_again_without_leaving_the_old_one(self):
        state = self.up()
        self.garage.keys.clear()                           # the volume was removed
        self.garage.keys['GKstale'] = {'name': 'magehand:demo-files', 'secret': 'x'}
        again = self.up(state=state, running=True)
        self.assertNotEqual(again['files']['access_key_id'], 'GK1')
        self.assertNotIn('GKstale', self.garage.keys)      # an earlier attempt's key is removed first

    def test_a_running_store_with_a_key_is_not_set_up_again_from_scratch(self):
        state = self.up()
        self.garage.calls.clear()
        self.engine_calls.clear()
        again = self.up(state=state, running=True)
        self.assertEqual(self.runs(), [])
        self.assertNotIn('CreateKey', [c[0] for c in self.garage.calls])
        self.assertEqual(again['files']['access_key_id'], 'GK1')

    def test_a_running_store_we_cannot_read_a_token_from_is_started_again(self):
        self.up(state={'files': {'access_key_id': 'GKx', 'port': '1'}}, running=True)   # unknown container
        self.assertEqual(len(self.runs()), 1)              # no admin token to drive it with

    def test_no_secret_is_written_to_the_state_file(self):
        state = self.up()
        token = self.containers['magehand-demo-files']['GARAGE_ADMIN_TOKEN']
        persisted = json.dumps(state)
        self.assertNotIn(token, persisted)
        self.assertNotIn('secret1', persisted)             # the key's secret
        self.assertNotIn('secret_access_key', state['files'])
        self.assertNotIn('admin_token', state['files'])
        # ... and a run still gets the key's secret, asked of the running Garage
        self.assertEqual(magehand.local_connection('bucket', 'files', state)['secret_access_key'], 'secret1')

    def test_a_stopped_store_is_a_dev_up_away_not_a_crash_with_a_stale_secret(self):
        state = self.up()
        self.containers.clear()                            # gone: nothing to ask
        with self.assertRaises(SystemExit) as caught:
            magehand.local_connection('bucket', 'files', state)
        self.assertIn('magehand dev up', str(caught.exception.code))

    def test_the_longest_app_and_block_names_still_fit_garage_bucket_names(self):
        app, block = 'a' * 40, 'b' * 30
        name = magehand.local_bucket_name(app, block)
        self.assertLessEqual(len(name), 63)
        self.assertRegex(name, r'^[a-z0-9][a-z0-9-]*[a-z0-9]$')
        self.assertEqual(name, magehand.local_bucket_name(app, block))        # stable
        self.assertNotEqual(name, magehand.local_bucket_name(app, 'b' * 29 + 'c'))   # collision-resistant
        self.assertEqual(magehand.local_bucket_name('demo', 'files'), 'demo-files')   # short names are as before

    def test_every_setup_step_is_checked_and_a_retry_repairs_it(self):
        for op in ('UpdateClusterLayout', 'ApplyClusterLayout', 'CreateBucket', 'UpdateBucket', 'CreateKey',
                   'AllowBucketKey'):
            garage = self.garage = FakeLocalGarage()
            magehand.garage_admin = garage                 # the patch holds the first fake: point it at this one
            garage.fail = {op}
            state = {}
            with self.assertRaises(SystemExit, msg=op):
                self.up(state=state)
            self.assertEqual(state, {}, f'{op}: a failed setup must not record a key')
            garage.fail = set()
            again = self.up(state=state)                   # the retry completes
            self.assertTrue(again['files']['access_key_id'], op)
            self.assertEqual(len([k for k in garage.keys.values() if k['name'] == 'magehand:demo-files']), 1, op)

    def test_the_connection_matches_the_platforms_keys(self):
        state = self.up(svc={'type': 'bucket', 'access': 'public'})
        host = magehand.local_connection('bucket', 'files', state)
        self.assertEqual(set(host), {'endpoint', 'bucket', 'access_key_id', 'secret_access_key', 'region',
                                     'access', 'path_style', 'url'})
        self.assertEqual((host['endpoint'], host['region'], host['path_style']),
                         ('http://127.0.0.1:41000', 'garage', 'true'))
        pod = magehand.local_connection('bucket', 'files', state, app_name='demo')
        self.assertEqual(pod['endpoint'], 'http://magehand-demo-files:3900')
        private = magehand.local_connection('bucket', 'files', self.up(svc={'type': 'bucket'}))
        self.assertNotIn('url', private)                  # only a public bucket has a url

    def test_the_builtin_schema_knows_path_style_and_the_public_only_url(self):
        spec = magehand.block_spec({}, 'bucket')           # no catalog: the fallback
        self.assertIn('path_style', spec['keys'])
        self.assertIn('url', spec['keys'])
        self.assertEqual(spec['key_options'], {'url': 'access'})


class FakeS3(http.server.BaseHTTPRequestHandler):
    """An S3 (path-style) with one bucket that checks each request's SigV4 signature itself."""

    store = {}          # key -> (body, headers)
    ACCESS, SECRET, REGION, BUCKET = 'GKtest', 'topsecret', 'garage', 'demo-files'
    PAGE = 2            # list this many per page, to exercise continuation tokens
    seen = []

    def log_message(self, *args):
        pass

    def verified(self, body):
        import hashlib as h, hmac as m
        auth = self.headers.get('Authorization', '')
        try:
            cred = auth.split('Credential=')[1].split(',')[0]
            names = auth.split('SignedHeaders=')[1].split(',')[0].split(';')
            given = auth.split('Signature=')[1]
        except IndexError:
            return False
        key_id, day, region, service, term = cred.split('/')
        if key_id != self.ACCESS or region != self.REGION:
            return False
        path, _, query = self.path.partition('?')
        canon_query = '&'.join(sorted(query.split('&'))) if query else ''
        hdrs = {n: ' '.join(self.headers.get(n if n != 'host' else 'Host', '').split()) for n in names}
        canon = '\n'.join([self.command, path, canon_query, ''.join(f'{n}:{hdrs[n]}\n' for n in names),
                           ';'.join(names), self.headers.get('x-amz-content-sha256')])
        if self.headers.get('x-amz-content-sha256') != h.sha256(body).hexdigest():
            return False
        stamp = self.headers.get('x-amz-date')
        sts = '\n'.join(['AWS4-HMAC-SHA256', stamp, f'{day}/{region}/s3/aws4_request',
                         h.sha256(canon.encode()).hexdigest()])
        k = f'AWS4{self.SECRET}'.encode()
        for part in (day, region, 's3', 'aws4_request'):
            k = m.new(k, part.encode(), h.sha256).digest()
        return m.new(k, sts.encode(), h.sha256).hexdigest() == given

    def reply(self, status, body=b'', headers=None):
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_any(self):
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        if not self.verified(body):
            return self.reply(403, b'<Error><Code>SignatureDoesNotMatch</Code><Message>bad signature</Message></Error>')
        path, _, query = self.path.partition('?')
        import urllib.parse as up
        parts = up.unquote(path).lstrip('/').split('/', 1)
        key = parts[1] if len(parts) > 1 else ''
        self.seen.append((self.command, key))
        if self.command == 'PUT':
            self.store[key] = (body, {k.lower(): v for k, v in self.headers.items()})
            return self.reply(200)
        if self.command == 'GET' and key:
            if key not in self.store:
                return self.reply(404, b'<Error><Code>NoSuchKey</Code><Message>no such key</Message></Error>')
            return self.reply(200, self.store[key][0])
        if self.command == 'GET':
            q = dict(up.parse_qsl(query))
            prefix, delim = q.get('prefix', ''), q.get('delimiter', '')
            keys = sorted(k for k in self.store if k.startswith(prefix))
            commons, items = [], []
            for k in keys:
                rest = k[len(prefix):]
                if delim and delim in rest:
                    c = prefix + rest.split(delim)[0] + delim
                    if c not in commons:
                        commons.append(c)
                else:
                    items.append(k)
            start = int(q.get('continuation-token', '0') or 0)
            page = items[start:start + self.PAGE]
            more = start + self.PAGE < len(items)
            xml = '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            for k in page:
                import hashlib as h
                xml += (f'<Contents><Key>{k}</Key><Size>{len(self.store[k][0])}</Size>'
                        f'<ETag>"{h.md5(self.store[k][0]).hexdigest()}"</ETag>'
                        '<LastModified>2026-10-13T12:00:00.000Z</LastModified></Contents>')
            for c in commons:
                xml += f'<CommonPrefixes><Prefix>{c}</Prefix></CommonPrefixes>'
            if more:
                xml += f'<NextContinuationToken>{start + self.PAGE}</NextContinuationToken>'
            xml += '</ListBucketResult>'
            return self.reply(200, xml.encode())
        return self.reply(405)

    do_GET = do_PUT = do_DELETE = handle_any


class BucketCommands(unittest.TestCase):
    """`magehand bucket ls/cp/sync/url` against a fake S3 that verifies SigV4."""

    def setUp(self):
        FakeS3.store, FakeS3.seen = {}, []
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), FakeS3)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        self.access = 'public'
        self.conn = {'endpoint': f'http://127.0.0.1:{self.port}', 'bucket': FakeS3.BUCKET,
                     'access_key_id': FakeS3.ACCESS, 'secret_access_key': FakeS3.SECRET, 'region': FakeS3.REGION,
                     'access': 'public', 'path_style': 'true', 'url': 'https://files.example/demo-dev/files/'}
        self.patches = [mock.patch.object(magehand, 'app_here', lambda name=None: {
                            'name': 'demo', 'services': {'files': {'type': 'bucket', 'access': self.access},
                                                         'db': {'type': 'postgres'}}}),
                        mock.patch.object(magehand, 'app_repo_here', lambda: 'demo'),
                        mock.patch.object(magehand, 'bucket_connection', lambda app, block, env: self.conn)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        os.chdir(self.cwd)
        self.server.shutdown()
        self.server.server_close()

    def run_cmd(self, *argv):
        args = magehand.build_parser().parse_args(['bucket', *argv])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            args.func(args)
        return out.getvalue()

    def die(self, *argv):
        with self.assertRaises(SystemExit) as caught:
            self.run_cmd(*argv)
        return str(caught.exception.code)

    def test_only_the_app_of_this_checkout(self):
        with mock.patch.object(magehand, 'app_repo_here', lambda: None):
            self.assertIn("inside the app's repo", self.die('ls', 'files'))
        for flag in ('--app', '-a'):
            self.assertIn("works only on demo's buckets, not other's", self.die(flag, 'other', 'ls', 'files'))
            self.assertIn('files', self.run_cmd(flag, 'demo', 'usage'))   # its own app: still accepted

    def test_context_is_the_checkouts_app(self):
        seen = []
        with mock.patch.object(magehand, 'app_repo_here', lambda: 'mine'), \
                mock.patch.object(magehand, 'app_here', lambda name=None: seen.append(name) or
                                  {'name': 'mine', 'services': {'files': {'type': 'bucket'}}}):
            self.run_cmd('usage', '--json')
        self.assertEqual(seen, ['mine'])

    def test_usage(self):
        FakeS3.store['a/one.txt'] = (b'x' * 300, 'text/plain')
        FakeS3.store['b.txt'] = (b'y' * 200, 'text/plain')
        data = json.loads(self.run_cmd('usage', '--json'))
        row = data['buckets'][0]
        self.assertEqual((data['app'], row['bucket'], row['objects'], row['bytes'], row['percent']),
                         ('demo', 'files', 2, 500, 0.0))
        self.assertEqual(row['cap_bytes'], 10**9)
        self.assertIn('2 objects, 500 bytes', self.run_cmd('usage', 'files'))

    def test_usage_unknown_bucket_and_none(self):
        self.assertIn('not a bucket block', self.die('usage', 'db'))
        self.access = 'internal'
        out = self.run_cmd('--env', 'dev', 'usage')
        self.assertIn('internal: reachable only with --env local', out)
        # asked for by name, the connection error is the answer (the mocked connection allows it here)
        self.assertIn('files', self.run_cmd('--env', 'local', 'usage'))

    def test_the_signer_reproduces_awss_published_s3_example(self):
        import datetime, hashlib
        h = magehand.s3_sign('GET', 'examplebucket.s3.amazonaws.com', '/test.txt', '', hashlib.sha256(b'').hexdigest(),
                             'AKIAIOSFODNN7EXAMPLE', 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY', 'us-east-1',
                             {'range': 'bytes=0-9'}, when=datetime.datetime(2013, 5, 24, tzinfo=datetime.timezone.utc))
        self.assertTrue(h['authorization'].endswith(
            'Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41'))

    def test_cp_uploads_with_type_cache_control_and_prints_the_public_url(self):
        pathlib.Path('latest.json').write_text('{"ok": true}')
        out = self.run_cmd('cp', 'latest.json', 'files/data/latest.json', '--cache-control', 'max-age=60')
        body, headers = FakeS3.store['data/latest.json']
        self.assertEqual(body, b'{"ok": true}')
        self.assertEqual(headers['content-type'], 'application/json')    # guessed from the key
        self.assertEqual(headers['cache-control'], 'max-age=60')
        self.assertIn('uploaded 12 bytes to files/data/latest.json', out)
        self.assertIn('public: https://files.example/demo-dev/files/data/latest.json', out)

    def test_cp_with_a_trailing_slash_keeps_the_file_name_and_spaces_sign_correctly(self):
        pathlib.Path('my report.txt').write_text('hi')
        self.run_cmd('cp', 'my report.txt', 'files/reports/', '--content-type', 'text/plain;  charset=utf-8')
        self.assertIn('reports/my report.txt', FakeS3.store)             # signed and decoded correctly

    def test_cp_downloads_atomically(self):
        pathlib.Path('up.bin').write_bytes(b'\x00\x01binary')
        self.run_cmd('cp', 'up.bin', 'files/b/up.bin')
        out = self.run_cmd('cp', 'files/b/up.bin', 'down.bin')
        self.assertEqual(pathlib.Path('down.bin').read_bytes(), b'\x00\x01binary')
        self.assertFalse(pathlib.Path('down.bin.part').exists())
        self.assertIn('downloaded 8 bytes', out)

    def test_a_download_never_touches_a_neighbouring_part_file_or_a_symlinks_target(self):
        pathlib.Path('up.bin').write_bytes(b'new bytes')
        self.run_cmd('cp', 'up.bin', 'files/b/up.bin')
        # an unrelated file that used to be the temporary path
        pathlib.Path('target.bin.part').write_bytes(b'someone else')
        # a symlink at the destination whose target must not be overwritten
        pathlib.Path('real.txt').write_bytes(b'precious')
        os.symlink('real.txt', 'target.bin')
        self.run_cmd('cp', 'files/b/up.bin', 'target.bin')
        self.assertEqual(pathlib.Path('target.bin.part').read_bytes(), b'someone else')
        self.assertEqual(pathlib.Path('real.txt').read_bytes(), b'precious')
        self.assertEqual(pathlib.Path('target.bin').read_bytes(), b'new bytes')
        self.assertEqual(sorted(p.name for p in pathlib.Path('.').glob('.target.bin.*')), [])   # no temp left

    def test_a_failed_download_leaves_no_temporary_file(self):
        pathlib.Path('up.bin').write_bytes(b'x')
        self.run_cmd('cp', 'up.bin', 'files/b/up.bin')
        with mock.patch.object(magehand.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.run_cmd('cp', 'files/b/up.bin', 'out.bin')
        self.assertEqual([p.name for p in pathlib.Path('.').iterdir() if p.name.startswith('.out.bin')], [])
        self.assertFalse(pathlib.Path('out.bin').exists())

    def test_bucket_key_is_remote_even_when_a_local_directory_has_the_same_name(self):
        pathlib.Path('files').mkdir()
        pathlib.Path('files/key').write_text('a local file that must not win')
        pathlib.Path('up.txt').write_text('payload')
        self.run_cmd('cp', 'up.txt', 'files/')                       # the documented trailing-slash upload
        self.assertEqual(FakeS3.store['up.txt'][0], b'payload')
        self.run_cmd('cp', 'up.txt', 'files/key')                    # a key that exists locally is still remote
        self.assertEqual(FakeS3.store['key'][0], b'payload')
        self.assertEqual(pathlib.Path('files/key').read_text(), 'a local file that must not win')

    def test_a_local_path_that_starts_with_a_bucket_name_is_written_with_dot_slash(self):
        pathlib.Path('files').mkdir()
        pathlib.Path('files/page.html').write_text('<p>x</p>')
        out = self.run_cmd('cp', './files/page.html', 'files/site/page.html')
        self.assertIn('uploaded 8 bytes to files/site/page.html', out)
        self.run_cmd('cp', 'files/site/page.html', './files/copy.html')
        self.assertEqual(pathlib.Path('files/copy.html').read_text(), '<p>x</p>')
        self.assertIn('starts with a bucket name', self.die('cp', 'files/a', 'files/b'))

    def test_every_action_can_print_one_json_object(self):
        pathlib.Path('up.json').write_text('{}')
        pathlib.Path('d').mkdir()
        pathlib.Path('d/x.txt').write_text('x')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            up = json.loads(self.run_cmd('cp', 'up.json', 'files/up.json', '--json'))
            down = json.loads(self.run_cmd('cp', 'files/up.json', 'got.json', '--json'))
            sync = json.loads(self.run_cmd('sync', 'd', 'files/d', '--json'))
            url = json.loads(self.run_cmd('url', 'files/up.json', '--json'))
        self.assertEqual((up['action'], up['direction'], up['bucket'], up['key'], up['bytes'], up['content_type']),
                         ('cp', 'upload', 'files', 'up.json', 2, 'application/json'))
        self.assertEqual(up['url'], 'https://files.example/demo-dev/files/up.json')
        self.assertEqual((down['direction'], down['bytes'], down['file']), ('download', 2, 'got.json'))
        self.assertEqual((sync['action'], sync['uploaded'], sync['unchanged'], sync['dry_run']),
                         ('sync', ['d/x.txt'], 0, False))
        self.assertEqual((url['action'], url['url']), ('url', 'https://files.example/demo-dev/files/up.json'))
        self.assertIn('uploaded d/x.txt', err.getvalue())               # progress went to stderr, not stdout

    def test_a_missing_object_says_why(self):
        self.assertIn('NoSuchKey', self.die('cp', 'files/nope', 'x'))

    def test_ls_lists_one_level_with_prefixes_and_pages(self):
        for key in ('a.txt', 'b.txt', 'c.txt', 'docs/x.md', 'docs/y.md'):
            FakeS3.store[key] = (key.encode(), {})
        out = self.run_cmd('ls', 'files')
        self.assertIn('PRE  docs/', out)
        for name in ('a.txt', 'b.txt', 'c.txt'):                    # three files across two pages
            self.assertIn(name, out)
        self.assertNotIn('docs/x.md', out)
        deep = self.run_cmd('ls', 'files/docs/')
        self.assertIn('docs/x.md', deep)
        everything = json.loads(self.run_cmd('ls', 'files', '-r', '--json'))
        self.assertEqual([o['key'] for o in everything['objects']], ['a.txt', 'b.txt', 'c.txt', 'docs/x.md', 'docs/y.md'])

    def test_sync_uploads_what_changed_and_never_deletes(self):
        site = pathlib.Path('site')
        (site / 'css').mkdir(parents=True)
        (site / 'index.html').write_text('<h1>hi</h1>')
        (site / 'css' / 'a.css').write_text('body{}')
        FakeS3.store['site/old.txt'] = (b'stays', {})
        self.assertIn('2 file(s), 0 unchanged', self.run_cmd('sync', 'site', 'files/site'))
        self.assertIn('site/css/a.css', FakeS3.store)
        FakeS3.seen.clear()
        (site / 'index.html').write_text('<h1>changed</h1>')
        out = self.run_cmd('sync', 'site', 'files/site')
        self.assertIn('1 file(s), 1 unchanged', out)
        self.assertEqual([k for m, k in FakeS3.seen if m == 'PUT'], ['site/index.html'])
        self.assertIn('site/old.txt', FakeS3.store)                  # nothing is deleted
        self.assertEqual(FakeS3.store['site/index.html'][1]['content-type'], 'text/html')

    def test_sync_dry_run_uploads_nothing(self):
        pathlib.Path('d').mkdir()
        pathlib.Path('d/x.txt').write_text('x')
        out = self.run_cmd('sync', 'd', 'files/p', '--dry-run')
        self.assertIn('would upload p/x.txt', out)
        self.assertEqual(FakeS3.store, {})

    def test_url_is_the_public_url_and_private_has_none(self):
        self.assertEqual(self.run_cmd('url', 'files/a b.json').strip(), 'https://files.example/demo-dev/files/a%20b.json')
        self.access = 'private'
        self.conn.pop('url')
        self.conn['access'] = 'private'
        self.assertIn('no public URL', self.die('url', 'files/a.json'))

    def test_a_service_that_is_not_a_bucket_lists_the_ones_that_are(self):
        message = self.die('ls', 'db')
        self.assertIn("'db' is not a bucket block", message)
        self.assertIn('files', message)

    def test_a_signature_the_server_refuses_is_reported_not_swallowed(self):
        self.conn['secret_access_key'] = 'wrong'
        self.assertIn('SignatureDoesNotMatch', self.die('ls', 'files'))


class BucketConnection(unittest.TestCase):
    """Where the keys come from: the local store, OpenBao for dev and prod, never for an internal one remotely."""

    APP = {'name': 'demo', 'services': {'files': {'type': 'bucket', 'access': 'private'},
                                        'scratch': {'type': 'bucket', 'access': 'internal'}}}

    def test_dev_and_prod_read_their_own_openbao_path(self):
        seen = []

        def bao(method, path, token=None, body=None, soft=False):
            seen.append(path)
            return 200, {'data': {'data': {'endpoint': 'https://r2', 'bucket': 'b'}}}
        with mock.patch.object(magehand, 'bao', bao), mock.patch.object(magehand, 'valid_token', lambda: 't'):
            magehand.bucket_connection(self.APP, 'files', 'dev')
            magehand.bucket_connection(self.APP, 'files', 'prod')
        self.assertEqual(seen, ['apps/data/demo-dev/bucket-files', 'apps/data/demo/bucket-files'])

    def test_an_internal_bucket_is_not_reachable_from_a_laptop(self):
        with self.assertRaises(SystemExit) as caught:
            magehand.bucket_connection(self.APP, 'scratch', 'dev')
        self.assertIn('only inside the cluster', str(caught.exception.code))
        self.assertIn('--env local', str(caught.exception.code))

    def test_not_provisioned_yet_says_so(self):
        with mock.patch.object(magehand, 'bao', lambda *a, **k: (404, None)), \
                mock.patch.object(magehand, 'valid_token', lambda: 't'):
            with self.assertRaises(SystemExit) as caught:
                magehand.bucket_connection(self.APP, 'files', 'dev')
        self.assertIn('not provisioned in dev yet', str(caught.exception.code))

    def test_local_needs_dev_up(self):
        with mock.patch.object(magehand, 'local_state', lambda _n: {}):
            with self.assertRaises(SystemExit) as caught:
                magehand.bucket_connection(self.APP, 'files', 'local')
        self.assertIn('magehand dev up', str(caught.exception.code))

    def test_app_summary_says_where_a_bucket_lives(self):
        lines = ' '.join(magehand.bucket_summary('demo', 'files', {'type': 'bucket', 'access': 'public'}))
        self.assertIn('https://files.davidlarrimore.com/demo-dev/files/', lines)
        self.assertIn('Garage', ' '.join(magehand.bucket_summary('demo', 's', {'type': 'bucket', 'access': 'internal'})))


class Resolve(unittest.TestCase):
    def setUp(self):
        self.saved = (magehand.bao, magehand.valid_token, magehand.local_state, magehand.container_env)
        self.paths = []
        magehand.valid_token = lambda: 'tok'
        magehand.local_state = lambda _name: LOCAL
        magehand.container_env = container_env_fake

        def fake_bao(method, path, token=None, body=None):
            self.paths.append(path)
            return 200, {'data': {'data': {'api_key': 'sk-test', 'base_url': 'https://llm.lab/v1'}}}
        magehand.bao = fake_bao
        self.cwd = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        os.chdir(self.tmp.name)

    def tearDown(self):
        magehand.bao, magehand.valid_token, magehand.local_state, magehand.container_env = self.saved
        os.chdir(self.cwd)
        self.tmp.cleanup()
        os.environ.pop('PORT', None)

    def test_resolves_every_source(self):
        env = magehand.resolve_env(APP)
        self.assertEqual(env['LLM_KEY'], 'sk-test')
        self.assertEqual(env['DATABASE_URL'], 'postgresql://app:pw@127.0.0.1:5555/app')
        self.assertEqual(len(env['SESSION_KEY']), 48)              # made for this run
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

    def bucket_app(self):
        return {**APP, 'services': {**APP['services'], 'files': {'type': 'bucket'}},
                'container': {'env': [{'name': 'S3_ENDPOINT',
                                       'valueFrom': {'secretKeyRef': {'name': 'files-connection', 'key': 'endpoint'}}}]}}

    def test_bucket_value_from_magehand_env_is_kept(self):
        pathlib.Path('.magehand.env').write_text('S3_ENDPOINT=http://127.0.0.1:9000\n')
        env = magehand.resolve_env(self.bucket_app())
        self.assertEqual(env['S3_ENDPOINT'], 'http://127.0.0.1:9000')

    def test_bucket_value_from_the_shell_is_not_blocked(self):
        os.environ['S3_ENDPOINT'] = 'http://127.0.0.1:9000'
        try:
            env = magehand.resolve_env(self.bucket_app())   # no SystemExit
        finally:
            os.environ.pop('S3_ENDPOINT')
        self.assertNotIn('S3_ENDPOINT', env)   # the shell's value reaches the app on its own

    def test_bucket_without_a_local_store_warns_and_does_not_stop(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            env = magehand.resolve_env(self.bucket_app())   # no SystemExit: a bucket is not needed to start
        self.assertNotIn('S3_ENDPOINT', env)
        self.assertIn('no local object store', err.getvalue())
        self.assertIn('magehand dev up', err.getvalue())

    def test_bucket_with_a_local_store_gets_its_keys(self):
        local = {**LOCAL, 'files': {'port': '4444', 'admin_port': '4445', 'bucket': 'demo-files',
                                    'access_key_id': 'GKx', 'container': 'magehand-demo-files',
                                    'access': 'private'}}   # no secret on disk: it is asked of Garage
        asked = lambda port, token, method, path, body=None: (200, {'secretAccessKey': 's3cret'})  # noqa: E731
        with mock.patch.object(magehand, 'local_state', lambda _n: local), \
                mock.patch.object(magehand, 'container_env', lambda _c: {'GARAGE_ADMIN_TOKEN': 't'}), \
                mock.patch.object(magehand, 'garage_admin', asked):
            env = magehand.resolve_env(self.bucket_app())
            self.assertEqual(env['S3_ENDPOINT'], 'http://127.0.0.1:4444')
            env = magehand.resolve_env(self.bucket_app(), container=True)   # over the app's Docker network
            self.assertEqual(env['S3_ENDPOINT'], 'http://magehand-demo-files:3900')

    def test_missing_key_fails(self):
        bad = {**APP, 'container': {'env': [
            {'name': 'X', 'valueFrom': {'secretKeyRef': {'name': 'ai-connection', 'key': 'nope'}}}]}}
        with self.assertRaises(SystemExit):
            magehand.resolve_env(bad)


class MissingKey(unittest.TestCase):
    """No dev key in OpenBao: say why (not registered, not deployed, being created) instead of a bare 404."""

    def setUp(self):
        self.saved = magehand.APPS_DIR, magehand.bao, magehand.valid_token, magehand.local_state, magehand.container_env
        self.apps = tempfile.TemporaryDirectory()
        magehand.APPS_DIR = self.apps.name
        magehand.bao = lambda *_a, **_k: (404, None)
        magehand.valid_token = lambda: 'tok'
        magehand.local_state = lambda _name: LOCAL
        magehand.container_env = container_env_fake

    def tearDown(self):
        magehand.APPS_DIR, magehand.bao, magehand.valid_token, magehand.local_state, magehand.container_env = self.saved
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


class LocalNetworkBlocked(unittest.TestCase):
    """macOS Local Network privacy: "No route to host" for this Python while Apple's curl gets an answer."""

    def blocked(self, error, curl=0, platform='darwin'):
        def urlopen(*_a, **_k):
            raise error
        curls = []

        def run(argv, **_k):
            curls.append(argv)
            return subprocess.CompletedProcess(argv, curl, b'', b'')
        with mock.patch.object(magehand.sys, 'platform', platform), \
                mock.patch.object(magehand.urllib.request, 'urlopen', urlopen), \
                mock.patch.object(magehand.subprocess, 'run', run):
            return magehand.local_network_blocked(), curls

    no_route = urllib.error.URLError(OSError(errno.EHOSTUNREACH, 'No route to host'))

    def test_no_route_here_while_curl_answers(self):
        found, curls = self.blocked(self.no_route)
        self.assertTrue(found)
        self.assertEqual(curls[0][0], '/usr/bin/curl')  # Apple's, which the permission doesn't apply to

    def test_anything_else_is_not_the_permission(self):
        self.assertFalse(self.blocked(self.no_route, curl=7)[0])  # curl can't reach it either: the network
        self.assertFalse(self.blocked(urllib.error.URLError(OSError(errno.ECONNREFUSED, 'refused')))[0])
        self.assertFalse(self.blocked(urllib.error.URLError(socket.gaierror(8, 'nodename nor servname')))[0])
        self.assertEqual(self.blocked(self.no_route, platform='linux'), (False, []))


    def test_apple_s_python_and_when_setup_can_move_onto_it(self):
        xcode = '/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3.9'
        for exe, apple in ((xcode, True), ('/Library/Developer/CommandLineTools/usr/bin/python3', True),
                           ('/opt/homebrew/Cellar/python@3.14/3.14.0/bin/python3.14', False),
                           ('/Users/me/.local/share/uv/python/cpython-3.13/bin/python3.13', False)):
            with mock.patch.object(magehand.os.path, 'realpath', lambda _p, exe=exe: exe):
                self.assertEqual(magehand.on_apple_python(), apple, exe)
        brew = '/opt/homebrew/bin/python3.14'

        def can(method='uv', uv='/opt/homebrew/bin/uv', tools=0, exe=brew):
            with mock.patch.object(magehand.sys, 'platform', 'darwin'), \
                    mock.patch.object(magehand.os.path, 'realpath', lambda _p: exe), \
                    mock.patch.object(magehand, 'install_method', lambda *a: method), \
                    mock.patch.object(magehand.shutil, 'which', lambda _c: uv), \
                    mock.patch.object(magehand.os.path, 'exists', lambda _p: True), \
                    mock.patch.object(magehand.subprocess, 'run',
                                      lambda argv, **k: subprocess.CompletedProcess(argv, tools)):
                return magehand.can_move_to_apple_python()
        self.assertTrue(can())
        self.assertFalse(can(exe=xcode))  # already there
        self.assertFalse(can(method='pipx'))
        self.assertFalse(can(uv=None))
        self.assertFalse(can(tools=2))  # no Command Line Tools: /usr/bin/python3 is only their installer

    def test_the_move_reinstalls_unpinned_on_apple_s_python_then_runs_setup(self):
        runs = []

        def run(argv, **_k):
            runs.append(argv)
            return subprocess.CompletedProcess(argv, 0)
        with mock.patch.object(magehand.subprocess, 'run', run), contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit) as stop:
            magehand.move_to_apple_python()
        self.assertEqual(stop.exception.code, 0)
        self.assertEqual(runs[0], ['uv', 'tool', 'install', '--reinstall', '--python', '/usr/bin/python3', 'magehand'])
        self.assertEqual(runs[1][1:], ['setup'])

    def test_doctor_names_the_permission(self):
        def urlopen(*_a, **_k):
            raise self.no_route
        with mock.patch.object(magehand.sys, 'platform', 'darwin'), \
                mock.patch.object(magehand.urllib.request, 'urlopen', urlopen), \
                mock.patch.object(magehand.subprocess, 'run', lambda argv, **k: subprocess.CompletedProcess(argv, 0)), \
                mock.patch.object(magehand, 'github_token', lambda: 't'), \
                mock.patch.object(magehand, 'saved_token', lambda: None), \
                mock.patch.object(magehand, 'current_repo', lambda *a: None), \
                mock.patch.object(magehand, 'configured_runtime', lambda: 'none'), \
                mock.patch.object(magehand, 'choose_runtime', lambda configured, found: None):
            reports = []
            for movable in (False, True):
                with mock.patch.object(magehand, 'can_move_to_apple_python', lambda movable=movable: movable):
                    code, out, _ = CommandLine.run_main(self, 'doctor', '--json')
                reports.append((code, next(c for c in json.loads(out)['checks'] if c['check'].startswith('reach '))))
        (code, reach), (_, movable) = reports
        self.assertEqual((code, reach['ok']), (1, False))
        self.assertIn('Privacy & Security > Local Network', reach['hint'])
        self.assertIn("`magehand setup` (it moves magehand onto Apple's Python", movable['hint'])


class Setup(unittest.TestCase):
    def setUp(self):
        self.names = ('reachable', 'lab_address', 'dns_answer', 'sudo_write_resolver', 'github_token',
                      'saved_token', 'cmd_login', 'shutil', 'install_skill', 'local_network_blocked',
                      'can_move_to_apple_python', 'move_to_apple_python')
        self.saved = {n: getattr(magehand, n) for n in self.names}
        magehand.local_network_blocked = lambda: False
        magehand.can_move_to_apple_python = lambda: False
        self.moved = []
        magehand.move_to_apple_python = lambda: self.moved.append(1) or sys.exit(0)
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

    def test_a_local_network_block_is_named_not_blamed_on_dns(self):
        # the lab resolves and curl reaches it, but macOS refuses this Python: --dns would change nothing
        magehand.reachable = lambda: False
        magehand.local_network_blocked = lambda: True
        magehand.lab_address = lambda _h: '192.0.2.10'
        with self.assertRaises(SystemExit) as stop:  # not a uv install: the permission is the only fix
            self.run_setup([])
        self.assertIn('Privacy & Security > Local Network', str(stop.exception))
        self.assertNotIn('--dns', str(stop.exception))
        self.assertEqual((self.written, self.moved), ([], []))

    def test_a_local_network_block_moves_magehand_onto_apple_s_python(self):
        magehand.reachable = lambda: False
        magehand.local_network_blocked = lambda: True
        magehand.can_move_to_apple_python = lambda: True
        with self.assertRaises(SystemExit):
            self.run_setup([])
        self.assertEqual((self.moved, self.written, self.logins), ([1], [], []))  # the moved copy's setup goes on

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


CATALOG = {'blocks': {'postgres': {'sizes': ['1Gi', '2Gi']}, 'redis': {}, 'secret': {}, 'llm': {}, 'bucket': {}},
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

    def bucket_app(self, env, **service):
        return check_app(env, services={'files': dict({'type': 'bucket'}, **service)})

    def test_bucket_keys_map_to_the_connection_secret(self):
        env = [ref(var, 'files-connection', key) for var, key in
               [('S3_ENDPOINT', 'endpoint'), ('S3_BUCKET', 'bucket'), ('S3_KEY', 'access_key_id'),
                ('S3_SECRET', 'secret_access_key'), ('S3_REGION', 'region')]]
        self.assertEqual(magehand.check_manifests(self.bucket_app(env, access='private'), CATALOG), [])

    def test_bucket_unknown_key(self):
        found = magehand.check_manifests(self.bucket_app([ref('X', 'files-connection', 'password')]), CATALOG)
        self.assertIn("has no key 'password'", texts(found, 'error')[0])

    def test_bucket_url_needs_access_public(self):
        catalog = {'blocks': {'bucket': {'keys': ['endpoint', 'url'], 'key_options': {'url': 'access'}}}}
        env = [ref('PUBLIC_URL', 'files-connection', 'url')]
        for service in ({}, {'access': 'private'}):
            found = magehand.check_manifests(self.bucket_app(env, **service), catalog)
            self.assertIn('`access: public`', texts(found, 'error')[0])
        self.assertEqual(magehand.check_manifests(self.bucket_app(env, access='public'), catalog), [])

    def test_boolean_key_option_still_needs_true(self):
        # an enumerated option's rule must not loosen the boolean ones
        app = check_app([ref('SEARCH', 'ai-connection', 'search_url')])
        app['services']['ai']['search'] = False
        self.assertIn('search: true', texts(magehand.check_manifests(app, CATALOG), 'error')[0])

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

    def test_bucket_blocks_are_validated_against_the_catalog(self):
        catalog = dict(CATALOG, blocks=dict(CATALOG['blocks'], bucket={
            'access_values': ['public', 'private', 'internal'], 'writer_values': ['openclaw']}))

        def errors(name='files', **svc):
            app = check_app([], services={name: dict({'type': 'bucket'}, **svc)})
            return ' | '.join(texts(magehand.check_manifests(app, catalog), 'error'))
        self.assertEqual(errors(access='internal'), '')
        self.assertEqual(errors(access='public', writers=['openclaw']), '')
        self.assertIn("access 'secret' not offered", errors(access='secret'))
        self.assertIn('writers must be a list', errors(writers=['mallory']))
        self.assertIn('writers must be a list', errors(writers='openclaw'))
        self.assertIn('not available on an internal bucket', errors(access='internal', writers=['openclaw']))
        self.assertIn('a bucket name is lowercase', errors(name='My_Files'))

    def test_model_type_size_and_image(self):
        app = check_app([], services={'ai': {'type': 'llm', 'models': ['nope']}, 'db': {'type': 'postgres', 'size': '9Gi'},
                                      'q': {'type': 'kafka'}}, image='nginx:latest')
        errors = ' | '.join(texts(magehand.check_manifests(app, CATALOG), 'error'))
        for part in ("model 'nope'", "size '9Gi'", "unknown type 'kafka'", 'image nginx:latest'):
            self.assertIn(part, errors)


# A Dockerfile the build accepts: pinned bases, a test stage before the final one, locked installs, non-root.
GOOD_DOCKERFILE = (f"FROM python:3.13-slim@sha256:{'a' * 64} AS base\n"
                   'COPY requirements.txt .\n'
                   'RUN pip install --no-cache-dir --require-hashes -r requirements.txt\n'
                   'FROM base AS test\n'
                   'RUN python -m unittest\n'
                   'FROM base\n'
                   'USER 65532\n')


class CheckCode(unittest.TestCase):
    def run_check(self, files, env=()):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'Dockerfile').write_text(GOOD_DOCKERFILE)
            (root / 'ui-check.yaml').write_text('x')
            for name, text in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text)
            return magehand.check_code(root, check_app(list(env)))

    def test_dockerfile_rules_of_the_build(self):
        good = GOOD_DOCKERFILE
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)

            def found(text):
                (root / 'Dockerfile').write_text(text)
                return [f['text'] for f in magehand.check_dockerfile(root)]
            self.assertEqual(found(good), [])
            self.assertIn("base image 'python:3.13-slim' must be pinned", found(good.replace(f"@sha256:{'a' * 64}", ''))[0])
            self.assertEqual(found(good.replace('FROM python', 'FROM --platform=$BUILDPLATFORM python')), [])
            self.assertEqual(found('FROM scratch AS test\nFROM scratch\nUSER 1\n'), [])
            self.assertIn('add a stage named test', found(good.replace(' AS test', ' AS checks'))[0])
            self.assertIn('must come before the final', found(good + 'FROM base AS test\n')[0])
            self.assertIn('needs USER', found(good.replace('USER 65532', 'USER root'))[0])
            self.assertIn('needs USER', found(good.replace('USER 65532', 'USER 0:0'))[0])
            self.assertIn('needs USER', found(good.replace('USER 65532\n', ''))[0])
            # The final stage's own user, not the last USER anywhere (a gap in CI's version of this rule).
            self.assertIn('needs USER', found('FROM scratch AS test\nUSER 65532\nFROM scratch\n')[0])
            inherited = (f"FROM python@sha256:{'a' * 64} AS base\nUSER 65532\nFROM base AS test\n"
                         'FROM base\n')
            self.assertEqual(found(inherited), [])
            self.assertIn('needs USER', found(inherited.replace('USER 65532', 'USER 65532\nUSER root'))[0])

    def test_dependencies_only_from_a_lockfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for run, why in (('pip install flask', '--require-hashes'), ('npm install', 'npm ci'),
                             ('uv sync', '--frozen'), ('yarn', 'yarn needs'), ('pnpm add x', 'pnpm add'),
                             ('npm ci && pip install \\\n  flask', '--require-hashes')):
                (root / 'Dockerfile').write_text(GOOD_DOCKERFILE.replace('RUN python -m unittest', f'RUN {run}'))
                texts = [f['text'] for f in magehand.check_dockerfile(root)]
                self.assertTrue(texts and why in texts[0], (run, texts))
            for run in ('npm ci', 'uv sync --frozen', 'yarn install --immutable', 'pnpm install --frozen-lockfile',
                        'python -m pip install --require-hashes -r r.txt', 'apt-get install -y curl'):
                (root / 'Dockerfile').write_text(GOOD_DOCKERFILE.replace('RUN python -m unittest', f'RUN {run}'))
                self.assertEqual(magehand.check_dockerfile(root), [], run)

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
    (root / 'deploy/dev').mkdir()
    (root / 'deploy/dev/kustomization.yaml').write_text('namespace: demo-dev\nresources: [../base]\n')
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

    def test_a_base_without_the_dev_overlay_is_an_error(self):
        # CI (build-app.yaml) fails without it, and the platform can't register the app: check must say so first.
        with tempfile.TemporaryDirectory() as tmp:
            root = deploy_repo(tmp)
            (root / 'deploy/dev/kustomization.yaml').unlink()
            found = magehand.check_deploy_files(root, 'demo')
        self.assertEqual([(f['level'], f['where']) for f in found], [('error', 'deploy/dev/kustomization.yaml')])
        self.assertIn('namespace: demo-dev', found[0]['text'])

    def rendered(self, dev, prod=None, failed=None):
        """check_rendered on a deploy/ whose overlays render to `dev` and `prod` (lists of documents)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = deploy_repo(tmp)
            if prod is not None:
                (root / 'deploy/prod').mkdir()
                (root / 'deploy/prod/kustomization.yaml').write_text('namespace: demo\nresources: [../base]\n')
            answers = {'dev': dev, 'prod': prod}
            render = lambda local, overlay: ('failed', failed) if failed else (answers[overlay], None)  # noqa: E731
            with mock.patch.object(magehand, 'render_overlay', render), \
                    mock.patch.object(magehand, 'check_admission', lambda name, rendered: ([], [])), \
                    mock.patch.object(magehand, 'check_schemas', lambda rendered: ([], [])):
                return magehand.check_rendered(root, 'demo')

    DEV_DEPLOYMENT = {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': 'demo', 'namespace': 'demo-dev'},
                      'spec': {'template': {'spec': {'containers': [{'name': 'app'}]}}}}

    @staticmethod
    def ingress(host, middlewares, namespace='demo-dev'):
        return {'apiVersion': 'networking.k8s.io/v1', 'kind': 'Ingress',
                'metadata': {'name': 'web', 'namespace': namespace,
                             'annotations': {'traefik.ingress.kubernetes.io/router.middlewares': middlewares}},
                'spec': {'rules': [{'host': host}]}}

    def test_rendered_overlays_follow_ci_rules(self):
        lab = magehand.LAB_AUTH
        good = [self.DEV_DEPLOYMENT, self.ingress(f'demo-dev.{magehand.LAB}', lab)]
        self.assertEqual(self.rendered(good), ([], []))
        texts = lambda found: [f['text'] for f in found[0]]  # noqa: E731
        # namespace, kinds, the app container
        moved = dict(self.DEV_DEPLOYMENT, metadata={'name': 'demo', 'namespace': 'other'})
        self.assertIn('namespace must be demo-dev', texts(self.rendered([moved]))[0])
        role = {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'Role', 'metadata': {'name': 'r'}}
        self.assertIn('Role (rbac.authorization.k8s.io) is not allowed', texts(self.rendered(good + [role]))[0])
        unnamed = {**self.DEV_DEPLOYMENT, 'spec': {'template': {'spec': {'containers': [{'name': 'web'}]}}}}
        self.assertIn('container named `app`', texts(self.rendered([unnamed]))[0])
        # Ingress: dev only on its lab host, with the forward auth
        self.assertIn('one host per Ingress', texts(self.rendered([self.ingress('demo.example.com', lab)]))[0])
        self.assertIn('needs the middleware', texts(self.rendered([self.ingress(f'demo-dev.{magehand.LAB}', '')]))[0])
        # prod: a public host strips the headers first and never uses the lab's forward auth
        domain = magehand.LAB.split('.', 1)[1]
        public_ok = self.ingress(f'demo.{domain}', f'{magehand.STRIP_AUTH},{magehand.PUBLIC_AUTH}', 'demo')
        self.assertEqual(self.rendered(good, prod=[public_ok]), ([], []))
        public_bad = self.ingress(f'demo.{domain}', lab, 'demo')
        self.assertIn('a public host needs', texts(self.rendered(good, prod=[public_bad]))[0])

    def test_a_prod_directory_without_its_kustomization_is_an_error(self):
        # CI renders deploy/prod whenever the directory exists: a patch alone fails there.
        with tempfile.TemporaryDirectory() as tmp:
            root = deploy_repo(tmp, extra={'deploy/prod/patch.yaml': 'kind: Deployment\n'})
            found, _ = magehand.check_rendered(root, 'demo')
        self.assertEqual([(f['level'], f['where']) for f in found], [('error', 'deploy/prod/kustomization.yaml')])
        self.assertIn('namespace: demo', found[0]['text'])

    def test_render_failure_is_an_error_and_no_kubectl_a_skip(self):
        found, skipped = self.rendered([], failed='accumulating resources: missing kustomization.yaml')
        self.assertEqual([(f['level'], f['where']) for f in found], [('error', 'deploy/dev')])
        self.assertIn('kustomize cannot render it', found[0]['text'])
        with tempfile.TemporaryDirectory() as tmp:
            root = deploy_repo(tmp)
            self.assertEqual(magehand.check_rendered(root, 'demo'),  # the module's stub: no kubectl
                             ([], ['deploy/dev: not rendered (kubectl is not installed); CI renders and checks it']))

    def test_a_skip_is_an_error_in_ci(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = deploy_repo(tmp)
            (root / 'Dockerfile').write_text(GOOD_DOCKERFILE)
            argv = ['check', 'demo', '--code', str(root), '--manifests-only', '--json']
            with mock.patch.object(magehand, 'github_file', lambda *a, **k: ''):
                local = json.loads(CommandLine.run_main(self, *argv)[1])
                with mock.patch.dict(os.environ, {'CI': 'true'}):
                    ci = json.loads(CommandLine.run_main(self, *argv)[1])
        self.assertEqual(local['skipped'], ['deploy/dev: not rendered (kubectl is not installed); CI renders and checks it'])
        self.assertEqual(ci['skipped'], [])
        self.assertIn(('error', 'deploy/dev'), [(f['level'], f['where']) for f in ci['findings']])

    KYVERNO_FAIL = ('Applying 4 policy rule(s) to 2 resource(s)...\n\n'
                    'policy require-non-root -> resource demo-dev/Deployment/demo failed:\n'
                    '1. autogen-run-as-non-root: validation error: Running as root is not allowed. rule '
                    'autogen-run-as-non-root failed at path /spec/template/spec/securityContext/runAsNonRoot/\n\n'
                    'pass: 7, fail: 1, warn: 0, error: 0, skip: 2\n')

    def cluster_rules(self, kyverno='', kubeconform='', tools=('kyverno', 'kubeconform'), policies=('pods.yaml',),
                      kustomize=0):
        """check_admission and check_schemas on one rendered dev overlay, with the tools answering as given."""
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw))
            if argv[:2] == ['kubectl', 'kustomize']:
                applied = pathlib.Path(argv[2])
                self.kustomization = json.loads((applied / 'kustomization.yaml').read_text())
                return subprocess.CompletedProcess(argv, kustomize, (applied / 'app.yaml').read_text(),
                                                   'Error: bad image' if kustomize else '')
            output = kyverno if argv[0].endswith('kyverno') else kubeconform
            failed = 'fail: 0, warn: 0, error: 0' not in output if argv[0].endswith('kyverno') else bool(output)
            return subprocess.CompletedProcess(argv, int(failed), output, '')
        files = {f'{magehand.POLICY_DIR}/{n}': 'kind: ClusterPolicy\n' for n in policies}
        with mock.patch.object(magehand.subprocess, 'run', run), \
                mock.patch.object(magehand.shutil, 'which', lambda t: f'/usr/local/bin/{t}' if t in tools else None), \
                mock.patch.object(magehand, 'github_dir', lambda repo, path, **k: [n.rsplit('/', 1)[1] for n in files]), \
                mock.patch.object(magehand, 'github_file', lambda repo, path, **k: files[path]):
            rendered = [('dev', [self.DEV_DEPLOYMENT])]
            admission, schemas = magehand.check_admission('demo', rendered), magehand.check_schemas(rendered)
        return admission, schemas, calls

    def test_the_cluster_s_admission_policies_run_as_ci_runs_them(self):
        ok = 'Applying 4 policy rule(s) to 2 resource(s)...\n\npass: 8, fail: 0, warn: 0, error: 0, skip: 2\n'
        admission, _, calls = self.cluster_rules(kyverno=ok)
        self.assertEqual(admission, ([], []))
        apply = calls[1][0]
        self.assertEqual((apply[1], apply[3]), ('apply', '--resource'))
        self.assertTrue(apply[2].endswith('/policies'))
        # as Argo CD applies it: the app's image pinned by a digest, and the Application's tracking id
        self.assertEqual(self.kustomization['images'], [{'name': 'ghcr.io/davidlarrimore/homelab-apps/demo',
                                                         'digest': magehand.POLICY_DIGEST}])
        self.assertEqual(self.kustomization['commonAnnotations'],
                         {'argocd.argoproj.io/tracking-id': 'demo-dev:policy-check'})

    def test_a_refusal_names_the_policy_the_resource_and_why(self):
        (found, skipped), _, _ = self.cluster_rules(kyverno=self.KYVERNO_FAIL)
        self.assertEqual(skipped, [])
        self.assertEqual([(f['level'], f['where']) for f in found], [('error', 'deploy/dev')])
        self.assertIn('the cluster would refuse it: require-non-root on demo: autogen-run-as-non-root: validation '
                      'error: Running as root is not allowed', found[0]['text'])
        self.assertIn('"Every workload must"', found[0]['see'])

    def test_kyverno_output_without_a_summary_is_an_error(self):
        (found, _), _, _ = self.cluster_rules(kyverno='Error: failed to load policies\n')
        self.assertIn('Kyverno gave no result: Error: failed to load policies', found[0]['text'])

    def test_a_render_kustomize_cannot_pin_is_an_error(self):
        (found, _), _, _ = self.cluster_rules(kustomize=1)
        self.assertIn('cannot prepare it for the admission policies: Error: bad image', found[0]['text'])

    def test_missing_tools_and_policies_are_skips_with_how_to_install(self):
        admission, schemas, calls = self.cluster_rules(tools=())
        self.assertEqual((admission[0], schemas[0], calls), ([], [], []))
        self.assertIn('kyverno is not installed: `brew install kyverno`', admission[1][0])
        self.assertIn('kubeconform is not installed: `brew install kubeconform`', schemas[1][0])
        admission, _, _ = self.cluster_rules(policies=())
        self.assertIn('cannot read davidlarrimore/homelab-apps .github/kyverno/policies', admission[1][0])

    def test_schemas_as_ci_checks_them(self):
        _, schemas, calls = self.cluster_rules(kyverno='pass: 1, fail: 0, warn: 0, error: 0')
        self.assertEqual(schemas, ([], []))
        argv, kw = next((argv, kw) for argv, kw in calls if argv[0].endswith('kubeconform'))
        self.assertEqual(argv[1:5], ['-strict', '-summary', '-schema-location', 'default'])
        self.assertIn('datreeio/CRDs-catalog', argv[6])
        self.assertIn('kind: Deployment', kw['input'])  # the render, on stdin
        bad = ('stdin - Deployment demo is invalid: problem validating schema. Check JSON formatting: '
               "jsonschema: '/spec/template/spec/containers/0' does not validate: additionalProperties 'imagePullPolicyy' "
               'not allowed\nSummary: 1 resource found parsing stdin - Valid: 0, Invalid: 1, Errors: 0, Skipped: 0\n')
        _, (found, _), _ = self.cluster_rules(kubeconform=bad)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]['text'].startswith('Deployment demo is invalid'))
        self.assertIn("'imagePullPolicyy' not allowed", found[0]['text'])
        offline = ('stdin - Deployment demo failed validation: could not find schema for Deployment\n'
                   'Summary: 1 resource found parsing stdin - Valid: 0, Invalid: 0, Errors: 1, Skipped: 0\n')
        _, (found, skipped), _ = self.cluster_rules(kubeconform=offline)
        self.assertEqual(found, [])
        self.assertIn('schemas not checked (cannot download them here)', skipped[0])

    def test_strict_makes_every_skip_an_error_and_ci_only_the_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = deploy_repo(tmp)
            (root / 'Dockerfile').write_text(GOOD_DOCKERFILE)
            argv = ['check', 'demo', '--code', str(root), '--manifests-only', '--json']
            missing = [magehand.tool_missing('kyverno', 'admission policies')]
            with mock.patch.object(magehand, 'github_file', lambda *a, **k: ''), \
                    mock.patch.object(magehand, 'check_rendered', lambda root, name: ([], list(missing))):
                with mock.patch.dict(os.environ, {'CI': 'true'}):
                    ci = json.loads(CommandLine.run_main(self, *argv)[1])
                    strict = CommandLine.run_main(self, *argv, '--strict')
        self.assertEqual((ci['skipped'], ci['errors']), (missing, 0))  # CI today: kyverno runs in its own step
        code, out = strict[0], json.loads(strict[1])
        self.assertEqual((code, out['skipped'], out['errors']), (1, [], 1))
        self.assertIn("--strict runs every rule CI's checks run", out['findings'][0]['text'])

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
            (root / 'Dockerfile').write_text(GOOD_DOCKERFILE)
            (root / 'ui-check.yaml').write_text('x')
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

    def fake(self, exists=None, collaborator=False, ruleset=False, ruleset_status=0, existing=None, invited=False):
        """`existing` is the ruleset already on the repo (default: ours when `ruleset`); `collaborator` is True
        (write), 'read' or False, and `invited` says an invitation for the bot is pending."""
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
                return (0, '', '') if collaborator else (1, '', 'gh: Not Found (HTTP 404)')
            if args[:1] == ('api',) and args[1].endswith('/collaborators/davidlarrimore-bot/permission'):
                return 0, {'read': 'read read', 'maintain': 'write maintain'}.get(collaborator, 'write write') + '\n', ''
            if args[:1] == ('api',) and args[1].startswith('repos/davidlarrimore/elvis/invitations'):
                return 0, json.dumps([{'invitee': {'login': 'davidlarrimore-bot'}}] if invited else []), ''
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

    def test_repairs_an_existing_repo_and_says_what_is_left(self):
        # issue #44: a repo made without `new` has neither the bot nor the ruleset.
        import contextlib
        import io
        self.fake(exists='false')
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            magehand.cmd_new(magehand.build_parser().parse_args(['new', 'elvis', '--no-clone']))
        self.assertIn(('api', '-X', 'PUT', 'repos/davidlarrimore/elvis/collaborators/davidlarrimore-bot',
                       '-f', 'permission=push'), self.calls)
        self.assertIn(('api', '-X', 'POST', 'repos/davidlarrimore/elvis/rulesets', '--input', '-'), self.calls)
        self.assertIn('invited with write', err.getvalue())
        self.assertIn("ruleset 'main: owner approves' added", err.getvalue())
        left = out.getvalue()
        self.assertIn('--visibility private', left)
        self.assertIn('magehand pr ready N', left)
        self.assertNotIn('write: the code', left)  # not the new-repo steps

    def test_a_read_only_or_invited_bot_is_given_write_again(self):
        import contextlib
        import io
        for collaborator, invited, said in (('read', False, 'raised from read to write'),
                                            (False, True, 'invitation renewed')):
            self.calls.clear()
            self.fake(exists='true', collaborator=collaborator, ruleset=True, invited=invited)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.run_new('elvis', '--no-clone')
            self.assertIn(said, err.getvalue())
            self.assertEqual([c for c in self.calls if '-X' in c],
                             [('api', '-X', 'PUT', 'repos/davidlarrimore/elvis/collaborators/davidlarrimore-bot',
                               '-f', 'permission=push')])

    def test_nothing_left_on_a_set_up_repo_and_no_clone_inside_its_checkout(self):
        import contextlib
        import io
        self.fake(exists='true', collaborator=True, ruleset=True)
        saved = magehand.current_repo
        magehand.current_repo = lambda *a: 'davidlarrimore/elvis'
        self.addCleanup(setattr, magehand, 'current_repo', saved)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            magehand.cmd_new(magehand.build_parser().parse_args(['new', 'elvis']))
        self.assertIn('nothing left for you', out.getvalue())
        self.assertFalse([c for c in self.calls if c[:2] == ('repo', 'clone')])

    def test_a_maintainer_bot_is_left_alone(self):
        self.fake(exists='true', collaborator='maintain', ruleset=True)
        self.run_new('elvis', '--no-clone')
        self.assertFalse([c for c in self.calls if '-X' in c])

    def test_repair_summary_keeps_an_app_name_it_could_not_write(self):
        # `new REPO --app-name APP` on an existing repo whose deploy/app.yaml names another app exits 1: the
        # summary must not say there is nothing left.
        for named in ('differs', 'failed'):
            text = magehand.repair_summary('davidlarrimore/lab-website', True, 'collaborator', 'exists', None,
                                           pathlib.Path('lab-website'), 'lab', named)
            self.assertIn('app: "lab"', text)
            self.assertNotIn('nothing left', text)

    def test_an_archived_repo_is_a_retired_app_and_left_alone(self):
        self.fake(exists='true\ntrue')
        with self.assertRaises(SystemExit):
            self.run_new('elvis', '--no-clone')
        self.assertFalse([c for c in self.calls if '-X' in c])

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
                # Offline: the failure is remembered (with the last version known) and retried after an hour
                magehand.LATEST_CACHE.write_text(json.dumps({'checked': magehand.time.time() - 2 * 86400,
                                                             'version': '9.9.8'}))
                with mock.patch.object(magehand.urllib.request, 'urlopen', mock.Mock(side_effect=OSError('offline'))) as net:
                    self.assertEqual((magehand.latest_version(), magehand.latest_version()), ('9.9.8', '9.9.8'))
                self.assertEqual(net.call_count, 1)
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
            with mock.patch.dict(os.environ, {'AI_AGENT': 'claude-code'}):  # an agent reads stderr: it is told
                stream = io.StringIO()
                with contextlib.redirect_stderr(stream):
                    magehand.update_notice()
                self.assertIn('magehand upgrade (tell the user', stream.getvalue())
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
        for text in ('magehand pr ready', 'magehand update-agents', 'gh pr create --draft', 'gh pr ready N --undo',
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
        """Run repo_state with gh answering from `answers` {path suffix: (code, out, err)}, over a private repo
        on which the bot, once a collaborator, can write."""
        saved = magehand.gh_cli
        answers = dict({'': lambda args: (0, 'main\n' if '.default_branch' in args else 'true\n', ''),
                        BOT_PATH + '/permission': (0, 'write write\n', ''), '/invitations': (0, '[]', '')}, **answers)

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
        return {'/rulesets': self.LISTED, '/rulesets/7': (0, json.dumps(detail or self.GOOD), ''),
                '/rules/branches/main': applied, BOT_PATH: bot}

    def test_repo_state(self):
        ok = self.with_gh(self.answers())
        self.assertEqual((ok['ruleset'], ok['bot'], ok['private']), ('ok', 'write', True))
        gone = (1, '', 'gh: Not Found (HTTP 404)')
        none = self.with_gh({'/rulesets': (0, '[]', ''), BOT_PATH: gone, '': (0, 'false\n', '')})
        self.assertEqual((none['ruleset'], none['bot'], none['private']), ('missing', 'missing', False))

    def test_an_archived_repo_is_not_checked(self):
        state = self.with_gh({'': (0, 'true\ntrue\n', '')})  # any other call would raise
        self.assertEqual((state['archived'], state['bot'], state['ruleset']), (True, None, None))

    def test_the_bot_needs_write_and_a_pending_invitation_is_not_missing(self):
        # issue #44: a repo without the bot made `gh pr edit --add-reviewer` add nobody, and nothing said so.
        gone = (1, '', 'gh: Not Found (HTTP 404)')
        self.assertEqual(self.with_gh(self.answers(bot=gone))['bot'], 'missing')
        for answer, state in (('read read', 'read'), ('read triage', 'read'), ('write maintain', 'write'),
                              ('admin admin', 'write')):
            got = self.with_gh(dict(self.answers(), **{BOT_PATH + '/permission': (0, answer + '\n', '')}))['bot']
            self.assertEqual(got, state, answer)
        invited = (0, json.dumps([{'invitee': {'login': 'someone-else'}}, {'invitee': {'login': magehand.BOT},
                                                                           'permissions': 'write'}]), '')
        self.assertEqual(self.with_gh(dict(self.answers(bot=gone), **{'/invitations': invited}))['bot'], 'invited')
        # Invitations only an admin can list: unknown, not missing.
        refused = (1, '', 'gh: Must have admin rights to Repository. (HTTP 403)')
        self.assertIsNone(self.with_gh(dict(self.answers(bot=gone), **{'/invitations': refused}))['bot'])

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
            state = self.with_gh({'/rulesets': (code, '', err), BOT_PATH: (code, '', err), '': (code, '', err)})
            self.assertEqual((state['ruleset'], state['bot'], state['private']), (None, None, None), err)

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
        self.assertEqual([c['ok'] for c in report['checks'] if 'elvis' in c['check']], [None, None, None])
        self.assertEqual(len(calls), 1)  # only the (mocked, failing) OpenBao health probe was attempted


REAL_LIFECYCLE = magehand.lifecycle_stages
REAL_STALE_AGENTS = magehand.stale_agents


def setUpModule():
    """No test renders deploy/ with this machine's kubectl (the tests run the same everywhere); the render rules'
    tests give check_rendered its documents."""
    patch = mock.patch.object(magehand, 'render_overlay', lambda local, overlay: (None, 'kubectl is not installed'))
    patch.start()
    unittest.addModuleCleanup(patch.stop)
    # status's lifecycle reads GitHub: its own tests call REAL_LIFECYCLE with GitHub mocked.
    patch = mock.patch.object(magehand, 'lifecycle_stages', lambda repo, name: [])
    patch.start()
    unittest.addModuleCleanup(patch.stop)
    # doctor reads every app repo's AGENTS.md from GitHub: its own tests call REAL_STALE_AGENTS with gh mocked.
    patch = mock.patch.object(magehand, 'stale_agents', lambda: (None, 'not read in tests'))
    patch.start()
    unittest.addModuleCleanup(patch.stop)
    # A coding agent running the tests (Claude Code, Codex) sets these: the tests run as in a plain shell.
    agents = {k: os.environ.pop(k) for k in magehand.AGENT_VARS if k in os.environ}
    unittest.addModuleCleanup(lambda: os.environ.update(agents))
    ci = os.environ.pop('CI', None)  # GitHub Actions sets it, and check treats a skip as an error there
    unittest.addModuleCleanup(lambda: ci is not None and os.environ.update(CI=ci))
    # No test writes this machine's Claude Code or Codex config (setup's skill and approvals): both point at a
    # temporary directory, where Codex is not installed unless a test makes its directory.
    config = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(config.cleanup)
    saved = {k: os.environ.get(k) for k in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME')}
    os.environ.update(CLAUDE_CONFIG_DIR=os.path.join(config.name, 'claude'), CODEX_HOME=os.path.join(config.name, 'codex'))
    unittest.addModuleCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                       for k, v in saved.items()])


class PrReady(unittest.TestCase):
    """`pr ready`: only on green checks, and the review request is confirmed, never assumed."""

    GREEN = [{'name': 'tests', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]

    def run_pr(self, checks=GREEN, draft=True, author='davidlarrimore', requested_before=False, sticks=True,
               access='write', pushed=False, owner='davidlarrimore', json_out=False, refused=None):
        state = {'requested': requested_before, 'reads': 0}
        writes = []

        def pr():
            state['reads'] += 1
            head = 'b' * 40 if pushed and state['reads'] > 1 else 'a' * 40
            return {'number': 12, 'url': f'https://github.com/{owner}/demo/pull/12', 'state': 'OPEN',
                    'isDraft': draft, 'headRefOid': head, 'author': {'login': author}, 'statusCheckRollup': checks,
                    'reviewRequests': [{'login': magehand.BOT}] if state['requested'] else []}

        def gh(*args, **kw):
            writes.append(args)
            if args[:3] == ('api', '-X', 'POST'):
                if refused:
                    return 1, '', refused
                if sticks:
                    state['requested'] = True
            return 0, '', ''
        with mock.patch.object(magehand, 'gh_query', lambda *a: (pr(), None)), \
                mock.patch.object(magehand, 'gh_cli', gh), \
                mock.patch.object(magehand, 'bot_access', lambda repo: (access, 'HTTP 403' if access is None else '')):
            code, out, _ = CommandLine.run_main(self, 'pr', 'ready', '12', *(['--json'] if json_out else []))
        return code, out, writes

    def test_refuses_until_checks_are_green(self):
        for checks, why in (([{'name': 'tests', 'status': 'IN_PROGRESS'}], 'still running: tests'),
                            ([{'name': 'tests', 'status': 'COMPLETED', 'conclusion': 'FAILURE'}], 'not green: tests (failure)'),
                            ([{'name': 'deploy', 'status': 'COMPLETED', 'conclusion': 'ACTION_REQUIRED'}],
                             'not green: deploy (action_required)'),
                            ([{'name': 'tests', 'status': 'COMPLETED', 'conclusion': 'STALE'}], 'not green: tests (stale)'),
                            ([{'name': 'tests', 'status': 'COMPLETED'}], 'not green: tests (no conclusion)'),
                            ([{'context': 'policy', 'state': 'ERROR'}], 'not green: policy (error)'),
                            ([{'context': 'policy', 'state': 'PENDING'}], 'still running: policy'),
                            ([], 'no checks have reported yet')):
            code, out, writes = self.run_pr(checks=checks)
            self.assertEqual((code, writes), (1, []), why)
            self.assertIn(why, out)
            self.assertTrue(out.splitlines()[-1].startswith('next: '), why)

    def test_neutral_and_skipped_checks_are_green(self):
        checks = self.GREEN + [{'name': 'docs', 'status': 'COMPLETED', 'conclusion': 'SKIPPED'},
                               {'name': 'lint', 'status': 'COMPLETED', 'conclusion': 'NEUTRAL'},
                               {'context': 'policy', 'state': 'SUCCESS'}]
        self.assertEqual(self.run_pr(checks=checks)[0], 0)

    def test_requests_confirms_then_readies(self):
        # the request first, on the draft: a request that didn't stick never leaves a ready PR without its reviewer
        code, out, writes = self.run_pr()
        self.assertEqual(code, 0)
        self.assertEqual([w[:2] for w in writes], [('api', '-X'), ('pr', 'ready')])
        self.assertEqual(writes[0][3:], (f'repos/davidlarrimore/demo/pulls/12/requested_reviewers',
                                         '-f', f'reviewers[]={magehand.BOT}'))
        self.assertIn(f'requested {magehand.BOT} (confirmed)', out)
        self.assertIn("wait for OpenClaw's review", out.splitlines()[-1])

    def test_a_request_that_did_not_stick_says_why_and_leaves_the_draft(self):
        for access, why, step in (('missing', 'is not a collaborator', 'magehand new demo'),
                                  ('read', 'can only read', 'magehand new demo'),
                                  ('invited', 'has not accepted', 'wait about 5 minutes'),
                                  ('write', 'though the bot can write', 'request it on the PR page'),
                                  (None, "could not check davidlarrimore-bot's access", '`magehand doctor`')):
            code, out, writes = self.run_pr(sticks=False, access=access)
            self.assertEqual(code, 1, access)
            self.assertEqual([w[:2] for w in writes], [('api', '-X')], access)  # not marked ready
            self.assertIn('NOT recorded', out)
            self.assertIn(why, out)
            self.assertIn(step, out.splitlines()[-1])
        # another owner's repo: magehand new can't repair it
        _, out, _ = self.run_pr(sticks=False, access='missing', owner='someone')
        self.assertIn(f'ask someone to add {magehand.BOT} with write', out)
        report = json.loads(self.run_pr(sticks=False, access='missing', json_out=True)[1])
        self.assertEqual((report['ok'], report['requested'], report['changed']), (False, False, []))

    def test_a_refused_request_is_diagnosed_like_a_silent_one(self):
        # REST refuses a bot that isn't a collaborator (HTTP 422): the read-back says why, as for a silent failure
        code, out, writes = self.run_pr(refused='gh: Reviews may only be requested from collaborators (HTTP 422)',
                                        access='missing')
        self.assertEqual((code, [w[:2] for w in writes]), (1, [('api', '-X')]))
        self.assertIn('is not a collaborator', out)
        # a GitHub hiccup with a bot that can write: try again, never "request it on the PR page"
        code, out, _ = self.run_pr(refused='gh: Something went wrong (HTTP 502)')
        self.assertEqual(code, 1)
        self.assertIn('GitHub refused it, though the bot can write (gh: Something went wrong (HTTP 502))', out)
        self.assertIn('wait a minute, then `magehand pr ready 12` again', out.splitlines()[-1])

    def test_a_push_after_the_checks_were_read_is_refused(self):
        code, out, writes = self.run_pr(pushed=True)
        self.assertEqual(code, 1)
        self.assertIn('a new commit arrived', out)
        self.assertNotIn(('pr', 'ready'), [w[:2] for w in writes])

    def test_nothing_to_do_and_openclaws_own_prs(self):
        code, out, writes = self.run_pr(draft=False, requested_before=True)
        self.assertEqual((code, writes), (0, []))
        self.assertIn('already ready', out)
        code, out, writes = self.run_pr(author=magehand.BOT)
        self.assertEqual([w[:2] for w in writes], [('pr', 'ready')])  # never requests the author's own review
        self.assertIn('the owner reviews', out.splitlines()[-1])
        code, out, writes = self.run_pr(author=magehand.BOT, draft=False)
        self.assertEqual((code, writes), (0, []))
        self.assertIn('no review is requested', out)


class Scaffold(unittest.TestCase):
    """`new` writes a deployable app's missing files into the checkout, never over one that exists."""

    def test_writes_a_deployable_app_that_passes_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            written = magehand.scaffold(root, 'demo', 'demo')
            self.assertEqual(sorted(written), sorted(magehand.SCAFFOLD_FILES))
            self.assertNotIn('app:', (root / 'deploy/app.yaml').read_text().split('services')[0].split('\n', 2)[-1])
            argv = ['check', 'demo', '--code', str(root), '--json']
            with mock.patch.object(magehand, 'github_file', lambda *a, **k: ''), \
                    mock.patch.object(magehand, 'check_agents_drift', lambda root: []), \
                    mock.patch.object(magehand, 'check_merged_branch', lambda root: []):
                code, out, _ = CommandLine.run_main(self, *argv)
            report = json.loads(out)
            self.assertEqual((code, report['errors']), (0, 0), report['findings'])
            self.assertEqual([f for f in report['findings'] if f['level'] == 'warning'], [])
            self.assertIn('deploy/dev', report['skipped'][0])  # rendering is the module's stub here

    def test_the_generated_app_passes_its_own_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            magehand.scaffold(tmp, 'demo', 'demo')
            # the Dockerfile's test stage runs exactly this, in /app
            run = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests'],
                                 cwd=tmp, capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_the_generated_page_names_its_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            magehand.scaffold(tmp, 'demo', 'demo')
            app = (pathlib.Path(tmp) / 'app.py').read_text()
        self.assertIn("<html lang='en'>", app)  # else the browser check's axe rule html-has-lang fails it

    def test_never_overwrites_and_keeps_another_stack(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'Dockerfile').write_text('FROM node\n')
            (root / 'deploy/base').mkdir(parents=True)
            (root / 'deploy/base/web.yaml').write_text('kind: Deployment\n')
            written = magehand.scaffold(root, 'demo', 'demo')
            self.assertEqual((root / 'Dockerfile').read_text(), 'FROM node\n')
            self.assertFalse(set(written) & set(magehand.SCAFFOLD_CODE + magehand.SCAFFOLD_BASE), written)
            self.assertEqual(sorted(written), ['deploy/app.yaml', 'deploy/dev/kustomization.yaml', 'ui-check.yaml'])
            self.assertEqual(magehand.scaffold(root, 'demo', 'demo'), [])  # safe to re-run

    def test_an_app_named_unlike_its_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            magehand.scaffold(tmp, 'lab', 'lab-website')
            root = pathlib.Path(tmp)
            self.assertIn('app: "lab"', (root / 'deploy/app.yaml').read_text())
            self.assertIn('namespace: "lab-dev"', (root / 'deploy/dev/kustomization.yaml').read_text())
            self.assertIn(f'host: lab-dev.{magehand.LAB}', (root / 'deploy/base/ingress.yaml').read_text())

    def checkout(self, tmp, origin):
        subprocess.run(['git', 'init', '-q', tmp], check=True)
        subprocess.run(['git', '-C', tmp, 'remote', 'add', 'origin', origin], check=True)
        return pathlib.Path(tmp)

    def gh(self, name):
        """GitHub as `new` sees it for an existing, set-up repo NAME with no deploy/app.yaml on main."""
        def gh(*args, **kw):
            if args[0] == 'api' and args[-1].endswith('/contents/deploy/app.yaml'):
                return (0, '', '') if '-X' in args else (1, '', 'gh: Not Found (HTTP 404)')
            if args[:2] == ('api', f'repos/davidlarrimore/{name}') and '.private, .archived' in args:
                return 0, 'true\nfalse\n', ''
            if args[0] == 'api' and args[1].endswith('/collaborators/davidlarrimore-bot/permission'):
                return 0, 'write write\n', ''
            if args[:2] == ('api', f'repos/davidlarrimore/{name}/rulesets'):
                return 0, json.dumps([{'id': 7, 'name': magehand.RULESET['name']}]), ''
            if args[:2] == ('api', f'repos/davidlarrimore/{name}/rulesets/7'):
                return 0, json.dumps(dict(magehand.RULESET, id=7)), ''
            if args[:2] == ('api', f'repos/davidlarrimore/{name}') and '.default_branch' in args:
                return 0, 'main\n', ''
            if args[0] == 'api' and args[1].startswith(f'repos/davidlarrimore/{name}/rules/branches/main'):
                return 0, json.dumps([{'type': 'pull_request', 'ruleset_id': 7}]), ''
            return 0, '', ''
        return gh

    def run_new_here(self, root, *argv, name='elvis', agents=None):
        with mock.patch.object(magehand, 'gh_cli', self.gh(name)), \
                mock.patch.object(magehand, 'current_repo', lambda *a: f'davidlarrimore/{name}'), \
                mock.patch.object(magehand, 'checkout_top', lambda *a: root), \
                mock.patch.object(magehand, 'agents_text', agents or (lambda path: (None, '# rules\n\n## This app\n', None))):
            code, out, _ = CommandLine.run_main(self, 'new', name, '--json', *argv)
        return code, json.loads(out)

    def test_new_writes_into_its_own_checkout_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.checkout(tmp, 'git@github.com:davidlarrimore/elvis.git')
            code, report = self.run_new_here(root)
            self.assertEqual(code, 0)
            self.assertIn('Dockerfile', report['wrote'])
            self.assertIn('AGENTS.md', report['wrote'])
            self.assertTrue((root / 'deploy/dev/kustomization.yaml').is_file())
            self.assertIn('commit what new wrote', report['next'])
        with tempfile.TemporaryDirectory() as tmp:  # a checkout of another repo: nothing written
            root = self.checkout(tmp, 'git@github.com:davidlarrimore/magehand.git')
            _, report = self.run_new_here(root)
            self.assertEqual(report['wrote'], [])
            self.assertEqual(list(root.iterdir()), [root / '.git'])
        with tempfile.TemporaryDirectory() as tmp:  # another owner's repo of the same name: nothing written
            root = self.checkout(tmp, 'https://github.com/example/elvis.git')
            _, report = self.run_new_here(root)
            self.assertEqual(report['wrote'], [])
        with tempfile.TemporaryDirectory() as tmp:  # a look-alike host: nothing written
            root = self.checkout(tmp, 'https://notgithub.com/davidlarrimore/elvis.git')
            _, report = self.run_new_here(root)
            self.assertEqual(report['wrote'], [])
        with tempfile.TemporaryDirectory() as tmp:  # a plain directory inside the right checkout: not its top
            top = self.checkout(tmp, 'git@github.com:davidlarrimore/elvis.git')
            (top / 'elvis').mkdir()
            with mock.patch.object(magehand, 'gh_cli', self.gh('elvis')), \
                    mock.patch.object(magehand, 'current_repo', lambda *a: None), \
                    mock.patch.object(magehand, 'clone_repo', lambda repo, dest: 'exists'):
                _, out, _ = CommandLine.run_main(self, 'new', 'elvis', '--dir', str(top / 'elvis'), '--json')
            self.assertEqual(json.loads(out)['wrote'], [])
            self.assertEqual(list((top / 'elvis').iterdir()), [])

    def test_the_origin_names_owner_and_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            for url, expected in (('git@github.com:davidlarrimore/elvis.git', 'davidlarrimore/elvis'),
                                  ('https://github.com/example/elvis', 'example/elvis'),
                                  ('https://github.com/example/my.app.git/', 'example/my.app'),
                                  ('ssh://git@github.com/davidlarrimore/elvis.git', 'davidlarrimore/elvis'),
                                  ('https://gitlab.example.com/x/elvis.git', None),
                                  ('https://notgithub.com/davidlarrimore/elvis.git', None),
                                  ('git@notgithub.com:davidlarrimore/elvis.git', None),
                                  ('https://github.com.example.com/davidlarrimore/elvis.git', None),
                                  ('https://example.com/github.com/davidlarrimore/elvis.git', None),
                                  ('/srv/git/github.com/davidlarrimore/elvis.git', None)):
                subprocess.run(['git', 'init', '-q', tmp], check=True)
                subprocess.run(['git', '-C', tmp, 'remote', 'remove', 'origin'], capture_output=True)
                subprocess.run(['git', '-C', tmp, 'remote', 'add', 'origin', url], check=True)
                self.assertEqual(magehand.origin_repo(tmp), expected, url)

    def test_names_yaml_would_retype_stay_strings(self):
        for app in ('null', 'true', '123'):
            with tempfile.TemporaryDirectory() as tmp:
                magehand.scaffold(tmp, app, app)
                for rel in magehand.SCAFFOLD_FILES:
                    if rel.endswith('.yaml') and rel.startswith('deploy/') and rel != 'deploy/app.yaml':
                        doc = magehand.yaml.safe_load((pathlib.Path(tmp) / rel).read_text())
                        if 'metadata' in doc:
                            self.assertEqual(doc['metadata']['name'], app, rel)
                        if rel.endswith('deployment.yaml'):
                            self.assertEqual(doc['spec']['selector']['matchLabels']['app'], app)
                            self.assertEqual(doc['spec']['template']['metadata']['labels']['app'], app)
                        if rel.endswith('service.yaml'):
                            self.assertEqual(doc['spec']['selector']['app'], app)
                        if rel.endswith('ingress.yaml'):
                            self.assertEqual(doc['spec']['rules'][0]['http']['paths'][0]['backend']['service']['name'], app)
                        if rel.endswith('dev/kustomization.yaml'):
                            self.assertEqual(doc['namespace'], f'{app}-dev')


class ScaffoldCases(unittest.TestCase):
    """The cases the local review found: the app's own name, a fresh seed, other code, no template."""

    checkout, gh, run_new_here = Scaffold.checkout, Scaffold.gh, Scaffold.run_new_here

    def test_the_checkouts_own_app_name_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.checkout(tmp, 'git@github.com:davidlarrimore/lab-website.git')
            (root / 'deploy').mkdir()
            (root / 'deploy/app.yaml').write_text('app: "lab"\nservices: {}\n')
            _, report = self.run_new_here(root, name='lab-website')
            self.assertIn('namespace: "lab-dev"', (root / 'deploy/dev/kustomization.yaml').read_text())
            self.assertNotIn('deploy/app.yaml', report['wrote'])

    def test_a_seed_just_committed_is_pulled_not_rivalled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.checkout(tmp, 'git@github.com:davidlarrimore/lab-website.git')
            _, report = self.run_new_here(root, '--app-name', 'lab', name='lab-website')
            self.assertFalse((root / 'deploy/app.yaml').exists())
            self.assertIn('namespace: "lab-dev"', (root / 'deploy/dev/kustomization.yaml').read_text())
            self.assertIn('pull', report['next'])
            self.assertEqual(report['dir'], str(root))

    def test_the_pull_step_is_in_the_human_output_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.checkout(tmp, 'git@github.com:davidlarrimore/lab-website.git')
            with mock.patch.object(magehand, 'gh_cli', self.gh('lab-website')), \
                    mock.patch.object(magehand, 'current_repo', lambda *a: 'davidlarrimore/lab-website'), \
                    mock.patch.object(magehand, 'checkout_top', lambda *a: root), \
                    mock.patch.object(magehand, 'agents_text', lambda path: (None, '# rules\n\n## This app\n', None)):
                _, out, _ = CommandLine.run_main(self, 'new', 'lab-website', '--app-name', 'lab')
        self.assertIn('1. run:   git -C', out)
        self.assertIn('pull', out.splitlines()[-1])

    def test_a_repo_with_its_own_code_gets_no_python_scaffold(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.checkout(tmp, 'git@github.com:davidlarrimore/elvis.git')
            (root / 'package.json').write_text('{}')
            _, report = self.run_new_here(root)
            self.assertFalse(set(report['wrote']) & set(magehand.SCAFFOLD_CODE))
            self.assertIn('deploy/dev/kustomization.yaml', report['wrote'])

    def test_no_template_still_reports_what_was_written(self):
        def offline(path):
            sys.exit('magehand: cannot read davidlarrimore/homelab-app-template/AGENTS.md from GitHub')
        with tempfile.TemporaryDirectory() as tmp:
            root = self.checkout(tmp, 'git@github.com:davidlarrimore/elvis.git')
            code, report = self.run_new_here(root, agents=offline)
        self.assertEqual(code, 0)
        self.assertIn('Dockerfile', report['wrote'])
        self.assertNotIn('AGENTS.md', report['wrote'])

    def test_the_commit_step_fits_what_was_written(self):
        self.assertIn('review the AGENTS.md diff', magehand.commit_step(['AGENTS.md']))
        self.assertNotIn('registers', magehand.commit_step(['ui-check.yaml']))
        self.assertIn('registers the app', magehand.commit_step(['deploy/app.yaml', 'AGENTS.md']))


class Approvals(unittest.TestCase):
    """setup's pre-approved commands: one list, as Codex rules and a Claude Code hook that agree."""

    def hook(self, command, tool='Bash'):
        """The installed hook's decision for `command`: (decision, reason) or None, run as Claude Code runs it."""
        event = json.dumps({'tool_name': tool, 'tool_input': {'command': command}})
        run = subprocess.run([sys.executable, '-I', str(self.plugin / 'hooks/approve.py')], input=event,
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        if not run.stdout.strip():
            return None
        out = json.loads(run.stdout)['hookSpecificOutput']
        self.assertEqual(out['hookEventName'], 'PreToolUse')
        return out['permissionDecision'], out['permissionDecisionReason']

    def setUp(self):
        self.claude, self.codex = magehand.claude_dir(), magehand.codex_dir()  # the module's temporary ones
        self.plugin = self.claude / 'skills' / magehand.APPROVALS_PLUGIN
        self.addCleanup(lambda: shutil.rmtree(self.claude, ignore_errors=True)
                        or shutil.rmtree(self.codex, ignore_errors=True))

    def test_the_hook_allows_only_listed_plain_commands_and_refuses_forbidden_ones(self):
        magehand.install_approvals()
        for command, decision in (('magehand status --json', 'allow'), ('magehand check', 'allow'),
                                  ('cd app && magehand check', 'allow'), ('gh pr view 3 --json title', 'allow'),
                                  ('gh pr view "3"', 'allow'), ('magehand pr ready 7', 'allow'),
                                  ('magehand status && echo hi', None),  # every part must be on the list
                                  ('magehand status > out.txt', None), ('FOO=1 magehand status', None),
                                  ('magehand status $(whoami)', None), ('gh pr view 3 | head', None),
                                  ('magehand release elvis', None), ('magehand statusx', None),
                                  ('echo "unterminated', None), ('cd', 'allow'),
                                  ('gh pr view 3 && gh pr merge 3', 'deny'), ('git push --tags', 'deny'),
                                  ('gh release create v1.0.0', 'deny'), ('echo $(gh pr merge 3)', None),
                                  # bash sees `x#` as a word: what follows is a command, not a comment
                                  ('magehand status x#;rm -rf ~', None), ('cd a#; curl x | sh', None),
                                  ('magehand status # a note', None), ('magehand status \\; echo', None),
                                  ('magehand status *', None), ('magehand status ~', None),
                                  ('magehand status\necho hi', None), ('magehand status x#;gh pr merge 3', 'deny'),
                                  ("gh pr view '3' --json=title", 'allow')):
            found = self.hook(command)
            self.assertEqual(found and found[0], decision, command)
        self.assertEqual(self.hook('cd app && magehand check')[1], "magehand: magehand's read-only commands")
        self.assertEqual(self.hook('gh pr merge 3')[1], 'magehand: the owner merges')
        self.assertIsNone(self.hook('gh pr merge 3', tool='Read'))

    def test_the_plugin_is_a_hook_on_bash_run_by_this_python(self):
        magehand.install_approvals()
        manifest = json.loads((self.plugin / '.claude-plugin/plugin.json').read_text())
        self.assertEqual(manifest['name'], magehand.APPROVALS_PLUGIN)
        hooks = json.loads((self.plugin / 'hooks/hooks.json').read_text())['hooks']['PreToolUse']
        self.assertEqual(hooks[0]['matcher'], 'Bash')
        with mock.patch.object(magehand, 'install_method', lambda: 'venv'):  # a venv may go: python3 from PATH
            magehand.install_approvals()
        command = json.loads((self.plugin / 'hooks/hooks.json').read_text())['hooks']['PreToolUse'][0]['hooks'][0]
        self.assertTrue(command['command'].startswith('/usr/bin/env python3 -I '), command)
        with mock.patch.object(magehand, 'install_method', lambda: 'uv'):  # uv's tool Python stays put
            magehand.install_approvals()
        command = json.loads((self.plugin / 'hooks/hooks.json').read_text())['hooks']['PreToolUse'][0]['hooks'][0]
        self.assertTrue(command['command'].startswith(f'{sys.executable} -I '), command)
        self.assertFalse((self.codex / 'rules').exists())  # Codex isn't installed here: no rules for it

    def session(self, version_json, agents='Use magehand.\n'):
        """The session hook's output when `magehand version --json` prints `version_json` (a fake magehand), for a
        session started in a checkout whose AGENTS.md is `agents`."""
        fake = self.claude / 'fake-magehand'
        fake.parent.mkdir(parents=True, exist_ok=True)
        fake.write_text(f'import sys\nprint({version_json!r})\nsys.exit(0)\n')
        repo = self.claude / 'repo'
        (repo / '.git').mkdir(parents=True, exist_ok=True)
        (repo / 'src').mkdir(exist_ok=True)
        (repo / 'AGENTS.md').write_text(agents)
        script = magehand.SESSION_HOOK.format(generated='x', magehand=json.dumps([sys.executable, str(fake)]))
        run = subprocess.run([sys.executable, '-I', '-c', script], capture_output=True, text=True, timeout=30,
                             input=json.dumps({'hook_event_name': 'SessionStart', 'cwd': str(repo / 'src')}))
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout

    def test_a_new_session_hears_when_magehand_is_out_of_date(self):
        magehand.install_approvals()
        hooks = json.loads((self.plugin / 'hooks/hooks.json').read_text())['hooks']
        start = hooks['SessionStart'][0]
        self.assertEqual(start['matcher'], 'startup')
        self.assertTrue(start['hooks'][0]['command'].endswith('-I "${CLAUDE_PLUGIN_ROOT}/hooks/session.py"'))
        self.assertTrue((self.plugin / 'hooks/session.py').is_file())
        out = json.loads(self.session('{"version": "0.40.0", "latest": "0.41.0", "newer": true}'))['hookSpecificOutput']
        self.assertEqual(out['hookEventName'], 'SessionStart')
        self.assertIn('magehand 0.41.0 is out; this machine has 0.40.0', out['additionalContext'])
        self.assertIn('`magehand upgrade`', out['additionalContext'])
        for quiet in ('{"version": "0.41.0", "latest": "0.41.0", "newer": false}', 'not json', '[]'):
            self.assertEqual(self.session(quiet), '', quiet)  # current, or a magehand that fails: nothing said
        # A project that doesn't use magehand hears nothing (and runs no magehand at all)
        self.assertEqual(self.session('{"version": "0.40.0", "latest": "0.41.0", "newer": true}', agents='Use npm.\n'), '')

    def test_version_json_says_whether_to_upgrade(self):
        with mock.patch.object(magehand, 'installed_version', lambda: '0.40.0'), \
                mock.patch.object(magehand, 'latest_version', lambda: '0.41.0'), \
                mock.patch.object(magehand, 'install_method', lambda: 'uv'):
            _, out, _ = CommandLine.run_main(self, 'version', '--json')
        self.assertEqual(json.loads(out), {'version': '0.40.0', 'latest': '0.41.0', 'managed': False, 'newer': True,
                                           'next': '`magehand upgrade`'})
        with mock.patch.object(magehand, 'installed_version', lambda: '0.40.0'), \
                mock.patch.object(magehand, 'latest_version', lambda: self.fail('asked GitHub in the VM')), \
                mock.patch.object(magehand, 'install_method', lambda: 'managed'):
            _, out, _ = CommandLine.run_main(self, 'version', '--json')
        self.assertEqual((json.loads(out)['managed'], json.loads(out)['next']), (True, None))

    def test_codex_rules_say_the_same_and_a_rejected_file_never_replaces_a_working_one(self):
        self.codex.mkdir(parents=True)
        with mock.patch.object(magehand, 'codex_rules_problem', lambda path: None):
            results = magehand.install_approvals()
        rules = self.codex / 'rules' / magehand.CODEX_RULES
        self.assertEqual(results[-1], (rules, None))
        text = rules.read_text()
        self.assertIn('prefix_rule(pattern = ["gh", "pr", "merge"], decision = "forbidden", '
                      'justification = "the owner merges")', text)
        self.assertIn('["magehand", ["status", "app", "check", "doctor", "guide", "version", "help"]]', text)
        self.assertEqual(text.count('prefix_rule('), len(magehand.APPROVALS))
        checked = []
        with mock.patch.object(magehand, 'codex_rules_problem', lambda path: checked.append(path) or 'parse error'):
            results = magehand.install_approvals()
        self.assertEqual(results[-1], (rules, 'parse error'))
        self.assertNotEqual(checked[0].parent, rules.parent)  # checked outside rules/, where Codex reads
        self.assertEqual(rules.read_text(), text)  # the working file stays
        self.assertEqual(sorted(p.name for p in self.codex.iterdir()), ['rules'])  # no draft left behind

    def test_a_rejected_codex_draft_is_not_reported_as_installed(self):
        """Claude's plugin installed and Codex's rules rejected: setup, refresh and doctor all say Codex lacks them."""
        self.codex.mkdir(parents=True)
        rejected = mock.patch.object(magehand, 'codex_rules_problem', lambda path: 'unknown function prefix_rule')
        with rejected:
            magehand.install_approvals()
        self.assertTrue(self.plugin.is_dir())
        self.assertEqual(magehand.approvals_missing(), ['Codex'])
        with rejected, mock.patch.object(magehand, 'github_token', lambda: 't'), \
                mock.patch.object(magehand, 'reachable', lambda: True):
            code, out, _ = CommandLine.run_main(self, 'setup', '--refresh')
        self.assertEqual(code, 0)
        self.assertIn('not for Codex', out)
        self.assertIn('needs setup: Codex rejected magehand\'s rules (unknown function prefix_rule)', out)
        self.assertEqual(out.splitlines()[-1], 'next: update Codex (`codex --version`), then `magehand setup`')
        with mock.patch.object(magehand, 'github_token', lambda: 't'), \
                mock.patch.object(magehand, 'github_file', lambda *a, **k: ''), \
                mock.patch.object(magehand.urllib.request, 'urlopen', mock.Mock(side_effect=OSError())), \
                mock.patch.object(magehand, 'install_method', lambda: 'uv'), \
                mock.patch.object(magehand, 'current_repo', lambda *a: None):
            _, out, _ = CommandLine.run_main(self, 'doctor', '--json')
        notes = [r['hint'] for r in json.loads(out)['checks'] if r['check'] == 'pre-approved commands']
        self.assertEqual(len(notes), 1)
        self.assertIn('for Codex: not installed', notes[0])

    def test_setup_fails_when_codex_rejects_its_rules(self):
        self.codex.mkdir(parents=True)
        setup = Setup()
        setup.setUp()
        self.addCleanup(setup.tearDown)
        magehand.reachable = lambda: True
        out = io.StringIO()
        with mock.patch.object(magehand, 'codex_rules_problem', lambda path: 'unknown function prefix_rule'), \
                contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as failed:
            magehand.main(['setup'])
        self.assertIn('setup is done except for Codex: Codex rejected', str(failed.exception.code))
        self.assertIn('not installed: ', out.getvalue())
        self.assertNotIn('Done: this machine is set up.', out.getvalue())
        self.assertTrue(self.plugin.is_dir())  # Claude Code's part is installed

    def test_codex_check_reads_the_decision(self):
        allow = mock.Mock(returncode=0, stdout='{"decision": "allow"}', stderr='')
        broken = mock.Mock(returncode=1, stdout='', stderr='starlark: bad rule')
        with mock.patch.object(magehand.shutil, 'which', lambda name: '/usr/bin/codex'):
            with mock.patch.object(magehand.subprocess, 'run', lambda *a, **k: allow):
                self.assertIsNone(magehand.codex_rules_problem('x.rules'))
            with mock.patch.object(magehand.subprocess, 'run', lambda *a, **k: broken):
                self.assertEqual(magehand.codex_rules_problem('x.rules'), 'starlark: bad rule')
        with mock.patch.object(magehand.shutil, 'which', lambda name: None):
            self.assertIsNone(magehand.codex_rules_problem('x.rules'))  # no codex CLI to ask

    def test_skill_install_never_writes_approvals(self):  # OpenClaw's VM runs `skill --install`
        self.codex.mkdir(parents=True)
        with contextlib.redirect_stdout(io.StringIO()):
            magehand.main(['skill', '--install'])
        self.assertFalse(self.plugin.exists())
        self.assertFalse((self.codex / 'rules').exists())

    def test_undo_removes_them(self):
        self.codex.mkdir(parents=True)
        with mock.patch.object(magehand, 'codex_rules_problem', lambda path: None):
            magehand.install_approvals()
        with mock.patch.object(magehand, 'RESOLVER', self.claude / 'absent'):
            code, out, _ = CommandLine.run_main(self, 'setup', '--undo')
        self.assertEqual(code, 0)
        self.assertFalse(self.plugin.exists())
        self.assertFalse((self.codex / 'rules' / magehand.CODEX_RULES).exists())
        self.assertIn(f'removed {self.plugin}', out)

    def test_refresh_rewrites_what_setup_installed_and_says_when_setup_is_needed(self):
        with mock.patch.object(magehand, 'github_token', lambda: 't'), \
                mock.patch.object(magehand, 'reachable', lambda: True):
            code, out, _ = CommandLine.run_main(self, 'setup', '--refresh')
            self.assertEqual(code, 0)
            self.assertIn('needs setup: the pre-approved commands', out)  # never installed: setup installs them
            self.assertEqual(out.splitlines()[-1], 'next: `magehand setup`')
            self.assertFalse(self.plugin.exists())  # refresh installs nothing new
            magehand.install_approvals()
            (self.plugin / 'hooks/approve.py').write_text('stale')
            code, out, _ = CommandLine.run_main(self, 'setup', '--refresh')
        self.assertIn('refreshed the pre-approved commands', out)
        self.assertNotEqual((self.plugin / 'hooks/approve.py').read_text(), 'stale')
        self.assertEqual(out.splitlines()[-1], 'next: nothing')
        with mock.patch.object(magehand, 'github_token', lambda: sys.exit('no token')), \
                mock.patch.object(magehand, 'reachable', lambda: False):
            _, out, _ = CommandLine.run_main(self, 'setup', '--refresh')
        self.assertIn('needs setup: GitHub sign-in', out)
        self.assertIn('is not reachable', out)

    def test_upgrade_runs_the_new_versions_quiet_setup(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd[1:])
            return mock.Mock(returncode=0, stdout='0.38.0\n', stderr='')
        with tempfile.TemporaryDirectory() as tmp:
            exe = pathlib.Path(tmp) / 'magehand'
            exe.write_text('')
            with mock.patch.object(magehand, 'install_method', lambda: 'uv'), \
                    mock.patch.object(magehand, 'installed_version', lambda: '0.38.0'), \
                    mock.patch.object(magehand, 'latest_version', lambda max_age=0: '0.38.0'), \
                    mock.patch.object(magehand.sys, 'executable', str(pathlib.Path(tmp) / 'python')), \
                    mock.patch.object(magehand.subprocess, 'run', run):
                code, out, _ = CommandLine.run_main(self, 'upgrade')
        self.assertEqual(code, 0)
        self.assertEqual(calls, [['version'], ['setup', '--refresh']])

    def upgrade_with_refresh(self, refresh_code):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd[1:])
            return mock.Mock(returncode=refresh_code if cmd[1:] == ['setup', '--refresh'] else 0,
                             stdout='0.38.0\n', stderr='')
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / 'magehand').write_text('')
            with mock.patch.object(magehand, 'install_method', lambda: 'uv'), \
                    mock.patch.object(magehand, 'installed_version', lambda: '0.38.0'), \
                    mock.patch.object(magehand, 'latest_version', lambda max_age=0: '0.38.0'), \
                    mock.patch.object(magehand.sys, 'executable', str(pathlib.Path(tmp) / 'python')), \
                    mock.patch.object(magehand.subprocess, 'run', run):
                code, out, _ = CommandLine.run_main(self, 'upgrade')
        return code, out, calls

    def test_upgrade_to_a_version_without_refresh_installs_its_skill(self):
        magehand.install_skill()
        code, out, calls = self.upgrade_with_refresh(2)
        self.assertEqual((code, calls[-1], out.splitlines()[-1]), (0, ['skill', '--install'], 'next: nothing'))
        code, out, calls = self.upgrade_with_refresh(1)  # a refresh that failed: setup is the step
        self.assertEqual((code, out.splitlines()[-1]), (0, 'next: `magehand setup`'))

    def test_doctor_notes_missing_approvals(self):
        with mock.patch.object(magehand, 'github_token', lambda: 't'), \
                mock.patch.object(magehand, 'github_file', lambda *a, **k: ''), \
                mock.patch.object(magehand.urllib.request, 'urlopen', mock.Mock(side_effect=OSError())), \
                mock.patch.object(magehand, 'install_method', lambda: 'uv'), \
                mock.patch.object(magehand, 'current_repo', lambda *a: None):
            _, out, _ = CommandLine.run_main(self, 'doctor', '--json')
            self.assertIn('pre-approved commands', [r['check'] for r in json.loads(out)['checks']])
            magehand.install_approvals()
            _, out, _ = CommandLine.run_main(self, 'doctor', '--json')
            self.assertNotIn('pre-approved commands', [r['check'] for r in json.loads(out)['checks']])


class StaleAgents(unittest.TestCase):
    """doctor lists the owner's app repos whose AGENTS.md platform rules aren't the template's."""
    TEMPLATE = '# Rules\nuse magehand\n## This app\n'

    def page(self, *nodes, more=None, template=TEMPLATE, owner=True):
        repos = {'pageInfo': {'hasNextPage': bool(more), 'endCursor': more}, 'nodes': list(nodes)}
        return {'data': {'template': {'agents': {'text': template}} if template else None,
                         'repositoryOwner': {'repositories': repos} if owner else None}}

    def node(self, name, text, template=True, archived=False):
        return {'name': name, 'isArchived': archived, 'agents': None if text is None else {'text': text},
                'templateRepository': {'nameWithOwner': magehand.TEMPLATE_REPO.upper() if template == 'upper'
                                       else magehand.TEMPLATE_REPO} if template else None}

    def stale(self, *pages, err=None):
        calls = []

        def query(*args):
            calls.append(args)
            return (None, err) if err else (pages[len(calls) - 1], None)
        with mock.patch.object(magehand, 'gh_query', query), \
                mock.patch.object(magehand, 'github_file', lambda *a, **k: self.fail('a second request')):
            return REAL_STALE_AGENTS(), calls

    def test_lists_only_template_repos_that_are_behind(self):
        (stale, err), _ = self.stale(self.page(
            self.node('current', self.TEMPLATE + 'notes\n'), self.node('behind', '# Rules\nold\n## This app\nnotes\n'),
            self.node('headless', '# Rules\nuse magehand\n'), self.node('none', None),
            self.node('cased', '# Rules\nold\n## This app\n', template='upper'),
            self.node('retired', 'old', archived=True), self.node('homelab', 'other', template=False)))
        self.assertEqual((stale, err), (['behind', 'headless (has no "## This app" heading)', 'none (no AGENTS.md)',
                                         'cased'], None))

    def test_reads_every_page(self):
        (stale, _), calls = self.stale(self.page(self.node('a', self.TEMPLATE), more='c1'),
                                       self.page(self.node('z', 'old\n## This app\n')))
        self.assertEqual(stale, ['z'])
        self.assertEqual((len(calls), calls[1][-2:]), (2, ('-f', 'cursor=c1')))

    def test_nothing_read_is_not_all_current(self):
        for pages, err, why in (((), 'HTTP 502\nmore detail', 'HTTP 502'),
                                ((self.page(owner=False),), None, 'cannot list the repos'),
                                ((self.page(template=None),), None, 'cannot read'),
                                ((self.page(self.node('homelab', 'x', template=False)),), None, 'no repo made from')):
            with self.subTest(why=why):
                (stale, problem), _ = self.stale(*pages, err=err)
                self.assertIsNone(stale)
                self.assertTrue(problem.startswith(why) or why in problem, problem)

    def doctor(self, found, *flags):
        with mock.patch.object(magehand, 'stale_agents', lambda: found), \
                mock.patch.object(magehand, 'github_token', lambda: 't'), \
                mock.patch.object(magehand, 'github_file', lambda *a, **k: 'x'), \
                mock.patch.object(magehand, 'install_method', lambda: 'uv'), \
                mock.patch.object(magehand, 'current_repo', lambda *a: None), \
                mock.patch.object(magehand, 'approvals_missing', lambda: []), \
                mock.patch.object(magehand, 'signin_left', lambda *a, **k: 3600), \
                mock.patch.object(magehand, 'configured_runtime', lambda: 'none'), \
                mock.patch.object(magehand.urllib.request, 'urlopen', lambda *a, **k: mock.Mock(status=200)):
            return CommandLine.run_main(self, 'doctor', *flags)

    def test_doctor_names_them_and_says_what_to_do_when_nothing_failed(self):
        code, out, _ = self.doctor((['elvis'], None), '--json')
        report = json.loads(out)
        self.assertEqual(code, 0)  # this machine is fine: the repos are a note, not a failure
        self.assertEqual([c['stale'] for c in report['checks'] if 'stale' in c], [['elvis']])
        self.assertEqual(report['next'], 'in each checkout: `magehand update-agents`, commit and open a PR (elvis)')
        code, out, _ = self.doctor((['elvis'], None))
        self.assertIn('--   AGENTS.md behind the template in 1 app repo(s): elvis', out)
        self.assertEqual(out.splitlines()[-1], 'next: in each checkout: `magehand update-agents`, commit and open a PR (elvis)')

    def test_doctor_says_whether_it_checked(self):
        code, out, _ = self.doctor(([], None), '--json')
        self.assertIn({'check': "app repos' AGENTS.md match the template", 'ok': True, 'hint': ''},
                      json.loads(out)['checks'])
        _, out, _ = self.doctor(([], None))
        self.assertIn("ok   app repos' AGENTS.md match the template", out)
        self.assertEqual(out.splitlines()[-1], 'next: nothing')
        _, out, _ = self.doctor((None, 'HTTP 502'))
        self.assertIn("--   app repos' AGENTS.md: not checked (HTTP 502)", out)
        self.assertEqual(out.splitlines()[-1], 'next: nothing')


class ManagedCopy(unittest.TestCase):
    """OpenClaw's VM (the managed copy): commands that can't work there say why and what to do instead."""

    run_main = None  # set below, from CommandLine

    def test_commands_that_cannot_work_in_the_vm_say_why(self):
        with mock.patch.object(magehand, 'install_method', lambda *a: 'managed'), \
                mock.patch.object(magehand, 'update_notice', lambda: None):
            for argv, why, step in ((['run', '--', 'true'], 'no container runtime', 'next: `magehand check`, then push'),
                                    (['dev', 'up'], 'no local blocks here', 'next: `magehand check`, then push'),
                                    (['bucket', 'ls', 'files'], "can't reach the app's bucket", 'next: ask the owner'),
                                    (['login'], 'nothing to sign in to', 'next: nothing'),
                                    (['check', '--ui'], "can't run the browser check", 'next: `magehand check`, then push'),
                                    (['setup'], 'set up by the platform', 'next: nothing')):
                code, out, err = self.run_main(*argv)
                self.assertEqual((code, out), (1, ''), argv)
                self.assertIn(why, err, argv)
                self.assertIn(step, err, argv)

    def test_the_same_commands_run_normally_elsewhere(self):
        ran = []
        with mock.patch.object(magehand, 'install_method', lambda *a: 'uv'), \
                mock.patch.object(magehand, 'update_notice', lambda: None), \
                mock.patch.object(magehand, 'cmd_login', lambda a: ran.append('login')):
            magehand.build_parser()
            with mock.patch.dict(magehand.SUBPARSERS['login']._defaults, {'func': lambda a: ran.append('login')}):
                self.run_main('login')
        self.assertEqual(ran, ['login'])

    def test_doctor_in_the_vm_checks_only_what_applies(self):
        names = ('current_repo', 'github_token', 'github_file', 'configured_runtime', 'choose_runtime', 'install_method')
        saved = {n: getattr(magehand, n) for n in names}
        magehand.current_repo = lambda *a: None
        magehand.github_token = lambda: 't'
        magehand.github_file = lambda *a, **k: 'x'
        magehand.configured_runtime = lambda: 'none'
        magehand.choose_runtime = lambda configured, found: None
        magehand.install_method = lambda *a: 'managed'
        try:
            with mock.patch.object(magehand.urllib.request, 'urlopen', lambda *a, **k: self.fail('asked OpenBao')):
                code, out, _ = self.run_main('doctor', '--json')
        finally:
            for n, f in saved.items():
                setattr(magehand, n, f)
        report = json.loads(out)
        self.assertEqual((code, report['ok'], report['next']), (0, True, None))
        self.assertEqual([c['ok'] for c in report['checks'] if c['check'] == 'OpenBao'], [None])


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
        also = next(line for line in out.splitlines() if line.startswith('also: '))
        for name in magehand.SUBPARSERS:  # every command is listed in a group, or on the "also" line
            self.assertTrue(f'\n  {name} ' in out or f' {name}' in also.replace(',', ''), name)
        for name in magehand.PLUMBING:
            self.assertNotIn(f'\n  {name} ', out)

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
        self.assertEqual(result['next'], 'fix the errors, then `magehand check` again')

    def test_every_workflow_command_says_what_next(self):
        # The output contract (AGENTS.md "CLI conventions"): a person or an agent never works out the next step.
        import inspect
        magehand.build_parser()
        names = vars(magehand)
        for name, parser in magehand.SUBPARSERS.items():
            if name in magehand.NO_NEXT:
                continue
            func = parser.get_default('func')
            called = set(re.findall(r'\b(\w+)\(', inspect.getsource(func))) & set(names)
            sources = [inspect.getsource(func)] + [inspect.getsource(names[c]) for c in called
                                                   if inspect.isfunction(names[c])]
            self.assertTrue(any('next_line(' in src for src in sources), f'{name}: no next: line')

    def test_status_next_names_the_app_it_reports(self):
        # `status demo` from outside demo's checkout: a bare `status --watch` would fail or follow another app.
        stage = lambda name, state, **kw: dict(stage=name, state=state, text='', **kw)  # noqa: E731
        ok = [stage('build', 'ok'), stage('deploy', 'ok'), stage('dev', 'ok'), stage('prod', 'ok', ahead=0)]
        report = lambda stages: {'app': 'demo', 'stages': stages}  # noqa: E731
        self.assertIsNone(magehand.status_next(report(ok), False, True))
        ahead = ok[:3] + [stage('prod', 'ok', ahead=2)]
        self.assertIn('`magehand release demo --dry-run`', magehand.status_next(report(ahead), False, True))
        unreleased = ok[:3] + [stage('prod', 'unknown', released=False)]
        self.assertIsNone(magehand.status_next(report(unreleased), False, True))  # dev-only: no deploy/prod/
        with_prod = [stage('config', 'ok', prod=True)] + unreleased
        self.assertIn('ready for production', magehand.status_next(report(with_prod), False, True))
        failed = [stage('build', 'failed')] + ok[1:]
        self.assertIn('fix the failed build', magehand.status_next(report(failed), True, False))
        self.assertIn('`magehand status demo --watch`', magehand.status_next(report(failed), True, False))
        pending = [stage('build', 'pending')] + ok[1:]
        self.assertIn('`magehand status demo --watch`', magehand.status_next(report(pending), False, False))
        error = [stage('build', 'error')] + ok[1:]
        self.assertIn('`magehand status demo` again', magehand.status_next(report(error), True, False))

    def test_release_next_names_the_app(self):
        plan = {'app': 'demo'}
        self.assertIn('`magehand release demo --dry-run` again', magehand.release_next(plan, 'not releasing'))
        self.assertIn('`magehand release demo`', magehand.release_next(plan, None))
        self.assertIn('`magehand status demo`', magehand.release_next(plan, None, created=True))

    def test_setup_undo_says_next(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(magehand, 'RESOLVER', pathlib.Path(tmp) / 'absent'):
            code, out, _ = self.run_main('setup', '--undo')
        self.assertEqual((code, out.splitlines()[-1]), (0, 'next: nothing'))

    def test_new_summaries_end_with_next(self):
        text = magehand.new_summary('davidlarrimore/elvis', 'elvis', 'elvis', pathlib.Path('elvis'), None, 'cloned')
        self.assertEqual(text.splitlines()[-1], 'next: cd elvis')
        done = magehand.repair_summary('davidlarrimore/elvis', True, 'collaborator', 'exists', None,
                                       pathlib.Path('elvis'))
        self.assertEqual(done.splitlines()[-1], 'next: nothing')
        public = magehand.repair_summary('davidlarrimore/elvis', False, 'collaborator', 'exists', None,
                                         pathlib.Path('elvis'))
        self.assertTrue(public.splitlines()[-1].startswith('next: gh repo edit davidlarrimore/elvis'))

    def test_ctrl_c_exits_130(self):
        saved = magehand.cmd_version
        magehand.cmd_version = lambda a: (_ for _ in ()).throw(KeyboardInterrupt())
        try:
            self.assertEqual(self.run_main('version')[0], 130)
        finally:
            magehand.cmd_version = saved


class ArgoAccess(unittest.TestCase):
    """status says when Argo cannot read the app's repo."""

    APP = {'spec': {'sources': [{'repoURL': 'https://github.com/davidlarrimore/elvis.git'},
                                {'repoURL': 'git@github.com:davidlarrimore/homelab.git'}]}}

    def app(self, kind, message):
        return {**self.APP, 'status': {'conditions': [{'type': kind, 'message': message}]}}

    def test_status_fails_with_the_fix_when_argo_cannot_read_the_repo(self):
        repo = 'https://github.com/davidlarrimore/elvis.git'
        for message in (f'rpc error: code = Unknown desc = authentication required for {repo}',
                        f'Failed to load target state: repository not found: {repo}',
                        f"failed to list refs: could not read Username for '{repo}'"):
            with self.subTest(message):
                stage = magehand.argo_problem(self.app('ComparisonError', message))
                self.assertEqual(stage['state'], 'failed')
                self.assertIn('cannot read elvis', stage['text'])
                self.assertIn('GitHub App installation', stage['text'])

    def test_other_comparison_errors_are_pending_and_shown_and_healthy_is_none(self):
        stage = magehand.argo_problem(self.app('ComparisonError', 'kustomize build failed: no such file'))
        self.assertEqual(stage['state'], 'pending')
        self.assertIn('no such file', stage['text'])
        self.assertIsNone(magehand.argo_problem({**self.APP, 'status': {'conditions': []}}))
        self.assertIsNone(magehand.argo_problem(self.app('OrphanedResourceWarning', 'x')))
        self.assertIsNone(magehand.argo_problem(None))

    def test_only_a_git_read_of_the_app_repo_is_an_access_problem(self):
        for label, message in {
                'local kustomize permission error': 'kustomize build failed: open base/x.yaml: permission denied',
                'ssh platform source': 'rpc error: ssh: handshake failed: Permission denied (publickey) '
                                       'for git@github.com:davidlarrimore/homelab.git',
                'another source authentication failure': 'failed to fetch davidlarrimore/homelab.git: authentication failed',
                'kustomize remote base': 'kustomize build failed: authentication required',
                'numbered other source': 'Failed to load target state: failed to generate manifest for source 2 of 2: '
                                         'rpc error: code = Unknown desc = failed to list refs: authentication required',
                'several sources, none named': 'failed to list refs: authentication required'}.items():
            with self.subTest(label):
                self.assertEqual(magehand.argo_problem(self.app('ComparisonError', message))['state'], 'pending')
        named = 'failed to list refs https://github.com/davidlarrimore/elvis.git: authentication required'
        self.assertEqual(magehand.argo_problem(self.app('ComparisonError', named))['state'], 'failed')
        numbered = 'failed to generate manifest for source 1 of 2: failed to list refs: authentication required'
        self.assertEqual(magehand.argo_problem(self.app('ComparisonError', numbered))['state'], 'failed')

    def test_a_failing_pod_wins_over_a_pending_render_error(self):
        app = self.app('ComparisonError', 'kustomize build failed: no such file')
        image = 'x@sha256:' + 'a' * 64
        pod = {'metadata': {'name': 'elvis-1'}, 'status': {'phase': 'Running', 'containerStatuses': [
            {'name': 'app', 'ready': False, 'imageID': image, 'state': {'waiting': {'reason': 'CrashLoopBackOff'}}}]}}
        calls = {'pods': ({'items': [pod]}, None), 'application.argoproj.io': (app, None)}
        with mock.patch.object(magehand, 'kubectl_json', lambda kind, *a: calls[kind]):
            stage = magehand.dev_stage('elvis', {'digest': 'sha256:abc', 'image': image})
        self.assertEqual(stage['state'], 'failed')
        self.assertIn('CrashLoopBackOff', stage['text'])

    def test_dev_stage_reports_it_before_the_pods(self):
        app = self.app('ComparisonError', 'repository not found: https://github.com/davidlarrimore/elvis.git')
        calls = {'pods': ({'items': []}, None), 'application.argoproj.io': (app, None)}
        with mock.patch.object(magehand, 'kubectl_json', lambda kind, *a: calls[kind]):
            stage = magehand.dev_stage('elvis', {'digest': 'sha256:abc', 'image': 'x@sha256:' + 'a' * 64})
        self.assertEqual(stage['state'], 'failed')
        self.assertEqual(stage['stage'], 'dev')


class PinnedLayout(unittest.TestCase):
    """homelab-apps holds only a pin for an app whose manifests are in its own repo."""
    PIN = {'repo': 'elvis', 'ref': 'a' * 40, 'image': 'ghcr.io/davidlarrimore/homelab-apps/elvis@sha256:' + 'b' * 64}
    DEPLOYMENT = ('apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: elvis}\n'
                  'spec: {template: {spec: {containers: [{name: app, image: ghcr.io/davidlarrimore/homelab-apps/elvis}]}}}\n')

    def test_pinned_source_is_a_source_with_a_ref(self):
        self.assertEqual(magehand.pinned_source({'source': self.PIN}), self.PIN)
        self.assertIsNone(magehand.pinned_source({'source': {'repo': 'elvis'}}))  # today's app.yaml
        self.assertIsNone(magehand.pinned_source({'services': {}}))
        self.assertIsNone(magehand.pinned_source(None))

    def test_load_app_reads_the_manifests_from_the_app_repo_at_the_ref(self):
        asked = []

        def file(repo, path, required=True, ref='main'):
            asked.append((repo, path, ref))
            return self.DEPLOYMENT
        with mock.patch.object(magehand, 'APPS_DIR', None), \
                mock.patch.object(magehand, 'registration', lambda name: {'source': self.PIN, 'services': {}}), \
                mock.patch.object(magehand, 'github_dir', lambda repo, path, ref='main': ['deployment.yaml', 'README.md']), \
                mock.patch.object(magehand, 'github_file', file):
            app = magehand.load_app('elvis')
        self.assertEqual(asked, [('davidlarrimore/elvis', 'deploy/base/deployment.yaml', 'a' * 40)])
        self.assertEqual(app['container']['name'], 'app')
        self.assertIn('davidlarrimore/elvis', app['where'])

    def pinned_world(self, base_texts, dev_texts=None, image=None):
        """A homelab-apps that only pins `elvis`, and the app repo's deploy/ at the pinned commit."""
        pin = dict(PinnedLayout.PIN, **({'image': image} if image else {}))
        repo_files = {f'deploy/base/{n}': t for n, t in base_texts.items()}
        repo_files.update({f'deploy/dev/{n}': t for n, t in (dev_texts or {}).items()})

        def listing(repo, path, ref='main'):
            self.assertEqual((repo, ref), ('davidlarrimore/elvis', 'a' * 40))
            return sorted(p.rsplit('/', 1)[1] for p in repo_files if p.rsplit('/', 1)[0] == path)

        def file(repo, path, required=True, ref='main'):
            if repo == magehand.APPS_REPO:  # try_load_app asks whether the app is registered
                self.assertEqual(path, 'apps/elvis/app.yaml')
                return 'services: {}\n'
            self.assertEqual((repo, ref), ('davidlarrimore/elvis', 'a' * 40))
            return repo_files[path]
        return (mock.patch.object(magehand, 'APPS_DIR', None),
                mock.patch.object(magehand, 'registration', lambda name: {'source': pin, 'services': {}}),
                mock.patch.object(magehand, 'github_dir', listing), mock.patch.object(magehand, 'github_file', file))

    def check_pinned(self, *args, **kw):
        patches = self.pinned_world(*args, **kw)
        with patches[0], patches[1], patches[2], patches[3], \
                mock.patch.object(magehand, 'check_app_name', lambda app: []):
            app = magehand.try_load_app('elvis')
            return app, magehand.check_manifests(app, {})

    def test_a_pinned_app_repo_base_may_name_its_image_without_a_digest(self):
        app, found = self.check_pinned({'deployment.yaml': self.DEPLOYMENT})
        self.assertEqual(texts(found, 'error'), [])
        self.assertEqual(app['pin']['ref'], 'a' * 40)

    def test_the_registered_pin_must_be_the_apps_own_digest(self):
        for image in ('ghcr.io/davidlarrimore/homelab-apps/other@sha256:' + 'b' * 64,
                      'ghcr.io/davidlarrimore/homelab-apps/elvis:v1', 'example.com/elvis@sha256:' + 'b' * 64):
            with self.subTest(image):
                _, found = self.check_pinned({'deployment.yaml': self.DEPLOYMENT}, image=image)
                self.assertTrue(any('pinned image' in t for t in texts(found, 'error')), texts(found))

    def test_secrets_the_pinned_repo_makes_are_found_at_the_same_ref(self):
        deployment = self.DEPLOYMENT.replace(
            'containers: [{name: app, image: ghcr.io/davidlarrimore/homelab-apps/elvis}]',
            'containers: [{name: app, image: ghcr.io/davidlarrimore/homelab-apps/elvis, '
            'env: [{name: TOKEN, valueFrom: {secretKeyRef: {name: own, key: token}}}]}]')
        es = 'kind: ExternalSecret\nmetadata: {name: own}\nspec: {target: {name: own}}\n'
        app, found = self.check_pinned({'deployment.yaml': deployment, 'es.yaml': es})
        self.assertIn('own', app['provided'])
        self.assertEqual(texts(found, 'error'), [])
        # and one in deploy/dev (the overlay), which homelab-apps no longer has a copy of
        app, found = self.check_pinned({'deployment.yaml': deployment}, {'s.yaml': 'kind: Secret\nmetadata: {name: own}\n'})
        self.assertEqual(texts(found, 'error'), [])
        # without it the Secret is reported, as it is for any app
        _, found = self.check_pinned({'deployment.yaml': deployment})
        self.assertTrue(any("'own'" in t or 'own' in t for t in texts(found, 'error')), texts(found))

    def test_a_checkouts_dev_overlay_is_read_from_the_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            deploy = pathlib.Path(tmp) / 'deploy'
            (deploy / 'base').mkdir(parents=True)
            (deploy / 'dev').mkdir()
            (deploy / 'app.yaml').write_text('services: {}\n')
            (deploy / 'dev' / 's.yaml').write_text('kind: Secret\nmetadata: {name: devonly}\n')
            self.assertEqual(magehand.provided_secrets('elvis', pathlib.Path(tmp)), {'devonly'})

    def test_a_homelab_apps_checkout_has_nothing_to_check_for_a_pinned_app(self):
        import contextlib
        import io
        with mock.patch.object(magehand, 'APPS_DIR', '/nonexistent'), \
                mock.patch.object(magehand, 'registration', lambda name: {'source': self.PIN}):
            with self.assertRaises(magehand.PinnedApp):
                magehand.load_app('elvis')
        out = io.StringIO()
        argv = ['magehand', 'check', 'elvis', '--apps-dir', '/nonexistent', '--manifests-only']
        with mock.patch.object(magehand.sys, 'argv', argv), \
                mock.patch.object(magehand, 'try_load_app', side_effect=magehand.PinnedApp('elvis', self.PIN)), \
                mock.patch.object(magehand, 'github_file', lambda *a, **k: ''), \
                contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            magehand.main()
        self.assertEqual(raised.exception.code, 0)
        self.assertIn('pinned to', out.getvalue())
        self.assertIn('a' * 7, out.getvalue())
        self.assertEqual(out.getvalue().splitlines()[-1], 'next: nothing')
        out = io.StringIO()
        with mock.patch.object(magehand.sys, 'argv', argv + ['--json']), \
                mock.patch.object(magehand, 'try_load_app', side_effect=magehand.PinnedApp('elvis', self.PIN)), \
                mock.patch.object(magehand, 'github_file', lambda *a, **k: ''), \
                contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            magehand.main()
        self.assertIsNone(json.loads(out.getvalue())['next'])


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

    def test_unreadable_specs_refuse_instead_of_guessing(self):
        problems = self.plan(files={'app/contents/deploy/app.yaml': None})['problems']  # absent on main: required
        self.assertTrue(any('deploy/app.yaml is missing' in p for p in problems))
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

    def lifecycle(self, repo=None, files=('app.yaml', 'base', 'dev'), registered=True, unreadable=None):
        """REAL_LIFECYCLE for demo, with its repo's setup, the deploy/ files on main and registration given."""
        state = dict({'bot': 'write', 'ruleset': 'ok', 'ruleset_detail': '', 'private': True, 'archived': False},
                     **(repo or {}))
        main = {'deploy': ['app.yaml'] if 'app.yaml' in files else [],
                'deploy/base': ['deployment.yaml'] if 'base' in files else [],
                'deploy/dev': ['kustomization.yaml'] if 'dev' in files else []}
        listing = unreadable or (lambda repo, path, ref='main': (main[path], None))
        spec = registered if isinstance(registered, dict) else \
            {'services': {'db': {'type': 'postgres'}}} if registered else None
        lookup = (lambda name: (None, registered)) if isinstance(registered, str) else (lambda name: (spec, None))
        with mock.patch.object(magehand, 'repo_state', lambda repo: state), \
                mock.patch.object(magehand, 'github_listing', listing), \
                mock.patch.object(magehand, 'registration_lookup', lookup):
            return {st['stage']: st for st in REAL_LIFECYCLE('davidlarrimore/demo', 'demo')}

    def test_lifecycle_of_a_set_up_registered_app(self):
        stages = self.lifecycle()
        self.assertEqual([st['state'] for st in stages.values()], ['ok', 'ok', 'ok'])
        self.assertIn('blocks: db (postgres)', stages['register']['text'])

    def test_lifecycle_repo_stage(self):
        broken = self.lifecycle({'bot': 'missing', 'ruleset': 'missing'})['repo']
        self.assertEqual(broken['state'], 'failed')
        self.assertIn("review can't be requested", broken['text'])
        self.assertEqual(broken['next'], 'ask the owner to run `magehand new demo` (it repairs the repo)')
        self.assertIn('it is public', self.lifecycle({'private': False})['repo']['text'])
        weak = self.lifecycle({'ruleset': 'weak', 'ruleset_detail': "enforcement is 'disabled'"})['repo']
        self.assertEqual((weak['state'], weak['next']), ('failed', "ask the owner to fix the ruleset in the repo's Settings > Rules"))
        self.assertEqual(self.lifecycle({'bot': 'invited'})['repo']['state'], 'pending')
        # OpenClaw's bot can't read the ruleset: unknown, never failed
        self.assertEqual(self.lifecycle({'ruleset': None})['repo']['state'], 'unknown')
        self.assertEqual(self.lifecycle({'archived': True, 'bot': None})['repo']['state'], 'unknown')

    def test_lifecycle_before_the_first_deploy(self):
        stages = self.lifecycle(files=('app.yaml',), registered=False)
        self.assertEqual(stages['config']['state'], 'pending')
        self.assertIn('deploy/base/, deploy/dev/', stages['config']['text'])
        self.assertIsNone(stages['register']['next'])  # config's step comes first
        merged = self.lifecycle(registered=False)['register']
        self.assertEqual((merged['state'], merged['next']), ('pending', '`magehand status demo --watch`'))
        report = {'app': 'demo', 'stages': list(self.lifecycle(files=(), registered=False).values())
                  + [{'stage': 'build', 'state': 'pending', 'text': ''}]}
        self.assertIn('write them', magehand.status_next(report, False, False))  # the first stage's step wins

    def test_a_failed_lookup_is_an_error_not_missing_config(self):
        stages = self.lifecycle(unreadable=lambda repo, path, ref='main': (None, 'HTTP 503'))
        self.assertEqual(stages['config']['state'], 'error')
        self.assertIn('cannot read deploy/ on main (deploy: HTTP 503)', stages['config']['text'])
        self.assertIn('access problem', stages['config']['next'])

    def test_github_listing_tells_missing_from_unreadable(self):
        for answer, expected in (((0, '[{"name": "app.yaml"}]', ''), (['app.yaml'], None)),
                                 ((1, '', 'gh: Not Found (HTTP 404)'), ([], None)),
                                 ((1, '', 'gh: API rate limit exceeded (HTTP 403)'),
                                  (None, 'gh: API rate limit exceeded (HTTP 403)')),
                                 ((None, '', 'gh did not answer'), (None, 'gh did not answer'))):
            with mock.patch.object(magehand, 'gh_probe', lambda *a, answer=answer: answer):
                self.assertEqual(magehand.github_listing('o/r', 'deploy'), expected, answer)

    def test_registration_lookup_tells_unregistered_from_unreadable(self):
        missing = (1, '', 'gh: Not Found (HTTP 404)')
        for file, repo, expected in (((0, 'services: {}\n', ''), None, ({'services': {}}, None)),
                                     (missing, (0, 'davidlarrimore/homelab-apps\n', ''), (None, None)),
                                     # a 404 for homelab-apps itself: this account can't see it, not "unregistered"
                                     (missing, missing, (None, 'cannot see davidlarrimore/homelab-apps '
                                                                '(gh: Not Found (HTTP 404))')),
                                     ((1, '', 'HTTP 502'), None, (None, 'HTTP 502')),
                                     ((0, '- a list\n', ''), None, (None, 'apps/demo/app.yaml is not a mapping')),
                                     ((0, 'a: [\n', ''), None, (None, 'apps/demo/app.yaml is not valid YAML'))):
            answer = lambda *a, file=file, repo=repo: file if 'contents' in ' '.join(a) else repo  # noqa: E731
            with mock.patch.object(magehand, 'gh_probe', answer), mock.patch.object(magehand, 'APPS_DIR', None):
                self.assertEqual(magehand.registration_lookup('demo'), expected, file)

    def test_lifecycle_older_layouts_and_errors(self):
        # registered, deployment in homelab-apps: no deploy/ in the repo is not pending forever
        legacy = self.lifecycle(files=(), registered={'services': {}})
        self.assertEqual(legacy['config']['state'], 'unknown')
        self.assertIn('homelab-apps apps/demo/', legacy['config']['text'])
        # pinned to the repo but deploy/ gone from main: the next deploy breaks
        pinned = self.lifecycle(files=('app.yaml',), registered={'source': {'repo': 'demo', 'ref': 'a' * 40,
                                                                             'image': 'x'}, 'services': {}})
        self.assertEqual(pinned['config']['state'], 'failed')
        # the registration couldn't be read: an error, and config doesn't claim deploy/ is missing
        unreadable = self.lifecycle(registered='HTTP 502')
        self.assertEqual(unreadable['register']['state'], 'error')
        stages = self.lifecycle(unreadable=lambda repo, path, ref='main': (None, 'HTTP 503'), registered=False)
        self.assertEqual(stages['register']['text'], 'not registered yet')

    def test_a_partial_deploy_dir_is_not_the_older_layout(self):
        partial = self.lifecycle(files=('app.yaml', 'base'), registered={'services': {}})['config']
        self.assertEqual(partial['state'], 'failed')
        self.assertIn('deploy/dev/', partial['text'])

    def test_a_public_repo_gets_the_visibility_command(self):
        public = self.lifecycle({'private': False})['repo']
        self.assertIn('--visibility private', public['next'])

    def test_a_lifecycle_error_mid_watch_does_not_end_the_watch(self):
        answers = [[{'stage': 'register', 'state': 'pending', 'text': '', 'next': 'w'}],
                   [{'stage': 'config', 'state': 'error', 'text': 'gh did not answer', 'next': 'fix'}],
                   [{'stage': 'register', 'state': 'ok', 'text': 'registered'}]]
        calls, polls = [], []

        def collect(*a, **k):
            polls.append(1)
            return {'app': 'demo', 'repo': 'o/demo', 'commit': 'a' * 40, 'subject': 's',
                    'stages': [{'stage': 'build', 'state': 'ok', 'text': ''}, {'stage': 'deploy', 'state': 'ok', 'text': ''}]}
        with mock.patch.object(magehand, 'status_target', lambda name=None: ('demo', 'o/demo')), \
                mock.patch.object(magehand, 'lifecycle_stages',
                                  lambda repo, name: calls.append(1) or answers[min(len(calls), 3) - 1]), \
                mock.patch.object(magehand, 'collect_status', collect):
            code, out, _ = CommandLine.run_main(self, 'status', '--watch', '--interval', '0', '--json')
        self.assertEqual((code, json.loads(out)['ok'], len(calls)), (0, True, 3))

    def test_watch_reads_the_lifecycle_again_when_main_moves(self):
        first = [{'stage': 'config', 'state': 'ok', 'text': 'deploy/ on main (app.yaml, base/, dev/)'}]
        second = [{'stage': 'config', 'state': 'ok', 'text': 'deploy/ on main (app.yaml, base/, dev/, prod/)'}]
        calls, polls = [], []

        def collect(*a, **k):
            polls.append(1)
            done = len(polls) > 1
            return {'app': 'demo', 'repo': 'o/demo', 'commit': ('b' if done else 'a') * 40, 'subject': 's',
                    'stages': [{'stage': 'build', 'state': 'ok', 'text': ''},
                               {'stage': 'deploy', 'state': 'ok' if done else 'pending', 'text': ''}]}
        with mock.patch.object(magehand, 'status_target', lambda name=None: ('demo', 'o/demo')), \
                mock.patch.object(magehand, 'lifecycle_stages',
                                  lambda repo, name: calls.append(1) or (first if len(calls) == 1 else second)), \
                mock.patch.object(magehand, 'collect_status', collect):
            code, out, _ = CommandLine.run_main(self, 'status', '--watch', '--interval', '0', '--json')
        report = json.loads(out)
        self.assertEqual((code, len(calls)), (0, 2))
        self.assertIn('prod/', report['stages'][0]['text'])  # the lifecycle of the commit it ended on

    def test_status_prefers_the_registered_repo(self):
        with mock.patch.object(magehand, 'repo_root', lambda name: pathlib.Path('/x')), \
                mock.patch.object(magehand, 'remote_repo', lambda root=None: 'myfork'), \
                mock.patch.object(magehand, 'registration_lookup', lambda name: ({'source': {'repo': 'demo'}}, None)):
            self.assertEqual(magehand.status_target('demo'), ('demo', 'davidlarrimore/demo'))

    def test_a_failure_outranks_a_stage_that_is_only_waiting(self):
        report = {'app': 'demo', 'stages': [
            {'stage': 'register', 'state': 'pending', 'text': '', 'next': '`magehand status demo --watch`'},
            {'stage': 'build', 'state': 'failed', 'text': ''}]}
        self.assertIn('fix the failed build', magehand.status_next(report, True, False))

    def test_a_setup_problem_does_not_stop_the_watch(self):
        broken = [{'stage': 'repo', 'state': 'failed', 'text': 'not set up', 'next': 'ask the owner'}]
        polls = []

        def collect(*a, **k):
            polls.append(1)
            state = 'ok' if len(polls) > 1 else 'pending'
            return {'app': 'demo', 'repo': 'o/demo', 'commit': 'a' * 40, 'subject': 's',
                    'stages': [{'stage': 'build', 'state': 'ok', 'text': ''},
                               {'stage': 'deploy', 'state': state, 'text': ''}]}
        with mock.patch.object(magehand, 'status_target', lambda name=None: ('demo', 'o/demo')), \
                mock.patch.object(magehand, 'lifecycle_stages', lambda repo, name: broken), \
                mock.patch.object(magehand, 'collect_status', collect):
            code, out, _ = CommandLine.run_main(self, 'status', '--watch', '--interval', '0', '--json')
        report = json.loads(out)
        self.assertEqual((code, len(polls), report['ok'], report['next']), (1, 2, False, 'ask the owner'))

    def test_watch_refreshes_a_pending_registration(self):
        # Registered while watching: status must notice, not finish "ok" with a stale pending line.
        answers = [[{'stage': 'register', 'state': 'pending', 'text': 'not registered yet', 'next': 'watch'}],
                   [{'stage': 'register', 'state': 'ok', 'text': 'registered'}]]
        calls = []
        ok = {'app': 'demo', 'repo': 'o/demo', 'commit': 'a' * 40, 'subject': 's',
              'stages': [{'stage': 'build', 'state': 'ok', 'text': ''}, {'stage': 'deploy', 'state': 'ok', 'text': ''}]}
        with mock.patch.object(magehand, 'status_target', lambda name=None: ('demo', 'o/demo')), \
                mock.patch.object(magehand, 'lifecycle_stages', lambda repo, name: calls.append(1) or answers[min(len(calls), 2) - 1]), \
                mock.patch.object(magehand, 'collect_status', lambda *a, **k: json.loads(json.dumps(ok))):
            code, out, _ = CommandLine.run_main(self, 'status', '--watch', '--interval', '0', '--json')
        report = json.loads(out)
        self.assertEqual((code, report['ok'], len(calls)), (0, True, 2))
        self.assertEqual(report['stages'][0]['state'], 'ok')
        # still pending when the time runs out: not ok
        with mock.patch.object(magehand, 'status_target', lambda name=None: ('demo', 'o/demo')), \
                mock.patch.object(magehand, 'lifecycle_stages', lambda repo, name: answers[0]), \
                mock.patch.object(magehand, 'collect_status', lambda *a, **k: json.loads(json.dumps(ok))):
            code, out, _ = CommandLine.run_main(self, 'status', '--json')
        self.assertFalse(json.loads(out)['ok'])

    def test_watch_waits_for_a_pending_invitation(self):
        invited = [{'stage': 'repo', 'state': 'pending', 'text': 'invited', 'next': 'wait'}]
        accepted = [{'stage': 'repo', 'state': 'ok', 'text': 'set up'}]
        ok = {'app': 'demo', 'repo': 'o/demo', 'commit': 'a' * 40, 'subject': 's',
              'stages': [{'stage': 'build', 'state': 'ok', 'text': ''}, {'stage': 'deploy', 'state': 'ok', 'text': ''}]}
        for answers, watch, done in (([invited, accepted], ['--watch', '--interval', '0'], True),
                                     ([invited], ['--watch', '--interval', '0', '--timeout', '0'], False)):
            calls = []
            with mock.patch.object(magehand, 'status_target', lambda name=None: ('demo', 'o/demo')), \
                    mock.patch.object(magehand, 'lifecycle_stages',
                                      lambda repo, name: calls.append(1) or answers[min(len(calls), len(answers)) - 1]), \
                    mock.patch.object(magehand, 'collect_status', lambda *a, **k: json.loads(json.dumps(ok))):
                code, out, _ = CommandLine.run_main(self, 'status', *watch, '--json')
            report = json.loads(out)
            self.assertEqual(report['ok'], done, answers)
            self.assertEqual(report['stages'][0]['state'], 'ok' if done else 'pending')
            if done:
                self.assertEqual((code, len(calls)), (0, 2))
            else:
                self.assertEqual(code, 1)  # timed out still invited

    def test_status_finds_a_new_app_without_its_deployment(self):
        with mock.patch.object(magehand, 'repo_root', lambda name: pathlib.Path('/x')), \
                mock.patch.object(magehand, 'remote_repo', lambda root=None: 'ai-learning-catalog'), \
                mock.patch.object(magehand, 'registration_lookup', lambda name: (None, None)), \
                mock.patch.object(magehand, 'load_app', lambda *a, **k: self.fail('loaded the Deployment')):
            self.assertEqual(magehand.status_target('ai-learning-catalog'),
                             ('ai-learning-catalog', 'davidlarrimore/ai-learning-catalog'))
        with mock.patch.object(magehand, 'repo_root', lambda name: None), \
                mock.patch.object(magehand, 'registration_lookup', lambda name: (None, None)), \
                self.assertRaises(SystemExit):
            magehand.status_target('nope')

    def test_a_repo_not_set_up_fails_status(self):
        failed = [{'stage': 'repo', 'state': 'failed', 'text': 'not set up', 'next': 'ask the owner'}]
        with mock.patch.object(magehand, 'lifecycle_stages', lambda repo, name: failed):
            code, report, _ = self.status_with()
        self.assertEqual((code, report['stages'][0]['stage'], report['next']), (1, 'repo', 'ask the owner'))

    def test_dev_stage_without_a_cluster_says_why_and_does_not_block(self):
        deploy = {'digest': 'sha256:aaaa', 'image': self.IMAGE}
        saved = magehand.kubectl_json
        try:
            magehand.kubectl_json = lambda *a: (None, 'kubectl is not installed')
            stage = magehand.dev_stage('demo', deploy)
            self.assertEqual(stage['state'], 'unknown')
            self.assertIn('not visible from this machine (kubectl is not installed: normal on a laptop)', stage['text'])
            magehand.kubectl_json = lambda *a: (None, 'The connection to the server localhost:8080 was refused')
            self.assertIn('not visible from this machine (no cluster access', magehand.dev_stage('demo', deploy)['text'])
            magehand.kubectl_json = lambda *a: (None, 'Forbidden: pods is forbidden')
            self.assertIn('not checked (Forbidden', magehand.dev_stage('demo', deploy)['text'])
            self.assertIn('--no-cluster', magehand.dev_stage('demo', deploy, cluster=False)['text'])
            magehand.kubectl_json = lambda *a: (({'items': [self.pod()]} if a[0] == 'pods' else self.argo()), None)
            self.assertEqual(magehand.dev_stage('demo', deploy)['state'], 'ok')
        finally:
            magehand.kubectl_json = saved

    def recorded(self, state, image=None, description='', statuses=True, error=None, payload=None):
        """A gh_query stub: GitHub's answer for one platform Deployment of HEAD with `state` as its newest status
        (`payload` replaces the platform's)."""
        def query(*args):
            path = args[1]
            if error:
                return None, error
            if '/statuses' in path:
                return ([{'state': state, 'description': description, 'created_at': '2026-10-24T09:15:00Z'}]
                        if statuses else []), None
            if 'deployments?' in path:
                self.asked = path
                mine = {'image': image or self.IMAGE, 'application': 'demo-dev'} if payload is None else payload
                return [{'id': 3, 'payload': mine, 'created_at': '2026-10-24T09:10:00Z'}], None
            return None, 'unexpected ' + path
        return query

    def dev(self, recorded, kubectl=(None, 'kubectl is not installed')):
        deploy = {'digest': 'sha256:aaaa', 'image': self.IMAGE}
        with mock.patch.object(magehand, 'gh_query', recorded), \
                mock.patch.object(magehand, 'kubectl_json', lambda *a: kubectl):
            return magehand.dev_stage('demo', deploy, repo='davidlarrimore/demo', head=self.HEAD)

    def test_dev_reads_the_platforms_deployment_from_any_machine(self):
        stage = self.dev(self.recorded('success'))
        self.assertEqual(stage['state'], 'ok')
        self.assertIn('running sha256:aaaa (the platform saw every pod Ready, 09:15Z)', stage['text'])
        self.assertIn(f'environment=dev&sha={self.HEAD}', self.asked)

    def test_dev_failure_and_rollout_from_the_deployment_without_a_cluster(self):
        stage = self.dev(self.recorded('failure', description="it doesn't run d8b3476 after 20 minutes"))
        self.assertEqual(stage['state'], 'failed')
        self.assertIn("it doesn't run d8b3476 after 20 minutes (the platform, 09:15Z)", stage['text'])
        self.assertEqual(self.dev(self.recorded('in_progress'))['state'], 'pending')
        self.assertEqual(self.dev(self.recorded('x', statuses=False))['state'], 'pending', 'no status yet: pending')

    def test_dev_prefers_the_pods_to_a_failure_when_it_can_read_them(self):
        pods = lambda *a: (({'items': [self.pod()]} if a[0] == 'pods' else self.argo()), None)  # noqa: E731
        deploy = {'digest': 'sha256:aaaa', 'image': self.IMAGE}
        with mock.patch.object(magehand, 'gh_query', self.recorded('failure')), \
                mock.patch.object(magehand, 'kubectl_json', pods):
            self.assertEqual(magehand.dev_stage('demo', deploy, repo='davidlarrimore/demo', head=self.HEAD)['state'], 'ok')

    def test_current_pods_win_over_a_recorded_success(self):
        crashing = lambda *a: (({'items': [self.pod(ready=False, waiting='CrashLoopBackOff')]} if a[0] == 'pods'  # noqa: E731
                                else self.argo(health='Degraded')), None)
        deploy = {'digest': 'sha256:aaaa', 'image': self.IMAGE}
        with mock.patch.object(magehand, 'gh_query', self.recorded('success')), \
                mock.patch.object(magehand, 'kubectl_json', crashing):
            stage = magehand.dev_stage('demo', deploy, repo='davidlarrimore/demo', head=self.HEAD)
        self.assertEqual(stage['state'], 'failed')
        self.assertIn('CrashLoopBackOff', stage['text'])
        with mock.patch.object(magehand, 'gh_query', self.recorded('success')), \
                mock.patch.object(magehand, 'kubectl_json', lambda *a: ({'items': []} if a[0] == 'pods' else self.argo(), None)):
            self.assertEqual(magehand.dev_stage('demo', deploy, repo='davidlarrimore/demo', head=self.HEAD)['state'],
                             'pending', 'no pods now: not running, whatever was recorded')

    def test_only_the_platforms_own_records_count(self):
        for payload in ('a string payload', {}, {'image': self.IMAGE}, {'image': self.IMAGE, 'application': 'other-dev'},
                        {'application': 'demo-dev'}, {'image': 'ghcr.io/example/app@sha256:' + 'b' * 64,
                                                      'application': 'demo-dev'}):
            with self.subTest(payload=payload):
                stage = self.dev(self.recorded('success', payload=payload))
                self.assertEqual(stage['state'], 'unknown')
                self.assertIn('not visible from this machine', stage['text'])

    def test_dev_without_a_usable_deployment_is_as_before(self):
        for recorded in (self.recorded('success', image='ghcr.io/example/app@sha256:' + 'b' * 64),
                         self.recorded('success', error='HTTP 403'), self.recorded('inactive')):
            stage = self.dev(recorded)
            self.assertEqual(stage['state'], 'unknown')
            self.assertIn('not visible from this machine', stage['text'])

    def test_prod_shows_what_the_platform_saw_run(self):
        merged = magehand.prod_stage('v1.2.0', 3, [{'number': 1, 'state': 'MERGED',
                                                    'title': 'demo: release v1.2.0 to production'}])
        at = '2026-10-24T09:15:00Z'
        running = magehand.prod_running(merged, {'state': 'success', 'description': '', 'at': at})
        self.assertEqual((running['state'], running['text']),
                         ('ok', 'v1.2.0 running (the platform saw every pod Ready, 09:15Z)  (3 commits on main since v1.2.0)'))
        failed = magehand.prod_running(merged, {'state': 'failure', 'description': 'it gave up', 'at': at})
        self.assertEqual(failed['state'], 'failed')
        self.assertIn('v1.2.0: it gave up', failed['text'])
        self.assertEqual(magehand.prod_running(merged, {'state': 'in_progress', 'description': '', 'at': at})['state'],
                         'pending')
        self.assertIs(magehand.prod_running(merged, None), merged)

    def test_watch_waits_for_the_pods_and_ignores_unreadable_ones(self):
        names = ('status_target', 'collect_status', 'time')
        before = {n: getattr(magehand, n) for n in names}
        magehand.status_target = lambda name=None: ('demo', 'davidlarrimore/demo')
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

    def status_with(self, deploy_checks=(), fail=None, prod=None):
        """Run `status` through a fake gh: an app whose build and release are fine, a dev deploy PR with
        `deploy_checks`, the platform's production Deployment of v1.2.0 in state `prod` (None: none), and every
        query whose arguments contain `fail` (a string) failing."""
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
            if line.endswith('/commits/v1.2.0'):
                return 0, json.dumps({'sha': 'f' * 40}), ''
            if prod and f'deployments?environment=production&sha={"f" * 40}' in line:
                return 0, json.dumps([{'id': 4, 'payload': {'application': 'demo-prod', 'image': 'x'}}]), ''
            if prod and '/deployments/4/statuses' in line:
                return 0, json.dumps([{'state': prod, 'description': 'it gave up', 'created_at': '2026-10-24T09:15:00Z'}]), ''
            return 1, '', 'unexpected: ' + line
        names = ('status_target', 'gh_probe')
        before = {n: getattr(magehand, n) for n in names}
        magehand.status_target = lambda name=None: ('demo', 'davidlarrimore/demo')
        magehand.gh_probe = probe
        try:
            code, out, err = self.run_main('status', '-a', 'demo', '--json')
        finally:
            for n, v in before.items():
                setattr(magehand, n, v)
        return code, json.loads(out), err

    def test_a_failed_production_rollout_fails_status(self):
        code, report, _ = self.status_with(prod='failure')
        prod = report['stages'][-1]
        self.assertEqual((code, report['ok'], prod['state']), (1, False, 'failed'))
        self.assertIn('v1.2.0: it gave up', prod['text'])
        self.assertIn('magehand release demo --dry-run', report['next'])
        code, report, _ = self.status_with(prod='success')
        self.assertEqual((code, report['stages'][-1]['state']), (0, 'ok'))
        self.assertIn('v1.2.0 running', report['stages'][-1]['text'])

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
        names = ('status_target', 'collect_status')
        before = {n: getattr(magehand, n) for n in names}
        magehand.status_target = lambda name=None: ('demo', 'davidlarrimore/demo')
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


SELECTED = ('apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: demo}\nspec:\n'
            '  selector: {matchLabels: {app.kubernetes.io/name: demo}}\n  template:\n'
            '    metadata: {labels: {app.kubernetes.io/name: demo}}\n    spec:\n'
            '      securityContext: {runAsNonRoot: true, runAsUser: 65532, seccompProfile: {type: RuntimeDefault}}\n'
            '      containers:\n        - name: app\n          image: ghcr.io/davidlarrimore/homelab-apps/demo\n'
            '          env:\n            - {name: PORT, value: "8000"}\n'
            '            - name: KEY\n              valueFrom: {secretKeyRef: {name: ai-connection, key: api_key}}\n')


class Jobs(unittest.TestCase):
    """`magehand job add/rm/ls` and the CronJob rules of `check` (homelab docs/app-jobs-plan.md)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = deploy_repo(self.tmp.name, extra={
            'deploy/base/kustomization.yaml': '# the base\nresources:\n  - deployment.yaml  # the app\n  - es.yaml\n'})
        (self.root / 'deploy/base/deployment.yaml').write_text(SELECTED)
        self.cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.cwd)
        for p in (mock.patch.object(magehand, 'app_repo_here', lambda: 'demo'),
                  mock.patch.object(magehand, 'app_here', lambda name=None: self.app())):
            p.start()
            self.addCleanup(p.stop)

    def app(self):
        app = magehand.try_load_app('demo', self.root)
        return app

    run_main = CommandLine.run_main

    def add(self, *extra):
        return self.run_main('job', 'add', 'collect', '--schedule', '15 6 * * *', *extra,
                             '--', 'python', '-m', 'jobs.collect', '--out', '/tmp/x.json')

    def test_cron_schedules(self):
        for good in ('15 6 * * *', '*/15 * * * *', '0 8 * * 1-5', '0 0 1,15 * *', '@daily', '30 2 * JAN MON'):
            self.assertIsNone(magehand.cron_problem(good), good)
        for bad, why in (('15 6 * *', 'five fields'), ('61 * * * *', '0 to 59'), ('0 24 * * *', '0 to 23'),
                         ('TZ=UTC 0 6 * * *', 'timeZone'), ('0 6 * * $', 'values are 0 to 6'), ('', 'five fields')):
            self.assertIn(why, magehand.cron_problem(bad), bad)

    def test_too_frequent(self):
        for often in ('* * * * *', '*/2 * * * *', '0-10 6 * * *', '0,3 * * * *', '58,1 * * * *'):
            self.assertTrue(magehand.cron_too_frequent(often), often)
        for fine in ('*/5 * * * *', '0 * * * *', '0,30 * * * *', '15 6 * * *', '@hourly', '5/10 * * * *'):
            self.assertFalse(magehand.cron_too_frequent(fine), fine)

    def test_durations_and_memory(self):
        self.assertEqual([magehand.duration_seconds(t) for t in ('90s', '10m', '2h', '0m', '10', 'm')],
                         [90, 600, 7200, None, None, None])
        self.assertEqual([magehand.memory_mi(t) for t in ('256Mi', '1Gi', '1G', '0Mi')], [256, 1024, None, None])

    def test_kustomization_edits_keep_the_file(self):
        block = '# the base\nresources:\n  - deployment.yaml  # the app\n  - es.yaml\nimages: []\n'
        added = magehand.add_resource(block, 'job-x.yaml')
        self.assertEqual(added, '# the base\nresources:\n  - deployment.yaml  # the app\n  - es.yaml\n'
                                '  - job-x.yaml\nimages: []\n')
        self.assertEqual(magehand.remove_resource(added, 'job-x.yaml'), block)
        flow = 'kind: Kustomization\nresources: [deployment.yaml, service.yaml]\n'
        self.assertEqual(magehand.kustomization_resources(magehand.add_resource(flow, 'job-x.yaml')),
                         ['deployment.yaml', 'service.yaml', 'job-x.yaml'])
        self.assertEqual(magehand.remove_resource(magehand.add_resource(flow, 'job-x.yaml'), 'job-x.yaml'), flow)
        self.assertEqual(magehand.kustomization_resources(magehand.add_resource('kind: Kustomization', 'j.yaml')),
                         ['j.yaml'])
        flush = 'resources:\n- deployment.yaml\n'  # items at column 0 are YAML too
        self.assertEqual(magehand.add_resource(flush, 'j.yaml'), 'resources:\n- deployment.yaml\n- j.yaml\n')

    def test_add_writes_a_cronjob_check_accepts(self):
        code, out, err = self.add()
        self.assertEqual(code, 0, err)
        self.assertIn('wrote deploy/base/job-collect.yaml', out)
        self.assertIn('job-collect.yaml', magehand.kustomization_resources(
            (self.root / 'deploy/base/kustomization.yaml').read_text()))
        self.assertIn('# the app', (self.root / 'deploy/base/kustomization.yaml').read_text())  # comments kept
        text = (self.root / 'deploy/base/job-collect.yaml').read_text()
        self.assertNotIn('&id', text)  # no YAML anchors
        doc = yaml_load(text)
        spec, job, pod, container = magehand.cronjob_parts(doc)
        self.assertEqual((spec['schedule'], spec['timeZone'], spec['concurrencyPolicy']),
                         ('15 6 * * *', 'Etc/UTC', 'Forbid'))
        self.assertEqual((job['backoffLimit'], job['activeDeadlineSeconds']), (1, 600))
        self.assertEqual(container['command'], ['python', '-m', 'jobs.collect', '--out', '/tmp/x.json'])
        self.assertEqual(container['image'], 'ghcr.io/davidlarrimore/homelab-apps/demo')
        self.assertEqual([e['name'] for e in container['env']], ['PORT', 'KEY'])  # the Deployment's env
        self.assertEqual(pod['securityContext']['runAsUser'], 65532)  # the Deployment's user
        self.assertFalse(pod['automountServiceAccountToken'])
        labels = job['template']['metadata']['labels']
        self.assertNotEqual(labels.get('app.kubernetes.io/name'), 'demo')  # the Service doesn't select it
        self.assertEqual(magehand.check_jobs(self.app(), CATALOG_MODELS), [])

    def test_add_refuses_bad_input_and_a_second_job_of_the_same_name(self):
        for extra, why in ((('--timeout', '10'), '--timeout'), (('--memory', '2Gi'), '--memory'),
                           (('--timezone', 'Mars/Base'), '--timezone')):
            code, _, err = self.add(*extra)
            self.assertEqual(code, 2, extra)
            self.assertIn(why, err)
        code, _, err = self.run_main('job', 'add', 'collect', '--schedule', '@daily')
        self.assertEqual(code, 2)
        self.assertIn('give the command after --', err)
        code, _, err = self.run_main('job', 'add', 'Bad_Name', '--schedule', '@daily', '--', 'true')
        self.assertEqual(code, 2)
        self.assertEqual(self.add()[0], 0)
        self.assertEqual(self.add()[0], 1)  # exists
        self.assertFalse(any(self.root.glob('deploy/base/job-*.yaml.*')))

    def test_rm_removes_the_file_and_its_line(self):
        before = (self.root / 'deploy/base/kustomization.yaml').read_text()
        self.add()
        code, out, _ = self.run_main('job', 'rm', 'collect', '--json')
        self.assertEqual((code, json.loads(out)['action']), (0, 'rm'))
        self.assertFalse((self.root / 'deploy/base/job-collect.yaml').exists())
        self.assertEqual((self.root / 'deploy/base/kustomization.yaml').read_text(), before)
        code, _, _ = self.run_main('job', 'rm', 'collect')
        self.assertEqual(code, 1)

    def test_only_this_checkouts_app(self):
        code, _, _ = self.run_main('job', '-a', 'other', 'ls', '--no-cluster')
        self.assertEqual(code, 1)

    def cronjob(self, **changes):
        doc = magehand.job_manifest(self.app(), 'nightly', '0 3 * * *', ['true'], 600, '256Mi', 'Etc/UTC')
        spec, job, pod, container = magehand.cronjob_parts(doc)
        for key, value in changes.items():
            target = {'spec': spec, 'job': job, 'pod': pod, 'container': container}[key.split('__')[0]]
            name = key.split('__')[1]
            if value is None:
                target.pop(name, None)
            else:
                target[name] = value
        return doc

    def write(self, doc, name='job-nightly.yaml', listed=True):
        import yaml as pyyaml
        (self.root / 'deploy/base' / name).write_text(pyyaml.safe_dump(doc, sort_keys=False))
        if listed:
            k = self.root / 'deploy/base/kustomization.yaml'
            k.write_text(magehand.add_resource(k.read_text(), name))

    def test_check_rules(self):
        self.write(self.cronjob(spec__concurrencyPolicy='Allow', spec__startingDeadlineSeconds=None,
                                job__activeDeadlineSeconds=None, job__backoffLimit=None,
                                container__image='ghcr.io/davidlarrimore/homelab-apps/other'))
        errors = texts(magehand.check_jobs(self.app(), CATALOG_MODELS), 'error')
        for why in ('concurrencyPolicy: Forbid', 'startingDeadlineSeconds', 'activeDeadlineSeconds', 'backoffLimit',
                    "the app's own image"):
            self.assertTrue(any(why in e for e in errors), why)

    def test_check_finds_a_job_the_service_selects_secrets_nothing_makes_and_an_unlisted_file(self):
        doc = self.cronjob(spec__schedule='*/2 * * * *',
                           container__env=[ref('X', 'missing-connection', 'uri')])
        doc['spec']['jobTemplate']['spec']['template']['metadata']['labels'] = {'app.kubernetes.io/name': 'demo'}
        self.write(doc, listed=False)
        found = magehand.check_jobs(self.app(), CATALOG_MODELS)
        errors = texts(found, 'error')
        self.assertTrue(any("Service would send requests" in e for e in errors))
        self.assertTrue(any("Secret 'missing-connection'" in e for e in errors))
        self.assertTrue(any('never deployed' in e for e in errors))
        self.assertTrue(any('more often than every 5' in w for w in texts(found, 'warning')))

    def test_a_bare_job_is_a_warning(self):
        (self.root / 'deploy/base/once.yaml').write_text('kind: Job\nmetadata: {name: once}\n')
        found = magehand.check_jobs(self.app(), CATALOG_MODELS)
        self.assertEqual([(f['level'], f['where']) for f in found], [('warning', 'deploy/base/once.yaml')])

    def test_check_command_runs_the_job_rules(self):
        self.write(self.cronjob(spec__concurrencyPolicy=None))
        with tempfile.TemporaryDirectory() as apps, mock.patch.object(magehand, 'github_file',
                                                                      lambda *a, **k: None):
            code, out, _ = self.run_main('check', 'demo', '--code', str(self.root), '--apps-dir', apps,
                                         '--manifests-only', '--json')
        self.assertEqual(code, 1)
        self.assertTrue(any('concurrencyPolicy' in f['text'] for f in json.loads(out)['findings']))

    def test_ls_with_dev_runs(self):
        self.add()
        cluster = {'items': [
            {'kind': 'CronJob', 'metadata': {'name': 'collect'}, 'spec': {'schedule': '15 6 * * *'},
             'status': {'lastScheduleTime': '2026-10-18T06:15:00Z', 'lastSuccessfulTime': '2026-10-17T06:15:20Z'}},
            {'kind': 'CronJob', 'metadata': {'name': 'old'}, 'spec': {'schedule': '@daily'}, 'status': {}},
            {'kind': 'Job', 'metadata': {'name': 'collect-1', 'ownerReferences': [{'kind': 'CronJob', 'name': 'collect'}]},
             'status': {'startTime': '2026-10-17T06:15:00Z', 'completionTime': '2026-10-17T06:15:20Z', 'succeeded': 1}},
            {'kind': 'Job', 'metadata': {'name': 'collect-2', 'ownerReferences': [{'kind': 'CronJob', 'name': 'collect'}]},
             'status': {'startTime': '2026-10-18T06:15:00Z', 'failed': 2, 'conditions': [
                 {'type': 'Failed', 'status': 'True', 'lastTransitionTime': '2026-10-18T06:16:00Z'}]}},
            {'kind': 'Job', 'metadata': {'name': 'other-1', 'ownerReferences': [{'kind': 'CronJob', 'name': 'x'}]},
             'status': {}}]}
        seen = []
        with mock.patch.object(magehand, 'kubectl_json', lambda *a: seen.append(a) or (cluster, None)):
            code, out, _ = self.run_main('job', 'ls')
            _, data, _ = self.run_main('job', 'ls', '--json')
        self.assertEqual(code, 0)
        self.assertEqual(seen[0], ('cronjobs,jobs', '-n', 'demo-dev'))
        self.assertIn('✗ collect-2 2026-10-18 06:15Z, 60s', out)
        self.assertIn('✓ collect-1 2026-10-17 06:15Z, 20s', out)
        self.assertLess(out.index('collect-2'), out.index('collect-1'))  # newest first
        self.assertIn('logs: kubectl -n demo-dev logs job/collect-2', out)
        self.assertIn('old: @daily', out)
        self.assertIn('in dev only', out)
        jobs = {j['name']: j for j in json.loads(data)['jobs']}
        self.assertEqual([r['job'] for r in jobs['collect']['dev']['runs']], ['collect-2', 'collect-1'])
        failed = 'next: read why its last run failed: `kubectl -n demo-dev logs job/collect-2`'
        self.assertEqual(out.splitlines()[-1], failed)
        self.assertEqual('next: ' + json.loads(data)['next'], failed)
        with mock.patch.object(magehand, 'kubectl_json', lambda *a: self.fail('read the cluster')):
            out = self.run_main('job', 'ls', '--no-cluster')[1]
            self.assertIn('not checked (--no-cluster)', out)
            self.assertEqual(out.splitlines()[-1], 'next: nothing')
            self.assertIsNone(json.loads(self.run_main('job', 'ls', '--no-cluster', '--json')[1])['next'])

    def test_ls_without_jobs_says_how_to_add_one(self):
        out = self.run_main('job', 'ls', '--no-cluster')[1]
        self.assertEqual(out.splitlines()[-1], 'next: `magehand job add NAME --schedule CRON -- COMMAND`')
        self.assertIn('job add', json.loads(self.run_main('job', 'ls', '--no-cluster', '--json')[1])['next'])

    def test_status_ignores_running_job_pods(self):
        image = 'ghcr.io/example/app@sha256:' + 'a' * 64
        app_pod = {'metadata': {'name': 'app-1'}, 'status': {'phase': 'Running', 'containerStatuses': [
            {'ready': True, 'imageID': image, 'state': {'running': {}}}]}}
        job_pod = {'metadata': {'name': 'collect-1-x', 'ownerReferences': [{'kind': 'Job', 'name': 'collect-1'}]},
                   'status': {'phase': 'Running', 'containerStatuses': [
                       {'ready': True, 'imageID': 'ghcr.io/example/app@sha256:' + 'b' * 64, 'state': {'running': {}}}]}}
        argo = {'status': {'sync': {'status': 'Synced'}, 'health': {'status': 'Healthy'}}}
        self.assertEqual(magehand.pod_stage({'items': [app_pod, job_pod]}, argo, image)['state'], 'ok')

    def test_in_process_scheduler_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            pathlib.Path(tmp, 'app.py').write_text('from apscheduler.schedulers.background import BackgroundScheduler\n')
            pathlib.Path(tmp, 'web.js').write_text("const cron = require('node-cron')\n")
            found = magehand.check_code(pathlib.Path(tmp), None)
        warnings = [f for f in found if 'scheduler in the app' in f['text']]
        self.assertEqual(sorted(f['where'] for f in warnings), ['app.py:1', 'web.js:1'])
        self.assertTrue(all(f['level'] == 'warning' for f in warnings))

    def test_cron_grammar_is_the_controllers(self):
        """robfig/cron v3 ParseStandard, which Kubernetes' CronJob controller uses: what it rejects, we reject."""
        for bad, why in (('*/0 * * * *', 'above 0'), ('*/no * * * *', 'above 0'), ('10-1 * * * *', 'high to low'),
                         ('foo * * * *', 'values are 0 to 59'), ('0 6 * * 7', 'values are 0 to 6'),
                         ('0 0 1,,2 * *', 'values are 1 to 31'), ('0 0 * FOO *', 'JAN'), ('0 0 * * MON-', 'SUN')):
            self.assertIn(why, magehand.cron_problem(bad) or '', bad)
        for good in ('0 9 * * MON-FRI', '0 0 1 jan,jul *', '0 0 ? * SUN', '5/10 * * * *', '@midnight', '@daily'):
            self.assertIsNone(magehand.cron_problem(good), good)
        # Descriptors: spelled exactly (one only when the schedule starts with @), and @every refused: its
        # duration is Go's (no tab after @every, no overflow), which we can't check.
        for bad, why in (('@every 1h', 'not supported'), ('@every\t1h', 'not supported'),
                         ('@every 10000000h', 'not supported'), ('@Daily', 'spelled exactly'),
                         ('@daily ', 'spelled exactly'), (' @daily', 'five fields')):
            self.assertIn(why, magehand.cron_problem(bad) or '', repr(bad))
        for schedule in ('@every\t1h', '@every 10000000h'):  # the command and check refuse them too
            self.assertEqual(self.run_main('job', 'add', 'x', '--schedule', schedule, '--', 'true')[0], 2, repr(schedule))
            doc = self.cronjob(spec__schedule=schedule)
            self.write(doc, name='job-every.yaml')
            self.assertTrue(any('not supported' in e for e in texts(magehand.check_jobs(self.app(), CATALOG_MODELS),
                                                                    'error')), repr(schedule))
            (self.root / 'deploy/base/job-every.yaml').unlink()
            k = self.root / 'deploy/base/kustomization.yaml'
            k.write_text(magehand.remove_resource(k.read_text(), 'job-every.yaml'))
        code, _, err = self.run_main('job', 'add', 'x', '--schedule', '*/0 * * * *', '--', 'true')
        self.assertEqual(code, 2)
        self.assertIn('above 0', err)

    def test_a_multiline_flow_list_keeps_every_resource(self):
        k = self.root / 'deploy/base/kustomization.yaml'
        k.write_text('kind: Kustomization\nresources: [\n  deployment.yaml,  # the app\n  es.yaml\n]\nimages: []\n')
        self.assertEqual(self.add()[0], 0)
        self.assertEqual(magehand.kustomization_resources(k.read_text()), ['deployment.yaml', 'es.yaml',
                                                                           'job-collect.yaml'])
        self.assertIn('images: []', k.read_text())
        self.assertEqual(self.run_main('job', 'rm', 'collect')[0], 0)
        self.assertEqual(magehand.kustomization_resources(k.read_text()), ['deployment.yaml', 'es.yaml'])

    def test_a_layout_it_cannot_edit_safely_writes_nothing(self):
        k = self.root / 'deploy/base/kustomization.yaml'
        odd = 'kind: Kustomization\nlist: &r [deployment.yaml, es.yaml]\nresources: *r\n'
        k.write_text(odd)
        code, _, _ = self.add()
        self.assertEqual(code, 1)
        self.assertEqual(k.read_text(), odd)
        self.assertFalse((self.root / 'deploy/base/job-collect.yaml').exists())
        self.assertIsNone(magehand.edit_kustomization(odd, 'job-x.yaml'))
        self.assertIsNone(magehand.edit_kustomization('resources: [a.yaml]\n', 'a.yaml'))  # already there

    def test_a_job_named_like_the_app_is_not_selected(self):
        code, _, err = self.run_main('job', 'add', 'demo', '--schedule', '@daily', '--', 'true')
        self.assertEqual(code, 0, err)
        doc = yaml_load((self.root / 'deploy/base/job-demo.yaml').read_text())
        labels = magehand.cronjob_parts(doc)[1]['template']['metadata']['labels']
        self.assertNotIn('app.kubernetes.io/name', labels)
        self.assertEqual(labels[magehand.JOB_LABEL], 'demo')
        self.assertEqual(magehand.check_jobs(self.app(), CATALOG_MODELS), [])

    def test_add_refuses_when_the_deployment_selects_what_a_job_carries(self):
        (self.root / 'deploy/base/deployment.yaml').write_text(
            SELECTED.replace('app.kubernetes.io/name: demo', 'app.kubernetes.io/part-of: demo'))
        code, _, _ = self.add()
        self.assertEqual(code, 1)
        self.assertFalse((self.root / 'deploy/base/job-collect.yaml').exists())


BUILD_APP = """jobs:
  build:
    steps:
      - name: Browser check
        env:
          PLAYWRIGHT_VERSION: 1.63.0
          AXE_VERSION: 4.13.0
          AXE_SHA256: 096912f6e77b4b695fc9304b7be65091db42d98de054da674ae222a11cb33f70
          POSTGRES_IMAGE: postgres:18-alpine@sha256:77f585114c32fbca283dc835b0596f4e52b51b4c6662d7810b2f4084f60a1873
        run: |
          /tmp/ui-venv/bin/pip install --quiet "playwright==$PLAYWRIGHT_VERSION" pyyaml==6.0.3
"""


class CheckUI(unittest.TestCase):
    """`check --ui` runs homelab-apps' browser check with build-app.yaml's pins, against the image built here."""

    def run_main(self, *argv):
        """CommandLine.run_main, with a die() message (the SystemExit's text) in stderr."""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                magehand.main(list(argv))
            except SystemExit as exit_:
                code = exit_.code if isinstance(exit_.code, int) else 1
                if isinstance(exit_.code, str):
                    err.write(exit_.code)
        return code, out.getvalue(), err.getvalue()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name) / 'demo'
        self.root.mkdir()
        (self.root / 'ui-check.yaml').write_text('port: 8080\nenv:\n  DATABASE_URL: ${POSTGRES_URL}\n  MODE: ui\n'
                                                 'pages:\n  - path: /\n')
        self.calls, self.runs, self.result = [], [], 0
        files = {magehand.BUILD_WORKFLOW: BUILD_APP, magehand.UI_SCRIPT: 'print("ui check")\n'}
        for patch in (mock.patch.object(magehand, 'CACHE', pathlib.Path(tmp.name) / 'cache'),
                      mock.patch.object(magehand, 'runtime', lambda: 'docker'),
                      mock.patch.object(magehand.shutil, 'which', lambda cli: '/usr/bin/' + cli),
                      mock.patch.object(magehand, 'checkout_app', lambda root: None),  # not a git checkout
                      mock.patch.object(magehand, 'update_notice', lambda: None),
                      mock.patch.object(magehand, 'install_method', lambda *a: 'uv'),
                      mock.patch.object(magehand, 'github_file', lambda repo, path, **k: files[path]),
                      mock.patch.object(magehand, 'engine', self.engine),
                      mock.patch.object(magehand.subprocess, 'run', self.fake_run),
                      mock.patch.object(magehand.time, 'sleep', lambda s: None)):
            patch.start()
            self.addCleanup(patch.stop)

    failing = ()  # engine() commands that fail, by their first two words

    def engine(self, *args, check=True, env=None):
        self.calls.append((args, env))
        if args[:2] in self.failing:
            return subprocess.CompletedProcess(args, 1, '', 'Error: pull access denied\n')
        if args[0] == 'cp':  # the Playwright container's /out, copied out as the user
            out = pathlib.Path(args[2])
            out.mkdir()
            if self.result == 'no answer':
                (out / magehand.UI_NO_ANSWER).write_text('')
            else:
                (out / 'report.md').write_text('# Browser check\n\n## ok home\n' if self.result == 0 else
                                               '# Browser check\n\n## FAIL home\n- overflow\n')
        return subprocess.CompletedProcess(args, 0, 'app log line\n', '')

    def fake_run(self, argv, **kwargs):
        self.runs.append(argv)
        return subprocess.CompletedProcess(argv, 1 if argv[1] == 'run' and self.result else 0)

    def check(self, *extra):
        return self.run_main('check', '--ui', '-a', 'demo', '--code', str(self.root), *extra)

    def test_runs_ci_s_script_with_ci_s_pins(self):
        code, out, _ = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn('## ok home', out)
        self.assertIn('next: look at the screenshots in', out)
        build, runner = self.runs
        self.assertEqual(build[:4], ['docker', 'build', '-t', 'magehand/demo:ui-check'])
        self.assertIn('mcr.microsoft.com/playwright/python:v1.63.0-noble', runner)
        self.assertIn('container:magehand-ui-demo-app', runner)
        for pin in ('AXE_SHA256=096912f6e77b4b695fc9304b7be65091db42d98de054da674ae222a11cb33f70',
                    'PYYAML_VERSION=6.0.3', 'PORT=8080'):
            self.assertIn(pin, runner)
        script = self.root.parent / 'cache/ui-check/demo/check/ui_check.py'
        self.assertEqual(script.read_text(), 'print("ui check")\n')  # the platform's script, not a copy

    def test_the_app_runs_as_in_the_cluster_with_its_env_out_of_argv(self):
        self.check()
        app = next((args, env) for args, env in self.calls if args[:1] == ('run',) and '--read-only' in args)
        args, env = app
        for flag in ('--read-only', '--cap-drop', 'no-new-privileges'):
            self.assertIn(flag, args)
        self.assertEqual(env, {'DATABASE_URL': magehand.UI_POSTGRES_URL, 'MODE': 'ui'})
        self.assertNotIn(magehand.UI_POSTGRES_URL, ' '.join(args))
        db = [args for args, _ in self.calls if args[:1] == ('run',) and 'ui-postgres' in args]
        self.assertEqual(len(db), 1)  # DATABASE_URL names it, so the throwaway Postgres starts

    def test_no_postgres_when_the_env_does_not_name_it(self):
        (self.root / 'ui-check.yaml').write_text('pages:\n  - path: /\n')
        self.check()
        self.assertFalse([a for a, _ in self.calls if a[:1] == ('run',) and 'ui-postgres' in a])

    def test_a_failure_exits_1_with_the_report_and_cleans_up(self):
        self.result = 1
        code, out, err = self.check('--json')
        report = json.loads(out)
        self.assertEqual((code, report['ok']), (1, False))
        self.assertIn(f'then `magehand check --ui -a demo --code {self.root}`', report['next'])
        self.assertIn('app log line', err)
        self.assertEqual(self.calls[-2][0], ('rm', '-f', 'magehand-ui-demo-app', 'magehand-ui-demo-postgres',
                                             'magehand-ui-demo-browser'))
        self.assertEqual(self.calls[-1][0], ('network', 'rm', 'magehand-ui-demo'))

    def test_an_app_that_never_answers_names_the_port(self):
        self.result = 'no answer'
        code, out, err = self.check()
        self.assertEqual((code, out), (1, ''))
        self.assertIn('did not answer on port 8080', err)
        self.assertEqual(self.calls[-1][0], ('network', 'rm', 'magehand-ui-demo'))

    def test_no_web_ui_is_nothing_to_check(self):
        (self.root / 'ui-check.yaml').write_text('web_ui: false\n')
        code, out, _ = self.check()
        self.assertEqual((code, self.runs), (0, []))
        self.assertIn('next: push', out)

    def test_a_missing_ui_check_yaml_says_how_to_add_one(self):
        (self.root / 'ui-check.yaml').unlink()
        code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn('web_ui: false', err)

    def test_a_failed_build_stops_before_any_container(self):
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1)
        with mock.patch.object(magehand.subprocess, 'run', run):
            code, _, err = self.check()
        self.assertEqual((code, self.calls), (1, []))
        self.assertIn('build failed', err)

    def test_a_container_that_fails_to_start_says_why_and_cleans_up(self):
        self.failing = (('run', '-d'),)
        code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn('starting Postgres failed: Error: pull access denied', err)
        self.assertEqual(self.calls[-1][0], ('network', 'rm', 'magehand-ui-demo'))

    def test_postgres_that_never_gets_ready_stops_the_run(self):
        self.failing = (('exec', 'magehand-ui-demo-postgres'),)
        code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn('Postgres was not ready within 30s', err)
        ready = next(a for a, _ in self.calls if a[0] == 'exec')
        self.assertIn('127.0.0.1', ready)  # over TCP: initdb's temporary server answers only on its socket

    def test_a_runtime_that_is_not_installed_says_so(self):
        with mock.patch.object(magehand.shutil, 'which', lambda cli: None):
            code, _, err = self.check()
        self.assertEqual((code, self.runs), (1, []))
        self.assertIn('`magehand runtime auto`', err)

    def test_another_app_s_checkout_is_not_checked_under_this_name(self):
        with mock.patch.object(magehand, 'checkout_app', lambda root: 'other'):
            code, _, err = self.check()
        self.assertEqual((code, self.runs), (1, []))
        self.assertIn("is other's code", err)

    def test_a_bad_port_names_the_field(self):
        (self.root / 'ui-check.yaml').write_text('port: 80a\n')
        code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("port: '80a' is not a port number", err)

    def test_a_second_run_for_the_same_app_waits_for_the_first(self):
        # runs share the cache directory, image tag, network and container names: a second one must not clear them
        (self.root.parent / 'cache/ui-check').mkdir(parents=True)
        with open(self.root.parent / 'cache/ui-check/demo.lock', 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)  # the first run, in another worktree
            code, out, err = self.check()
            self.assertEqual((code, out, self.calls, self.runs), (1, '', [], []))  # nothing built, removed or run
            self.assertIn('another `magehand check --ui` for demo is running', err)
        self.assertEqual(self.check()[0], 0)  # once it has finished

    def test_a_last_run_that_cannot_be_cleared_says_what_to_remove(self):
        with mock.patch.object(magehand.shutil, 'rmtree', mock.Mock(side_effect=PermissionError('denied'))):
            code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn('cannot clear the last run', err)

    def test_it_is_the_browser_check_only(self):
        code, _, err = self.check('--manifests-only')
        self.assertEqual(code, 2)
        self.assertIn('--ui runs the browser check only', err)

    def test_an_env_name_that_is_not_a_variable_is_refused(self):
        with self.assertRaises(SystemExit):
            magehand.ui_env({'env': {'A B': 'x'}})

    def test_a_workflow_without_the_step_says_to_tell_the_owner(self):
        with mock.patch.object(magehand, 'github_file', lambda *a, **k: 'jobs: {}\n'), \
                self.assertRaises(SystemExit) as stop:
            magehand.ui_pins()
        self.assertIn('tell the owner', str(stop.exception.code))


def yaml_load(text):
    import yaml as pyyaml
    return pyyaml.safe_load(text)


ManagedCopy.run_main = CommandLine.run_main
