"""Engine + session factory. A shared in-memory engine (StaticPool) keeps tests
on a single connection so the schema survives between requests."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from .orm import Base


def make_sessionmaker(db_url: str) -> sessionmaker:
    """Create the engine, create all tables, and return a session factory."""
    if db_url in ("sqlite://", "sqlite:///:memory:"):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        engine = create_engine(db_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)
