"""报送后台本地 SQLite 数据层。

设计要点：
- 本地库只记录"工作进度"，与远端旧站没有任何分布式事务；
  远端保存与本地状态更新是两个独立步骤，绝不放在同一提交里。
- worker_lock 保证每批（实际是整个后台）同一时刻只跑一份浏览器任务；
- 重启恢复由 recover_stale_runs() 完成：中断在 SAVING 的行一律转 UNVERIFIED，
  等待重启后先查询核实，不允许盲目重填。
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

# 行状态
PENDING = "PENDING"                # 待报送
QUERY_FAILED = "QUERY_FAILED"      # 查询失败（绝不能当作"不存在"）
CONFLICT = "CONFLICT"              # 远端已存在异值记录，人工处理，批次停止
UNVERIFIED = "UNVERIFIED"          # 保存后回执丢失/进程退出，待核实
ALREADY_SENT = "ALREADY_SENT"      # 远端已存在同值记录，视为已送
SAVED = "SAVED"                    # 本次确认新增成功，拿到远端记录编号
SAVING = "SAVING"                  # 正在提交（仅瞬时存在，崩溃后被恢复为 UNVERIFIED）

ROW_STATUS_LABELS = {
    PENDING: "待报送",
    QUERY_FAILED: "查询失败",
    CONFLICT: "异值冲突·停批",
    UNVERIFIED: "待核实",
    ALREADY_SENT: "已送（远端同值）",
    SAVED: "已新增",
    SAVING: "提交中",
}

# 批次状态
BATCH_RUNNING = "RUNNING"
BATCH_STOPPED = "STOPPED"
BATCH_DONE = "DONE"

# 续跑时需要处理的行（终态 SAVED / ALREADY_SENT / CONFLICT 不再动）
ACTIONABLE = (PENDING, QUERY_FAILED, UNVERIFIED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rows(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    seq INTEGER NOT NULL,
    sample_no TEXT NOT NULL,
    project TEXT NOT NULL,
    result TEXT NOT NULL,
    status TEXT NOT NULL,
    remote_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    screenshot TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(batch_id, seq)
);
CREATE TABLE IF NOT EXISTS row_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    row_id INTEGER NOT NULL REFERENCES rows(id),
    event TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS worker_lock(
    id INTEGER PRIMARY KEY CHECK (id = 1),
    worker_id TEXT NOT NULL,
    batch_id INTEGER NOT NULL,
    acquired_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


class LockBusy(RuntimeError):
    pass


def acquire_lock(conn: sqlite3.Connection, batch_id: int) -> str:
    worker_id = uuid.uuid4().hex
    try:
        conn.execute(
            "INSERT INTO worker_lock(id, worker_id, batch_id, acquired_at) "
            "VALUES(1, ?, ?, ?)",
            (worker_id, batch_id, now()),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise LockBusy("已有报送任务在运行，每批同一时刻只允许一份") from exc
    return worker_id


def release_lock(conn: sqlite3.Connection, worker_id: str) -> None:
    conn.execute("DELETE FROM worker_lock WHERE worker_id=?", (worker_id,))
    conn.commit()


def current_lock(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM worker_lock WHERE id=1").fetchone()


def recover_stale_runs(conn: sqlite3.Connection) -> list[int]:
    """进程启动时调用：清理上次进程残留的运行态。"""
    recovered: list[int] = []
    lock = current_lock(conn)
    if lock:
        conn.execute("DELETE FROM worker_lock")
        recovered.append(lock["batch_id"])
    cur = conn.execute(
        "UPDATE rows SET status=?, message='进程退出，保存结果待核实', updated_at=? "
        "WHERE status=?",
        (UNVERIFIED, now(), SAVING),
    )
    if cur.rowcount:
        recovered.append(cur.rowcount)
    conn.execute(
        "UPDATE batches SET status=?, note=COALESCE(note,'') || ?, updated_at=? "
        "WHERE status=?",
        (BATCH_STOPPED, f"[{now()}] 进程重启，批次转暂停待续跑；", now(), BATCH_RUNNING),
    )
    conn.commit()
    return recovered


def create_batch(conn: sqlite3.Connection, filename: str) -> int:
    ts = now()
    cur = conn.execute(
        "INSERT INTO batches(filename, status, created_at, updated_at) "
        "VALUES(?,?,?,?)",
        (filename, BATCH_STOPPED, ts, ts),
    )
    conn.commit()
    return cur.lastrowid


def add_row(
    conn: sqlite3.Connection,
    batch_id: int,
    seq: int,
    sample_no: str,
    project: str,
    result: str,
) -> int:
    ts = now()
    cur = conn.execute(
        "INSERT INTO rows(batch_id, seq, sample_no, project, result, status, updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (batch_id, seq, sample_no, project, result, PENDING, ts),
    )
    conn.commit()
    return cur.lastrowid


def set_batch_status(conn: sqlite3.Connection, batch_id: int, status: str,
                     note: str | None = None) -> None:
    if note is None:
        conn.execute(
            "UPDATE batches SET status=?, updated_at=? WHERE id=?",
            (status, now(), batch_id),
        )
    else:
        conn.execute(
            "UPDATE batches SET status=?, note=?, updated_at=? WHERE id=?",
            (status, note, now(), batch_id),
        )
    conn.commit()


def mark_batch_running(conn: sqlite3.Connection, batch_id: int, resume: bool) -> None:
    action = "续跑" if resume else "开始报送"
    conn.execute(
        "UPDATE batches SET status=?, note=COALESCE(note,'') || ?, updated_at=? "
        "WHERE id=?",
        (BATCH_RUNNING, f"[{now()}] {action}；", now(), batch_id),
    )
    conn.commit()


def get_batch(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()


def list_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM batches ORDER BY id DESC").fetchall())


def get_rows(conn: sqlite3.Connection, batch_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM rows WHERE batch_id=? ORDER BY seq", (batch_id,)
    ).fetchall())


def get_row(conn: sqlite3.Connection, row_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM rows WHERE id=?", (row_id,)).fetchone()


def update_row(conn: sqlite3.Connection, row_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(
        f"UPDATE rows SET {cols} WHERE id=?",
        (*fields.values(), row_id),
    )
    conn.commit()


def add_event(conn: sqlite3.Connection, row_id: int, event: str,
              detail: str = "") -> None:
    conn.execute(
        "INSERT INTO row_events(row_id, event, detail, created_at) "
        "VALUES(?,?,?,?)",
        (row_id, event, detail, now()),
    )
    conn.commit()


def get_events(conn: sqlite3.Connection, row_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM row_events WHERE row_id=? ORDER BY id", (row_id,)
    ).fetchall())


def row_counts(conn: sqlite3.Connection, batch_id: int) -> dict[str, int]:
    counts = {s: 0 for s in ROW_STATUS_LABELS}
    for r in conn.execute(
        "SELECT status, COUNT(*) c FROM rows WHERE batch_id=? GROUP BY status",
        (batch_id,),
    ):
        counts[r["status"]] = r["c"]
    return counts
