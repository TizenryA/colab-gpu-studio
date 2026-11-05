"""Batch and lease HTTP boundary; admission stays injected by application assembly."""
import os

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from .common import APIError, current_user, get_store, ok, own
from .gpu.manifest import digest
from .gpu.repository import Repository
from .gpu.scheduler import Scheduler


def build_batch_router(JobInput, admit_job, public_job):
    router=APIRouter()
    class BatchInput(BaseModel):
        model_config = ConfigDict(extra="forbid")
        jobs: list[JobInput] = Field(min_length=1, max_length=100)
        mode: str = "save"
        idle_seconds: int = Field(default=120, ge=0, le=300)


    @router.post("/batches", status_code=202)
    def create_batch(body: BatchInput, request: Request, user=Depends(current_user), st=Depends(get_store)):
        key = request.headers.get("Idempotency-Key", "")
        if not key or len(key) > 200:
            raise APIError("IDEMPOTENCY_REQUIRED", "批次必须提供 Idempotency-Key")
        if body.mode not in {"save", "smart", "batch"}:
            raise APIError("POLICY_INVALID", "未知运行策略")
        providers = {job.execution_policy.provider for job in body.jobs}
        if not providers <= {'mock', 'colab'}:
            raise APIError("PROVIDER_INVALID", "不支持该 Provider")
        if 'colab' in providers:
            from .gpu.compute import GpuConfig
            if os.getenv("STUDIO_OFFLINE_TEST") == "1" or not GpuConfig.from_env().real_enabled:
                raise APIError("REAL_GPU_DISABLED", "真实 GPU 未启用，不能提交真实批次", 403)
            if len(providers) > 1 or body.mode != "batch":
                raise APIError("POLICY_INVALID", "真实 GPU 批次只支持「批次」策略，且同批必须全是真实任务", 422)
        repo = Repository(st)
        raw_request=body.model_dump()
        prior=next((batch for batch in repo.list('batches',user['id']) if batch.get('request_key')==key),None)
        if prior:
            if prior.get('request_hash')!=digest(raw_request):
                raise APIError('IDEMPOTENCY_CONFLICT','相同幂等键不能用于不同批次参数',409)
            return ok(prior,request,202)
        validated = [admit_job(st,user,j.model_dump(),f'validate:{key}:{i}',validate_only=True)
                     for i,j in enumerate(body.jobs)]
        with st.admission_lock() as acquired:
            if not acquired:
                raise APIError("SCHEDULER_BUSY", "请沿用幂等键重试", 503, True)
            try:
                batch, _ = repo.create_batch(user['id'],validated,key,
                                             {'mode':body.mode,'idle_seconds':body.idle_seconds},request_body=raw_request)
            except ValueError as exc:
                raise APIError("IDEMPOTENCY_CONFLICT", str(exc), 409) from exc
        return ok(batch, request, 202)


    @router.get("/batches/{batch_id}")
    def batch_status(batch_id: str, request: Request, user=Depends(current_user), st=Depends(get_store)):
        repo=Repository(st)
        batch=own(repo,'batches',batch_id,user)
        jobs=[j for j in repo.list('jobs',user['id']) if j.get('batch_id')==batch_id]
        counts={state:sum(j['state']==state for j in jobs) for state in ('completed','failed','cancelled')}
        finished=sum(counts.values())
        if jobs and finished==len(jobs):
            state='completed' if counts['completed']==len(jobs) else 'cancelled' if counts['cancelled']==len(jobs) else 'failed' if counts['failed']==len(jobs) else 'partial'
        else:
            state='running' if any(j['state']!='queued' for j in jobs) else 'queued'
        return ok({**batch,'state':state,'counts':counts,'finished_jobs':finished,
                   'jobs':[public_job(j) for j in sorted(jobs,key=lambda j:j.get('queue_order',0))]}, request)


    @router.post("/batches/{batch_id}/cancel")
    def batch_cancel(batch_id: str, request: Request, user=Depends(current_user), st=Depends(get_store)):
        repo=Repository(st)
        own(repo,'batches',batch_id,user)
        scheduler=Scheduler(st)
        for job in repo.list('jobs',user['id']):
            if job.get('batch_id')==batch_id:
                scheduler.cancel(user['id'],job['id'])
        return batch_status(batch_id,request,user,st)


    @router.get("/leases")
    def list_leases(request: Request, user=Depends(current_user), st=Depends(get_store)):
        allowed={'id','state','provider','current_job_id','batch_id','last_active_at','deadline_epoch','delete_attempts'}
        return ok([{k:v for k,v in row.items() if k in allowed}
                   for row in Repository(st).list('leases',user['id'])],request)


    return router
