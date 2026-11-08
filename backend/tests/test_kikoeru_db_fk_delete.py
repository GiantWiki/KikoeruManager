"""Kikoeru 数据库删除的外键行为测试（真实外键声明，--noconftest 可独立运行）

复刻 kikoeru-express 的建库声明：r_tag_work 两个外键都没有 on delete cascade，
r_va_work / t_review 声明了 CASCADE，t_work.circle_id 无级联且 NOT NULL。
写入连接带 ``PRAGMA foreign_keys=ON``，所以不先清关联行就会
``FOREIGN KEY constraint failed``。

运行: cd backend && python -m pytest --noconftest tests/test_kikoeru_db_fk_delete.py -q
"""
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import kikoeru_db_service as kdb  # noqa: E402
from app.core.kikoeru_db_service import KikoeruDbError, KikoeruDbService  # noqa: E402


@pytest.fixture()
def fk_env(tmp_path, monkeypatch):
    db_path = tmp_path / "kikoeru_fk.sqlite3"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE t_circle (id integer primary key autoincrement, name text not null);
        CREATE TABLE t_work (
            id integer primary key autoincrement,
            title text not null,
            dir text not null,
            circle_id integer not null,
            FOREIGN KEY (circle_id) REFERENCES t_circle(id)
        );
        CREATE TABLE t_tag (id integer primary key autoincrement, name text not null);
        CREATE TABLE t_va (id text primary key, name text not null);
        CREATE TABLE r_tag_work (
            tag_id integer,
            work_id integer,
            PRIMARY KEY (tag_id, work_id),
            FOREIGN KEY (tag_id) REFERENCES t_tag(id),
            FOREIGN KEY (work_id) REFERENCES t_work(id)
        );
        CREATE TABLE r_va_work (
            va_id text,
            work_id integer,
            PRIMARY KEY (va_id, work_id),
            FOREIGN KEY (va_id) REFERENCES t_va(id) ON DELETE CASCADE,
            FOREIGN KEY (work_id) REFERENCES t_work(id) ON DELETE CASCADE
        );
        CREATE TABLE t_track (
            id integer primary key autoincrement,
            work_id integer,
            title text,
            FOREIGN KEY (work_id) REFERENCES t_work(id)
        );
        CREATE TABLE t_review (
            user_name text,
            work_id text,
            rating integer,
            PRIMARY KEY (user_name, work_id),
            FOREIGN KEY (work_id) REFERENCES t_work(id) ON DELETE CASCADE
        );

        INSERT INTO t_circle (id, name) VALUES (1, 'サークルA');
        INSERT INTO t_tag (id, name) VALUES (1, 'tag1'), (2, 'tag2');
        INSERT INTO t_va (id, name) VALUES ('va-1', '声優A');
        INSERT INTO t_work (id, title, dir, circle_id) VALUES (1, '作品A', 'RJ000001', 1);
        INSERT INTO r_tag_work (tag_id, work_id) VALUES (1, 1), (2, 1);
        INSERT INTO r_va_work (va_id, work_id) VALUES ('va-1', 1);
        """
    )
    conn.commit()
    conn.close()

    fake_cfg = SimpleNamespace(
        rename=SimpleNamespace(template="{rjcode} {work_name}"),
        kikoeru_db=SimpleNamespace(
            enabled=True, db_path=str(db_path), backup_dir=str(backup_dir),
            backup_interval_hours=24.0, backup_retention=5, snapshot_retention=5,
            scan_listen_enabled=False, scan_poll_interval_minutes=30,
            scan_checkpoint='', last_scan_finished_at=''),
    )
    monkeypatch.setattr(kdb, "get_config", lambda: fake_cfg)
    return SimpleNamespace(service=KikoeruDbService(), db_path=db_path)


def _rows(env, table):
    return env.service.query_table(table)["rows"]


# ------------------------------------------------------------ 关系表自动清理
def test_delete_work_cleans_tag_relations(fk_env):
    """删作品：r_tag_work 无级联外键，必须先清关联行，否则 FK 失败。"""
    result = fk_env.service.delete_row("t_work", 1)

    assert result["deleted"] == 1
    assert _rows(fk_env, "r_tag_work") == []
    cascaded = {item["table"]: item["deleted"] for item in result["cascaded"]}
    assert cascaded["r_tag_work"] == 2
    assert _rows(fk_env, "t_work") == []


def test_delete_tag_cleans_relations(fk_env):
    """删标签同样受 r_tag_work.tag_id 无级联外键影响。"""
    result = fk_env.service.delete_row("t_tag", 1)

    assert result["deleted"] == 1
    assert {(r["tag_id"], r["work_id"]) for r in _rows(fk_env, "r_tag_work")} == {(2, 1)}


def test_delete_work_lets_sqlite_cascade_declared_children(fk_env):
    """r_va_work 声明了 ON DELETE CASCADE，交给 SQLite，不重复计入 cascaded。"""
    result = fk_env.service.delete_row("t_work", 1)

    assert _rows(fk_env, "r_va_work") == []
    assert "r_va_work" not in {item["table"] for item in result.get("cascaded", [])}


# ------------------------------------------------------------ 非级联外键阻断
def test_delete_circle_in_use_returns_readable_409(fk_env):
    with pytest.raises(KikoeruDbError) as exc_info:
        fk_env.service.delete_row("t_circle", 1)

    assert exc_info.value.status == 409
    message = str(exc_info.value)
    assert "t_work.circle_id" in message
    assert "ON DELETE" in message
    # 事务回滚：作品还在
    assert len(_rows(fk_env, "t_work")) == 1


def test_delete_work_with_track_rows_returns_readable_409(fk_env):
    conn = sqlite3.connect(str(fk_env.db_path))
    conn.executemany("INSERT INTO t_track (work_id, title) VALUES (1, ?)", [("01",), ("02",)])
    conn.commit()
    conn.close()

    with pytest.raises(KikoeruDbError) as exc_info:
        fk_env.service.delete_row("t_work", 1)

    assert exc_info.value.status == 409
    message = str(exc_info.value)
    assert "t_track.work_id 2 行" in message
    assert "只读表" in message
    assert len(_rows(fk_env, "t_work")) == 1
    # 阻断时不能留下"关系表已被清掉"的副作用
    assert len(_rows(fk_env, "r_tag_work")) == 2


def test_delete_work_without_tracks_reports_zero(fk_env):
    result = fk_env.service.delete_row("t_work", 1)

    assert result["related_tracks"] == 0
    assert result["track_on_delete"] is None
    assert "cache_note" in result


# ------------------------------------------------------------ FK 预检漏判兜底
def test_fk_error_falls_back_to_readable_message(fk_env, monkeypatch):
    """预检漏判时（新增表/父列名缺失），不能把裸 FOREIGN KEY 错误抛成 500。"""
    monkeypatch.setattr(
        KikoeruDbService, "_inspect_fk_dependents", staticmethod(lambda *a, **k: [])
    )

    with pytest.raises(KikoeruDbError) as exc_info:
        fk_env.service.delete_row("t_work", 1)

    assert exc_info.value.status == 409
    assert "外键" in str(exc_info.value)
    assert "事务" not in str(exc_info.value)


def test_junction_detection(fk_env):
    conn = sqlite3.connect(str(fk_env.db_path))
    conn.row_factory = sqlite3.Row
    try:
        assert KikoeruDbService._is_junction_table(conn, "r_tag_work") is True
        assert KikoeruDbService._is_junction_table(conn, "r_va_work") is True
        assert KikoeruDbService._is_junction_table(conn, "t_review") is False
        assert KikoeruDbService._is_junction_table(conn, "t_work") is False
    finally:
        conn.close()
