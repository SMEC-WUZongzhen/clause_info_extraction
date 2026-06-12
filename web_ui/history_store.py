"""history_store.py — 历史记录 SQLite 存储层
============================================
文件路径: web_ui/history.db
- WAL 模式提升并发性能
- 列表接口仅返回摘要（不含大 JSON 字段）
- get_record / get_records_by_ids 返回完整数据
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, List, Optional


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
    warranty_items     TEXT DEFAULT '[]'
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """建表（如不存在），启动时调用一次。"""
    conn = _connect()
    try:
        conn.execute(_CREATE_TABLE_SQL)
        conn.commit()
    finally:
        conn.close()


# ---------- 摘要字段列表（list_records 使用） ----------
_SUMMARY_COLS = (
    "id, created_at, filename, contract_type, contract_type_label, "
    "type_summary, ratio_summary, amount_summary, step2_elapsed"
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
    }


def _row_to_full(row: sqlite3.Row) -> Dict[str, Any]:
    d = _row_to_summary(row)
    d["step1Data"] = json.loads(row["step1_data"]) if row["step1_data"] else None
    d["step2Data"] = json.loads(row["step2_data"]) if row["step2_data"] else None
    d["paymentItems"] = json.loads(row["payment_items"]) if row["payment_items"] else []
    d["warrantyItems"] = json.loads(row["warranty_items"]) if row["warranty_items"] else []
    return d


# ---------- CRUD ----------

def list_records() -> List[Dict[str, Any]]:
    """返回所有记录摘要（按 created_at 倒序）。"""
    conn = _connect()
    try:
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


def create_record(data: Dict[str, Any]) -> None:
    """插入一条历史记录。"""
    conn = _connect()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO history
               (id, created_at, filename, contract_type, contract_type_label,
                type_summary, ratio_summary, amount_summary, step2_elapsed,
                step1_data, step2_data, payment_items, warranty_items)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                json.dumps(data.get("step1Data"), ensure_ascii=False) or "",
                json.dumps(data.get("step2Data"), ensure_ascii=False) or "",
                json.dumps(data.get("paymentItems", []), ensure_ascii=False),
                json.dumps(data.get("warrantyItems", []), ensure_ascii=False),
            ),
        )
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


def delete_all() -> int:
    """清空全部记录，返回删除行数。"""
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM history")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
