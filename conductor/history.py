"""Evidence-backed selection of historical runs for explicit migration."""
from __future__ import annotations

import sqlite3
from contextlib import closing

from .backends import DockerBackend, KubernetesBackend, WORKER_LABEL, RUN_LABEL, command
from .storage import IDENTIFIER



def _read_db(path):
    connection = sqlite3.connect(path.resolve(strict=True).as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def migration_workers(config, store, *, include_imports=False, backend=None):
    """Read fleet/coverage identities and current runtime inventory under history_guard."""
    backend = backend or (DockerBackend(config) if config.backend == 'docker' else KubernetesBackend(config))
    runtimes = backend.list_workers()
    active = set()
    if isinstance(backend, DockerBackend):
        # Check all owned worker containers on this host, including other fleets.
        labels = command(['docker', 'ps', '--filter', f'label={WORKER_LABEL}=true',
                          '--format', '{{.Label "' + RUN_LABEL + '"}}']).splitlines()
        for run_id in labels:
            if not IDENTIFIER.fullmatch(run_id):
                raise ValueError('Active runtime has no reliable run identity')
            active.add(run_id)
    for runtime in runtimes:
        if runtime.get('state') not in {'completed', 'failed'}:
            run_id = runtime.get('runId')
            if not isinstance(run_id, str) or not IDENTIFIER.fullmatch(run_id):
                raise ValueError('Active runtime has no reliable run identity')
            active.add(run_id)
    with closing(_read_db(config.state_dir / 'fleet.sqlite3')) as db:
        fleet = list(db.execute('SELECT run_id,state,ingested FROM workers'))
    known = {row['run_id'] for row in fleet}
    workers = [{'runId': row['run_id'], 'state': row['state'],
                'coverageIngested': bool(row['ingested'])}
               for row in fleet if row['run_id'] not in active]
    if not include_imports: return workers
    with closing(_read_db(config.state_dir / 'coverage.sqlite3')) as db:
        coverage = {row['run_id']: row['manifest_sha'] for row in db.execute('SELECT run_id,manifest_sha FROM runs')}
    for row in store.snapshot()['runs']:
        run_id = row['run_id']
        if (row['state'] == 'delivered' and run_id not in known and run_id not in active
                and coverage.get(run_id) == row['manifest_sha']):
            workers.append({'runId': run_id, 'state': 'managed_import', 'coverageIngested': True,
                            'provenance': 'managed_import'})
    return workers
