"""SQLAlchemy 2.0 ORM tables. Kept apart from the domain: adapters map rows to the
plain domain dataclasses so the core never imports SQLAlchemy."""
from __future__ import annotations

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UserRow(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    pw_hash: Mapped[str] = mapped_column(String)


class SessionRow(Base):
    __tablename__ = "sessions"
    token: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[int] = mapped_column()   # epoch seconds


class StationRow(Base):
    __tablename__ = "stations"
    dev_eui: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    name: Mapped[str] = mapped_column(String, default="")
    lat: Mapped[float] = mapped_column(default=0.0)
    lon: Mapped[float] = mapped_column(default=0.0)
    utc_offset_min: Mapped[int] = mapped_column(default=0)
    mode: Mapped[str] = mapped_column(String, default="forward")
    last_rssi: Mapped[int | None] = mapped_column(nullable=True)
    last_snr: Mapped[float | None] = mapped_column(nullable=True)
    last_uplink_at: Mapped[int | None] = mapped_column(nullable=True)


class SoilReadingRow(Base):
    __tablename__ = "soil_readings"
    dev_eui: Mapped[str] = mapped_column(String, primary_key=True)
    ts_hour_s: Mapped[int] = mapped_column(primary_key=True)
    hs10: Mapped[float | None] = mapped_column(nullable=True)
    hs30: Mapped[float | None] = mapped_column(nullable=True)
    ta: Mapped[float | None] = mapped_column(nullable=True)


class ForecastRow(Base):
    __tablename__ = "forecasts"
    id: Mapped[int] = mapped_column(primary_key=True)
    dev_eui: Mapped[str] = mapped_column(String, index=True)
    run_ts_s: Mapped[int] = mapped_column()
    horizon_h: Mapped[int] = mapped_column()
    hs30: Mapped[float] = mapped_column()


class DownlinkLogRow(Base):
    __tablename__ = "downlink_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    dev_eui: Mapped[str] = mapped_column(String, index=True)
    ts_s: Mapped[int] = mapped_column()
    kind: Mapped[str] = mapped_column(String)
    payload_hex: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)


class UplinkLogRow(Base):
    __tablename__ = "uplink_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    dev_eui: Mapped[str] = mapped_column(String, index=True)
    ts_s: Mapped[int] = mapped_column()
    u_type: Mapped[str] = mapped_column(String)
    payload_hex: Mapped[str] = mapped_column(String)
    rssi: Mapped[int | None] = mapped_column(nullable=True)
    snr: Mapped[float | None] = mapped_column(nullable=True)


class LinkOutboxRow(Base):
    """Downlinks waiting for a station on the HTTP link (LINK_MODE=http)."""
    __tablename__ = "link_outbox"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    dev_id: Mapped[str] = mapped_column(String, index=True)
    f_port: Mapped[int] = mapped_column()
    payload_hex: Mapped[str] = mapped_column(String)
    created_s: Mapped[int] = mapped_column()
    delivered_s: Mapped[int | None] = mapped_column(nullable=True)


class LinkPauseRow(Base):
    """A station whose outbox is held: its queue keeps filling, but nothing is
    handed out until the operator resumes it. A row exists only while paused --
    its own table rather than a column on `stations`, so the schema grows by
    creation at startup and no deployment needs a migration."""
    __tablename__ = "link_pause"
    dev_id: Mapped[str] = mapped_column(String, primary_key=True)
    paused_s: Mapped[int] = mapped_column()
