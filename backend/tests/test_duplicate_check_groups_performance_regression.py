"""查重列表的查询次数与事件循环隔离回归测试（不连接真实数据库）。"""

import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import app.api.routes as routes  # noqa: E402


class _RecordingDB:
    def __init__(self, rows, *, scalar_result=0):
        self.rows = list(rows)
        self.scalar_result = scalar_result
        self.scalar_calls = 0
        self.all_calls = 0
        self.execution_thread_ids = set()
        self.filters = []
        self.orders = []
        self.offsets = []
        self.limits = []

    def query(self, *_columns):
        return _RecordingQuery(self)

    def close(self):
        pass


class _RecordingQuery:
    def __init__(self, db):
        self.db = db

    def filter(self, clause):
        self.db.filters.append(clause)
        return self

    def group_by(self, *_columns):
        return self

    def having(self, *_clauses):
        return self

    def select_from(self, *_clauses):
        return self

    def subquery(self):
        return self

    def scalar(self):
        self.db.scalar_calls += 1
        self.db.execution_thread_ids.add(threading.get_ident())
        return self.db.scalar_result

    def order_by(self, *clauses):
        self.db.orders.extend(clauses)
        return self

    def offset(self, value):
        self.db.offsets.append(value)
        return self

    def limit(self, value):
        self.db.limits.append(value)
        return self

    def all(self):
        self.db.all_calls += 1
        self.db.execution_thread_ids.add(threading.get_ident())
        return list(self.db.rows)


def _group_row(*, total_count=4):
    return SimpleNamespace(
        rjcode="RJ123456",
        version_count=2,
        library_count=2,
        file_count=10,
        total_size=2048,
        max_mtime=123456,
        root_paths=["library/RJ123456", "backup/RJ123456"],
        total_count=total_count,
    )


def _patch_dependencies(monkeypatch, db):
    from app.models import database as dbmod

    monkeypatch.setattr(routes, "_duplicate_excluded_library_ids", lambda: set())
    monkeypatch.setattr(routes, "_duplicate_input_library_id", lambda: "")
    return mock.patch.object(dbmod, "get_db", lambda: iter([db]))


@pytest.mark.asyncio
async def test_normal_page_executes_one_aggregate_query(monkeypatch):
    """正常分页从窗口总数取 total，不再重复执行整组聚合。"""
    db = _RecordingDB([_group_row(total_count=4)], scalar_result=4)

    with _patch_dependencies(monkeypatch, db):
        response = await routes.get_duplicate_groups(page=1, page_size=20)

    assert db.all_calls == 1
    assert db.scalar_calls == 0
    assert response.total == 4
    assert response.page == 1
    assert response.page_size == 20
    assert len(response.groups) == 1
    assert response.groups[0].rjcode == "RJ123456"
    assert response.groups[0].names == ["RJ123456"]
    assert response.groups[0].project_name == "RJ123456"


@pytest.mark.asyncio
async def test_database_work_does_not_run_on_event_loop_thread(monkeypatch):
    """同步 SQLAlchemy 工作必须移出 async 路由所在的事件循环线程。"""
    db = _RecordingDB([_group_row()], scalar_result=4)
    event_loop_thread = threading.get_ident()

    with _patch_dependencies(monkeypatch, db):
        await routes.get_duplicate_groups(page=1, page_size=20)

    assert db.execution_thread_ids
    assert event_loop_thread not in db.execution_thread_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("sort", ["version_count", "total_size", "rjcode", "max_mtime"])
async def test_sort_search_and_pagination_contract_is_preserved(monkeypatch, sort):
    db = _RecordingDB([_group_row()], scalar_result=4)

    with _patch_dependencies(monkeypatch, db):
        response = await routes.get_duplicate_groups(
            page=2,
            page_size=5,
            sort=sort,
            search="123",
        )

    assert response.page == 2
    assert response.page_size == 5
    assert db.offsets == [5]
    assert db.limits == [5]
    assert len(db.orders) == 1
    assert any("LIKE" in str(clause).upper() for clause in db.filters)


@pytest.mark.asyncio
async def test_empty_first_page_skips_count_query(monkeypatch):
    db = _RecordingDB([], scalar_result=0)

    with _patch_dependencies(monkeypatch, db):
        response = await routes.get_duplicate_groups(page=1, page_size=20, search="missing")

    assert response.total == 0
    assert response.groups == []
    assert db.scalar_calls == 0


@pytest.mark.asyncio
async def test_empty_out_of_range_page_preserves_total(monkeypatch):
    """窗口函数在空页没有行可携带总数时，回退到原 count 查询。"""
    db = _RecordingDB([], scalar_result=9)

    with _patch_dependencies(monkeypatch, db):
        response = await routes.get_duplicate_groups(page=3, page_size=20)

    assert response.total == 9
    assert response.groups == []
    assert db.scalar_calls == 1
