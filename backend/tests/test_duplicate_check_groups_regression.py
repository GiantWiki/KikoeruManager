"""仓库查重列表端点回归测试（不连数据库）。

背景：afa4086 引入「查重排除」过滤时把 base_q.filter(...) 插到了 base_q
定义之前——只要设置里勾选了「查重排除」（excluded_library_ids 非空），
GET /api/duplicate-check/groups 就直接 UnboundLocalError 500（线上实测，
v2.6.39 镜像 routes.py:23131）。

打桩原则：monkeypatch 掉 get_db / 两个 helper / DuplicateGroupListResponse
响应模型所需的全部 DB 交互，只验证**控制流**：
1. excluded_library_ids 非空时函数能走通（不再 UnboundLocalError）；
2. 排除过滤确实以 ~library_id.in_(...) 形式进入了 base_q 的 where 条件。
"""

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import app.api.routes as routes  # noqa: E402


class _FakeColumn:
    """够用的 SQLAlchemy 列替身：支持 .in_/.isnot/.ilike/.is_/比较等链式调用。"""

    def __init__(self, name="col"):
        self.name = name

    def in_(self, _):
        return _FakeExpr(f"{self.name}:in")

    def notin_(self, _):
        return ("notin", self.name)

    def isnot(self, _):
        return ("isnot", self.name)

    def is_(self, _):
        return ("is", self.name)

    def asc(self):
        return _FakeExpr(f"{self.name}:asc")

    def desc(self):
        return _FakeExpr(f"{self.name}:desc")

    def ilike(self, _):
        return ("ilike", self.name)

    def __eq__(self, _):
        return ("eq", self.name)

    def __ne__(self, _):
        return ("ne", self.name)

    def __gt__(self, _):
        return ("gt", self.name)

    def __hash__(self):
        return hash(self.name)


class _FakeFunc:
    """func.xxx(...) 替身：返回带 label 的哑表达式。"""

    def __getattr__(self, fname):
        def _call(*_args, **_kwargs):
            return _FakeExpr(fname)

        return _call


class _FakeExpr:
    def __init__(self, tag):
        self.tag = tag

    def label(self, name):
        return _FakeExpr(f"{self.tag}:{name}")

    def over(self):
        return _FakeExpr(f"{self.tag}:over")

    def desc(self):
        return self

    def asc(self):
        return self

    def nullslast(self):
        return self

    def isnot(self, _):
        return ("isnot", self.tag)

    def is_(self, _):
        return ("is", self.tag)

    def op(self, _opname):
        return lambda _arg: _FakeExpr("op")

    def __invert__(self):
        return _FakeExpr("not")

    def __add__(self, _other):
        return self

    def __sub__(self, _other):
        return self

    def __gt__(self, _):
        return ("gt", self.tag)

    def __hash__(self):
        return hash(self.tag)


class _FakeCase:
    """case((cond, val), ...) → 直接返回最后一个 value 的哑实现。"""

    def __call__(self, *_whens, else_=None):
        return else_ if else_ is not None else _FakeExpr("case")


class _FakeAnd:
    def __call__(self, *args):
        return _FakeExpr("and")


def _make_fake_db():
    """记录 filter 调用的 query 替身；.all() 返回空行列表。"""
    filters = []

    class _Q:
        def query(self, *_cols):
            return self

        def filter(self, cond):
            filters.append(cond)
            return self

        def group_by(self, *_):
            return self

        def having(self, *_):
            return self

        def select_from(self, *_):
            return self

        def scalar(self):
            return 0

        def order_by(self, *_):
            return self

        def offset(self, _):
            return self

        def limit(self, _):
            return self

        def all(self):
            return []

        def subquery(self):
            return "subq"

    class _DB2:
        def __init__(self):
            self.query_obj = _Q()
            self.filters = filters

        def query(self, *_cols):
            return self.query_obj

    # 简化：DB 直接暴露 query
    class _DB:
        def __init__(self):
            self.filters = filters

        def query(self, *_cols):
            return _Q()

        def close(self):
            pass

    return _DB()


