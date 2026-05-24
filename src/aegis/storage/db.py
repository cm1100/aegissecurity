from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DEFAULT_DB_URL = os.environ.get("AEGIS_DB_URL", "sqlite:///./aegis.db")


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal = None


def _build(url: str):
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args, future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return engine, SessionLocal


def init_db(url: str | None = None) -> None:
    global _engine, _SessionLocal
    from aegis.storage import models  # noqa: F401  (register tables)

    url = url or DEFAULT_DB_URL
    _engine, _SessionLocal = _build(url)
    Base.metadata.create_all(_engine)


def reset_db(url: str | None = None) -> None:
    global _engine, _SessionLocal
    from aegis.storage import models  # noqa: F401

    url = url or DEFAULT_DB_URL
    _engine, _SessionLocal = _build(url)
    Base.metadata.drop_all(_engine)
    Base.metadata.create_all(_engine)


@contextmanager
def get_session() -> Iterator[Session]:
    if _SessionLocal is None:
        init_db()
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
