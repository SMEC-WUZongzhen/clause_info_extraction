"""history_store.py — 历史记录 SQLite 存储层
============================================
文件路径: web_ui/history.db
- WAL 模式提升并发性能
- 列表接口仅返回摘要（不含大 JSON 字段）
- get_record / get_records_by_ids 返回完整数据
- 支持 operation_type 分类：extract / analyze
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, List, Optional


class DuplicateRecordError(Exception):
    """主键冲突时抛出，由上层 API 转 409。"""


_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.db")
MAX_RECORDS = 500  # 存储上限，超出则淘汰最旧记录

_CREATE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS history (
    id                 TEXT PRIMARY KEY,
    created_at         TEXT NOT NULL,
    filename           TEXT DEFAULT '',
    contract_type      TEXT DEFAULT '',
    contract_type_label TEXT DEFAULT '',
    type_summary       TEXT DEFAULT '-',
    ratio_summary      TEXT DEFAULT '-',
    amount_summary     TEXT DEFAULT '-',
    step2_elapsed      REAL,
    step1_data         TEXT DEFAULT '',
    step2_data         TEXT DEFAULT '',
    payment_items      TEXT DEFAULT '[]',
    warranty_items     TEXT DEFAULT '[]',
    operation_type     TEXT DEFAULT 'extract',
    gt_data            TEXT DEFAULT '',
    compare_data       TEXT DEFAULT ''
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """建表（如不存在），并兼容旧数据库添加新列。"""
    conn = _connect()
    try:
        conn.execute(_CREATE_TABLE_SQL)
        # 兼容旧数据库：尝试添加新列（若已存在则忽略错误）
        for col_sql in (
            "ALTER TABLE history ADD COLUMN operation_type TEXT DEFAULT 'extract'",
            "ALTER TABLE history ADD COLUMN gt_data TEXT DEFAULT ''",
            "ALTER TABLE history ADD COLUMN compare_data TEXT DEFAULT ''",
        ):
            try:
                conn.execute(col_sql)
            except sqlite3.OperationalError:
                pass  # 列已存在
        conn.commit()
    finally:
        conn.close()


# ---------- 摘要字段列表（list_records 使用） ----------
_SUMMARY_COLS = (
    "id, created_at, filename, contract_type, contract_type_label, "
    "type_summary, ratio_summary, amount_summary, step2_elapsed, operation_type"
)


def _row_to_summary(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "createdAt": row["created_at"],
        "filename": row["filename"],
        "contractType": row["contract_type"],
        "contractTypeLabel": row["contract_type_label"],
        "typeSummary": row["type_summary"],
        "ratioSummary": row["ratio_summary"],
        "amountSummary": row["amount_summary"],
        "step2Elapsed": row["step2_elapsed"],
        "operationType": row["operation_type"],
    }


def _row_to_full(row: sqlite3.Row) -> Dict[str, Any]:
    d = _row_to_summary(row)
    d["step1Data"] = json.loads(row["step1_data"]) if row["step1_data"] else None
    d["step2Data"] = json.loads(row["step2_data"]) if row["step2_data"] else None
    d["paymentItems"] = json.loads(row["payment_items"]) if row["payment_items"] else []
    d["warrantyItems"] = json.loads(row["warranty_items"]) if row["warranty_items"] else []
    d["gtData"] = json.loads(row["gt_data"]) if row["gt_data"] else None
    d["compareData"] = json.loads(row["compare_data"]) if row["compare_data"] else None
    return d


# ---------- CRUD ----------

def list_records(operation_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """返回记录摘要（按 created_at 倒序）。

    Args:
        operation_type: 过滤操作类型，None 则返回全部
    """
    conn = _connect()
    try:
        if operation_type:
            rows = conn.execute(
                f"SELECT {_SUMMARY_COLS} FROM history "
                "WHERE operation_type = ? ORDER BY created_at DESC",
                (operation_type,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_SUMMARY_COLS} FROM history ORDER BY created_at DESC"
            ).fetchall()
        return [_row_to_summary(r) for r in rows]
    finally:
        conn.close()


def get_record(record_id: str) -> Optional[Dict[str, Any]]:
    """返回单条完整记录，不存在返回 None。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM history WHERE id = ?", (record_id,)
        ).fetchone()
        return _row_to_full(row) if row else None
    finally:
        conn.close()


def get_records_by_ids(ids: List[str]) -> List[Dict[str, Any]]:
    """批量获取完整记录（保持传入顺序）。"""
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM history WHERE id IN ({placeholders})", ids
        ).fetchall()
        # 按传入 ids 顺序排列
        by_id = {r["id"]: _row_to_full(r) for r in rows}
        return [by_id[i] for i in ids if i in by_id]
    finally:
        conn.close()


def _dump_optional_json(value: Any) -> str:
    """None → ""；其它值 → json 字符串。避免 'null' 与空值混淆。"""
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


def create_record(data: Dict[str, Any]) -> None:
    """插入一条历史记录。

    Raises:
        DuplicateRecordError: 当 id 已存在时抛出（不再静默覆盖）。
    """
    conn = _connect()
    try:
        try:
            conn.execute(
                """INSERT INTO history
                   (id, created_at, filename, contract_type, contract_type_label,
                    type_summary, ratio_summary, amount_summary, step2_elapsed,
                    step1_data, step2_data, payment_items, warranty_items,
                    operation_type, gt_data, compare_data)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    data.get("id", ""),
                    data.get("createdAt", ""),
                    data.get("filename", ""),
                    data.get("contractType", ""),
                    data.get("contractTypeLabel", ""),
                    data.get("typeSummary", "-"),
                    data.get("ratioSummary", "-"),
                    data.get("amountSummary", "-"),
                    data.get("step2Elapsed"),
                    _dump_optional_json(data.get("step1Data")),
                    _dump_optional_json(data.get("step2Data")),
                    json.dumps(data.get("paymentItems", []), ensure_ascii=False),
                    json.dumps(data.get("warrantyItems", []), ensure_ascii=False),
                    data.get("operationType", "extract"),
                    _dump_optional_json(data.get("gtData")),
                    _dump_optional_json(data.get("compareData")),
                ),
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateRecordError(str(e)) from e
        conn.commit()
        # FIFO 淘汰：超出上限时删除最旧记录
        count = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
        if count > MAX_RECORDS:
            conn.execute(
                "DELETE FROM history WHERE id IN "
                "(SELECT id FROM history ORDER BY created_at ASC LIMIT ?)",
                (count - MAX_RECORDS,),
            )
            conn.commit()
    finally:
        conn.close()


def delete_record(record_id: str) -> bool:
    """删除单条记录，返回是否实际删除。"""
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM history WHERE id = ?", (record_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_all(operation_type: Optional[str] = None) -> int:
    """清空记录，返回删除行数。

    Args:
        operation_type: 仅清空指定类型，None 则清空全部
    """
    conn = _connect()
    try:
        if operation_type:
            cur = conn.execute(
                "DELETE FROM history WHERE operation_type = ?",
                (operation_type,),
            )
        else:
            cur = conn.execute("DELETE FROM history")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
