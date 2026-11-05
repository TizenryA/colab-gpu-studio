"""Regression: a runtime handle must always follow the lease's current owner job.

Bug: batch handoff moved lease.owner_job_id to the next job while the stored handle still named the
first job, so real Colab refused cleanup ("拒绝操作未登记归属的 runtime"). The mock provider hid it.
"""
import httpx
import pytest
from app.db import Store
from app.gpu.colab import ColabError, GoogleColabProvider
from app.gpu.compute import MockColabProvider, MockGPUExecutor
from app.gpu.repository import Repository
from app.gpu.scheduler import Scheduler

SIG = "sig"


def two_jobs(provider="colab"):
    repo = Repository(Store("sqlite:///:memory:"))
    jobs = [{"project_id": "p", "snapshot": {"manifest": {}}, "execution_policy": {"provider": provider}}
            for _ in range(2)]
    repo.create_batch("u", jobs, "k", {"mode": "batch"})
    first, second = sorted(repo.list("jobs", "u"), key=lambda j: j["queue_order"])
    return repo, first, second


def make_lease(repo, job, provider="colab", **extra):
    handle = {"runtime_name": "runtimes/studio-abc", "runtime_id": "studio-abc", "request_id": "r1",
              "job_id": job["id"], "owner_job_id": job["id"], "owner_id": "u"}
    return repo.create("leases", "u", {
        "owner_job_id": job["id"], "provider": provider, "state": "active", "request_id": "r1",
        "runtime_id": "studio-abc", "deadline_epoch": 10**10, "handle": handle,
        "compatibility_signature": SIG, **extra})


def colab_provider(repo):
    return GoogleColabProvider(repo, "u", client=httpx.Client(), google=object())


def test_handoff_retargets_handle_for_real_colab_ownership_check():
    repo, first, second = two_jobs()
    sig = first["compatibility_signature"]
    lease = make_lease(repo, first)
    repo.update("jobs", first["id"], {"state": "cleaning_up", "lease_id": lease["id"]}, "u")
    assert repo.handoff_lease("u", first["id"], second["id"], lease["id"], 1.0)
    moved = repo.get("leases", lease["id"], "u")
    assert moved["owner_job_id"] == second["id"]
    assert moved["handle"]["job_id"] == moved["handle"]["owner_job_id"] == second["id"]
    assert colab_provider(repo)._owned(moved["handle"])["id"] == lease["id"]
    # a handle that still names the first job is refused: exactly the original failure
    stale = {**moved["handle"], "job_id": first["id"], "owner_job_id": first["id"]}
    with pytest.raises(ColabError, match="未登记归属"):
        colab_provider(repo)._owned(stale)
    assert sig


def test_idle_attach_retargets_handle_for_real_colab_ownership_check():
    repo, first, second = two_jobs()
    sig = first["compatibility_signature"]
    lease = make_lease(repo, first, state="idle", idle_deadline_at=500.0, compatibility_signature=sig)
    repo.update("leases", lease["id"], {"state": "idle"}, "u")
    repo.update("jobs", second["id"], {"state": "waiting_for_capacity"}, "u")
    assert repo.attach_idle_lease("u", second["id"], lease["id"], 1.0)
    attached = repo.get("leases", lease["id"], "u")
    assert attached["owner_job_id"] == second["id"]
    assert attached["handle"]["owner_job_id"] == attached["handle"]["job_id"] == second["id"]
    assert colab_provider(repo)._owned(attached["handle"])["id"] == lease["id"]


class StrictMock(MockColabProvider):
    """Mock that checks handle ownership against the lease owner like the real provider does."""

    def __init__(self, repo, **kw):
        super().__init__(**kw)
        self.repo = repo

    def _verify_handle(self, handle):
        owners = {x["owner_job_id"] for x in self.repo.list("leases", "u") if x.get("runtime_id") == handle.get("runtime_id")}
        if (handle.get("owner_job_id") or handle.get("job_id")) not in owners:
            raise ValueError("拒绝操作未登记归属的 runtime")
        return super()._verify_handle(handle)


