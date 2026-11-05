"""Owner-bound worker sessions for the offline provider (no subprocess/network)."""
import threading
import uuid


class SessionError(ValueError):
    pass


class MockWorkerSessions:
    _lock = threading.RLock()
    _sessions = {}

    @classmethod
    def start(cls, runtime, owner, signature, execution):
        if not runtime or not owner or not execution:
            raise SessionError('incomplete session identity')
        with cls._lock:
            session = cls._sessions.get(runtime)
            if session:
                if session['owner_id'] != owner:
                    raise SessionError('worker session owner mismatch')
                if session['signature'] != signature:
                    raise SessionError('worker session compatibility mismatch')
                if execution in session['executions']:
                    return dict(session)
                if session['active_execution']:
                    raise SessionError('worker session already executing')
            else:
                session = {'id': str(uuid.uuid4()), 'owner_id': owner, 'signature': signature,
                           'executions': [], 'active_execution': None, 'service_starts': 1,
                           'restored_signature': None, 'restore_skipped': {}, 'mock': True}
                cls._sessions[runtime] = session
            # Like the real worker: the runtime remembers which environment it last restored.
            session['restore_skipped'][execution] = session.get('restored_signature') == signature
            session['restored_signature'] = signature
            session['executions'].append(execution)
            session['active_execution'] = execution
            return dict(session)

    @classmethod
    def validate_poll(cls, runtime, owner, execution):
        with cls._lock:
            session=cls._sessions.get(runtime)
            if not session or session['owner_id']!=owner or execution not in session['executions']:
                raise SessionError('unknown execution session')
            if session['executions'][-1]!=execution:
                raise SessionError('stale execution poll')

    @classmethod
    def finish(cls, runtime, owner, execution):
        with cls._lock:
            session = cls._sessions.get(runtime)
            if not session or session['owner_id'] != owner:
                raise SessionError('unknown or foreign worker session')
            if session['active_execution'] == execution:
                session['active_execution'] = None

    @classmethod
    def close(cls, runtime):
        # Only called after provider has verified its own mock handle and deletion.
        with cls._lock:
            cls._sessions.pop(runtime, None)
