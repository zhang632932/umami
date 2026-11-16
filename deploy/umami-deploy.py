#!/usr/bin/env python3
"""Deploy the public Umami fork, retaining a recoverable database transaction."""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = Path('/www/wwwroot/umami')
SOURCE = ROOT / 'source'
STATE = ROOT / 'deploy-state'
COMPOSE = ROOT / 'docker-compose.yml'
OVERRIDE = ROOT / 'docker-compose.override.yml'
BACKUPS = Path('/www/backup/umami')
ORIGIN = 'https://github.com/zhang632932/umami.git'
APP, DB = 'umami-umami-1', 'umami-db-1'
ALLOWED_MAJOR = 3
PORT = 3100


class DeployError(Exception):
    pass


def log(message):
    print(f'{dt.datetime.now(dt.timezone.utc).isoformat()} {message}', flush=True)


def run(args, *, cwd=None, stdin=None, stdout=None, timeout=120, label=None, output_log=None):
    """Capture all child output; commands and credentials never enter the journal."""
    log_handle = output_log.open('wb') if output_log else None
    if log_handle:
        os.chmod(output_log, 0o600)
    try:
        result = subprocess.run(args, cwd=cwd, stdin=stdin,
                                stdout=log_handle or stdout or subprocess.PIPE,
                                stderr=subprocess.STDOUT if log_handle else subprocess.PIPE,
                                timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise DeployError(f'{label or args[0]} timed out') from exc
    except OSError as exc:
        raise DeployError(f'{label or args[0]} could not execute') from exc
    finally:
        if log_handle:
            log_handle.close()
    if result.returncode:
        if not output_log:
            path = STATE / 'last-command.log'
            with path.open('wb') as handle:
                os.chmod(path, 0o600)
                handle.write((result.stdout or b'') + b'\n' + (result.stderr or b''))
        log(f'Private diagnostic output: {output_log or STATE / "last-command.log"}')
        raise DeployError(f'{label or args[0]} failed (exit {result.returncode})')
    return (result.stdout or b'').decode().strip() if stdout is None else ''


def atomic_json(path, value):
    temp = path.with_suffix('.tmp')
    with temp.open('w') as handle:
        os.chmod(temp, 0o600)
        json.dump(value, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    sync_directory(path.parent)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else None


def compose(*args):
    files = ['docker', 'compose', '-p', 'umami', '-f', str(COMPOSE)]
    if OVERRIDE.exists():
        files += ['-f', str(OVERRIDE)]
    return run(files + list(args), cwd=ROOT, timeout=180, label='Compose application change')


def set_image(image):
    if not re.fullmatch(r'[a-z0-9][a-z0-9./:_-]*', image):
        raise DeployError('Invalid application image reference')
    temp = OVERRIDE.with_suffix('.tmp')
    temp.write_text(f'services:\n  umami:\n    image: {image}\n    pull_policy: never\n')
    os.chmod(temp, 0o600)
    with temp.open('rb') as handle:
        os.fsync(handle.fileno())
    os.replace(temp, OVERRIDE)
    sync_directory(ROOT)


def inspect(name):
    return json.loads(run(['docker', 'inspect', name], label='Inspect container'))[0]


def database_identity():
    env = dict(item.split('=', 1) for item in inspect(DB)['Config']['Env'] if '=' in item)
    user = env.get('POSTGRES_USER', 'postgres')
    return {'user': user, 'name': env.get('POSTGRES_DB', user)}


def psql(identity, sql, maintenance=False):
    return run(['docker', 'exec', DB, 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-t', '-A',
                '-U', identity['user'], '-d', 'postgres' if maintenance else identity['name'],
                '-c', sql], label='Database verification')


def database_status(identity):
    return json.loads(psql(identity, '''SELECT json_build_object(
        'users', (SELECT count(*) FROM "user"),
        'websites', (SELECT count(*) FROM website),
        'migrations', (SELECT count(*) FROM "_prisma_migrations" WHERE finished_at IS NOT NULL),
        'unfinished', (SELECT count(*) FROM "_prisma_migrations"
                      WHERE finished_at IS NULL AND rolled_back_at IS NULL));'''))


def check_health(identity, previous, timeout=150, container=APP, port=PORT, compare_counts=True):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = inspect(container)['State']
            if not state.get('Running') or state.get('Restarting'):
                raise DeployError('Application is not running')
            for path in ('/api/heartbeat', '/login', '/script.js'):
                req = urllib.request.Request(f'http://127.0.0.1:{port}{path}',
                                             headers={'Cache-Control': 'no-cache'})
                with urllib.request.urlopen(req, timeout=5) as response:
                    body = response.read()
                    if response.status != 200 or not body:
                        raise DeployError('HTTP health check failed')
                    if path == '/api/heartbeat' and json.loads(body).get('ok') is not True:
                        raise DeployError('Heartbeat failed')
                    if path == '/script.js' and 'javascript' not in response.headers.get('Content-Type', ''):
                        raise DeployError('Tracking script health check failed')
            current = database_status(identity)
            if current['unfinished']:
                raise DeployError('Database has unfinished migrations')
            if compare_counts and any(current[key] != previous[key] for key in ('users', 'websites')):
                raise DeployError('Database record counts changed')
            if current['migrations'] < previous['migrations']:
                raise DeployError('Database migration count decreased')
            return current
        except (DeployError, OSError, ValueError):
            time.sleep(3)
    raise DeployError('Application/database health checks timed out')


def mark(transaction, phase):
    transaction['phase'] = phase
    atomic_json(STATE / 'transaction.json', transaction)


def finish(transaction, counts):
    atomic_json(STATE / 'success.json', {
        'revision': transaction['revision'], 'image': transaction['new_image'],
        'version': transaction['version'], 'backup': transaction['backup'],
        'counts': counts, 'completed_at': dt.datetime.now(dt.timezone.utc).isoformat(),
    })
    (STATE / 'transaction.json').unlink()
    sync_directory(STATE)
    (STATE / 'failure.json').unlink(missing_ok=True)
    log(f'Deployment healthy: {transaction["revision"][:12]} (v{transaction["version"]})')


def cleanup_candidate(transaction):
    name = transaction.get('candidate_name')
    if name:
        found = run(['docker', 'container', 'ls', '-a', '--filter', f'name=^/{name}$',
                     '--format', '{{.Names}}'], label='Find isolated candidate')
        if found == name:
            try:
                run(['docker', 'logs', name], output_log=Path(transaction['backup']) / 'candidate.log',
                    label='Save isolated candidate diagnostic log')
            except DeployError:
                log('Candidate diagnostic log could not be fully collected')
            run(['docker', 'rm', '-f', name], label='Remove isolated candidate')
    (Path(transaction['backup']) / 'candidate.env').unlink(missing_ok=True)


def publish(transaction):
    # Once this marker is durable, public traffic may reach the new schema.
    # Recovery must roll forward; restoring the backup could discard new events.
    mark(transaction, 'publishing')
    cleanup_candidate(transaction)
    set_image(transaction['new_image'])
    compose('up', '-d', '--no-deps', 'umami')
    counts = check_health(transaction['database'], transaction['counts'], compare_counts=False)
    mark(transaction, 'verified')
    finish(transaction, counts)


def recover(transaction):
    """A durable phase is written before anything can touch the database schema."""
    log(f'Recovering interrupted/failed deployment ({transaction["phase"]})')
    if transaction['phase'] in ('publishing', 'verified'):
        log('Public cutover started; preserving the current database and retrying the new application')
        publish(transaction)
        return
    run(['docker', 'image', 'inspect', transaction['old_image']], label='Verify rollback image')
    already_restored = transaction['phase'] == 'database_restored'
    if not already_restored:
        compose('stop', 'umami')
    cleanup_candidate(transaction)
    if transaction['phase'] in ('candidate_starting', 'candidate_verifying', 'candidate_verified', 'restoring'):
        mark(transaction, 'restoring')
        escaped = transaction['database']['name'].replace("'", "''")
        psql(transaction['database'], 'SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
             f"WHERE datname = '{escaped}' AND pid <> pg_backend_pid();", maintenance=True)
        with (Path(transaction['backup']) / 'database.dump').open('rb') as handle:
            run(['docker', 'exec', '-i', DB, 'pg_restore', '-U', transaction['database']['user'],
                 '--create', '--clean', '--if-exists', '--exit-on-error', '-d', 'postgres'],
                stdin=handle, timeout=1800, label='Restore database backup')
    if not already_restored and transaction.get('counts'):
        restored = database_status(transaction['database'])
        if any(restored[key] != transaction['counts'][key] for key in ('users', 'websites')):
            raise DeployError('Restored database record counts differ from the backup baseline')
    # Persist this BEFORE the old application can accept public events again.
    mark(transaction, 'database_restored')
    set_image(transaction['old_image'])
    compose('up', '-d', '--no-deps', 'umami')
    counts = transaction.get('counts')
    if counts is None:
        counts = database_status(transaction['database'])
    check_health(transaction['database'], counts, compare_counts=False)
    # Persist the failed revision before removing the recovery marker.
    atomic_json(STATE / 'failure.json', {'revision': transaction['revision'],
                'reason': 'Deployment failed; old application/database restored',
                'backup': transaction['backup']})
    (STATE / 'transaction.json').unlink()
    sync_directory(STATE)
    log('Previous application and database are healthy; failed revision is blocked')


def source_revision():
    if not (SOURCE / '.git').exists():
        if SOURCE.exists() and any(SOURCE.iterdir()):
            raise DeployError('Source directory is nonempty and is not a Git checkout')
        run(['git', 'clone', '--branch', 'master', '--single-branch', ORIGIN, str(SOURCE)],
            timeout=600, label='Clone public repository')
    origin = run(['git', 'remote', 'get-url', 'origin'], cwd=SOURCE, label='Read Git origin')
    if origin != ORIGIN:
        raise DeployError('Source origin differs from the configured public repository')
    if run(['git', 'status', '--porcelain'], cwd=SOURCE, label='Check source changes'):
        raise DeployError('Source checkout has local changes; update stopped')
    run(['git', 'fetch', 'origin', 'master'], cwd=SOURCE, timeout=180, label='Fetch repository')
    run(['git', 'merge', '--ff-only', 'FETCH_HEAD'], cwd=SOURCE, label='Fast-forward source')
    revision = run(['git', 'rev-parse', 'HEAD'], cwd=SOURCE, label='Read revision')
    fetched = run(['git', 'rev-parse', 'FETCH_HEAD'], cwd=SOURCE, label='Read fetched revision')
    if revision != fetched:
        raise DeployError('Local source is ahead of the public repository; update stopped')
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise DeployError('Invalid Git revision')
    version = json.loads((SOURCE / 'package.json').read_text())['version']
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', version)
    if not match or int(match[1]) != ALLOWED_MAJOR:
        raise DeployError(f'Only stable major {ALLOWED_MAJOR} is allowed')
    return revision, version


def deploy(revision, version):
    image = f'umami-local:{revision}'
    try:
        existing = json.loads(run(['docker', 'image', 'inspect', image],
                                  label='Check existing build'))[0]
        label = (existing.get('Config', {}).get('Labels') or {}).get('org.opencontainers.image.revision')
        if image not in (existing.get('RepoTags') or []) or (label and label != revision):
            raise DeployError('Existing build does not match the requested revision')
    except DeployError:
        log(f'Building revision {revision[:12]} before stopping the application')
        run(['docker', 'build', '--label', f'org.opencontainers.image.revision={revision}',
             '-t', image, '.'], cwd=SOURCE, timeout=2400,
            label='Build application image', output_log=STATE / f'build-{revision}.log')
    identity = database_identity()
    if database_status(identity)['unfinished']:
        raise DeployError('Existing database has unfinished migrations')
    old_id = inspect(APP)['Image']
    networks = set(inspect(APP)['NetworkSettings']['Networks']) & set(inspect(DB)['NetworkSettings']['Networks'])
    if not networks:
        raise DeployError('Application and database have no shared Docker network')
    environment = json.loads(compose('config', '--format', 'json'))['services']['umami'].get('environment', {})
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    backup = BACKUPS / stamp
    backup.mkdir(mode=0o700, parents=True)
    for path in (COMPOSE, OVERRIDE, ROOT / '.env'):
        if path.exists():
            target = backup / path.name
            shutil.copy2(path, target)
            os.chmod(target, 0o600)
            with target.open('rb') as handle:
                os.fsync(handle.fileno())
    old_tag = f'umami-rollback:{stamp.lower()}'
    run(['docker', 'tag', old_id, old_tag], label='Retain previous application image')
    transaction = {'revision': revision, 'version': version, 'new_image': image,
                   'old_image': old_tag, 'old_image_id': old_id, 'backup': str(backup),
                   'database': identity, 'candidate_name': f'umami-candidate-{stamp.lower()}',
                   'network': sorted(networks)[0]}
    with (backup / 'candidate.env').open('w') as handle:
        os.chmod(handle.name, 0o600)
        for key, value in environment.items():
            if value is None:
                continue
            if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', key) or any(c in str(value) for c in '\n\r\0'):
                raise DeployError('Unsupported multiline or invalid container environment')
            handle.write(f'{key}={value}\n')
        handle.flush()
        os.fsync(handle.fileno())
    atomic_json(backup / 'deployment.json', transaction)
    mark(transaction, 'prepared')
    compose('stop', 'umami')
    transaction['counts'] = database_status(transaction['database'])
    if transaction['counts']['unfinished']:
        raise DeployError('Existing database has unfinished migrations')
    mark(transaction, 'backing_up')
    with (backup / 'database.dump').open('wb') as handle:
        os.chmod(handle.name, 0o600)
        run(['docker', 'exec', DB, 'pg_dump', '-U', transaction['database']['user'],
             '-d', transaction['database']['name'], '--format=custom'], stdout=handle,
            timeout=1800, label='Back up database')
        handle.flush()
        os.fsync(handle.fileno())
    with (backup / 'database.dump').open('rb') as handle:
        run(['docker', 'exec', '-i', DB, 'pg_restore', '--list'], stdin=handle,
            label='Validate database backup')
    sync_directory(backup)
    mark(transaction, 'candidate_starting')
    run(['docker', 'run', '-d', '--name', transaction['candidate_name'],
         '--network', transaction['network'], '--env-file', str(backup / 'candidate.env'),
         '--publish', '127.0.0.1::3000', image], label='Start isolated candidate')
    binding = run(['docker', 'port', transaction['candidate_name'], '3000/tcp'],
                  label='Read candidate loopback port')
    match = re.fullmatch(r'127\.0\.0\.1:(\d+)', binding)
    if not match:
        raise DeployError('Candidate must bind only to a random loopback port')
    transaction['candidate_port'] = int(match[1])
    mark(transaction, 'candidate_verifying')
    check_health(identity, transaction['counts'], container=transaction['candidate_name'],
                 port=transaction['candidate_port'])
    mark(transaction, 'candidate_verified')
    cleanup_candidate(transaction)
    publish(transaction)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--retry', action='store_true', help='Retry the blocked revision')
    parser.add_argument('--recover-only', action='store_true', help='Recover an interrupted transaction')
    args = parser.parse_args()
    os.umask(0o077)
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    os.chmod(BACKUPS, 0o700)
    with (STATE / 'deploy.lock').open('w') as lock:
        return locked_main(args, lock)


def locked_main(args, lock):
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log('Another deployment is running')
        return 0
    transaction = read_json(STATE / 'transaction.json')
    if transaction:
        recover(transaction)
    if args.recover_only:
        return 0
    revision = None
    try:
        revision, version = source_revision()
        success = read_json(STATE / 'success.json')
        if success and success['revision'] == revision:
            return 0
        failure = read_json(STATE / 'failure.json')
        if failure and failure['revision'] == revision and not args.retry:
            return 0
        deploy(revision, version)
        return 0
    except Exception as exc:
        # Never print arbitrary exception strings: upstream tooling may include credentials.
        log(str(exc) if isinstance(exc, DeployError) else f'Deployment failed ({type(exc).__name__})')
        transaction = read_json(STATE / 'transaction.json')
        if transaction:
            recover(transaction)
        elif revision:
            atomic_json(STATE / 'failure.json', {'revision': revision, 'reason': 'Build or preflight failed'})
        return 1


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise DeployError('Deployment interrupted; recovery required')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        sys.exit(main())
    except Exception as exc:
        log(str(exc) if isinstance(exc, DeployError) else f'Recovery failed ({type(exc).__name__})')
        log('Transaction marker retained; run --recover-only before changing the application')
        sys.exit(1)