@pytest.mark.asyncio
async def test_duplicate_groups_with_excluded_libraries_no_unbound_error(monkeypatch):
    """勾选「查重排除」后（excluded 非空）列表端点不再 UnboundLocalError。"""
    from app.models import database as dbmod

    fake_db = _make_fake_db()

    async def _fake_next(gen):
        return fake_db

    monkeypatch.setattr(
        routes, "_duplicate_excluded_library_ids", lambda: {"lib-excluded"}
    )
    monkeypatch.setattr(routes, "_duplicate_input_library_id", lambda: "")

    captured = {}

    class _FakeResp:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    # func / case / and_ 打桩（sqlalchemy 惰性构造，不触方言）
    monkeypatch.setattr(routes, "DuplicateGroupListResponse", _FakeResp)
    with mock.patch.object(routes, "func", _FakeFunc(), create=True), \
            mock.patch.object(routes, "case", _FakeCase(), create=True), \
            mock.patch.object(routes, "and_", _FakeAnd(), create=True), \
            mock.patch.object(
                dbmod, "get_db", lambda: iter([fake_db])
            ), \
            mock.patch.object(
                dbmod.LibraryIndexEntry,
                "rjcode",
                _FakeColumn("rjcode"),
            ), \
            mock.patch.object(
                dbmod.LibraryIndexEntry,
                "relative_path",
                _FakeColumn("relative_path"),
            ), \
            mock.patch.object(
                dbmod.LibraryIndexEntry,
                "library_id",
                _FakeColumn("library_id"),
            ), \
            mock.patch.object(
                dbmod.LibraryIndexEntry,
                "entry_type",
                _FakeColumn("entry_type"),
            ), \
            mock.patch.object(
                dbmod.LibraryIndexEntry,
                "parent_path",
                _FakeColumn("parent_path"),
            ), \
            mock.patch.object(
                dbmod.LibraryIndexEntry,
                "size",
                _FakeColumn("size"),
            ), \
            mock.patch.object(
                dbmod.LibraryIndexEntry,
                "file_count",
                _FakeColumn("file_count"),
            ), \
            mock.patch.object(
                dbmod.LibraryIndexEntry,
                "mtime",
                _FakeColumn("mtime"),
            ):
        resp = await routes.get_duplicate_groups(page=1, page_size=20)
    assert resp is not None
    # 走通即证明 UnboundLocalError 已修复；再验证排除过滤确实进了 where：
    # ~in_ 经 __invert__ 产生 not 包装（tag 会丢来源，只验存在 not 前缀过滤）
    filter_tags = [getattr(f, "tag", f) for f in fake_db.filters]
    assert filter_tags[:2] == [("isnot", "rjcode"), ("ne", "rjcode")]
    assert "not" in filter_tags, filter_tags  # 排除库过滤（~in_）进 where


@pytest.mark.asyncio
async def test_duplicate_groups_without_excluded_libraries_ok(monkeypatch):
    """未勾选排除（excluded 为空）时端点照常工作（回归保护）。"""
    from app.models import database as dbmod

    fake_db = _make_fake_db()
    monkeypatch.setattr(routes, "_duplicate_excluded_library_ids", lambda: set())
    monkeypatch.setattr(routes, "_duplicate_input_library_id", lambda: "")

    captured = {}

    class _FakeResp:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(routes, "DuplicateGroupListResponse", _FakeResp)
    with mock.patch.object(routes, "func", _FakeFunc(), create=True), \
            mock.patch.object(routes, "case", _FakeCase(), create=True), \
            mock.patch.object(routes, "and_", _FakeAnd(), create=True), \
            mock.patch.object(
                dbmod, "get_db", lambda: iter([fake_db])
            ), \
            mock.patch.object(
                dbmod.LibraryIndexEntry,
                "rjcode",
                _FakeColumn("rjcode"),
            ), \
            mock.patch.object(
                dbmod.LibraryIndexEntry,
                "relative_path",
                _FakeColumn("relative_path"),
            ), \
            mock.patch.object(
                dbmod.LibraryIndexEntry,
                "library_id",
                _FakeColumn("library_id"),
            ), \
            mock.patch.object(
                dbmod.LibraryIndexEntry,
                "entry_type",
                _FakeColumn("entry_type"),
            ), \
            mock.patch.object(
                dbmod.LibraryIndexEntry,
                "parent_path",
                _FakeColumn("parent_path"),
            ), \
            mock.patch.object(
                dbmod.LibraryIndexEntry,
                "size",
                _FakeColumn("size"),
            ), \
            mock.patch.object(
                dbmod.LibraryIndexEntry,
                "file_count",
                _FakeColumn("file_count"),
            ), \
            mock.patch.object(
                dbmod.LibraryIndexEntry,
                "mtime",
                _FakeColumn("mtime"),
            ):
        resp = await routes.get_duplicate_groups(page=1, page_size=20)
    assert resp is not None
    assert captured.get("groups") == []
    assert captured.get("total") == 0
