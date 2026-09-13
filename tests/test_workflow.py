"""Offline CI contracts; synthetic mutations and local Git only, no requests."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from deployment import run_ci

ROOT = Path(__file__).resolve().parents[1]


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def fixture(root):
    # Only saved structured data is used; no raw source/evidence dependency.
    value = json.loads((ROOT / 'data/notices.json').read_text(encoding='utf-8'))
    value = copy.deepcopy(value)
    dump(root / 'data/notices.json', value)
    dump(root / 'data/refresh_state.json', {'inbox': {}, 'nextPage': 3})
    return value


def fake_build(root):
    (root / 'dist/data').mkdir(parents=True, exist_ok=True)
    data = run_ci.read(root / 'data/notices.json')
    (root / 'dist/index.html').write_text(data.get('refresh', {}).get('status', 'initial'), encoding='utf-8')
    dump(root / 'dist/data/notices.json', data)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.before = fixture(self.root)

    def prepare(self, collector, collect=True, sanitizer=lambda root: None):
        return run_ci.prepare(self.root, collect, collector, fake_build, sanitizer)

    def test_disabled_makes_no_collection_and_retains_last_attempt(self):
        def forbidden(**kwargs):
            raise AssertionError('collection must be gated')
        result = self.prepare(forbidden, collect=False)
        self.assertEqual(result['collection_status'], 'skipped')
        self.assertEqual(run_ci.read(self.root / 'data/notices.json'), self.before)
        self.assertEqual(result['build_ready'], 'true')

    def test_fatal_preserves_records_and_marks_failed_for_publication(self):
        def broken(**kwargs):
            altered = copy.deepcopy(self.before)
            altered['notices'] = []
            dump(self.root / 'data/notices.json', altered)
            raise UnicodeError('synthetic decoding failure')
        with patch('traceback.print_exc'):
            result = self.prepare(broken)
        data = run_ci.read(self.root / 'data/notices.json')
        self.assertEqual(data['notices'], self.before['notices'])
        self.assertEqual(data['asOfDate'], self.before['asOfDate'])
        self.assertEqual(data['refresh']['lastSuccessAt'], self.before['refresh']['lastSuccessAt'])
        self.assertEqual(result['collection_status'], 'failed')
        self.assertEqual((self.root / 'dist/index.html').read_text(encoding='utf-8'), 'failed')
        self.assertTrue((self.root / 'data/refresh_error.json').exists())

    def test_fatal_does_not_erase_an_access_stop(self):
        blocked = {'httpStatus': 403, 'receivedAt': '2026-09-14T00:00:00+00:00'}
        def broken(**kwargs):
            dump(self.root / 'data/refresh_state.json', {'originBlocked': blocked})
            raise OSError('synthetic late write error')
        with patch('traceback.print_exc'):
            self.prepare(broken)
        self.assertEqual(run_ci.read(self.root / 'data/refresh_state.json')['originBlocked'], blocked)

    def test_partial_success_is_buildable_and_not_reported_success(self):
        def partial(**kwargs):
            data = copy.deepcopy(self.before)
            data['refresh']['status'] = 'partial_success'
            dump(self.root / 'data/notices.json', data)
            return {'status': 'partial_success'}
        result = self.prepare(partial)
        self.assertEqual(result, {'collection_status': 'partial_success', 'build_ready': 'true'})
        self.assertEqual((self.root / 'dist/index.html').read_text(encoding='utf-8'), 'partial_success')

    def test_recovered_run_removes_old_fatal_marker(self):
        dump(self.root / 'data/refresh_error.json', {'status': 'failed'})
        self.prepare(lambda **kwargs: {'status': 'success'})
        self.assertFalse((self.root / 'data/refresh_error.json').exists())

    def test_sanitizer_runs_before_build(self):
        observed = []
        def sanitizer(root):
            observed.append('sanitize')
        def builder(root):
            observed.append('build')
            fake_build(root)
        run_ci.prepare(self.root, False, builder=builder, sanitizer=sanitizer)
        self.assertEqual(observed, ['sanitize', 'build'])

    def test_failed_sanitizer_prevents_build(self):
        def sanitizer(root):
            raise ValueError('unsafe data')
        with self.assertRaisesRegex(ValueError, 'unsafe data'):
            self.prepare(None, collect=False, sanitizer=sanitizer)
        self.assertFalse((self.root / 'dist/index.html').exists())

    def test_outputs_utf8_no_locale_dependency(self):
        path = self.root / 'outputs.txt'
        with patch.dict('os.environ', {'GITHUB_OUTPUT': str(path)}):
            run_ci.emit_outputs({'collection_status': 'failed'})
        self.assertEqual(path.read_text(encoding='utf-8'), 'collection_status=failed\n')


class GitPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.remote = self.folder / 'origin.git'
        self.root = self.folder / 'work'
        subprocess.run(['git', 'init', '--bare', str(self.remote)], check=True, capture_output=True)
        self.root.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.email', 'offline-test@example.invalid')
        self.git('config', 'user.name', 'Offline test')
        dump(self.root / 'data/notices.json', {'notices': []})
        (self.root / 'README.md').write_text('initial\n', encoding='utf-8')
        self.git('add', '.')
        self.git('commit', '-m', 'initial')
        self.git('remote', 'add', 'origin', str(self.remote))
        self.git('push', '-u', 'origin', 'main')

    def git(self, *args):
        return run_ci.git(self.root, *args)

    def test_only_explicit_json_persists_and_duplicate_run_has_no_commit(self):
        dump(self.root / 'data/notices.json', {'notices': ['changed']})
        dump(self.root / 'data/refresh_state.json', {'nextPage': 4})
        dump(self.root / 'evidence/response.json', {'raw': 'not public'})
        (self.root / 'README.md').write_text('unstaged private change\n', encoding='utf-8')
        first = run_ci.persist(self.root, 'main')
        second = run_ci.persist(self.root, 'main')
        self.assertEqual(first['commit'], second['commit'])
        self.assertEqual(second['changed'], 'false')
        names = self.git('show', '--format=', '--name-only', 'HEAD').splitlines()
        self.assertEqual(set(names), {'data/notices.json', 'data/refresh_state.json'})
        self.assertEqual(self.git('show', 'HEAD:README.md'), 'initial')
        self.assertEqual(self.git('ls-files', 'evidence'), '')

    def test_pre_staged_unrelated_file_is_rejected(self):
        (self.root / 'secret.txt').write_text('synthetic only', encoding='utf-8')
        self.git('add', 'secret.txt')
        with self.assertRaisesRegex(RuntimeError, 'pre-staged'):
            run_ci.persist(self.root, 'main')

    def test_deleted_fatal_marker_is_persisted(self):
        dump(self.root / 'data/refresh_error.json', {'status': 'failed'})
        run_ci.persist(self.root, 'main')
        (self.root / 'data/refresh_error.json').unlink()
        result = run_ci.persist(self.root, 'main')
        self.assertEqual(result['changed'], 'true')
        self.assertEqual(self.git('ls-files', 'data/refresh_error.json'), '')

    def test_push_conflict_fails_without_force_or_lost_remote_change(self):
        second = self.folder / 'other'
        subprocess.run(['git', 'clone', '-b', 'main', str(self.remote), str(second)], check=True, capture_output=True)
        (second / 'README.md').write_text('human update\n', encoding='utf-8')
        run_ci.git(second, 'add', 'README.md')
        run_ci.git(second, '-c', 'user.name=Other', '-c', 'user.email=other@example.invalid',
                   'commit', '-m', 'human update')
        run_ci.git(second, 'push', 'origin', 'main')
        remote_before = run_ci.git(second, 'rev-parse', 'HEAD')
        dump(self.root / 'data/notices.json', {'notices': ['unpublished update']})
        with self.assertRaises(subprocess.CalledProcessError):
            run_ci.persist(self.root, 'main')
        remote_after = subprocess.run(['git', '--git-dir', str(self.remote), 'rev-parse', 'refs/heads/main'],
                                      check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(remote_before, remote_after)


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import yaml
        except ImportError:
            raise unittest.SkipTest('PyYAML is an optional offline validation tool, not runtime dependency')
        cls.workflow = yaml.load((ROOT / '.github/workflows/refresh-pages.yml').read_text(encoding='utf-8'), Loader=yaml.BaseLoader)

    def test_manual_and_gated_six_hour_schedule(self):
        w = self.workflow
        self.assertIn('workflow_dispatch', w['on'])
        self.assertEqual(w['on']['schedule'][0]['cron'], '17 */6 * * *')
        self.assertIn("vars.AUTO_REFRESH_ENABLED == 'true'", w['jobs']['collect']['if'])
        self.assertEqual(w['concurrency']['cancel-in-progress'], 'false')

    def test_separate_least_privilege_jobs_and_no_raw_artifact(self):
        collect = self.workflow['jobs']['collect']
        deploy = self.workflow['jobs']['deploy']
        self.assertEqual(collect['permissions'], {'contents': 'write'})
        self.assertEqual(deploy['permissions'], {'pages': 'write', 'id-token': 'write'})
        artifact = next(step for step in collect['steps'] if step.get('uses', '').startswith('actions/upload-pages-artifact'))
        self.assertEqual(artifact['with']['path'], 'dist')
        self.assertIn("steps.persist.outputs.persisted == 'true'", artifact['if'])
        self.assertIn("vars.PAGES_DEPLOY_ENABLED == 'true'", deploy['if'])

    def test_failure_status_publishes_before_overall_run_reports_failure(self):
        report = self.workflow['jobs']['report']
        self.assertEqual(report['needs'], ['collect', 'deploy'])
        run = report['steps'][0]['run']
        self.assertIn('partial_success', run)
        self.assertIn('failed', run)
        self.assertIn('exit 1', run)
        prep = next(s for s in self.workflow['jobs']['collect']['steps'] if s.get('id') == 'prepare')
        self.assertIn('COLLECTION_ENABLED', prep['env'])


if __name__ == '__main__':
    unittest.main()
