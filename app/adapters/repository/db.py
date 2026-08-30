"""Engine + session factory.

Production runs on PostgreSQL (DATABASE_URL=postgresql+psycopg://...). The
sqlite in-memory URL is kept for the test suite only: a shared StaticPool engine
keeps every test on one connection so the schema survives between requests.
"""
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
    elif db_url.startswith("sqlite"):
        engine = create_engine(db_url, connect_args={"check_same_thread": False})
    else:
        # PostgreSQL: pre-ping drops stale pooled connections after a server restart
        # instead of failing the first request with them.
        engine = create_engine(db_url, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)
