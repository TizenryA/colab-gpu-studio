"""Independent GPU repository backed by app.db; no Atelier imports."""

from __future__ import annotations

import copy
import time
import uuid
from collections.abc import Mapping

from sqlalchemy import String, UniqueConstraint, inspect, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Mapped, mapped_column

from app import db as db_module
from app.db import Base, CommonMixin, Store, utcnow

from .manifest import digest


class GpuModelRow(CommonMixin, Base):
    __tablename__ = "gpu_models"


class GpuWorkflowRow(CommonMixin, Base):
    __tablename__ = "gpu_workflows"
    workflow_key: Mapped[str] = mapped_column(String(255), nullable=False)
    # Stored as text so both legacy integer versions and semver versions have
    # one database identity.  _row converts digit-only legacy values back to int.
    workflow_version: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "owner_id", "workflow_key", "workflow_version", name="uq_gpu_workflows_owner_key_version"
        ),
    )


class GpuJobRow(CommonMixin, Base):
    __tablename__ = "gpu_jobs"
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (UniqueConstraint("owner_id", "idempotency_key", name="uq_gpu_jobs_owner_idempotency"),)


class GpuBatchRow(CommonMixin, Base):
    __tablename__ = "gpu_batches"
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (UniqueConstraint("owner_id", "idempotency_key", name="uq_gpu_batches_owner_key"),)


class GpuExecutionRow(CommonMixin, Base):
    __tablename__ = "gpu_executions"


class GpuLeaseRow(CommonMixin, Base):
    __tablename__ = "gpu_leases"


class GpuEventRow(CommonMixin, Base):
    __tablename__ = "gpu_events"


class GpuPresetRow(CommonMixin, Base):
    __tablename__ = "gpu_presets"


class GpuUsageRow(CommonMixin, Base):
    __tablename__ = "gpu_usage"


class GpuFileRow(CommonMixin, Base):
    __tablename__ = "gpu_files"


class GpuCredentialRow(CommonMixin, Base):
    __tablename__ = "gpu_credentials"


_GPU_MODELS = {
    "batches": GpuBatchRow,
    "executions": GpuExecutionRow,
    "models": GpuModelRow,
    "workflows": GpuWorkflowRow,
    "jobs": GpuJobRow,
    "leases": GpuLeaseRow,
    "events": GpuEventRow,
    "presets": GpuPresetRow,
    "usage": GpuUsageRow,
    "files": GpuFileRow,
    "credentials": GpuCredentialRow,
}
_GPU_TABLES = [model.__table__ for model in _GPU_MODELS.values()]
for _kind, _model in _GPU_MODELS.items():
    db_module.MODELS.setdefault("gpu_" + _kind, _model)


def ensure_schema(store: Store) -> None:
    for attempt in range(6):
        try:
            with store.db_lock():
                Base.metadata.create_all(store.engine, tables=_GPU_TABLES, checkfirst=True)
                # create_all does not migrate a database made by an older
                # build.  Add the two identity columns and the unique index
                # idempotently before any workflow can be written.
                columns = {column["name"] for column in inspect(store.engine).get_columns("gpu_workflows")}
                with store.engine.begin() as connection:
                    if "workflow_key" not in columns:
                        connection.execute(
                            text("ALTER TABLE gpu_workflows ADD COLUMN workflow_key VARCHAR(255)")
                        )
                    if "workflow_version" not in columns:
                        connection.execute(
                            text("ALTER TABLE gpu_workflows ADD COLUMN workflow_version VARCHAR(64)")
                        )
                    if store.engine.dialect.name in {"sqlite", "postgresql"}:
                        connection.execute(
                            text(
                                "CREATE UNIQUE INDEX IF NOT EXISTS uq_gpu_workflows_owner_key_version ON gpu_workflows (owner_id, workflow_key, workflow_version)"
                            )
                        )
            return
        except OperationalError:
            if attempt == 5:
                raise
            time.sleep(0.03 * (attempt + 1))


