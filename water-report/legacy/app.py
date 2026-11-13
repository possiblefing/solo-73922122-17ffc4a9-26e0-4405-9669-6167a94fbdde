"""模拟"只有网页表单"的旧水质数据报送站。

特点（用于演练真实场景）：
- 仅提供浏览器页面：查询页 /search、录入页 /new、保存 /save，没有数据 API；
- 允许 (样本号, 项目) 业务键重复入库（无唯一约束）；
- 内置"保存后断连"开关：开启后所有 POST 返回 500 错误页；
  其中 /save 会先提交数据再返回断连页，模拟"保存成功但回执丢失"；
- SQLite 数据文件落在挂载卷 /data，容器重建后数据保留。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

DB_PATH = os.environ.get("LEGACY_DB", "/data/legacy.db")
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="legacy-water-station", docs_url=None, redoc_url=None)


def get_db() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS records(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sample_no TEXT NOT NULL,
            project   TEXT NOT NULL,
            result    TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_state(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _flag(conn: sqlite3.Connection, key: str) -> bool:
    row = conn.execute(
        "SELECT value FROM app_state WHERE key=?", (key,)
    ).fetchone()
    return bool(row and row["value"] == "1")


def disconnect_save_on(conn: sqlite3.Connection) -> bool:
    """保存后断连：/save 先落库再返回错误页（回执丢失）。"""
    return _flag(conn, "disconnect_save")


def disconnect_all_on(conn: sqlite3.Connection) -> bool:
    """全站断连：所有 POST（含查询）都失败。"""
    return _flag(conn, "disconnect_all")


def set_flag(conn: sqlite3.Connection, key: str, enabled: bool) -> None:
    conn.execute(
        "INSERT INTO app_state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, "1" if enabled else "0"),
    )
    conn.commit()


def flags_view(conn: sqlite3.Connection) -> dict[str, bool]:
    return {
        "disconnect": disconnect_save_on(conn) or disconnect_all_on(conn),
        "disconnect_save": disconnect_save_on(conn),
        "disconnect_all": disconnect_all_on(conn),
    }


def error_page(request: Request, message: str, status: int = 500) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "error.html",
        {"message": message, "disconnect": True},
        status_code=status,
    )


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) c FROM records").fetchone()["c"]
        return templates.TemplateResponse(
            request,
            "index.html",
            {**flags_view(conn), "total": total},
        )
    finally:
        conn.close()


@app.post("/__admin/toggle-disconnect")
def toggle_disconnect(mode: str = Form("off")):
    """演练用管理员开关，不属于报送数据接口。

    mode=save：仅"保存后断连"（/save 先落库再返回错误页，模拟回执丢失）
    mode=all ：全站断连（查询也失败，用于演练查询失败不得当成不存在）
    mode=off ：恢复正常
    """
    conn = get_db()
    try:
        set_flag(conn, "disconnect_save", mode == "save")
        set_flag(conn, "disconnect_all", mode == "all")
    finally:
        conn.close()
    return RedirectResponse(url="/", status_code=303)


@app.get("/search", response_class=HTMLResponse)
def search_form(request: Request):
    conn = get_db()
    try:
        return templates.TemplateResponse(
            request,
            "search.html",
            {**flags_view(conn), "rows": None,
             "sample_no": "", "project": ""},
        )
    finally:
        conn.close()


@app.post("/search", response_class=HTMLResponse)
def search_submit(
    request: Request,
    sample_no: str = Form(...),
    project: str = Form(...),
):
    conn = get_db()
    try:
        if disconnect_all_on(conn):
            # 查询失败：明确的错误页，调用方绝不能把它当成"查询无结果"。
            return error_page(request, "网络连接中断，查询失败，请稍后重试。")
        rows = conn.execute(
            "SELECT * FROM records WHERE sample_no=? AND project=? "
            "ORDER BY id ASC",
            (sample_no.strip(), project.strip()),
        ).fetchall()
        return templates.TemplateResponse(
            request,
            "search.html",
            {"disconnect": False, "rows": rows,
             "sample_no": sample_no, "project": project},
        )
    finally:
        conn.close()


@app.get("/new", response_class=HTMLResponse)
def new_form(request: Request):
    conn = get_db()
    try:
        return templates.TemplateResponse(
            request,
            "new.html",
            {**flags_view(conn), "errors": [],
             "sample_no": "", "project": "", "result": ""},
        )
    finally:
        conn.close()


@app.post("/save")
def save_record(
    request: Request,
    sample_no: str = Form(...),
    project: str = Form(...),
    result: str = Form(...),
):
    conn = get_db()
    try:
        errors = []
        sample_no, project, result = sample_no.strip(), project.strip(), result.strip()
        if not sample_no:
            errors.append("样本号不能为空")
        if not project:
            errors.append("项目不能为空")
        try:
            value = Decimal(result)
            if value < 0 or not value.is_finite():
                errors.append("结果必须是非负十进制数")
        except InvalidOperation:
            errors.append("结果必须是非负十进制数")
        if errors:
            return templates.TemplateResponse(
                request, "new.html",
                {**flags_view(conn), "errors": errors,
                 "sample_no": sample_no, "project": project, "result": result},
                status_code=400,
            )

        cur = conn.execute(
            "INSERT INTO records(sample_no, project, result, created_at) "
            "VALUES(?,?,?,?)",
            (sample_no, project, result, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()  # 先落库：旧站允许重复键，插入永远成功
        record_id = cur.lastrowid

        if disconnect_save_on(conn) or disconnect_all_on(conn):
            # 数据已保存，但浏览器拿到的是断连错误 -> 回执丢失场景。
            return error_page(
                request,
                "记录已写入服务器，但保存后网络连接中断，回执丢失。",
                status_code=500,
            )
        return RedirectResponse(url=f"/saved/{record_id}", status_code=303)
    finally:
        conn.close()


@app.get("/saved/{record_id}", response_class=HTMLResponse)
def saved(request: Request, record_id: int):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM records WHERE id=?", (record_id,)
        ).fetchone()
        if row is None:
            return error_page(request, "记录不存在", status_code=404)
        return templates.TemplateResponse(
            request, "saved.html",
            {**flags_view(conn), "row": row},
        )
    finally:
        conn.close()


@app.get("/records", response_class=HTMLResponse)
def list_records(request: Request):
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM records ORDER BY id").fetchall()
        return templates.TemplateResponse(
            request, "records.html",
            {**flags_view(conn), "rows": rows},
        )
    finally:
        conn.close()


@app.get("/records.json")
def records_json():
    """仅供演示核对与自动化测试使用，报送流程本身不调用它。"""
    conn = get_db()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, sample_no, project, result, created_at "
            "FROM records ORDER BY id").fetchall()]
        return JSONResponse(rows)
    finally:
        conn.close()
