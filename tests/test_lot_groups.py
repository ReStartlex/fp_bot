from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import Base
from src.db.repo import classify_lot_group, ensure_default_lot_groups, list_lot_groups
from src.mapping.groups import group_match_score


@pytest.fixture()
async def db_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def test_group_match_score_detects_battle_net_aliases():
    keywords = "battle.net\nbattle net\nblizzard"
    assert group_match_score("Подарочная карта Battle.net 5 USD", keywords) > 0
    assert group_match_score("Blizzard Gift Card | US | 5 USD", keywords) > 0
    assert group_match_score("Steam Gift Card 5 USD", keywords) == 0


@pytest.mark.asyncio
async def test_default_groups_are_seeded_and_classify_titles(db_session_factory):
    async with db_session_factory() as session:
        await ensure_default_lot_groups(session)
        await session.commit()

        groups = await list_lot_groups(session)
        slugs = {group.slug for group in groups}
        assert {"battle-net", "steam", "app-store-itunes", "pubg-mobile"} <= slugs

        battle = await classify_lot_group(
            session, "Blizzard Gift Card | US | 5 USD"
        )
        steam = await classify_lot_group(session, "Steam Wallet 10 EUR")

        assert battle is not None
        assert battle.slug == "battle-net"
        assert steam is not None
        assert steam.slug == "steam"
