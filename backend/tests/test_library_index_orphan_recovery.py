"""building generation 孤儿状态自愈测试。

重建标记只有「成功切代」和「捕获到异常」两条路径会清空。进程在重建途中被
重启时标记会永久残留，此后每次重建都报「当前库存已有 building generation」，
库存索引刷新彻底停摆（watcher 每 5 秒失败重试一次刷满日志）。

这里用 SQLite 直接验状态机：心跳停滞的孤儿要被回收，心跳新鲜或本进程仍在
构建的要保留。
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import Integer, MetaData, create_engine, text
from sqlalchemy.orm import sessionmaker

import app.core.library_index.service as service_module
from app.core.library_index.service import (
    ORPHANED_BUILDING_GENERATION_MESSAGE,
    LibraryIndexService,
)
from app.models.database import (
    LibraryIndexGeneration,
    LibraryIndexStatus,
    get_local_now,
)

LIBRARY_ID = "真库存"


class _FakeStore:
    def __init__(self) -> None:
        self.broadcasts: list[str | None] = []

    def get_status(self, library_id: str):
        return None

    def _broadcast_status_change(self, snapshot, reason=None) -> None:
        self.broadcasts.append(reason)


@pytest.fixture
def orphan_env(tmp_path, monkeypatch):
    """建一个只含两张索引表的 SQLite 环境，并把 SessionLocal 指过去。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'index.db'}", future=True)
    LibraryIndexStatus.__table__.create(engine)
    # BigInteger 主键在 SQLite 上不是 rowid 别名，自增插不进去：建表副本时换成 Integer。
    generation_metadata = MetaData()
    generation_table = LibraryIndexGeneration.__table__.to_metadata(generation_metadata)
    generation_table.c.id.type = Integer()
    generation_table.create(engine)
    # _create_building_generation 会查 prepared mutation。这两张表在 PostgreSQL 上
    # 带 JSONB 列，SQLite 建不出来，这里只建查询用到的列。
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE library_index_mutation_operations ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " operation_id VARCHAR(36),"
            " state VARCHAR(20))"
        ))
        conn.execute(text(
            "CREATE TABLE library_index_pending_masks ("
            " library_id VARCHAR(60),"
            " operation_id VARCHAR(36))"
        ))
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(service_module, "SessionLocal", session_factory)
    monkeypatch.setattr(
        service_module,
        "require_library_index_generation_contract_ready",
        lambda _conn: {"ready": True},
    )
    monkeypatch.setenv("KIKOERUMANAGER_LIBRARY_INDEX_GENERATION_CONTRACT", "1")
    monkeypatch.setenv("KIKOERUMANAGER_LIBRARY_INDEX_DATABASE_FREE_BYTES", str(10**12))
    # 空间预检依赖 PostgreSQL 的 pg_total_relation_size，与本次状态机无关。
    monkeypatch.setattr(
        LibraryIndexService,
        "_require_generation_capacity",
        lambda *_args, **_kwargs: None,
    )

    store = _FakeStore()
    env = type("OrphanEnv", (), {})()
    env.Session = session_factory
    env.store = store
    env.service = LibraryIndexService(store=store)
    env.root = tmp_path / "library"
    env.root.mkdir()
    yield env
    engine.dispose()


def _seed(
    env,
    *,
    building_generation=3,
    active_generation=2,
    status="syncing",
    heartbeat_age_seconds=None,
    generation_state="building",
    accepted_seq=0,
    materialized_seq=0,
) -> None:
    """写入一行状态 + 一行候选 generation。heartbeat_age_seconds=None 表示当前。"""
    now = get_local_now()
    heartbeat_at = (
        now
        if heartbeat_age_seconds is None
        else now - timedelta(seconds=heartbeat_age_seconds)
    )
    db = env.Session()
    try:
        db.add(LibraryIndexStatus(
            library_id=LIBRARY_ID,
            status=status,
            watcher_mode="watchdog",
            accepted_seq=accepted_seq,
            materialized_seq=materialized_seq,
            state_revision=1,
            view_revision=1,
            active_generation=active_generation,
            building_generation=building_generation,
            catchup_state="rebuilding",
            materializer_epoch=0,
            updated_at=int(time.time() * 1000),
        ))
        if building_generation is not None:
            db.add(LibraryIndexGeneration(
                id=1,
                library_id=LIBRARY_ID,
                generation=building_generation,
                state=generation_state,
                build_base_seq=0,
                reconciled_seq=0,
                created_at=heartbeat_at,
                updated_at=heartbeat_at,
            ))
        db.commit()
    finally:
        db.close()


def _status_row(env):
    db = env.Session()
    try:
        return db.query(LibraryIndexStatus).filter_by(library_id=LIBRARY_ID).one()
    finally:
        db.close()


def _generation_row(env, generation=3):
    db = env.Session()
    try:
        return db.query(LibraryIndexGeneration).filter_by(
            library_id=LIBRARY_ID,
            generation=generation,
        ).one()
    finally:
        db.close()


def test_stale_building_generation_is_reclaimed(orphan_env):
    env = orphan_env
    _seed(env, heartbeat_age_seconds=3600)

    recovered = env.service.recover_orphaned_building_generations()

    assert recovered == [{"library_id": LIBRARY_ID, "generation": 3}]
    row = _status_row(env)
    assert row.building_generation is None
    assert row.status == "ready"
    assert row.catchup_state == "idle"
    assert row.error == ORPHANED_BUILDING_GENERATION_MESSAGE
    candidate = _generation_row(env)
    assert candidate.state == "failed"
    assert candidate.error == ORPHANED_BUILDING_GENERATION_MESSAGE
    assert candidate.delete_after is not None


