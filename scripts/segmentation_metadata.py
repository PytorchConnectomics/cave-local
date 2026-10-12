"""Serialize linked segmentation-table creation across CAVE services/workers.

Annotation ingestion and immediate materialization can both create the same
linked table. Their upstream check/insert sequences otherwise race. Keep the
lock, metadata check, table DDL and metadata insert in one PostgreSQL
transaction. Existing compatible entries retain their fields and timestamps.
"""
import datetime
import hashlib


def ensure_table(engine, model, fields):
    from dynamicannotationdb.models import SegmentationMetadata
    from sqlalchemy import text
    if engine.dialect.name != "postgresql":
        raise ValueError("Serialized segmentation metadata requires PostgreSQL")
    name = fields["table_name"]
    key = int.from_bytes(hashlib.sha256(("cave-segmentation:" + engine.url.database + ":" + name).encode()).digest()[:8], "big", signed=True)
    metadata = SegmentationMetadata.__table__
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL lock_timeout = '60s'"))
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
        row = connection.execute(metadata.select().where(metadata.c.table_name == name)).fetchone()
        if row is not None:
            for field in ("annotation_table", "schema_type", "pcg_table_name", "valid"):
                if row[field] != fields[field]:
                    raise ValueError(f"Existing segmentation metadata {name} has incompatible {field}")
        # Without metadata, an existing physical table belongs to an unexplained
        # earlier operation. Do not silently adopt it. Concurrent callers wait
        # for this transaction and then see the committed compatible metadata.
        model.__table__.create(bind=connection, checkfirst=row is not None)
        if row is None:
            connection.execute(metadata.insert().values(**fields))
    return name


def install_dynamic():
    from dynamicannotationdb.segmentation import DynamicSegmentationClient, build_segmentation_table_name
    cls = DynamicSegmentationClient
    if getattr(cls, "_cave_serialized_segmentation_creation", False):
        return

    def create(self, table_name, schema_type, segmentation_source, table_metadata=None, with_crud_columns=False):
        name = build_segmentation_table_name(table_name, segmentation_source)
        model = self.schema.create_segmentation_model(table_name, schema_type,
            segmentation_source, table_metadata, with_crud_columns)
        return ensure_table(self.db.engine, model, {"annotation_table": table_name,
            "schema_type": schema_type, "table_name": name, "valid": True,
            "created": datetime.datetime.utcnow(), "pcg_table_name": segmentation_source})

    cls.create_segmentation_table = create
    cls._cave_serialized_segmentation_creation = True


def install_materialization():
    from materializationengine.workflows.ingest_new_annotations import create_missing_segmentation_table
    from materializationengine.database import db_manager
    from materializationengine.utils import create_segmentation_model
    task = create_missing_segmentation_table._get_current_object()
    if getattr(task, "_cave_serialized_segmentation_creation", False):
        return

    def create(mat_metadata):
        metadata = mat_metadata
        model = create_segmentation_model(metadata)
        ensure_table(db_manager.get_engine(metadata["database"]), model,
            {"annotation_table": metadata["annotation_table_name"], "schema_type": metadata["schema"],
             "table_name": metadata["segmentation_table_name"], "valid": True,
             "created": datetime.datetime.utcnow(), "pcg_table_name": metadata["pcg_table_name"],
             "last_updated": metadata.get("last_updated")})
        return True

    # All imported aliases reference this same registered Celery task. Replacing
    # its run callable keeps scheduling, acknowledgements and upstream callers.
    task.run = create
    task._cave_serialized_segmentation_creation = True