def retarget_handle(handle, job_id):
    """Return the runtime handle re-owned by job_id (providers check handle ownership against the lease owner)."""
    if not isinstance(handle, dict):
        return handle
    return {**handle, 'job_id': job_id, 'owner_job_id': job_id}


class Repository:
    def __init__(self, store: Store):
        self.store = store
        ensure_schema(store)

    def dispatch_lock(self):
        """Expose the Store's cross-process tick lock to scheduler callers."""
        return self.store.dispatch_lock()

    @staticmethod
    def _model(kind):
        try:
            return _GPU_MODELS[kind]
        except KeyError as exc:
            raise ValueError(f"未知GPU实体类型: {kind}") from exc

    @staticmethod
    def _row(row):
        if row is None:
            return None
        reserved = {"id", "owner_id", "project_id", "state", "version", "created_at", "updated_at"}
        result = {
            "id": row.id,
            "owner_id": row.owner_id,
            "project_id": row.project_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        result.update({k: copy.deepcopy(v) for k, v in (row.data or {}).items() if k not in reserved})
        if isinstance(row, GpuWorkflowRow):
            workflow_version = row.workflow_version
            if isinstance(workflow_version, str) and workflow_version.isdigit():
                workflow_version = int(workflow_version)
            result["workflow_key"] = row.workflow_key
            result["workflow_version"] = workflow_version
        if row.state is not None:
            result["state"] = row.state
        if row.version:
            result["version"] = row.version
        if isinstance(row, GpuJobRow):
            result.update(idempotency_key=row.idempotency_key, body_hash=row.body_hash)
        return result

    def create(self, kind, owner_id, data, project_id=None, record_id=None):
        model = self._model(kind)
        if not isinstance(data, Mapping):
            raise ValueError("GPU记录data必须是对象")  # noqa: TRY004
        payload = copy.deepcopy(dict(data))
        rid = record_id or str(uuid.uuid4())
        effective_project = project_id if project_id is not None else payload.get("project_id")
        workflow_key = workflow_version = None
        if kind == "workflows":
            workflow_key = payload.get("workflow_key")
            workflow_version = payload.get("workflow_version")
            if not isinstance(workflow_key, str) or not workflow_key:
                raise ValueError("workflow_key必填")
            if not (
                (
                    isinstance(workflow_version, int)
                    and not isinstance(workflow_version, bool)
                    and workflow_version > 0
                )
                or (isinstance(workflow_version, str) and workflow_version)
            ):
                raise ValueError("workflow_version格式无效")
        if kind == "files" and effective_project is None:
            storage_key = payload.get("storage_key")
            parts = storage_key.split("/") if isinstance(storage_key, str) else []
            if len(parts) >= 2 and parts[0] == "gpu":
                source_job = self.get("jobs", parts[1], owner_id)
                effective_project = source_job.get("project_id") if source_job else None
        kwargs = {
            "id": rid,
            "owner_id": owner_id,
            "project_id": effective_project,
            "state": payload.get("state"),
            "data": payload,
            "version": 0,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        }
        if model is GpuWorkflowRow:
            kwargs.update(workflow_key=workflow_key, workflow_version=str(workflow_version))
        if model is GpuJobRow:
            key = payload.get("idempotency_key") or f"__direct__:{rid}"
            kwargs.update(
                idempotency_key=key,
                body_hash=payload.get("body_hash") or digest(payload.get("request_body", payload)),
            )
        try:
            with self.store.db_lock(), self.store.Session.begin() as session:
                row = model(**kwargs)
                session.add(row)
                session.flush()
                return self._row(row)
        except IntegrityError as exc:
            raise ValueError("GPU记录已存在或违反唯一约束") from exc

    def get(self, kind, id, owner_id=None):
        model = self._model(kind)
        with self.store.db_lock(), self.store.Session() as session:
            stmt = select(model).where(model.id == id)
            if owner_id is not None:
                stmt = stmt.where(model.owner_id == owner_id)
            return self._row(session.scalar(stmt))

    def list(self, kind, owner_id=None, project_id=None):
        model = self._model(kind)
        with self.store.db_lock(), self.store.Session() as session:
            stmt = select(model).order_by(model.created_at.desc())
            if owner_id is not None:
                stmt = stmt.where(model.owner_id == owner_id)
            if project_id is not None:
                stmt = stmt.where(model.project_id == project_id)
            return [self._row(row) for row in session.scalars(stmt).all()]

    def update(self, kind, id, patch, owner_id=None):
        model = self._model(kind)
        if not isinstance(patch, Mapping):
            raise ValueError("patch必须是对象")  # noqa: TRY004
        with self.store.db_lock(), self.store.Session.begin() as session:
            stmt = select(model).where(model.id == id)
            if owner_id is not None:
                stmt = stmt.where(model.owner_id == owner_id)
            row = session.scalar(stmt)
            if row is None:
                raise KeyError(id)
            if kind == "workflows" and row.state == "published" and set(patch) - {"active"}:
                raise ValueError("已发布workflow版本不可覆写")
            changes = copy.deepcopy(dict(patch))
            row.data = {**(row.data or {}), **changes}
            if "state" in changes:
                row.state = changes["state"]
            if "project_id" in changes:
                row.project_id = changes["project_id"]
            if kind == "workflows":
                if "workflow_key" in changes:
                    if not isinstance(changes["workflow_key"], str) or not changes["workflow_key"]:
                        raise ValueError("workflow_key格式无效")
                    row.workflow_key = changes["workflow_key"]
                if "workflow_version" in changes:
                    value = changes["workflow_version"]
                    if not (
                        (isinstance(value, int) and not isinstance(value, bool) and value > 0)
                        or (isinstance(value, str) and value)
                    ):
                        raise ValueError("workflow_version格式无效")
                    row.workflow_version = str(value)
            row.version += 1
            row.updated_at = utcnow()
            if kind=='jobs' and row.data.get('batch_id'):
                self._refresh_batch(session,row.data['batch_id'],row.owner_id)
            session.flush()
            return self._row(row)

    def create_job(self, owner_id, body, idempotency_key, *, admitted=None):
        if not isinstance(body, Mapping):
            raise ValueError("job body必须是对象")  # noqa: TRY004
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key必填")
        raw = copy.deepcopy(dict(body))
        body_hash = digest(raw)
        for attempt in range(8):
            try:
                with self.store.db_lock(), self.store.Session.begin() as session:
                    existing = session.scalar(
                        select(GpuJobRow).where(
                            GpuJobRow.owner_id == owner_id, GpuJobRow.idempotency_key == idempotency_key
                        )
                    )
                    if existing:
                        if existing.body_hash != body_hash:
                            raise ValueError("同一owner的idempotency_key已用于不同body")
                        return self._row(existing), False
                    policy = raw.get("execution_policy") or {}
                    payload = {
                        **raw,
                        "request_body": raw,
                        "idempotency_key": idempotency_key,
                        "body_hash": body_hash,
                        "state": "queued",
                        "provider": policy.get("provider", "mock"),
                        "workflow_key": raw.get("workflow_key"),
                        "workflow_version": raw.get("workflow_version"),
                        "model_id": raw.get("model_id"),
                        "params": copy.deepcopy(raw.get("params", {})),
                        "resolved_seed": None,
                        "snapshot": None,
                        "max_runtime_minutes": policy.get("max_runtime_minutes"),
                        "estimated_units": None,
                        "unit_rate": None,
                        "requested_outcome": None,
                        "cancel_requested": False,
                        "output_asset_ids": [],
                    }
                    if admitted is not None:
                        payload.update(copy.deepcopy(admitted))
                    row = GpuJobRow(
                        id=str(uuid.uuid4()),
                        owner_id=owner_id,
                        project_id=raw.get("project_id"),
                        state="queued",
                        data=payload,
                        version=0,
                        idempotency_key=idempotency_key,
                        body_hash=body_hash,
                        created_at=utcnow(),
                        updated_at=utcnow(),
                    )
                    session.add(row)
                    session.flush()
                    return self._row(row), True
            except IntegrityError:
                if attempt == 7:
                    raise ValueError("GPU job创建竞争失败，请重试")
                time.sleep(0.01 * (attempt + 1))
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 7:
                    raise
                time.sleep(0.02 * (attempt + 1))
        raise RuntimeError("GPU job创建未完成")

    def create_batch(self, owner_id, jobs, idempotency_key, policy, request_body=None):
        for attempt in range(8):
            try:
                return self._create_batch_once(owner_id,jobs,idempotency_key,policy,request_body)
            except IntegrityError:
                if attempt==7:
                    raise ValueError('batch creation contention; retry')
            except OperationalError as exc:
                if 'locked' not in str(exc).lower() or attempt==7:
                    raise
            time.sleep(0.02*(attempt+1))

    def _refresh_batch(self, session, batch_id, owner_id):
        session.flush()
        batch=session.get(GpuBatchRow,batch_id)
        if not batch or batch.owner_id!=owner_id:
            return
        jobs=list(session.scalars(select(GpuJobRow).where(
            GpuJobRow.owner_id==owner_id,GpuJobRow.id.in_(batch.data.get('job_ids',[])))))
        counts={state:sum(job.state==state for job in jobs) for state in ('completed','failed','cancelled')}
        finished=sum(counts.values())
        if jobs and finished==len(jobs):
            state=next((name for name,count in counts.items() if count==len(jobs)),'partial')
        else:
            state='running' if any(job.state!='queued' for job in jobs) else 'queued'
        batch.state=state
        batch.data={**batch.data,'state':state,'finished_jobs':finished,'counts':counts}
        batch.version+=1
        batch.updated_at=utcnow()

    def _create_batch_once(self, owner_id, jobs, idempotency_key, policy, request_body=None):
        if not isinstance(jobs, list) or not 1 <= len(jobs) <= 100:
            raise ValueError("batch requires 1..100 jobs")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
            raise ValueError("invalid idempotency key")
        if policy.get("mode", "save") not in {"save", "smart", "batch"}:
            raise ValueError("invalid lifecycle mode")
        body_hash = digest(request_body if request_body is not None else {"jobs": jobs, "policy": policy})
        with self.store.db_lock(), self.store.Session.begin() as session:
            existing = session.scalar(select(GpuBatchRow).where(
                GpuBatchRow.owner_id == owner_id, GpuBatchRow.idempotency_key == idempotency_key))
            if existing:
                if existing.body_hash != body_hash:
                    raise ValueError("batch idempotency conflict")
                return self._row(existing), False
            batch_id = str(uuid.uuid4())
            payload = {"policy_snapshot": copy.deepcopy(policy), "total_jobs": len(jobs),
                       "finished_jobs": 0, "mode": policy.get("mode", "save"), "job_ids": [],
                       "request_key":idempotency_key,"request_hash":body_hash}
            for index, job in enumerate(jobs):
                job_id = str(uuid.uuid4())
                data = copy.deepcopy(job)
                raw_jobs=(request_body or {}).get('jobs',[])
                data['request_body']=copy.deepcopy(raw_jobs[index] if index<len(raw_jobs) else {
                    k:v for k,v in job.items() if k in {'project_id','model_id','workflow_key','workflow_version','params','asset_ids','execution_policy','preset_id','loras'}})
                data.update(batch_id=batch_id, queue_order=index, state="queued",
                            execution_policy={**data.get("execution_policy", {}), **policy},
                            provider=data.get("execution_policy", {}).get("provider", "mock"))
                if data['provider'] not in {'mock', 'colab'}:
                    raise ValueError('batch provider is not authorized')
                data['compatibility_signature'] = digest({
                    'provider':data['provider'],
                    'environment':{k:v for k,v in (data.get('snapshot') or {}).items()
                                   if k not in {'resolved_params','bound_graph','input_files'}},
                    'workflow_key':data.get('workflow_key'), 'workflow_version':data.get('workflow_version'),
                    'requested_spec':data.get('requested_spec')})
                row = GpuJobRow(id=job_id, owner_id=owner_id, project_id=data.get('project_id'),
                                state='queued', data=data, version=0,
                                idempotency_key=f'batch:{batch_id}:{index}', body_hash=digest(job),
                                created_at=utcnow(), updated_at=utcnow())
                session.add(row)
                payload['job_ids'].append(job_id)
            batch = GpuBatchRow(id=batch_id, owner_id=owner_id,
                                project_id=jobs[0].get('project_id'), state='queued', version=0,
                                data=payload, idempotency_key=idempotency_key, body_hash=body_hash,
                                created_at=utcnow(), updated_at=utcnow())
            session.add(batch)
            session.flush()
            return self._row(batch), True

    def attach_idle_lease(self, owner_id, job_id, lease_id, now):
        with self.store.db_lock(), self.store.Session.begin() as session:
            job=session.get(GpuJobRow,job_id)
            lease=session.get(GpuLeaseRow,lease_id)
            if not job or not lease or job.owner_id!=owner_id or lease.owner_id!=owner_id:
                return False
            if (lease.state!='idle' or job.state!='waiting_for_capacity' or job.data.get('cancel_requested')
                    or not job.data.get('compatibility_signature')
                    or job.data['compatibility_signature']!=lease.data.get('compatibility_signature')
                    or now>=min(float(lease.data.get('idle_deadline_at') or 0),float(lease.data.get('deadline_epoch') or 0))):
                return False
            lease.state='active'
            lease.data={**lease.data,'state':'active','owner_job_id':job_id,'current_job_id':job_id,
                        'last_active_at':now,'idle_deadline_at':None,
                        **({'handle':retarget_handle(lease.data['handle'],job_id)} if isinstance(lease.data.get('handle'),dict) else {})}
            job.data={**job.data,'lease_id':lease_id,'deadline_epoch':lease.data['deadline_epoch']}
            for row in (lease,job):
                row.version+=1
                row.updated_at=utcnow()
            return True

    def handoff_lease(self, owner_id, job_id, next_job_id, lease_id, now):
        """Finish old job and transfer exclusive lease ownership in one DB transaction."""
        with self.store.db_lock(), self.store.Session.begin() as session:
            job=session.get(GpuJobRow,job_id)
            following=session.get(GpuJobRow,next_job_id)
            lease=session.get(GpuLeaseRow,lease_id)
            if not all(row is not None and row.owner_id==owner_id for row in (job,following,lease)):
                raise ValueError('lease handoff ownership mismatch')
            if job.state!='cleaning_up' or following.state not in {'queued','validating','waiting_for_capacity'}:
                return False
            if following.data.get('lease_id') not in {None,lease_id}:
                return False
            others=session.scalars(select(GpuLeaseRow).where(GpuLeaseRow.owner_id==owner_id)).all()
            if any(row.id!=lease_id and row.data.get('owner_job_id')==next_job_id and row.state!='deleted' for row in others):
                return False
            if following.data.get('cancel_requested') or lease.data.get('owner_job_id')!=job_id:
                return False
            if (job.data.get('compatibility_signature')!=following.data.get('compatibility_signature')
                    or not job.data.get('compatibility_signature')
                    or lease.state not in {'active','busy'}
                    or now>=float(lease.data.get('deadline_epoch') or 0)):
                return False
            job.data={**job.data,'state':'completed','runtime_deleted':False,'stopped_at':now}
            job.state='completed'
            following.data={**following.data,'lease_id':lease_id,
                            'deadline_epoch':lease.data['deadline_epoch']}
            handle=lease.data.get('handle')
            # Handle ownership must follow the lease owner for every provider (real ones verify it).
            lease.data={**lease.data,'owner_job_id':next_job_id,'current_job_id':next_job_id,
                        'last_active_at':now,'state':'active',
                        **({'handle':retarget_handle(handle,next_job_id)} if isinstance(handle,dict) else {})}
            lease.state='active'
            execution=session.get(GpuExecutionRow,job.data.get('execution_id'))
            if execution is not None:
                execution.state='completed'
                execution.data={**execution.data,'state':'completed','finished_at':now}
            for row in (job,following,lease):
                row.version+=1
                row.updated_at=utcnow()
            if job.data.get('batch_id'):
                self._refresh_batch(session,job.data['batch_id'],owner_id)
            return True

    def record_usage_once(self, owner_id, payload):
        # Deterministic primary key provides DB-enforced dedupe across processes.
        identity=digest([owner_id,payload['job_id'],payload.get('execution_id')])
        try:
            return self.create('usage',owner_id,payload,record_id=identity)
        except (IntegrityError, ValueError):
            existing=self.get('usage',identity,owner_id)
            if existing is None:
                raise
            return existing

    def retry_attempt(self, job_id, owner_id, key):
        """Requeue a cleaned failed Mock job; immutable snapshot and executions survive."""
        if not key or len(key)>200:
            raise ValueError('idempotency key required')
        with self.store.db_lock(), self.store.Session.begin() as session:
            job=session.scalar(select(GpuJobRow).where(GpuJobRow.id==job_id,GpuJobRow.owner_id==owner_id).with_for_update())
            if not job:
                raise KeyError(job_id)
            data=copy.deepcopy(job.data)
            keys=data.get('attempt_retry_keys') or []
            if key in keys:
                return self._row(job)
            if job.state not in {'failed','cancelled'}:
                raise ValueError('job must be failed or cancelled')
            if data.get('provider',(data.get('execution_policy') or {}).get('provider','mock'))!='mock':
                raise ValueError('same-job retry is currently Mock only')
            snapshot=data.get('snapshot') or {}
            if data.get('prep') or any(snapshot.get(k) or data.get(k) for k in ('system_smoke','workflow_smoke','prep')) or (data.get('request_body') or {}).get('kind'):
                raise ValueError('special job requires its confirmation entry')
            leases=session.scalars(select(GpuLeaseRow).where(GpuLeaseRow.owner_id==owner_id)).all()
            if any(row.data.get('owner_job_id')==job_id and row.state!='deleted' for row in leases):
                raise ValueError('lease cleanup must be confirmed before retry')
            history=data.get('attempt_history') or []
            history.append({k:copy.deepcopy(v) for k,v in data.items() if k not in {'snapshot','request_body','attempt_history','attempt_retry_keys'}})
            transient={'execution_id','lease_id','outputs','output_asset_ids','persisted_outputs','error_code','error_message',
                'cancel_requested','requested_outcome','cleanup_final_state','cleanup_attempts','cleanup_next_epoch',
                'upload_attempts','started_epoch','stopped_at','deadline_epoch','validated_epoch','budget_reserved',
                'runtime_deleted','actual_units','prep_report','progress','awaiting_prep',
                'stage','worker_timings','output_bytes','started_at','finished_at','poll_retry_count','poll_retry_at'}
            for k in transient:
                data.pop(k,None)
            data.update(state='queued',attempt_history=history,attempt_retry_keys=[*keys,key])
            job.data=data
            job.state='queued'
            job.version+=1
            job.updated_at=utcnow()
            session.add(GpuEventRow(id=str(uuid.uuid4()),owner_id=owner_id,project_id=job.project_id,state='queued',
                version=0,created_at=utcnow(),updated_at=utcnow(),data={'job_id':job_id,'state':'queued','message':'Mock retry attempt queued; previous evidence retained'}))
            if data.get('batch_id'):
                self._refresh_batch(session,data['batch_id'],owner_id)
            session.flush()
            return self._row(job)

    def prepare_execution(self, job_id, owner_id, lease_id, now):
        """Persist execution identity before start, atomically with its attempt record."""
        with self.store.db_lock(), self.store.Session.begin() as session:
            job=session.scalar(select(GpuJobRow).where(GpuJobRow.id==job_id,GpuJobRow.owner_id==owner_id).with_for_update())
            lease=session.get(GpuLeaseRow,lease_id)
            if not job or not lease or lease.owner_id!=owner_id:
                raise KeyError(job_id)
            if job.data.get('execution_id'):
                return self._row(job)
            if job.state!='loading':
                raise ValueError('job is not ready to start')
            execution_id=str(uuid.uuid4())
            attempt=int(job.data.get('attempt_number') or 0)+1
            job.data={**job.data,'execution_id':execution_id,'attempt_number':attempt}
            job.version+=1
            job.updated_at=utcnow()
            session.add(GpuExecutionRow(id=execution_id,owner_id=owner_id,project_id=job.project_id,
                state='starting',version=0,created_at=utcnow(),updated_at=utcnow(),
                data={'job_id':job_id,'lease_id':lease_id,'attempt':attempt,
                      'worker_session_id':lease_id,'started_at':now,'state':'starting'}))
            session.flush()
            return self._row(job)

    def transition_job(self, job_id, owner_id, state, message, patch, now, expected_state=None, expected_execution_id=...):
        """Commit job, execution, batch aggregate and audit event together."""
        terminal={'completed','failed','cancelled'}
        progress=patch.get('progress')
        if progress is not None and (isinstance(progress,bool) or not isinstance(progress,(int,float)) or not 0<=progress<=1):
            raise ValueError('progress必须在0到1之间')
        with self.store.db_lock(), self.store.Session.begin() as session:
            job=session.scalar(select(GpuJobRow).where(GpuJobRow.id==job_id,GpuJobRow.owner_id==owner_id).with_for_update())
            if not job:
                raise KeyError(job_id)
            if expected_state is not None and job.state!=expected_state:
                return self._row(job)
            if expected_execution_id is not ... and job.data.get('execution_id')!=expected_execution_id:
                return self._row(job)
            if job.state in terminal:
                return self._row(job)
            job.state=state
            job.data={**job.data,**copy.deepcopy(patch),'state':state}
            job.version+=1
            job.updated_at=utcnow()
            execution_id=job.data.get('execution_id')
            if execution_id and state in terminal|{'running','uploading'}:
                execution=session.get(GpuExecutionRow,execution_id)
                if execution and execution.owner_id!=owner_id:
                    raise ValueError('execution owner mismatch')
                if execution and execution.data.get('job_id') not in {None,job_id}:
                    raise ValueError('execution job identity mismatch')
                if execution:
                    changes={'state':state}
                    if state in terminal:
                        changes.update(finished_at=now,error_code=job.data.get('error_code'))
                    execution.state=state
                    execution.data={**execution.data,**changes}
                    execution.version+=1
                    execution.updated_at=utcnow()
            data={'job_id':job_id,'state':state,'message':message}
            if progress is not None:
                data['progress']=progress
            session.add(GpuEventRow(id=str(uuid.uuid4()),owner_id=owner_id,
                project_id=job.project_id,state=state,data=data,version=0,
                created_at=utcnow(),updated_at=utcnow()))
            if job.data.get('batch_id'):
                self._refresh_batch(session,job.data['batch_id'],owner_id)
            session.flush()
            return self._row(job)

    def event(self, job_id, owner_id, state, message, progress=None):
        job = self.get("jobs", job_id, owner_id)
        if job is None:
            raise KeyError(job_id)
        if progress is not None and (
            isinstance(progress, bool) or not isinstance(progress, (int, float)) or not 0 <= progress <= 1
        ):
            raise ValueError("progress必须在0到1之间")
        data = {"job_id": job_id, "message": message, "state": state}
        if progress is not None:
            data["progress"] = progress
        return self.create("events", owner_id, data, project_id=job.get("project_id"))
