"""Explicit, restartable upgrade. Never runs implicitly on API import.

MySQL DDL auto-commits: preflight happens first, each step is idempotent, and
the version is recorded only after every step succeeds. Back up before upgrade.
"""

from sqlalchemy import inspect, text
from . import db

VERSION = "20261015_01"


def upgrade():
    engine = db.engine
    if engine.dialect.name not in {"mysql", "sqlite"}:
        raise RuntimeError("该升级仅支持 MySQL 与本地 SQLite 测试库")
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    if "item_snapshots" in existing:
        with engine.connect() as conn:
            duplicate = conn.execute(
                text(
                    "SELECT target_id,item_id,COUNT(*) AS n FROM item_snapshots GROUP BY target_id,item_id HAVING COUNT(*)>1 LIMIT 1"
                )
            ).first()
            if duplicate:
                raise RuntimeError(
                    "存在重复快照，升级未开始。请先备份并人工核对重复 target_id/item_id，不自动删除历史数据"
                )
    db.metadata.create_all(engine)
    # Additive fields on pre-existing tables; new installations already have them.
    additions = {
        "monitor_targets": [
            ("revision", "INTEGER NOT NULL DEFAULT 0"),
            ("baseline_reset", "BOOLEAN NOT NULL DEFAULT 0"),
        ],
        "events": [("price_currency_old", "VARCHAR(20) NULL")],
    }
    with engine.begin() as conn:
        for table, columns in additions.items():
            known = {row["name"] for row in inspect(conn).get_columns(table)}
            for name, definition in columns:
                if name not in known:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
        if engine.dialect.name == "mysql":
            for table, columns in {
                "monitor_targets": ["min_price"],
                "item_snapshots": ["price"],
                "events": ["price", "price_old"],
                "price_trend": ["min_price", "top10_avg_price"],
            }.items():
                types = {row["name"]: row for row in inspect(conn).get_columns(table)}
                for column in columns:
                    current = types[column]
                    kind = current["type"]
                    if getattr(kind, "precision", None) == 20 and getattr(kind, "scale", None) == 6:
                        continue
                    nullable = "NULL" if current["nullable"] else "NOT NULL"
                    conn.execute(
                        text(f"ALTER TABLE {table} MODIFY COLUMN {column} DECIMAL(20,6) {nullable}")
                    )
        elif engine.dialect.name != "sqlite":
            raise RuntimeError("该升级仅支持 MySQL 与本地 SQLite 测试库")
        # SQLite keeps INTEGER affinity on legacy columns; it accepts fractional
        # values, whereas MySQL requires the explicit DECIMAL conversions above.
        for model_table in db.metadata.sorted_tables:
            known = {index["name"] for index in inspect(conn).get_indexes(model_table.name)}
            for index in model_table.indexes:
                if index.name not in known:
                    index.create(conn)
        unique = inspect(conn).get_unique_constraints("item_snapshots")
        unique += [row for row in inspect(conn).get_indexes("item_snapshots") if row.get("unique")]
        if not any(set(row["column_names"]) == {"target_id", "item_id"} for row in unique):
            conn.execute(
                text("CREATE UNIQUE INDEX unique_target_item ON item_snapshots(target_id,item_id)")
            )
        conn.execute(
            text("CREATE TABLE IF NOT EXISTS schema_version (version VARCHAR(32) PRIMARY KEY)")
        )
        if (
            conn.execute(
                text("SELECT version FROM schema_version WHERE version=:v"), {"v": VERSION}
            ).first()
            is None
        ):
            conn.execute(text("INSERT INTO schema_version(version) VALUES (:v)"), {"v": VERSION})
