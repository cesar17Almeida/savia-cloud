"""SQLite/SQLAlchemy implementations of the persistence ports. Each method opens a
short session, commits, and maps rows back to plain domain dataclasses."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from ...domain.models import Session, SoilReading, Station, User
from ...domain.ports import (
    DownlinkLogRepository,
    ForecastRepository,
    ReadingRepository,
    SessionRepository,
    StationRepository,
    UserRepository,
)
from . import orm


def _station(row: orm.StationRow) -> Station:
    return Station(
        dev_eui=row.dev_eui,
        user_id=row.user_id,
        name=row.name,
        lat=row.lat,
        lon=row.lon,
        utc_offset_min=row.utc_offset_min,
        mode=row.mode,
        last_rssi=row.last_rssi,
        last_snr=row.last_snr,
        last_uplink_at=row.last_uplink_at,
    )


class SqlUserRepository(UserRepository):
    def __init__(self, sm: sessionmaker):
        self._sm = sm

    def get_by_email(self, email: str) -> User | None:
        with self._sm() as s:
            row = s.scalar(select(orm.UserRow).where(orm.UserRow.email == email))
            return User(row.email, row.pw_hash, row.id) if row else None

    def get_by_id(self, user_id: int) -> User | None:
        with self._sm() as s:
            row = s.get(orm.UserRow, user_id)
            return User(row.email, row.pw_hash, row.id) if row else None

    def add(self, email: str, pw_hash: str) -> User:
        with self._sm() as s:
            row = orm.UserRow(email=email, pw_hash=pw_hash)
            s.add(row)
            s.commit()
            return User(row.email, row.pw_hash, row.id)


class SqlSessionRepository(SessionRepository):
    def __init__(self, sm: sessionmaker):
        self._sm = sm

    def create(self, session: Session) -> None:
        with self._sm() as s:
            s.add(orm.SessionRow(
                token=session.token,
                user_id=session.user_id,
                expires_at=session.expires_at,
            ))
            s.commit()

    def get(self, token: str) -> Session | None:
        with self._sm() as s:
            row = s.get(orm.SessionRow, token)
            return Session(row.token, row.user_id, row.expires_at) if row else None


class SqlStationRepository(StationRepository):
    def __init__(self, sm: sessionmaker):
        self._sm = sm

    def get(self, dev_eui: str) -> Station | None:
        with self._sm() as s:
            row = s.get(orm.StationRow, dev_eui)
            return _station(row) if row else None

    def save(self, station: Station) -> None:
        with self._sm() as s:
            row = s.get(orm.StationRow, station.dev_eui) or orm.StationRow(dev_eui=station.dev_eui)
            row.user_id = station.user_id
            row.name = station.name
            row.lat = station.lat
            row.lon = station.lon
            row.utc_offset_min = station.utc_offset_min
            row.mode = station.mode
            row.last_rssi = station.last_rssi
            row.last_snr = station.last_snr
            row.last_uplink_at = station.last_uplink_at
            s.merge(row)
            s.commit()

    def list_by_mode(self, mode: str) -> list[Station]:
        with self._sm() as s:
            rows = s.scalars(select(orm.StationRow).where(orm.StationRow.mode == mode)).all()
            return [_station(r) for r in rows]


class SqlReadingRepository(ReadingRepository):
    def __init__(self, sm: sessionmaker):
        self._sm = sm

    def upsert_soil(self, reading: SoilReading) -> None:
        with self._sm() as s:
            s.merge(orm.SoilReadingRow(
                dev_eui=reading.dev_eui,
                ts_hour_s=reading.ts_hour_s,
                hs10=reading.hs10,
                hs30=reading.hs30,
                ta=reading.ta,
            ))
            s.commit()

    def window(self, dev_eui: str, from_ts: int, to_ts: int) -> list[SoilReading]:
        with self._sm() as s:
            rows = s.scalars(
                select(orm.SoilReadingRow)
                .where(orm.SoilReadingRow.dev_eui == dev_eui)
                .where(orm.SoilReadingRow.ts_hour_s >= from_ts)
                .where(orm.SoilReadingRow.ts_hour_s <= to_ts)
                .order_by(orm.SoilReadingRow.ts_hour_s)
            ).all()
            return [SoilReading(r.dev_eui, r.ts_hour_s, r.hs10, r.hs30, r.ta) for r in rows]


class SqlForecastRepository(ForecastRepository):
    def __init__(self, sm: sessionmaker):
        self._sm = sm

    def add_run(self, dev_eui: str, run_ts_s: int, hs30: list[float]) -> None:
        with self._sm() as s:
            for h, value in enumerate(hs30, start=1):
                s.add(orm.ForecastRow(dev_eui=dev_eui, run_ts_s=run_ts_s, horizon_h=h, hs30=value))
            s.commit()


class SqlDownlinkLogRepository(DownlinkLogRepository):
    def __init__(self, sm: sessionmaker):
        self._sm = sm

    def add(self, dev_eui: str, ts_s: int, kind: str, payload_hex: str, status: str) -> None:
        with self._sm() as s:
            s.add(orm.DownlinkLogRow(
                dev_eui=dev_eui, ts_s=ts_s, kind=kind, payload_hex=payload_hex, status=status,
            ))
            s.commit()
