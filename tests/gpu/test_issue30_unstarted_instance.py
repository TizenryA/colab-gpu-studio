"""#30：运行时还没创建任务就失败时，实例不能一直停在「释放中」。"""
import sqlite3

from app.db import Store
from app.gpu.compute import MockColabProvider, MockGPUExecutor
from app.gpu.repository import Repository
from app.gpu.scheduler import Scheduler


def test_bind_failure_before_runtime_closes_instance(tmp_path):
    repo = Repository(Store('sqlite:///' + str(tmp_path / 'flow.db')))
    provider, executor = MockColabProvider(), MockGPUExecutor()
    scheduler = Scheduler(repo, provider_factory=lambda _: provider, executor_factory=lambda _: executor, clock=lambda: 1000.0)

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError('database is locked')

    scheduler.instances.bind_lease = locked
    job = repo.create_job('owner-1', {
        'provider': 'mock', 'project_id': 'project-1',
        'snapshot': {'manifest': {'mock': True}},
        'execution_policy': {'provider': 'mock', 'lifecycle': 'save'}, 'estimated_units': 0,
    }, 'k')[0]
    for _ in range(20):
        scheduler.tick()
        job = repo.get('jobs', job['id'], 'owner-1')
        if job['state'] in {'completed', 'failed', 'cancelled'}:
            break
    assert job['state'] == 'failed', job
    assert job['error_code'] == 'gpu_instance_conflict'
    assert repo.get('instances', job['instance_id'], 'owner-1')['state'] == 'deleted'


def test_file_sqlite_uses_wal(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'wal.db'))
    with store.engine.connect() as conn:
        assert conn.exec_driver_sql('PRAGMA journal_mode').scalar() == 'wal'
