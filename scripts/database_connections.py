"""Recover idle DB connections without replaying annotation writes.

Materialization clones an aligned-volume database by terminating its sessions.
The pinned DynamicAnnotationDB otherwise retains one session across requests.
Use its existing thread-scoped session factory, validate pool checkouts, and
release only the current thread's session after a request or Celery task.
"""
from contextlib import contextmanager
from threading import RLock
from weakref import WeakSet


def install():
    import dynamicannotationdb.database as database
    if hasattr(database, '_cave_release_sessions'):
        return database._cave_release_sessions
    instances = WeakSet()
    lock = RLock()
    original_engine = database.create_engine
    original_init = database.DynamicAnnotationDB.__init__

    def checked_engine(*args, **kwargs):
        kwargs['pool_pre_ping'] = True
        return original_engine(*args, **kwargs)

    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        with lock:
            instances.add(self)

    @contextmanager
    def session_scope(self):
        session = self.session()
        try:
            yield session
        except Exception:
            session.rollback()
            raise

    def release(*args, **kwargs):
        with lock:
            current = tuple(instances)
        for instance in current:
            # scoped_session.remove() acts on this request/task's thread. Keep
            # the engine and model caches for other concurrent requests.
            instance.session.remove()

    database.create_engine = checked_engine
    cls = database.DynamicAnnotationDB
    cls.__init__ = initialize
    cls.cached_session = property(lambda self: self.session())
    cls.session_scope = session_scope
    database._cave_release_sessions = release
    return release
