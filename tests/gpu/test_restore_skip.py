"""Regression: a second job on the same runtime must not restore weights again.

Bug: after a batch lease handoff the second job re-ran restore_env + restore_weights (~2 min on L4)
because every job got a fresh /content/ai-studio/<execution> tree.
"""
import json
from pathlib import Path

from app.db import Store
from app.gpu import runtime_worker as worker
from app.gpu.compute import MockGPUExecutor
from app.gpu.repository import Repository
from app.gpu.scheduler import Scheduler
from tests.gpu.test_lease_handle_ownership import StrictMock

COMMIT = "a" * 40
SHA = "b" * 64


def weights(size=4, sha=SHA, target="checkpoints/m.safetensors"):
    return [{"target": target, "sha256": sha, "size": size, "source": "drive", "drive_file_id": "1" * 33}]


def make_tree(root: Path, items):
    for item in items:
        path = root / "ComfyUI" / "models" / item["target"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * item["size"])


def test_fingerprint_changes_with_commit_nodes_and_weights():
    base = worker._warm_fingerprint(COMMIT, [], weights())
    assert base == worker._warm_fingerprint(COMMIT, [], weights())
    assert base != worker._warm_fingerprint("c" * 40, [], weights())
    assert base != worker._warm_fingerprint(COMMIT, [], weights(sha="d" * 64))
    assert base != worker._warm_fingerprint(COMMIT, [], weights(size=5))
    assert base != worker._warm_fingerprint(COMMIT, [], weights(target="loras/x.safetensors"))
    node = {"repo": "r", "commit": "c", "dir": "d"}
    assert base != worker._warm_fingerprint(COMMIT, [node], weights())


def test_warm_roundtrip_reuses_same_fingerprint(tmp_path):
    items = weights()
    first, second = tmp_path / "job1", tmp_path / "job2"
    first.mkdir(), second.mkdir()
    make_tree(first, items)
    (first / "ComfyUI" / "input").mkdir()
    (first / "ComfyUI" / "input" / "ref.png").write_bytes(b"old")
    fp = worker._warm_fingerprint(COMMIT, [], items)
    assert not worker._warm_take(second, fp, [], items)  # nothing parked yet
    worker._warm_put(first, fp)
    assert not (first / "ComfyUI").exists()
    assert worker._warm_take(second, fp, [], items)
    assert (second / "ComfyUI" / "models" / items[0]["target"]).is_file()
    assert not (second / "ComfyUI" / "input").exists()  # previous job's reference images are gone
    # while a job holds the tree, the marker is gone: a third job restores normally
    third = tmp_path / "job3"
    third.mkdir()
    assert not worker._warm_take(third, fp, [], items)


def test_warm_not_reused_when_fingerprint_or_files_differ(tmp_path):
    items = weights()
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir(), two.mkdir()
    make_tree(one, items)
    fp = worker._warm_fingerprint(COMMIT, [], items)
    worker._warm_put(one, fp)
    other = weights(sha="d" * 64)
    assert not worker._warm_take(two, worker._warm_fingerprint(COMMIT, [], other), [], other)
    # a weight file that shrank or vanished is not trusted either
    (tmp_path / ".warm" / "ComfyUI" / "models" / items[0]["target"]).write_bytes(b"x")
    assert not worker._warm_take(two, fp, [], items)


def test_warm_requires_reviewed_custom_node_dir(tmp_path):
    items = weights()
    node = {"repo": "r", "commit": "c", "dir": "nodedir"}
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir(), two.mkdir()
    make_tree(one, items)
    fp = worker._warm_fingerprint(COMMIT, [node], items)
    worker._warm_put(one, fp)
    assert not worker._warm_take(two, fp, [node], items)


def test_run_comfy_second_job_skips_restore_and_changed_workflow_restores(tmp_path, monkeypatch):
    calls = {"env": 0, "weights": 0}
    items = weights()

    def fake_env(root, *a, **k):
        calls["env"] += 1
        return {"source": "install"}

    def fake_weights(comfy, manifest, *a, **k):
        calls["weights"] += 1
        make_tree(comfy.parent, manifest["weight_files"])
        return [{"target": w["target"]} for w in manifest["weight_files"]]

    class Done(Exception):
        pass

    monkeypatch.setattr(worker, "_restore_env", fake_env)
    monkeypatch.setattr(worker, "_restore_weights", fake_weights)
    # stop right after restore: Comfy itself is not under test here
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(Done()))

    def manifest(w):
        return {"comfy_commit": COMMIT, "node_allowlist": ["X"], "weight_files": w}

    def run(name, w):
        root = tmp_path / name
        root.mkdir()
        stages = worker._Stages(root)
        try:
            worker._run_comfy(root, {"snapshot": {}}, manifest(w), {}, stages)
        except Done:
            pass
        stages.end()
        return stages.timings

    t1 = run("e1", items)
    assert "restore_weights" in t1 and "warm_reuse" not in t1
    t2 = run("e2", items)  # same workflow on the same runtime
    assert "warm_reuse" in t2 and "restore_weights" not in t2 and "restore_env" not in t2
    assert calls == {"env": 1, "weights": 1}
    t3 = run("e3", weights(sha="d" * 64))  # different weights
    assert "restore_weights" in t3 and "warm_reuse" not in t3
    assert calls == {"env": 2, "weights": 2}
    t4 = run("e4", weights(sha="d" * 64))
    assert "warm_reuse" in t4


def test_warm_marker_is_valid_json_with_fingerprint(tmp_path):
    items = weights()
    make_tree(tmp_path / "j", items)
    fp = worker._warm_fingerprint(COMMIT, [], items)
    worker._warm_put(tmp_path / "j", fp)
    assert json.loads((tmp_path / ".warm" / "state.json").read_text())["fingerprint"] == fp


def run_batch(count):
    repo = Repository(Store("sqlite:///:memory:"))
    jobs = [{"project_id": "p", "snapshot": {"manifest": {"mock": True}}, "execution_policy": {"provider": "mock"}}
            for _ in range(count)]
    repo.create_batch("u", jobs, "k", {"mode": "batch"})
    provider, executor = StrictMock(repo), MockGPUExecutor()
    scheduler = Scheduler(repo, provider_factory=lambda j: provider, executor_factory=lambda j: executor)
    for _ in range(300):
        scheduler.tick()
        if all(j["state"] in {"completed", "failed", "cancelled"} for j in repo.list("jobs", "u")):
            break
    return repo


def test_batch_second_job_skips_restore_and_lease_records_signature():
    repo = run_batch(3)
    jobs = sorted(repo.list("jobs", "u"), key=lambda j: j["queue_order"])
    assert [j["state"] for j in jobs] == ["completed"] * 3
    assert [j["restore_skipped"] for j in jobs] == [False, True, True]
    assert "restore_weights" in jobs[0]["worker_timings"]
    assert "warm_reuse" in jobs[1]["worker_timings"] and "restore_weights" not in jobs[1]["worker_timings"]
    (lease,) = repo.list("leases", "u")
    assert lease["restored_signature"] == jobs[0]["compatibility_signature"]
