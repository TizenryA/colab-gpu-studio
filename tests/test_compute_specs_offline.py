from __future__ import annotations

from app import main
from app.common import APIError, current_user, get_store
from app.db import Store
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient


def client(tmp_path):
    app = FastAPI()

    async def api_error(_request: Request, exc: APIError):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

    app.add_exception_handler(APIError, api_error)
    app.include_router(main.router, prefix="/api/gpu")
    store = Store(f"sqlite:///{tmp_path / 's.sqlite'}")
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[current_user] = lambda: {"id": "u1", "role": "user"}
    return TestClient(app)


def test_colab_specs_offline_is_clear_409_not_500(monkeypatch, tmp_path):
    monkeypatch.setenv("STUDIO_OFFLINE_TEST", "1")
    r = client(tmp_path).get("/api/gpu/compute/specs?provider=colab")
    assert r.status_code == 409 and r.json()["error"]["code"] == "OFFLINE_MODE"


def test_colab_specs_real_disabled_is_clear_409(monkeypatch, tmp_path):
    monkeypatch.delenv("STUDIO_OFFLINE_TEST", raising=False)
    monkeypatch.setenv("GPU_REAL_ENABLED", "0")
    r = client(tmp_path).get("/api/gpu/compute/specs?provider=colab")
    assert r.status_code == 409 and r.json()["error"]["code"] == "REAL_GPU_DISABLED"


def test_mock_specs_still_work(tmp_path):
    assert client(tmp_path).get("/api/gpu/compute/specs?provider=mock").status_code == 200


def _batch_client(tmp_path):
    from app.batch_routes import build_batch_router
    from app.job_schemas import JobInput
    app = FastAPI()

    async def api_error(_request: Request, exc: APIError):
        return JSONResponse({"error": {"code": exc.code}}, status_code=exc.status)

    app.add_exception_handler(APIError, api_error)
    app.include_router(build_batch_router(JobInput, lambda *a, **k: {}, lambda j: j), prefix="/api/gpu")
    app.dependency_overrides[get_store] = lambda: Store(f"sqlite:///{tmp_path / 'b.sqlite'}")
    app.dependency_overrides[current_user] = lambda: {"id": "u1", "role": "user"}
    return TestClient(app)


def _body(mode):
    job = {"project_id": "p", "model_id": "m", "workflow_key": "w", "workflow_version": 1,
           "execution_policy": {"provider": "colab"}}
    return {"jobs": [job], "mode": mode}


def test_real_batch_blocked_when_offline_or_wrong_mode(monkeypatch, tmp_path):
    c = _batch_client(tmp_path)
    monkeypatch.setenv("STUDIO_OFFLINE_TEST", "1")
    r = c.post("/api/gpu/batches", json=_body("batch"), headers={"Idempotency-Key": "k"})
    assert r.status_code == 403
    monkeypatch.delenv("STUDIO_OFFLINE_TEST")
    monkeypatch.setenv("GPU_REAL_ENABLED", "1")
    r = c.post("/api/gpu/batches", json=_body("smart"), headers={"Idempotency-Key": "k"})
    assert r.status_code == 422
