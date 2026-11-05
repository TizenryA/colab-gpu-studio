"""Persistent short-tick scheduler for independent Colab GPU Studio API."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .compute import (
    ComputeError,
    DisabledProvider,
    GpuConfig,
    ProviderUnavailable,
    default_executor_factory,
    default_provider_factory,
)
from .google import GoogleError
from .mcp import MCPError
from .prep import PREP_SPEC_KEY, cache_key, create_prep_job, prep_idempotency_key, prep_jobs_for

TERMINAL_STATES = {"completed", "failed", "cancelled"}
ACTIVE_STATES = {
    "queued",
    "validating",
    "waiting_for_capacity",
    "allocating",
    "preparing",
    "loading",
    "running",
    "uploading",
    "cleaning_up",
    "cleanup_pending",
}
MAX_OUTPUT_BYTES = 100 * 1024 * 1024


# Stage markers written by runtime_worker.py into status.json while a job is running.
_STAGE_TEXT = {
    "restore_env": "正在恢复运行环境",
    "restore_weights": "正在恢复模型文件",
    "warm_reuse": "同一台机器已有模型，直接复用",
    "comfy_start": "正在启动 ComfyUI",
    "sampling": "采样中",
    "prep_env": "正在构建环境缓存",
    "prep_weights": "正在补齐模型缓存",
}


_PREP_TEXT = {
    "GPU job validating": "正在校验缓存准备任务",
    "GPU job validated": "缓存准备任务已校验",
    "GPU capacity available": "CPU 运行时可用",
    "GPU runtime allocated": "CPU 运行时已分配",
    "GPU runtime is ready": "CPU 运行时已就绪",
    "GPU execution started": "开始构建缓存",
    "GPU execution completed": "缓存构建完成",
    "GPU outputs persisted": "缓存已保存到 Drive",
    "GPU cleanup completed": "CPU 运行时已关闭（缓存保留在 Drive）",
    "GPU cleanup is pending": "正在关闭 CPU 运行时",
}


class SchedulerError(RuntimeError):
    pass


class Scheduler:
    """Advance one persisted phase per tick; never hold the lock over adapter I/O."""

    def __init__(
        self,
        store,
        provider_factory: Callable | None = None,
        executor_factory: Callable | None = None,
        storage_factory: Callable | None = None,
        config: GpuConfig | None = None,
        clock: Callable[[], float] | None = None,
    ):
        # Base Store owns the database and cross-process lock; GPU records go
        # through the independent Repository (gpu_jobs, gpu_leases, ...).
        if hasattr(store, "Session") and hasattr(store, "engine"):
            from .repository import Repository

            self.db_store = store
            self.store = Repository(store)
        elif hasattr(store, "store") and hasattr(store, "list") and hasattr(store, "create"):
            self.store = store
            self.db_store = store.store
        else:
            self.store = store
            self.db_store = store
        self.config = config or GpuConfig.from_env()
        self.provider_factory = provider_factory or (
            lambda job: default_provider_factory(
                job, self.config, store=self.store, owner_id=job.get("owner_id")
            )
        )
        self.executor_factory = executor_factory or (
            lambda job: default_executor_factory(
                job, self.config, store=self.store, owner_id=job.get("owner_id")
            )
        )
        self.storage_factory = storage_factory
        self.clock = clock or time.time

    def _factory(self, factory: Callable, job: dict):
        """Allow injected adapters to receive both job and the real base Store."""
        import inspect

        try:
            parameters = inspect.signature(factory).parameters.values()
            positional = [p for p in parameters if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
            if any(p.kind == p.VAR_POSITIONAL for p in parameters) or len(positional) >= 2:
                return factory(job, self.db_store)
        except (TypeError, ValueError):
            pass
        return factory(job)

    def _provider(self, job: dict):
        return self._factory(self.provider_factory, job)

    def _executor(self, job: dict):
        return self._factory(self.executor_factory, job)

    # Store/Repository compatibility surface.
    def _list(self, kind: str, owner_id: str | None = None) -> list[dict]:
        return list(self.store.list(kind, owner_id=owner_id))

    def _get(self, kind: str, record_id: str, owner_id: str | None = None) -> dict | None:
        return self.store.get(kind, record_id, owner_id=owner_id)

    def _create(self, kind: str, owner_id: str, data: dict, record_id: str | None = None) -> dict:
        return self.store.create(kind, owner_id, data, record_id=record_id)

    def _update(self, kind: str, record_id: str, patch: dict, owner_id: str | None = None) -> dict:
        return self.store.update(kind, record_id, patch, owner_id=owner_id)

    def _event(self, job: dict, state: str, message: str, progress: float | None = None) -> None:
        event = getattr(self.store, "event", None)
        if event:
            event(job["id"], job.get("owner_id"), state, message, progress)

    def _set_state(self, job: dict, state: str, message: str, **patch) -> dict:
        if state in TERMINAL_STATES:
            # Worker and watchdog can both finish the same cleanup; only the first one records it.
            current = self.store.get("jobs", job["id"], job.get("owner_id")) or job
            if current.get("state") == state:
                return current
        if job.get("prep"):
            message = _PREP_TEXT.get(message, message.replace("GPU", "CPU"))
        transition = getattr(self.store, 'transition_job', None)
        if transition:
            return transition(job['id'],job.get('owner_id'),state,message,patch,self.clock(),expected_state=job.get('state'),expected_execution_id=job.get('execution_id'))
        updated = self._update("jobs", job["id"], {"state": state, **patch}, job.get("owner_id"))
        execution_id = updated.get('execution_id')
        if execution_id and updated.get('batch_id') and state in TERMINAL_STATES | {'running', 'uploading'}:
            execution = self._get('executions', execution_id, updated.get('owner_id'))
            if execution:
                execution_patch = {'state': state}
                if state in TERMINAL_STATES:
                    execution_patch.update(finished_at=self.clock(), error_code=updated.get('error_code'))
                self._update('executions', execution_id, execution_patch, updated.get('owner_id'))
        self._event(updated, state, message, patch.get("progress"))
        return updated

    def _provider_name(self, job: dict) -> str:
        return job.get("provider") or (job.get("execution_policy") or {}).get("provider", "mock")

    def _effective_runtime(self, job: dict) -> int:
        requested = job.get("max_runtime_minutes")
        if requested is None:
            requested = (job.get("execution_policy") or {}).get("max_runtime_minutes")
        requested = self.config.max_runtime_minutes if requested is None else int(requested)
        if requested <= 0:
            raise SchedulerError("max runtime must be positive")
        return min(requested, self.config.max_runtime_minutes)

    def _usage_totals(self, owner_id: str) -> tuple[float, float]:
        now = self.clock()
        daily = monthly = 0.0
        for row in self._list("usage", owner_id):
            units = float(row.get("units") or 0)
            stamp = float(row.get("created_epoch") or row.get("epoch") or 0)
            monthly += units if stamp >= now - 31 * 86400 else 0
            daily += units if stamp >= now - 86400 else 0
        for row in self._list("jobs", owner_id):
            if row.get("budget_reserved") and row.get("state") not in TERMINAL_STATES:
                units = float(row["budget_reserved"])
                daily += units
                monthly += units
        return daily, monthly

    def _reserve_budget(self, job: dict, provider: Any) -> tuple[bool, str | None, dict]:
        # The requested provider remains authoritative even when a test double
        # is injected for a colab job.
        is_mock = self._provider_name(job) == "mock"
        raw_units = job.get("budget_units", job.get("estimated_units"))
        if raw_units is None and self.config.unit_rate is not None:
            raw_units = self._effective_runtime(job) * self.config.unit_rate
        if raw_units is None:
            if is_mock:
                raw_units = 0.0
            else:
                return False, "gpu_budget_unknown", {"error_message": "real GPU task has no unit estimate"}
        try:
            units = float(raw_units)
        except (TypeError, ValueError):
            return False, "gpu_budget_invalid", {"error_message": "estimated units are invalid"}
        if units < 0 or units > self.config.max_job_units:
            return False, "gpu_budget_exceeded", {"error_message": "job unit limit exceeded"}
        if is_mock:
            return True, None, {"simulated_units": units}
        daily, monthly = self._usage_totals(job["owner_id"])
        if daily + units > self.config.daily_units or monthly + units > self.config.monthly_units:
            return False, "gpu_budget_exceeded", {"error_message": "GPU budget exceeded"}
        return True, None, {"budget_reserved": units}

    def _validate(self, job: dict) -> dict:
        if not self.config.enabled:
            return self._set_state(
                job,
                "failed",
                "GPU scheduler is disabled",
                error_code="gpu_disabled",
                error_message="GPU is disabled",
            )
        try:
            runtime = self._effective_runtime(job)
            provider = self._provider(job)
            ok, code, patch = self._reserve_budget(job, provider)
        except Exception as exc:
            return self._set_state(
                job,
                "failed",
                "GPU provider unavailable",
                error_code="gpu_provider_unavailable",
                error_message=self._safe_error(exc),
            )
        if not ok:
            return self._set_state(job, "failed", "GPU budget rejected", error_code=code, **patch)
        patch.update({"max_runtime_minutes": runtime, "validated_epoch": self.clock()})
        return self._set_state(job, "waiting_for_capacity", "GPU job validated", **patch)

    def _leases_for(self, owner_id: str | None = None) -> list[dict]:
        return self._list("leases", owner_id)

    @staticmethod
    def _lease_blocks(lease: dict) -> bool:
        return lease.get("state") in {"creating", "active", "busy", "idle", "cleanup_pending", "quarantined", "releasing"}

    def _has_capacity_blocker(self, job: dict) -> bool:
        return any(self._lease_blocks(x) and x.get("owner_job_id") != job["id"] for x in self._leases_for())

    def _await_prep(self, job: dict) -> bool:
        """A GPU job waits for its workflow's CPU cache prep; the prep job is created on the first miss.

        One failed or cancelled prep stops the waiting (the job then runs uncached); it is never retried here.
        """
        snapshot = job.get("snapshot") or {}
        sha = snapshot.get("workflow_sha256")
        if (
            self._provider_name(job) != "colab"
            or snapshot.get("prep")
            or snapshot.get("system_smoke")
            or not sha
            or not (snapshot.get("manifest") or {}).get("weight_files")
            or os.getenv("GPU_PREP", "on").strip().lower() == "off"
        ):
            return False
        preps = prep_jobs_for(self.store, job["owner_id"], snapshot)
        done = next((p for p in preps if p["state"] == "completed"), None)
        if done:
            if done["id"] == job.get("prep_job_id") and job.get("prep_seconds") is None:
                # The prep ran for this job: show it as this job's first stage.
                spent = round(sum((done.get("worker_timings") or {}).values()), 1)
                self._update("jobs", job["id"], {"prep_seconds": spent}, job.get("owner_id"))
            return False
        active = next((p for p in preps if p["state"] in ACTIVE_STATES), None)
        if active:
            if job.get("prep_job_id") != active["id"]:
                self._update("jobs", job["id"], {"prep_job_id": active["id"]}, job.get("owner_id"))
            return True
        if preps:
            return False
        with self.db_store.dispatch_lock() as acquired:
            if not acquired:
                return True
            meta = {
                "workflow_sha256": sha,
                "model_id": job.get("model_id"),
                "workflow_key": job.get("workflow_key"),
                "workflow_version": job.get("workflow_version"),
            }
            prep = create_prep_job(
                self.store, job["owner_id"], job.get("project_id"), snapshot, meta,
                prep_idempotency_key(cache_key(snapshot)), for_job_id=job["id"],
            )
        self._update("jobs", job["id"], {"prep_job_id": prep["id"]}, job.get("owner_id"))
        self._event(job, "waiting_for_capacity", "准备缓存（CPU）：为这个任务自动准备环境和模型缓存，完成后自动开始", None)
        return True

    def _lease(self, job: dict) -> dict | None:
        if job.get("lease_id"):
            lease=self._get("leases", job["lease_id"], job.get("owner_id"))
            return lease if lease and lease.get('owner_job_id')==job['id'] else None
        return next(
            (x for x in self._leases_for(job.get("owner_id")) if x.get("owner_job_id") == job["id"]
             and not (job.get('attempt_retry_keys') and x.get('state')=='deleted')), None
        )

    def _ensure_lease(self, job: dict) -> dict:
        current = self._lease(job)
        if current:
            return current
        request_id, runtime_id = str(uuid.uuid4()), f"studio-{uuid.uuid4().hex}"
        now = self.clock()
        return self._create(
            "leases",
            job["owner_id"],
            {
                "owner_job_id": job["id"],
                "provider": self._provider_name(job),
                "state": "creating",
                "request_id": request_id,
                "runtime_id": runtime_id,
                "created_epoch": now,
                "last_seen": now,
                "delete_attempts": 0,
                "create_attempts": 0,
                "deadline_epoch": now + self._effective_runtime(job) * 60,
            },
        )

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        """Expose only messages from deliberately safe adapter exception classes."""
        if not isinstance(exc, (ComputeError, GoogleError, MCPError)):
            logging.getLogger(__name__).warning("unexpected GPU error: %s: %s", type(exc).__name__, str(exc)[:300])
            return f"GPU operation failed ({type(exc).__name__})"
        message = str(exc).replace("token", "[redacted]").replace("secret", "[redacted]")
        return message[:1400] or "GPU operation failed"

    @staticmethod
    def _runtime_id(provider: str, runtime_id: str) -> str:
        if provider == "colab":
            return f"runtimes/{runtime_id}"
        return f"mock-runtime-{runtime_id}"

    def _lease_handle(self, lease: dict) -> dict | None:
        """The lease's runtime handle, always owned by the lease's current owner job.

        Lease reuse (handoff / idle attach) moves owner_job_id; providers verify the handle against it,
        so a handle that still names an earlier job is re-owned (and persisted) before any provider call.
        """
        handle = lease.get("handle")
        owner = lease.get("owner_job_id")
        if not isinstance(handle, dict) or not isinstance(owner, str):
            return handle
        if handle.get("job_id") == owner and handle.get("owner_job_id") == owner:
            return handle
        fixed = {**handle, "job_id": owner, "owner_job_id": owner}
        try:
            self._update("leases", lease["id"], {"handle": fixed}, lease.get("owner_id"))
        except Exception:
            logging.getLogger(__name__).warning("could not persist re-owned lease handle")
        return fixed

    def _intent_handle(self, job: dict, lease: dict) -> dict | None:
        """Build only a lease-owned handle; never infer a platform runtime name."""
        runtime_id = lease.get("runtime_id")
        if not (
            isinstance(runtime_id, str)
            and lease.get("owner_job_id") == job.get("id")
            and isinstance(lease.get("request_id"), str)
            and lease.get("request_id")
        ):
            return None
        handle = {
            "runtime_name": self._runtime_id(self._provider_name(job), runtime_id),
            "runtime_id": runtime_id,
            "request_id": lease["request_id"],
            "job_id": job["id"],
            "owner_job_id": job["id"],
            "owner_id": job.get("owner_id"),
            "intent": "gpu-studio",
        }
        if lease.get("operation_name"):
            handle["operation_name"] = lease["operation_name"]
        if self._provider_name(job) == "mock":
            handle["mock"] = True
        return handle

    @staticmethod
    def _value(value: Any, *keys: str) -> Any:
        if isinstance(value, dict):
            for key in keys:
                if key in value:
                    return value[key]
        return None

    def _manifest_runtime(self, job: dict) -> str | None:
        manifest = (job.get("snapshot") or {}).get("manifest")
        value = self._value(
            manifest, "runtime", "runtime_key", "runtime_id", "runtime_version", "colab_runtime"
        )
        if isinstance(value, dict):
            value = self._value(value, "name", "key", "id", "version")
        return value if isinstance(value, str) and value else None

    def _spec_key(self, spec: dict) -> str | None:
        value = self._value(spec, "key", "name", "id", "spec_id")
        if value is None and isinstance(spec.get("runtimeSpec"), dict):
            value = self._value(spec["runtimeSpec"], "key", "name", "id")
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return value if isinstance(value, str) and value else None

    def _spec_runtime(self, spec: dict) -> str | None:
        value = self._value(spec, "runtime", "runtime_key", "runtime_id", "runtime_version", "colab_runtime")
        if isinstance(value, dict):
            value = self._value(value, "name", "key", "id", "version")
        if value is None and isinstance(spec.get("runtimeSpec"), dict):
            value = self._value(spec["runtimeSpec"], "runtime", "runtime_key", "runtime_id", "version")
        return value if isinstance(value, str) and value else None

    def _has_gpu(self, spec: dict) -> bool:
        # Official enum is authoritative; do not infer new GPUs from model names.
        key = spec.get("key")
        if isinstance(key, dict) and "variant" in key:
            return key["variant"] in {"GPU", "VARIANT_GPU"}
        for key in ("gpu", "has_gpu", "accelerator", "accelerator_type", "acceleratorType", "device"):
            value = spec.get(key)
            if value is True:
                return True
            if isinstance(value, str) and "gpu" in value.lower():
                return True
            if isinstance(value, dict) and any(str(v).lower() == "gpu" for v in value.values()):
                return True
        text = json.dumps(spec, ensure_ascii=False).lower()
        return any(token in text for token in ("t4", "l4", "a100", "v100", "p100", "h100"))

    def _known_vram(self, spec: dict) -> bool:
        for key in ("vram_gb", "vram", "memory_gb", "gpu_memory_gb"):
            value = spec.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return True
        for value in spec.values():
            if isinstance(value, dict) and self._known_vram(value):
                return True
        return False

    def _select_spec(self, job: dict, provider: Any) -> dict:
        try:
            specs = provider.list_specs()
        except Exception as exc:
            raise ComputeError(self._safe_error(exc)) from exc
        if not isinstance(specs, list):
            raise ComputeError("GPU runtime规格响应无效")
        requested = job.get("requested_spec") or (job.get("execution_policy") or {}).get("requested_spec")
        requested_key = requested if isinstance(requested, str) else self._spec_key(requested or {})
        manifest_runtime = self._manifest_runtime(job)
        snapshot = job.get("snapshot") or {}
        smoke = snapshot.get("system_smoke") is True
        prep = snapshot.get("prep") is True
        # 官方 runtimespecs 只返回 key(variant/accelerator/shape)，没有 runtime 版本或显存字段。
        # 经审查的工作流在 manifest.compute_spec 里写明已冒烟验证过的规格 key，
        # 普通任务只允许这个规格；管理员真实冒烟任务则按其请求的规格 key 运行。
        reviewed_key = self._spec_key({"key": (snapshot.get("manifest") or {}).get("compute_spec")})
        if prep:  # cache prep never needs a GPU: only the fixed CPU spec is allowed
            requested_key = reviewed_key = self._spec_key({"key": PREP_SPEC_KEY})
        if requested_key is None and reviewed_key is not None:
            requested_key = reviewed_key
        trusted = smoke or snapshot.get("workflow_smoke") is True or reviewed_key is not None
        if smoke and requested_key is None:
            raise ComputeError("system smoke必须绑定已选择的GPU规格")
        eligible = []
        for spec in specs:
            if not isinstance(spec, dict) or spec.get("eligible") is not True or self._has_gpu(spec) == prep:
                continue
            key = self._spec_key(spec)
            if requested_key is not None and key != requested_key:
                continue
            runtime = self._spec_runtime(spec)
            if manifest_runtime is not None and runtime != manifest_runtime:
                continue
            if manifest_runtime is None and not trusted:
                continue
            if not trusted and not self._known_vram(spec) and key not in self.config.spec_allowlist:
                continue
            eligible.append(spec)
        if not eligible:
            raise ComputeError("没有符合工作流runtime的GPU规格")
        return eligible[0]

    def _recover_unknown_create(self, job: dict, lease: dict, provider: Any) -> str | bool:
        """Reconcile a create that may have succeeded before its response was lost."""
        handle = self._intent_handle(job, lease)
        if handle is None:
            return False
        try:
            if getattr(provider,'is_mock',False) and hasattr(provider,'recover_handle'):
                handle=provider.recover_handle(handle)
            status = provider.inspect(handle)
        except Exception as exc:
            self._update(
                "leases",
                lease["id"],
                {"last_error": self._safe_error(exc), "last_seen": self.clock()},
                job.get("owner_id"),
            )
            return False
        if status.get("deleted"):
            self._update("leases", lease["id"], {"last_seen": self.clock()}, job.get("owner_id"))
            return "absent"
        self._update(
            "leases",
            lease["id"],
            {"handle": handle, "state": "cleanup_pending", "last_seen": self.clock()},
            job.get("owner_id"),
        )
        try:
            confirmed = bool(provider.delete(handle))
        except Exception as exc:
            confirmed = False
            error = self._safe_error(exc)
        else:
            error = None
        if confirmed:
            self._update(
                "leases", lease["id"], {"state": "deleted", "last_seen": self.clock()}, job.get("owner_id")
            )
            return "cleaned"
        self._update(
            "leases",
            lease["id"],
            {"last_error": error or "delete not confirmed", "last_seen": self.clock()},
            job.get("owner_id"),
        )
        return False

    def _allocation(self, job: dict) -> dict:
        # Persist intent under the short lock, then release it before provider I/O.
        with self.db_store.dispatch_lock() as acquired:
            if not acquired:
                return job
            lease = self._ensure_lease(job)
        try:
            provider = self._provider(job)
        except Exception as exc:
            return self._begin_failure(job, "gpu_provider_unavailable", self._safe_error(exc))
        if lease.get("handle"):
            return self._set_state(job, "preparing", "GPU runtime allocation recovered")
        try:
            if isinstance(provider, DisabledProvider):
                raise ProviderUnavailable(provider.reason)
            spec = (
                {"provider": self._provider_name(job)}
                if self._provider_name(job) == "mock"
                else self._select_spec(job, provider)
            )
        except ProviderUnavailable as exc:
            return self._begin_failure(job, "gpu_provider_unavailable", self._safe_error(exc))
        except Exception as exc:
            return self._begin_failure(job, "gpu_spec_invalid", self._safe_error(exc))
        attempts = int(lease.get("create_attempts") or 0)
        if attempts > self.config.create_retries:
            return self._begin_failure(job, "gpu_create_failed", "GPU runtime allocation failed")
        attempts += 1
        lease = self._update(
            "leases",
            lease["id"],
            {
                "create_attempts": attempts,
                "create_started": self.clock(),
                "selected_spec": spec,
                "last_seen": self.clock(),
            },
            job.get("owner_id"),
        )
        try:
            handle = provider.create(spec, lease["request_id"], job["id"], lease["runtime_id"])
            if not isinstance(handle, dict) or not handle.get("runtime_name"):
                raise ComputeError("provider returned an unknown runtime handle")
            if getattr(provider,'is_mock',False) and hasattr(provider,'bind_owner'):
                handle=provider.bind_owner(handle,job.get('owner_id'))
            handle = {
                **handle,
                "runtime_id": lease["runtime_id"],
                "request_id": lease["request_id"],
                "job_id": job["id"],
                "owner_job_id": job["id"],
                "owner_id": job.get("owner_id"),
            }
        except ProviderUnavailable as exc:
            return self._begin_failure(job, "gpu_provider_unavailable", self._safe_error(exc))
        except Exception:
            recovered = self._recover_unknown_create(job, lease, provider)
            if recovered == "cleaned":
                return self._begin_failure(job, "gpu_create_failed", "GPU runtime allocation failed")
            if recovered is False and attempts > self.config.create_retries:
                return self._begin_failure(job, "gpu_create_failed", "GPU runtime allocation failed")
            return job
        self._update(
            "leases",
            lease["id"],
            {"state": "active", "handle": handle, "last_seen": self.clock()},
            job.get("owner_id"),
        )
        return self._set_state(
            job,
            "preparing",
            "GPU runtime allocated",
            request_id=lease["request_id"],
            runtime_id=lease["runtime_id"],
            deadline_epoch=lease["deadline_epoch"],
            started_epoch=self.clock(),
            started_at=self.clock(),
        )

    def _begin_failure(self, job: dict, code: str, message: str) -> dict:
        current = self.store.get("jobs", job["id"], job.get("owner_id"))
        if current and current.get("state") in TERMINAL_STATES | {"cleaning_up", "cleanup_pending"}:
            return current
        lease = self._lease(job)
        # No provider create was attempted, so a lease without a handle cannot
        # refer to an external runtime. Mark it reconciled without an API call.
        if lease and not lease.get("handle") and not int(lease.get("create_attempts") or 0):
            self._update(
                "leases", lease["id"], {"state": "deleted", "last_seen": self.clock()}, job.get("owner_id")
            )
        if lease:
            return self._set_state(
                job,
                "cleaning_up",
                message,
                requested_outcome="failed",
                error_code=code,
                error_message=message,
                cleanup_final_state="failed",
            )
        return self._set_state(
            job, "failed", message, requested_outcome="failed", error_code=code, error_message=message
        )

    def _expired(self, job: dict) -> bool:
        lease = self._lease(job)
        deadline = job.get("deadline_epoch") or (lease or {}).get("deadline_epoch")
        return deadline is not None and self.clock() > float(deadline)

    def _prepare(self, job: dict) -> dict:
        lease = self._lease(job)
        if not lease or not lease.get("handle"):
            return self._begin_failure(job, "gpu_lease_missing", "GPU lease is missing")
        try:
            status = self._provider(job).inspect(self._lease_handle(lease))
        except Exception as exc:
            return self._begin_failure(job, "gpu_runtime_inspect_failed", self._safe_error(exc))
        if status.get("deleted"):
            return self._begin_failure(job, "gpu_runtime_missing", "GPU runtime disappeared")
        return self._set_state(job, "loading", "GPU runtime is ready") if status.get("ready") else job

    def _load(self, job: dict) -> dict:
        lease = self._lease(job)
        if not lease:
            return self._begin_failure(job, "gpu_lease_missing", "GPU lease is missing")
        execution_id = job.get("execution_id")
        if execution_id:
            execution=self._get('executions',execution_id,job.get('owner_id'))
            if not execution or execution.get('state')!='starting':
                return self._set_state(job, "running", "GPU execution recovered")
        prepare=getattr(self.store,'prepare_execution',None)
        if prepare:
            job=prepare(job['id'],job['owner_id'],lease['id'],self.clock())
            execution_id=job['execution_id']
        else:
            execution_id = str(uuid.uuid4())
            job = self._update('jobs',job['id'],{'execution_id':execution_id},job.get('owner_id'))
        try:
            # The Drive token is attached to an in-memory copy only; it is never persisted.
            result = self._executor(job).start(
                self._lease_handle(lease), {**job, "drive_cache": self._model_cache(job)}, execution_id
            )
        except Exception as exc:
            return self._begin_failure(job, "gpu_start_failed", self._safe_error(exc))
        if isinstance(result, dict) and result.get("execution_id") not in {None, execution_id}:
            return self._begin_failure(job, "gpu_execution_id_mismatch", "executor changed execution ID")
        return self._set_state(job, "running", "GPU execution started")

    def _note_restored(self, job: dict, lease: dict, timings: dict) -> dict:
        """Remember on the lease which environment the runtime now has, and whether this job reused it."""
        signature = job.get("compatibility_signature")
        skipped = "warm_reuse" in timings
        try:
            if signature and lease.get("restored_signature") != signature:
                self._update("leases", lease["id"], {"restored_signature": signature}, job.get("owner_id"))
            return self._update("jobs", job["id"], {"restore_skipped": skipped}, job.get("owner_id"))
        except Exception:  # noqa: BLE001 - bookkeeping must never fail a finished job
            logging.getLogger(__name__).warning("could not record restored environment")
            return job

    def _model_cache(self, job: dict) -> dict | None:
        """Drive cache for reviewed model weights; None means download from Hugging Face."""
        if os.getenv("GPU_MODEL_CACHE", "drive").strip().lower() != "drive":
            return None
        if self._provider_name(job) != "colab":
            return None
        manifest = (job.get("snapshot") or {}).get("manifest") or {}
        if not manifest.get("weight_files"):
            return None
        try:
            from .drive import DriveStorage

            return DriveStorage(job, self.store).model_cache()
        except Exception as exc:  # noqa: BLE001 - the cache is an optimisation, not a requirement
            logging.getLogger(__name__).warning("model cache unavailable: %s", self._safe_error(exc))
            return None

    def _run(self, job: dict) -> dict:
        if float(job.get('poll_retry_at') or 0) > self.clock():
            return job
        lease = self._lease(job)
        if not lease:
            return self._begin_failure(job, "gpu_lease_missing", "GPU lease is missing")
        try:
            result = self._executor(job).poll(self._lease_handle(lease), job)
        except Exception as exc:
            from .mcp import MCPTransient

            retries = int(job.get('poll_retry_count') or 0) + 1
            if isinstance(exc, MCPTransient) and retries <= 5:
                return self._set_state(job, 'running', 'GPU connection interrupted; retry pending',
                    poll_retry_count=retries, poll_retry_at=self.clock() + min(2**retries, 30))
            return self._begin_failure(job, "gpu_poll_failed", self._safe_error(exc))
        if job.get('poll_retry_count') or job.get('poll_retry_at'):
            job = self._set_state(job, 'running', 'GPU connection recovered', poll_retry_count=0, poll_retry_at=0)
        if result.get('execution_id') not in {None,job.get('execution_id')}:
            return self._begin_failure(job,'gpu_execution_id_mismatch','poll execution identity mismatch')
        state = result.get("state")
        timings = result.get("timings") if isinstance(result.get("timings"), dict) else None
        if state == "completed":
            job = self._note_restored(job, lease, timings or {})
        if state == "running":
            stage = result.get("stage")
            if stage in _STAGE_TEXT and stage != job.get("stage"):
                updated = self._update(
                    "jobs", job["id"], {"stage": stage, "worker_timings": timings or {}}, job.get("owner_id")
                )
                self._event(updated, "running", _STAGE_TEXT[stage], None)
                return updated
            return job
        if state == "failed":
            detail = str(result.get("message") or "")[:1400]
            if timings:
                job = self._update("jobs", job["id"], {"worker_timings": timings}, job.get("owner_id"))
            return self._begin_failure(
                job,
                result.get("error_code", "gpu_execution_failed"),
                "GPU execution failed" + (": " + detail if detail else ""),
            )
        if state != "completed":
            return self._begin_failure(job, "gpu_invalid_poll", "invalid GPU poll state")
        if 'artifacts' in result:
            from .output_protocol import verify_output
            try:
                manifest=result['artifacts']
                if not isinstance(manifest,list) or not 1 <= len(manifest) <= 100:
                    raise ComputeError('invalid artifact manifest count')
                if result.get('execution_id') != job.get('execution_id'):
                    raise ComputeError('artifact execution mismatch')
                verified=[verify_output(job,item)[0] for item in manifest]
                return self._set_state(job,'uploading','GPU execution completed',outputs=verified,
                                       output_bytes=sum(item['size_bytes'] for item in verified),
                                       worker_timings=timings or {})
            except Exception as exc:
                return self._begin_failure(job,'gpu_output_invalid',self._safe_error(exc))
        raw_outputs = result.get("outputs")
        if not isinstance(raw_outputs, list) or not raw_outputs:
            return self._begin_failure(job, "gpu_empty_outputs", "GPU execution produced no outputs")
        # Job snapshots are JSON columns. Keep binary provider outputs durable
        # without ever putting raw bytes or more than 100MB into the database.
        outputs, total = [], 0
        try:
            for output in raw_outputs:
                if not isinstance(output, dict):
                    raise ComputeError("GPU output format invalid")
                item = dict(output)
                content = item.get("content", b"")
                if item.get("content_encoding") == "base64":
                    content = base64.b64decode(content, validate=True)
                elif isinstance(content, str):
                    content = content.encode()
                if not isinstance(content, (bytes, bytearray)) or not content:
                    raise ComputeError("GPU output is empty")
                total += len(content)
                if total > MAX_OUTPUT_BYTES:
                    raise ComputeError("GPU output exceeds 100MB limit")
                item["content"] = base64.b64encode(bytes(content)).decode("ascii")
                item["content_encoding"] = "base64"
                outputs.append(item)
        except (ValueError, TypeError, ComputeError) as exc:
            return self._begin_failure(job, "gpu_output_invalid", self._safe_error(exc))
        return self._set_state(
            job,
            "uploading",
            "GPU execution completed",
            outputs=outputs,
            output_bytes=total,
            worker_timings=timings or {},
        )

    def _result_metadata(self, job: dict) -> dict:
        snapshot = job.get("snapshot") if isinstance(job.get("snapshot"), dict) else {}
        raw = json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
        return {
            "job_id": job["id"],
            "snapshot_sha256": snapshot.get("workflow_sha256") or hashlib.sha256(raw).hexdigest(),
            "workflow_key": job.get("workflow_key"),
            "workflow_version": job.get("workflow_version"),
            "model_id": job.get("model_id"),
            "model_revision": snapshot.get("model_revision"),
            "resolved_params": snapshot.get("resolved_params", {}),
            "system_smoke": snapshot.get("system_smoke") is True,
        }

    def _write_local_metadata(self, job: dict, metadata: dict) -> dict | None:
        try:
            from app.config import settings

            root = (Path(settings.data_dir) / "gpu-results" / job["owner_id"] / job["id"]).resolve()
            root.mkdir(parents=True, exist_ok=True)
            target = root / "metadata.json"
            content = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            tmp = target.with_suffix(".partial")
            tmp.write_bytes(content)
            tmp.replace(target)
            if target.read_bytes() != content:
                raise OSError("metadata verification failed")
            return {
                "filename": "metadata.json",
                "storage_key": str(target),
                "sha256": hashlib.sha256(content).hexdigest(),
                "mime": "application/json",
                "metadata": metadata,
            }
        except (ImportError, OSError, ValueError):
            return None

    def _upload(self, job: dict) -> dict:
        outputs, assets = job.get("outputs") or [], []
        report = None
        if self.storage_factory:
            storage = self._factory(self.storage_factory, job)
        elif os.getenv("GPU_STORAGE", "local").strip().lower() == "drive":
            from .drive import DriveStorage

            storage = DriveStorage(job, self.store)
        else:
            storage = None
        result_metadata = self._result_metadata(job)
        if result_metadata["system_smoke"]:
            result_metadata["system_smoke"] = True
        if report is not None:
            result_metadata["prep_report"] = report
        metadata_record = None
        try:
            if job.get('prep'):
                for output in outputs:
                    if 'storage' in output:
                        from .output_protocol import read_diagnostic_report
                        report=read_diagnostic_report(job,output)
                    elif isinstance(output.get('metadata'),dict):
                        report=output['metadata']
                if not isinstance(report,dict):
                    raise ComputeError('prep output missing verified diagnostic report')
                outputs=[]
                result_metadata['prep_report']=report
            for index, output in enumerate(outputs):
                if 'storage' in output:
                    from .output_protocol import commit_output
                    assets.append(commit_output(self.db_store,job,output,result_metadata,storage=storage))
                    continue
                content = output.get("content", b"")
                if output.get("content_encoding") == "base64":
                    content = base64.b64decode(content, validate=True)
                elif isinstance(content, str):
                    content = content.encode()
                if (
                    not isinstance(content, (bytes, bytearray))
                    or not content
                    or len(content) > MAX_OUTPUT_BYTES
                ):
                    raise ComputeError("GPU output exceeds 100MB or is empty")
                metadata = {**result_metadata, **dict(output.get("metadata") or {})}
                if self._provider_name(job) == "mock":
                    metadata["mock"] = True
                else:
                    metadata.pop("mock", None)
                if storage is not None:
                    key = f"outputs/{job['id']}/{index}-{output.get('filename', 'output')}"
                    cloud_record = storage.put(
                        key, bytes(content), output.get("mime", "application/octet-stream")
                    )
                    if not storage.verify(cloud_record, bytes(content)):
                        raise ComputeError("stored GPU output failed verification")
                    try:
                        from app.storage import save_asset
                    except (ImportError, ModuleNotFoundError):
                        record = cloud_record
                    else:
                        record = save_asset(
                            self.db_store,
                            job["owner_id"],
                            job.get("project_id"),
                            bytes(content),
                            output.get("filename", "output"),
                            job["id"],
                            {**metadata, "cloud_record": cloud_record},
                        )
                else:
                    try:
                        from app.storage import save_asset
                    except (ImportError, ModuleNotFoundError):
                        record = {
                            "id": f"gpu-{job['id']}-{index}",
                            "filename": output.get("filename"),
                            "mime": output.get("mime"),
                            "metadata": metadata,
                            "content_b64": base64.b64encode(bytes(content)).decode(),
                        }
                    else:
                        record = save_asset(
                            self.db_store,
                            job["owner_id"],
                            job.get("project_id"),
                            bytes(content),
                            output.get("filename", "output"),
                            job["id"],
                            metadata,
                        )
                assets.append(record)
            metadata_content = json.dumps(
                result_metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
            if storage is not None:
                metadata_record = storage.put(
                    f"outputs/{job['id']}/metadata.json", metadata_content, "application/json"
                )
                if not storage.verify(metadata_record, metadata_content):
                    raise ComputeError("stored GPU metadata failed verification")
            else:
                metadata_record = self._write_local_metadata(job, result_metadata)
        except Exception as exc:
            attempts = int(job.get("upload_attempts") or 0) + 1
            if attempts >= 3:
                return self._begin_failure(job, "gpu_upload_failed", self._safe_error(exc))
            return self._update("jobs", job["id"], {"upload_attempts": attempts}, job.get("owner_id"))
        # Drive metadata file IDs are not authenticated application asset IDs.
        output_asset_ids = [a.get("id") for a in assets if a.get("id")]
        if metadata_record:
            assets.append(metadata_record)
        return self._set_state(
            job,
            "cleaning_up",
            "GPU outputs persisted",
            output_asset_ids=output_asset_ids,
            persisted_outputs=assets,
            **({'prep_report':report} if report is not None else {}),
            cleanup_final_state="completed",
        )

    def _record_usage(self, job: dict, lease: dict, stopped_at: float) -> None:
        if self._list("usage", job.get("owner_id")):
            if any(row.get("job_id") == job.get("id") and row.get('execution_id')==job.get('execution_id') for row in self._list("usage", job.get("owner_id"))):
                return
        started = float(job.get("started_epoch") or lease.get("created_epoch") or stopped_at)
        elapsed_minutes = max(0.0, stopped_at - started) / 60.0
        is_mock = self._provider_name(job) == "mock"
        rate = float(job.get("unit_rate") or self.config.unit_rate or 0)
        estimate = 0.0 if is_mock else elapsed_minutes * rate
        payload = {
            "job_id": job["id"],
            "execution_id": job.get('execution_id'),
            "provider": self._provider_name(job),
            "mock": is_mock,
            "units": estimate,
            "units_estimate": estimate,
            "units_actual": 0.0 if is_mock else None,
            "source": "simulated" if is_mock else "administrator_estimate",
            "unit_rate": rate,
            "elapsed_minutes": elapsed_minutes,
            "started_at": started,
            "stopped_at": stopped_at,
            "created_epoch": stopped_at,
            "simulated": is_mock,
            "simulated_units": float(job.get("simulated_units") or 0),
        }
        record_usage=getattr(self.store,'record_usage_once',None)
        if record_usage:
            record_usage(job['owner_id'],payload)
        else:
            self._create("usage", job["owner_id"], payload)
        self._update(
            "jobs",
            job["id"],
            {"actual_units": 0.0 if is_mock else None, "stopped_at": stopped_at, "runtime_deleted": True},
            job.get("owner_id"),
        )

    def _cleanup(self, job: dict) -> dict:
        lease = self._lease(job)
        if lease and job.get("batch_id") and job.get("cleanup_final_state") == "completed":
            waiting = [j for j in self._list("jobs", job["owner_id"])
                       if j.get("batch_id") == job["batch_id"] and j["state"] not in TERMINAL_STATES
                       and j["id"] != job["id"] and not j.get("cancel_requested")]
            waiting.sort(key=lambda j: j.get("queue_order", 0))
            next_job = waiting[0] if waiting else None
            if next_job and hasattr(self.store,'handoff_lease'):
                with self.db_store.dispatch_lock() as acquired:
                    if not acquired:
                        return job
                    transferred=self.store.handoff_lease(job['owner_id'],job['id'],next_job['id'],lease['id'],self.clock())
                if transferred:
                    self._event(job,'completed','Mock outputs verified; lease reused')
                    return self._get('jobs',job['id'],job['owner_id'])
        final_state = job.get("cleanup_final_state") or job.get("requested_outcome") or "failed"
        policy=job.get('execution_policy') or {}
        if (lease and job.get('batch_id') and final_state=='completed'
                and policy.get('mode')=='smart' and self._provider_name(job)=='mock'
                and self.clock()<float(lease.get('deadline_epoch') or 0)):
            with self.db_store.dispatch_lock() as acquired:
                if not acquired:
                    return job
                self._update('leases',lease['id'],{'state':'idle','current_job_id':None,
                    'compatibility_signature':job.get('compatibility_signature'),
                    'idle_deadline_at':min(self.clock()+int(policy.get('idle_seconds',120)),lease['deadline_epoch']),
                    'last_active_at':self.clock()},job['owner_id'])
                if job.get('execution_id'):
                    self._update('executions',job['execution_id'],{'state':'completed','finished_at':self.clock()},job['owner_id'])
                return self._set_state(job,'completed','Mock outputs verified; idle lease retained')
        if not lease:
            return self._set_state(job, final_state, "GPU cleanup completed")
        if lease.get("state") == "deleted":
            self._record_usage(job, lease, self.clock())
            return self._set_state(job, final_state, "GPU cleanup completed")
        next_epoch = float(lease.get("cleanup_next_epoch") or 0)
        if self.clock() < next_epoch:
            return job
        provider = self._provider(job)
        handle = self._lease_handle(lease) or self._intent_handle(job, lease)
        if handle is None:
            self._update(
                "leases",
                lease["id"],
                {
                    "state": "quarantined",
                    "last_error": "lease intent is incomplete",
                    "last_seen": self.clock(),
                },
                job.get("owner_id"),
            )
            return self._set_state(job, "failed", "GPU cleanup was refused", error_code="gpu_cleanup_refused")
        try:
            status = provider.inspect(handle)
            confirmed = bool(status.get("deleted"))
            if not confirmed:
                confirmed = bool(provider.delete(handle))
            error = None
        except Exception as exc:
            confirmed, error = False, self._safe_error(exc)
        attempts = int(lease.get("delete_attempts") or 0) + 1
        if not confirmed:
            delay = min(
                self.config.cleanup_retry_max_seconds,
                self.config.cleanup_retry_base_seconds * (2 ** max(0, attempts - 1)),
            )
            self._update(
                "leases",
                lease["id"],
                {
                    "state": "cleanup_pending",
                    "delete_attempts": attempts,
                    "cleanup_next_epoch": self.clock() + delay,
                    "last_error": error or "delete not confirmed",
                    "last_seen": self.clock(),
                },
                job.get("owner_id"),
            )
            return self._set_state(
                job,
                "cleanup_pending",
                "GPU cleanup is pending",
                cleanup_attempts=attempts,
                cleanup_next_epoch=self.clock() + delay,
                error_code="gpu_cleanup_pending",
            )
        self._update(
            "leases",
            lease["id"],
            {"state": "deleted", "delete_attempts": attempts, "last_seen": self.clock()},
            job.get("owner_id"),
        )
        self._record_usage(job, lease, self.clock())
        return self._set_state(job, final_state, "GPU cleanup completed")

    def _advance(self, job: dict) -> dict:
        state = job.get("state")
        if state in TERMINAL_STATES:
            return job
        if job.get("cancel_requested") and state not in {"cleaning_up", "cleanup_pending"}:
            if self._lease(job):
                job = self._set_state(
                    job,
                    "cleaning_up",
                    "GPU cancellation requested",
                    requested_outcome="cancelled",
                    cleanup_final_state="cancelled",
                )
            else:
                return self._set_state(job, "cancelled", "GPU job cancelled", requested_outcome="cancelled")
        if job.get("state") == "cleanup_pending":
            return self._cleanup(job)
        if self._expired(job) and job.get("state") not in {
            "queued",
            "validating",
            "waiting_for_capacity",
            "cleaning_up",
            "cleanup_pending",
        }:
            return self._begin_failure(job, "gpu_timeout", "GPU job exceeded its runtime limit")
        state = job.get("state")
        if state == "queued":
            return self._set_state(job, "validating", "GPU job validating")
        if state == "validating":
            return self._validate(job)
        if state == "waiting_for_capacity":
            if job.get('compatibility_signature') and hasattr(self.store,'attach_idle_lease'):
                with self.db_store.dispatch_lock() as acquired:
                    if not acquired:
                        return job
                    for idle in self._leases_for(job['owner_id']):
                        if self.store.attach_idle_lease(job['owner_id'],job['id'],idle['id'],self.clock()):
                            job=self._get('jobs',job['id'],job['owner_id'])
                            break
            if self._has_capacity_blocker(job):
                return job
            if self._await_prep(job):
                return job if job.get("awaiting_prep") else self._update("jobs", job["id"], {"awaiting_prep": True}, job.get("owner_id"))
            return self._set_state(job, "allocating", "GPU capacity available", awaiting_prep=False)
        if state == "allocating":
            return self._allocation(job)
        if state == "preparing":
            return self._prepare(job)
        if state == "loading":
            return self._load(job)
        if state == "running":
            return self._run(job)
        if state == "uploading":
            return self._upload(job)
        if state == "cleaning_up":
            return self._cleanup(job)
        return self._begin_failure(job, "gpu_invalid_state", f"unknown GPU state: {state}")

    def _claim_next_job(self) -> tuple[dict | None, str | None]:
        now = self.clock()
        jobs = self._list("jobs")
        jobs.sort(
            key=lambda x: (
                # A job parked on its cache prep goes last, so it cannot starve the prep job it waits on.
                0 if x.get("state") in {"cleanup_pending", "cleaning_up"} else 2 if x.get("awaiting_prep") else 1,
                x.get("created_epoch", x.get("created_at", "")),
            )
        )
        for job in jobs:
            if job.get("state") not in ACTIVE_STATES:
                continue
            until = float(job.get("claim_until") or 0)
            if job.get("claim_token") and until > now:
                continue
            token = uuid.uuid4().hex
            claimed = self._update(
                "jobs",
                job["id"],
                {"claim_token": token, "claim_until": now + self.config.claim_ttl_seconds},
                job.get("owner_id"),
            )
            return claimed, token
        return None, None

    def _release_claim(self, job_id: str, token: str | None, owner_id: str | None = None) -> None:
        if not token:
            return
        current = self._get("jobs", job_id, owner_id)
        if current and current.get("claim_token") == token:
            self._update("jobs", job_id, {"claim_token": None, "claim_until": None}, owner_id)

    def _next_job(self) -> dict | None:
        return next(
            (x for x in self._list("jobs") if x.get("state") in ACTIVE_STATES and not x.get("claim_token")),
            None,
        )

    def tick(self) -> dict:
        if not self.config.enabled:
            return {"acquired": True, "advanced": False, "reason": "disabled"}
        with self.db_store.dispatch_lock() as acquired:
            if not acquired:
                return {"acquired": False, "advanced": False, "reason": "busy"}
            job, claim = self._claim_next_job()
        if job is None:
            return {"acquired": True, "advanced": False}
        before = job.get("state")
        try:
            after = self._advance(job)
        finally:
            self._release_claim(job["id"], claim, job.get("owner_id"))
        return {
            "acquired": True,
            "advanced": before != after.get("state"),
            "job_id": job["id"],
            "state": after.get("state"),
        }

    def _cleanup_orphan_lease(self, lease: dict) -> bool:
        owner_job_id = lease.get("owner_job_id")
        owner_id = lease.get("owner_id")
        if not (
            isinstance(owner_job_id, str)
            and isinstance(owner_id, str)
            and lease.get("request_id")
            and lease.get("runtime_id")
        ):
            return False
        job = {"id": owner_job_id, "owner_id": owner_id, "provider": lease.get("provider", "mock")}
        handle = self._lease_handle(lease) or self._intent_handle(job, lease)
        if handle is None:
            return False
        if self.clock() < float(lease.get("cleanup_next_epoch") or 0):
            return False
        try:
            provider = self._provider(job)
            if getattr(provider,'is_mock',False) and hasattr(provider,'recover_handle'):
                handle=provider.recover_handle(handle)
            status = provider.inspect(handle)
            confirmed = bool(status.get("deleted"))
            if not confirmed:
                confirmed = bool(provider.delete(handle))
            error = None
        except Exception as exc:
            confirmed, error = False, self._safe_error(exc)
        if confirmed:
            self._update("leases", lease["id"], {"state": "deleted", "last_seen": self.clock()}, owner_id)
            return True
        attempts = int(lease.get("delete_attempts") or 0) + 1
        delay = min(
            self.config.cleanup_retry_max_seconds,
            self.config.cleanup_retry_base_seconds * (2 ** max(0, attempts - 1)),
        )
        self._update(
            "leases",
            lease["id"],
            {
                "state": "cleanup_pending",
                "delete_attempts": attempts,
                "cleanup_next_epoch": self.clock() + delay,
                "last_error": error or "delete not confirmed",
                "last_seen": self.clock(),
            },
            owner_id,
        )
        return False

    def watchdog(self) -> dict:
        """Independent process entrypoint for timeout, orphan, and cleanup compensation."""
        with self.db_store.dispatch_lock() as acquired:
            if not acquired:
                return {"acquired": False, "recovered": 0, "reason": "busy"}
            jobs = [
                x
                for x in self._list("jobs")
                if x.get("state") in ACTIVE_STATES
                and (x.get("state") in {"cleanup_pending", "cleaning_up"} or self._expired(x))
            ]
            job_by_id = {x["id"]: x for x in self._list("jobs")}
            leases = [
                x
                for x in self._leases_for()
                if x.get("state") != "deleted"
                and (
                    x.get("owner_job_id") not in job_by_id
                    or (
                        job_by_id.get(x.get("owner_job_id"), {}).get("state") in TERMINAL_STATES
                        and x.get("owner_id") == job_by_id.get(x.get("owner_job_id"), {}).get("owner_id")
                    )
                )
            ]
        recovered = 0
        for job in jobs:
            before = job.get("state")
            self._advance(job)
            current = self._get("jobs", job["id"], job.get("owner_id"))
            if current and current.get("state") != before:
                recovered += 1
        for lease in leases:
            if lease.get('state')=='idle' and self.clock()<min(
                    float(lease.get('idle_deadline_at') or 0),float(lease.get('deadline_epoch') or 0)):
                continue
            associated = job_by_id.get(lease.get("owner_job_id"))
            expired = float(lease.get("deadline_epoch") or 0) < self.clock()
            if (associated is None and expired) or (
                associated
                and associated.get("state") in TERMINAL_STATES
                and lease.get("owner_id") == associated.get("owner_id")
            ):
                if self._cleanup_orphan_lease(lease):
                    recovered += 1
        return {"acquired": True, "recovered": recovered}

    def cancel(self, owner_id: str, job_id: str) -> dict:
        job = self._get("jobs", job_id, owner_id)
        if not job:
            raise KeyError(job_id)
        if job.get("state") in TERMINAL_STATES:
            return job
        if job.get("state") == "queued" and not self._lease(job):
            return self._set_state(
                job, "cancelled", "GPU job cancelled", cancel_requested=True, requested_outcome="cancelled"
            )
        return self._update(
            "jobs", job_id, {"cancel_requested": True, "requested_outcome": "cancelled"}, owner_id
        )

    def stop(self, owner_id: str) -> dict:
        count = 0
        for job in self._list("jobs", owner_id):
            if job.get("state") not in TERMINAL_STATES:
                self.cancel(owner_id, job["id"])
                count += 1
        return {"owner_id": owner_id, "cancelled": count}


__all__ = ["Scheduler", "SchedulerError", "TERMINAL_STATES", "ACTIVE_STATES"]
