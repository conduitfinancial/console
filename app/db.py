"""Engine + session factory. One async engine per process."""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache
def engine() -> AsyncEngine:
    return create_async_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def sessionmaker() -> async_sessionmaker:
    # expire_on_commit=False: transitions commit and hand the row back to callers.
    return async_sessionmaker(engine(), expire_on_commit=False)
