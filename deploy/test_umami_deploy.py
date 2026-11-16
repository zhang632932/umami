"""Exercise deployment/recovery boundaries without a Docker daemon or production DB."""
import contextlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('umami_deploy', Path(__file__).with_name('umami-deploy.py'))
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)
REVISION = 'a' * 40


class DeploymentLifecycle(unittest.TestCase):
    def setUp(self):
        self.context = contextlib.ExitStack()
        self.addCleanup(self.context.close)
        root = Path(self.context.enter_context(tempfile.TemporaryDirectory()))
        for name, value in {'ROOT': root, 'SOURCE': root / 'source', 'STATE': root / 'state',
                            'BACKUPS': root / 'backups', 'COMPOSE': root / 'docker-compose.yml',
                            'OVERRIDE': root / 'docker-compose.override.yml'}.items():
            self.context.enter_context(patch.object(deploy, name, value))
        deploy.COMPOSE.write_text('services: {}\n')
        (root / '.env').write_text('APP_SECRET=secret-marker\n')
        self.counts = {'users': 2, 'websites': 4, 'migrations': 5, 'unfinished': 0}
        self.original = self.counts.copy()
        self.image = 'old'
        self.candidate_name = None
        self.events = 0
        self.actions = []
        self.cached = True
        self.build_fails = self.backup_fails = self.unhealthy = self.power_loss = False
        self.restore_power_loss = False
        self.publish_power_loss = self.rollback_power_loss = self.public_unhealthy = False
        self.context.enter_context(patch.object(deploy, 'run', side_effect=self.command))
        self.context.enter_context(patch.object(deploy, 'inspect', side_effect=self.inspect))
        self.context.enter_context(patch.object(deploy, 'compose', side_effect=self.compose))
        self.context.enter_context(patch.object(deploy, 'database_identity', return_value={'user': 'umami', 'name': 'umami'}))
        self.context.enter_context(patch.object(deploy, 'database_status', side_effect=lambda _: self.counts.copy()))
        self.context.enter_context(patch.object(deploy, 'psql', return_value=''))
        self.context.enter_context(patch.object(deploy, 'check_health', side_effect=self.health))
        self.context.enter_context(patch.object(deploy, 'source_revision', return_value=(REVISION, '3.5.0')))
        self.context.enter_context(patch('sys.argv', ['umami-deploy.py']))
        self.context.enter_context(patch.object(deploy, 'log'))

    def command(self, args, **kwargs):
        if args[:3] == ['docker', 'image', 'inspect']:
            if not self.cached and args[-1].startswith('umami-local:'):
                raise deploy.DeployError('No cached image')
            return json.dumps([{'RepoTags': [args[-1]], 'Config': {'Labels': {
                'org.opencontainers.image.revision': REVISION}}}])
        if args[:2] == ['docker', 'build']:
            self.actions.append('build')
            if self.build_fails:
                raise deploy.DeployError('Build failed')
            return ''
        if 'pg_dump' in args:
            self.actions.append('backup')
            if self.backup_fails:
                raise deploy.DeployError('Backup failed')
            kwargs['stdout'].write(b'PGDMP simulated complete backup')
        if 'pg_restore' in args and '--list' not in args:
            self.actions.append('restore')
            self.assertEqual(kwargs['stdin'].read(), b'PGDMP simulated complete backup')
            self.assertIn('--create', args)
            self.assertEqual(args[-2:], ['-d', 'postgres'])
            if self.restore_power_loss:
                self.restore_power_loss = False
                self.counts['websites'] = 0
                raise SystemExit('Simulated power loss during database restore')
            self.counts = self.original.copy()
            self.events = 0
        if args[:2] == ['docker', 'run']:
            self.actions.append('candidate')
            self.candidate_name = args[args.index('--name') + 1]
            self.assertEqual(args[args.index('--publish') + 1], '127.0.0.1::3000')
            self.counts['migrations'] = 6
            if self.power_loss:
                self.power_loss = False
                raise SystemExit('Simulated power loss after candidate migrations')
        if args[:2] == ['docker', 'port']:
            return '127.0.0.1:43210'
        if args[:3] == ['docker', 'container', 'ls']:
            return self.candidate_name or ''
        if args[:2] == ['docker', 'rm']:
            self.candidate_name = None
        return ''

    def inspect(self, _):
        return {'Image': 'sha256:old-image', 'State': {'Running': True},
                'NetworkSettings': {'Networks': {'umami_default': {}}}}

    def compose(self, *args):
        self.actions.append(args[0])
        if args[0] == 'config':
            return json.dumps({'services': {'umami': {'environment': {'APP_SECRET': 'secret-marker'}}}})
        if args[0] == 'up':
            self.assertIn('--no-deps', args)
            image = next(line.split(': ', 1)[1] for line in deploy.OVERRIDE.read_text().splitlines()
                         if line.strip().startswith('image:'))
            self.image = 'new' if image.startswith('umami-local:') else 'old'
            if self.image == 'new' and self.publish_power_loss:
                self.publish_power_loss = False
                self.events = 9
                raise SystemExit('Simulated power loss after public cutover')
            if self.image == 'old' and self.rollback_power_loss:
                self.rollback_power_loss = False
                self.events = 7
                raise SystemExit('Simulated power loss after old app resumed')

    def health(self, _, previous, **kwargs):
        candidate = kwargs.get('container', '').startswith('umami-candidate-')
        if candidate and self.unhealthy:
            raise deploy.DeployError('Unhealthy new application')
        if not candidate and self.image == 'new' and self.public_unhealthy:
            raise deploy.DeployError('Public application failed after cutover')
        if kwargs.get('compare_counts', True):
            self.assertEqual(self.counts['users'], previous['users'])
            self.assertEqual(self.counts['websites'], previous['websites'])
        return self.counts.copy()

    def test_success_reuses_exact_image_and_atomic_revision(self):
        self.assertEqual(deploy.main(), 0)
        self.assertNotIn('build', self.actions)
        self.assertLess(self.actions.index('backup'), self.actions.index('up'))
        self.assertEqual(deploy.read_json(deploy.STATE / 'success.json')['revision'], REVISION)
        self.assertFalse((deploy.STATE / 'transaction.json').exists())
        backup = next(deploy.BACKUPS.iterdir())
        self.assertEqual((backup.stat().st_mode & 0o777), 0o700)
        self.assertEqual(((backup / '.env').stat().st_mode & 0o777), 0o600)
        before = self.actions.copy()
        self.assertEqual(deploy.main(), 0)
        self.assertEqual(self.actions, before, 'Unchanged successful revision must do no Docker work')

    def test_build_failure_never_stops_application_and_is_blocked(self):
        self.cached = False
        self.build_fails = True
        self.assertEqual(deploy.main(), 1)
        self.assertEqual(self.actions, ['build'])
        self.assertEqual(self.image, 'old')
        self.assertEqual(deploy.main(), 0)
        self.assertEqual(self.actions, ['build'])

    def test_new_build_completes_before_application_stops(self):
        self.cached = False
        self.assertEqual(deploy.main(), 0)
        self.assertLess(self.actions.index('build'), self.actions.index('stop'))

    def test_failed_backup_restarts_old_app_without_restoring_incomplete_dump(self):
        self.backup_fails = True
        self.assertEqual(deploy.main(), 1)
        self.assertNotIn('restore', self.actions)
        self.assertEqual(self.image, 'old')
        self.assertEqual(self.counts, self.original)
        self.assertIsNone(self.candidate_name)

    def test_failed_health_restores_database_and_old_image(self):
        self.unhealthy = True
        self.assertEqual(deploy.main(), 1)
        self.assertEqual(self.actions.count('restore'), 1)
        self.assertEqual(self.image, 'old')
        self.assertEqual(self.counts, self.original)
        self.assertFalse((deploy.STATE / 'success.json').exists())
        self.assertEqual(deploy.read_json(deploy.STATE / 'failure.json')['revision'], REVISION)

    def test_power_loss_after_migration_recovers_before_next_attempt(self):
        self.power_loss = True
        with self.assertRaises(SystemExit):
            deploy.main()
        self.assertEqual(deploy.read_json(deploy.STATE / 'transaction.json')['phase'], 'candidate_starting')
        self.assertEqual(self.counts['migrations'], 6)
        self.assertEqual(deploy.main(), 0)
        self.assertEqual(self.image, 'old')
        self.assertEqual(self.counts, self.original)
        self.assertEqual(self.actions.count('restore'), 1)

    def test_public_cutover_crash_rolls_forward_without_discarding_new_events(self):
        self.publish_power_loss = True
        with self.assertRaises(SystemExit):
            deploy.main()
        self.assertEqual(deploy.read_json(deploy.STATE / 'transaction.json')['phase'], 'publishing')
        self.counts['websites'] += 1  # Legitimate admin change after the port became public.
        self.assertEqual(deploy.main(), 0)
        self.assertEqual(self.image, 'new')
        self.assertEqual(self.events, 9)
        self.assertNotIn('restore', self.actions)

    def test_public_health_failure_retains_marker_and_never_restores_database(self):
        self.public_unhealthy = True
        with self.assertRaises(deploy.DeployError):
            deploy.main()
        self.assertEqual(deploy.read_json(deploy.STATE / 'transaction.json')['phase'], 'publishing')
        self.assertNotIn('restore', self.actions)
        self.public_unhealthy = False
        self.assertEqual(deploy.main(), 0)

    def test_old_app_resume_crash_does_not_restore_database_twice(self):
        self.unhealthy = True
        self.rollback_power_loss = True
        with self.assertRaises(SystemExit):
            deploy.main()
        self.assertEqual(deploy.read_json(deploy.STATE / 'transaction.json')['phase'], 'database_restored')
        self.assertEqual(deploy.main(), 0)
        self.assertEqual(self.actions.count('restore'), 1)
        self.assertEqual(self.events, 7)

    def test_interrupted_restore_can_repeat_from_durable_backup(self):
        self.unhealthy = True
        self.restore_power_loss = True
        with self.assertRaises(SystemExit):
            deploy.main()
        self.assertEqual(deploy.read_json(deploy.STATE / 'transaction.json')['phase'], 'restoring')
        self.assertEqual(deploy.main(), 0)
        self.assertEqual(self.actions.count('restore'), 2)
        self.assertEqual(self.counts, self.original)
        self.assertEqual(self.image, 'old')

    def test_verified_crash_finishes_commit_without_database_rollback(self):
        with patch.object(deploy, 'finish', side_effect=SystemExit('Power loss before success record')):
            with self.assertRaises(SystemExit):
                deploy.main()
        self.assertEqual(deploy.read_json(deploy.STATE / 'transaction.json')['phase'], 'verified')
        self.assertEqual(deploy.main(), 0)
        self.assertNotIn('restore', self.actions)
        self.assertEqual(deploy.read_json(deploy.STATE / 'success.json')['revision'], REVISION)


class CommandOutput(unittest.TestCase):
    def test_successful_build_output_is_saved_privately_without_decoding_none(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'build.log'
            self.assertEqual(deploy.run([sys.executable, '-c', 'print("build complete")'],
                                        output_log=path), '')
            self.assertEqual(path.read_text().strip(), 'build complete')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_command_error_keeps_output_out_of_exception_and_journal(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(deploy, 'STATE', Path(folder)), \
                patch.object(deploy, 'log') as logger:
            with self.assertRaises(deploy.DeployError) as error:
                deploy.run([sys.executable, '-c', 'import sys; print("secret-marker"); sys.exit(3)'],
                           label='Build')
            self.assertNotIn('secret-marker', str(error.exception))
            self.assertNotIn('secret-marker', str(logger.call_args_list))
            self.assertIn('secret-marker', (Path(folder) / 'last-command.log').read_text())
            self.assertEqual((Path(folder) / 'last-command.log').stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