def test_fresh_heartbeat_is_kept(orphan_env):
    env = orphan_env
    _seed(env, heartbeat_age_seconds=10)

    assert env.service.recover_orphaned_building_generations() == []
    row = _status_row(env)
    assert row.building_generation == 3
    assert row.status == "syncing"
    assert _generation_row(env).state == "building"


def test_grace_boundary_uses_last_heartbeat(orphan_env):
    env = orphan_env
    _seed(env, heartbeat_age_seconds=301)

    assert env.service.recover_orphaned_building_generations(grace_seconds=300.0)
    assert _status_row(env).building_generation is None


def test_running_rebuild_task_prevents_reclaim(orphan_env):
    env = orphan_env
    _seed(env, heartbeat_age_seconds=7200)
    env.service._has_running_rebuild_task = lambda _library_id: True

    assert env.service.recover_orphaned_building_generations() == []
    assert _status_row(env).building_generation == 3


def test_held_rebuild_lock_prevents_reclaim(orphan_env):
    env = orphan_env
    _seed(env, heartbeat_age_seconds=7200)
    lock = env.service._get_lock(LIBRARY_ID)
    lock.acquire()
    try:
        assert env.service.recover_orphaned_building_generations() == []
        assert _status_row(env).building_generation == 3
    finally:
        lock.release()


def test_missing_candidate_row_is_treated_as_orphan(orphan_env):
    env = orphan_env
    _seed(env, heartbeat_age_seconds=None)
    db = env.Session()
    try:
        db.query(LibraryIndexGeneration).delete()
        db.commit()
    finally:
        db.close()

    recovered = env.service.recover_orphaned_building_generations()
    assert recovered == [{"library_id": LIBRARY_ID, "generation": 3}]
    assert _status_row(env).building_generation is None


def test_recover_is_noop_without_building_generation(orphan_env):
    env = orphan_env
    _seed(env, building_generation=None, status="ready")

    assert env.service.recover_orphaned_building_generations() == []
    assert _status_row(env).building_generation is None


def test_recover_keeps_catching_up_when_seq_pending(orphan_env):
    env = orphan_env
    _seed(env, heartbeat_age_seconds=3600, accepted_seq=12, materialized_seq=5)

    env.service.recover_orphaned_building_generations()

    row = _status_row(env)
    assert row.building_generation is None
    assert row.catchup_state == "catching_up"


def test_recover_swallows_database_errors(orphan_env, monkeypatch):
    env = orphan_env
    _seed(env, heartbeat_age_seconds=3600)

    def broken_session():
        raise RuntimeError("db down")

    monkeypatch.setattr(service_module, "SessionLocal", broken_session)
    # 列清单就失败：不能把启动流程带崩。
    assert env.service.recover_orphaned_building_generations() == []


def test_create_building_generation_self_heals_orphan(orphan_env):
    env = orphan_env
    _seed(env, heartbeat_age_seconds=3600)

    generation, base_seq = env.service._create_building_generation(
        LIBRARY_ID,
        str(env.root),
    )

    assert generation == 4
    assert base_seq == 0
    row = _status_row(env)
    assert row.building_generation == 4
    assert row.status == "syncing"
    assert row.error is None
    old = _generation_row(env, generation=3)
    assert old.state == "failed"


def test_create_building_generation_rejects_live_build(orphan_env):
    env = orphan_env
    _seed(env, heartbeat_age_seconds=5)

    with pytest.raises(RuntimeError, match="已有 building generation"):
        env.service._create_building_generation(LIBRARY_ID, str(env.root))

    assert _status_row(env).building_generation == 3


def test_touch_building_generation_refreshes_candidate(orphan_env):
    env = orphan_env
    _seed(env, heartbeat_age_seconds=600)

    env.service._touch_building_generation(LIBRARY_ID, 3)

    candidate = _generation_row(env)
    assert (get_local_now() - candidate.updated_at).total_seconds() < 60


def test_touch_building_generation_fences_reclaimed_candidate(orphan_env):
    env = orphan_env
    _seed(env, heartbeat_age_seconds=600, generation_state="failed")

    with pytest.raises(RuntimeError, match="已被回收"):
        env.service._touch_building_generation(LIBRARY_ID, 3)


def test_heartbeat_is_not_refreshed_by_status_writes(orphan_env):
    """status 行被 materializer 刷新不应延长候选代寿命。"""
    env = orphan_env
    _seed(env, heartbeat_age_seconds=3600)

    db = env.Session()
    try:
        db.query(LibraryIndexStatus).filter_by(library_id=LIBRARY_ID).update(
            {LibraryIndexStatus.updated_at: int(time.time() * 1000)},
            synchronize_session=False,
        )
        db.commit()
    finally:
        db.close()

    assert env.service.recover_orphaned_building_generations()


def test_transient_generation_block_is_classified():
    from app.core.library_index.watcher_driver import _is_transient_generation_block

    assert _is_transient_generation_block(RuntimeError("当前库存已有 building generation"))
    assert _is_transient_generation_block(
        RuntimeError("当前库存有 prepared mutation，暂不能开始重建")
    )
    assert not _is_transient_generation_block(RuntimeError("scan failed"))
    assert not _is_transient_generation_block(OSError("disk full"))
