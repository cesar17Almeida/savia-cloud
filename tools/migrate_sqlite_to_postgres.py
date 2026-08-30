#!/usr/bin/env python3
"""One-off data migration: copy every table from the old SQLite file into PostgreSQL.

    .venv/bin/python tools/migrate_sqlite_to_postgres.py \
        --sqlite /var/lib/savia-cloud/savia.db \
        --pg "postgresql+psycopg://savia@/savia?host=/var/run/postgresql"

Creates the schema on the target if missing, copies rows table by table in
dependency order, and resets the serial sequences so new rows do not collide with
migrated ids. Refuses to run against a target that already holds rows unless
--append is given.
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

sys.path.insert(0, ".")
from app.adapters.repository.orm import (  # noqa: E402
    Base, DownlinkLogRow, ForecastRow, SessionRow, SoilReadingRow, StationRow,
    UplinkLogRow, UserRow,
)

TABLES = [UserRow, SessionRow, StationRow, SoilReadingRow, ForecastRow, DownlinkLogRow, UplinkLogRow]
SERIAL = {"users": "id", "forecasts": "id", "downlink_log": "id", "uplink_log": "id"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", required=True, help="path to the SQLite file")
    ap.add_argument("--pg", required=True, help="SQLAlchemy URL of the PostgreSQL target")
    ap.add_argument("--append", action="store_true", help="allow a non-empty target")
    a = ap.parse_args()

    src = create_engine(f"sqlite:///{a.sqlite}")
    dst = create_engine(a.pg)
    Base.metadata.create_all(dst)

    with Session(dst) as d:
        if not a.append:
            for t in TABLES:
                n = d.scalar(select(func.count()).select_from(t))
                if n:
                    print(f"target table {t.__tablename__} already has {n} rows; use --append")
                    return 2

    total = 0
    with Session(src) as s, Session(dst) as d:
        for t in TABLES:
            rows = s.scalars(select(t)).all()
            cols = [c.key for c in t.__table__.columns]
            for r in rows:
                d.merge(t(**{c: getattr(r, c) for c in cols}))
            d.commit()
            print(f"{t.__tablename__:14s} {len(rows)} rows")
            total += len(rows)
        for table, col in SERIAL.items():
            d.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{table}', '{col}'), "
                f"COALESCE((SELECT MAX({col}) FROM {table}), 0) + 1, false)"
            ))
        d.commit()
    print(f"done: {total} rows copied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