def run_batch(count=3):
    repo = Repository(Store("sqlite:///:memory:"))
    jobs = [{"project_id": "p", "snapshot": {"manifest": {"mock": True}}, "execution_policy": {"provider": "mock"}}
            for _ in range(count)]
    repo.create_batch("u", jobs, "k", {"mode": "batch"})
    provider, executor = StrictMock(repo), MockGPUExecutor()
    scheduler = Scheduler(repo, provider_factory=lambda j: provider, executor_factory=lambda j: executor)
    return repo, provider, executor, scheduler


def test_batch_with_strict_ownership_provider_shares_lease_and_cleans_up():
    repo, provider, executor, scheduler = run_batch(3)
    for _ in range(200):
        scheduler.tick()
        if all(j["state"] in {"completed", "failed", "cancelled"} for j in repo.list("jobs", "u")):
            break
    assert [j["state"] for j in repo.list("jobs", "u")] == ["completed"] * 3
    assert provider.create_calls == 1 and provider.delete_calls == 1
    leases = repo.list("leases", "u")
    assert len(leases) == 1 and leases[0]["state"] == "deleted"
    last = max(repo.list("jobs", "u"), key=lambda j: j["queue_order"])
    assert leases[0]["owner_job_id"] == last["id"]
    assert leases[0]["handle"]["owner_job_id"] == last["id"]


def test_scheduler_heals_stale_handle_before_cleanup_and_cancel():
    repo, provider, executor, scheduler = run_batch(2)
    stale_seen = False
    for _ in range(200):
        scheduler.tick()
        lease = next(iter(repo.list("leases", "u")), None)
        jobs = repo.list("jobs", "u")
        if lease and lease.get("handle") and lease["owner_job_id"] != lease["handle"]["owner_job_id"]:
            stale_seen = True
        if lease and lease.get("handle") and lease["state"] == "active":
            first = next(j for j in jobs if j["id"] == lease["owner_job_id"])
            if first["queue_order"] == 1:
                break
    # simulate a legacy / racing writer leaving the first job's id on the handle, then cancel the 2nd job
    lease = repo.list("leases", "u")[0]
    first_id = next(j["id"] for j in repo.list("jobs", "u") if j["queue_order"] == 0)
    repo.update("leases", lease["id"], {"handle": {**lease["handle"], "job_id": first_id, "owner_job_id": first_id}}, "u")
    scheduler.cancel("u", lease["owner_job_id"])
    for _ in range(100):
        scheduler.tick()
        if repo.list("leases", "u")[0]["state"] == "deleted":
            break
    healed = repo.list("leases", "u")[0]
    assert healed["state"] == "deleted" and provider.delete_calls == 1
    assert healed["handle"]["owner_job_id"] == healed["owner_job_id"]
    assert not stale_seen


def test_watchdog_orphan_cleanup_uses_lease_owner_handle():
    repo, provider, executor, scheduler = run_batch(2)
    for _ in range(200):
        scheduler.tick()
        lease = next(iter(repo.list("leases", "u")), None)
        if lease and lease.get("handle") and lease["owner_job_id"] == next(
                j["id"] for j in repo.list("jobs", "u") if j["queue_order"] == 1):
            break
    lease = repo.list("leases", "u")[0]
    owner = lease["owner_job_id"]
    first_id = next(j["id"] for j in repo.list("jobs", "u") if j["queue_order"] == 0)
    repo.update("leases", lease["id"], {"handle": {**lease["handle"], "job_id": first_id, "owner_job_id": first_id}}, "u")
    # owner job becomes terminal (e.g. crashed worker) -> watchdog must still be able to delete the runtime
    repo.update("jobs", owner, {"state": "failed"}, "u")
    scheduler.watchdog()
    assert repo.list("leases", "u")[0]["state"] == "deleted"
    assert provider.delete_calls == 1
