from peewee import CharField, DateTimeField, IntegerField, Model, SqliteDatabase

from src.config import DB_PATH

# WAL lets the Flask API, sync sweeper, command stream, and the UI process
# read/write concurrently; busy_timeout retries instead of raising
# "database is locked" when writes do collide.
db = SqliteDatabase(
    DB_PATH,
    pragmas={
        "journal_mode": "wal",
        "busy_timeout": 5000,
        "synchronous": "normal",
    },
)

PERMANENT_FAILURE = "PERMANENT_FAILURE"


class Person(Model):
    uniqueId = CharField(primary_key=True)
    name = CharField(null=False)
    admissionNumber = CharField(null=True)
    roomId = CharField(null=True)
    pictureFileName = CharField(null=False)
    personType = CharField(null=False)  # Cadet, Employee
    syncedAt = CharField(null=True)
    error = CharField(null=True)

    class Meta:
        database = db


class Room(Model):
    roomId = CharField(primary_key=True)
    roomName = CharField()
    syncedAt = DateTimeField()

    class Meta:
        database = db


class CadetAttendance(Model):
    personId = CharField()
    attendanceTimeStamp = DateTimeField()
    sessionId = CharField()
    syncedAt = CharField(null=True)
    error = CharField(null=True)

    class Meta:
        database = db


class Session(Model):
    id = CharField(primary_key=True)
    name = CharField()
    startTimestamp = DateTimeField()
    plannedEndTimestamp = DateTimeField()
    plannedDurationInMinutes = IntegerField()
    actualEndTimestamp = DateTimeField(null=True)
    syncedAt = DateTimeField(null=True)

    class Meta:
        database = db


class FaceIdentityMap(Model):
    """Mapping between InspireFace FeatureHub identity IDs and our Person.uniqueId.

    Embeddings persist inside InspireFace FeatureHub tables in the same SQLite DB.
    We store only the mapping so we can resolve recognitions to people.
    """

    hubId = IntegerField(primary_key=True)
    personId = CharField(unique=True)

    class Meta:
        database = db


class SyncCursor(Model):
    """Key/value store for sync state (e.g. last SSE event id)."""

    key = CharField(primary_key=True)
    value = CharField(null=True)

    class Meta:
        database = db


def _migrate_schema() -> None:
    """Add columns/tables introduced after initial deploy."""
    tables = db.get_tables()
    if "cadetattendance" in tables:
        cols = {row[1] for row in db.execute_sql("PRAGMA table_info(cadetattendance)")}
        if "error" not in cols:
            db.execute_sql("ALTER TABLE cadetattendance ADD COLUMN error TEXT")
        # Early deploys created syncedAt NOT NULL, but rows must start with
        # syncedAt NULL (unsynced) until the cloud confirms — rebuild the
        # table to relax the constraint, since SQLite can't ALTER it away.
        synced_at_not_null = any(
            row[1] == "syncedAt" and row[3]
            for row in db.execute_sql("PRAGMA table_info(cadetattendance)")
        )
        if synced_at_not_null:
            with db.atomic():
                db.execute_sql(
                    "ALTER TABLE cadetattendance RENAME TO cadetattendance_legacy"
                )
                db.create_tables([CadetAttendance], safe=True)
                db.execute_sql(
                    'INSERT INTO cadetattendance '
                    '(id, "personId", "attendanceTimeStamp", "sessionId", "syncedAt", error) '
                    'SELECT id, "personId", "attendanceTimeStamp", "sessionId", "syncedAt", error '
                    'FROM cadetattendance_legacy'
                )
                db.execute_sql("DROP TABLE cadetattendance_legacy")
    if "person" in tables:
        cols = {row[1] for row in db.execute_sql("PRAGMA table_info(person)")}
        if "error" not in cols:
            db.execute_sql("ALTER TABLE person ADD COLUMN error TEXT")


def ensure_db_schema() -> None:
    """Create tables if they do not already exist."""
    db.connect(reuse_if_open=True)
    db.create_tables(
        [Person, Room, CadetAttendance, Session, FaceIdentityMap, SyncCursor],
        safe=True,
    )
    _migrate_schema()
    db.close()


if __name__ == "__main__":
    ensure_db_schema()
