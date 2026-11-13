"""报送后台 Web 应用（FastAPI）。

职责：上传 CSV 建批、启动/停止/续跑浏览器报送、展示逐行状态与远端记录编号。
对旧站的所有操作都封装在 browser_worker 里，经真实浏览器完成。
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .browser_worker import ReportingWorker, WorkerManager
from .csvkit import CsvError, parse_csv

DB_PATH = os.environ.get("REPORTER_DB", "/data/reporter.db")
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/data/uploads"))
SHOT_DIR = Path(os.environ.get("SCREENSHOT_DIR", "/data/screenshots"))
LEGACY_URL = os.environ.get("LEGACY_URL", "http://legacy:8000")
HEADLESS = os.environ.get("HEADLESS", "1") != "0"
SLOW_MO = int(os.environ.get("SLOW_MO_MS", "0"))
BROWSER_CHANNEL = os.environ.get("BROWSER_CHANNEL") or None

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# 必须在挂载 StaticFiles 前建好目录，否则全新数据卷首启会抛异常
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
SHOT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="water-reporting-console")
app.mount("/screenshots", StaticFiles(directory=str(SHOT_DIR)),
          name="screenshots")

manager = WorkerManager()


def make_worker() -> ReportingWorker:
    return ReportingWorker(
        base_url=LEGACY_URL,
        db_path=DB_PATH,
        screenshot_dir=str(SHOT_DIR),
        headless=HEADLESS,
        slow_mo_ms=SLOW_MO,
        channel=BROWSER_CHANNEL,
    )


@app.on_event("startup")
def _startup() -> None:
    db.init_db(DB_PATH)
    conn = db.connect(DB_PATH)
    try:
        recovered = db.recover_stale_runs(conn)
        if recovered:
            print(f"[startup] 恢复上次异常退出：{recovered}（SAVING 行已转待核实）")
    finally:
        conn.close()


def _row_view(conn, row) -> dict:
    return {
        **dict(row),
        "status_label": db.ROW_STATUS_LABELS.get(row["status"], row["status"]),
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    conn = db.connect(DB_PATH)
    try:
        batches = []
        for b in db.list_batches(conn):
            counts = db.row_counts(conn, b["id"])
            batches.append({
                **dict(b),
                "counts": counts,
                "total": sum(counts.values()),
                "busy": manager.busy and manager.worker
                and manager.worker.snapshot().get("batch_id") == b["id"],
            })
        return templates.TemplateResponse(
            request, "index.html",
            {"batches": batches, "busy": manager.busy,
             "legacy_url": LEGACY_URL,
             "status_labels": db.ROW_STATUS_LABELS},
        )
    finally:
        conn.close()


@app.post("/batches")
async def create_batch(request: Request, file: UploadFile = File(...)):
    content = await file.read()
    if not content:
        return templates.TemplateResponse(
            request, "error.html", {"message": "上传文件为空"}, status_code=400)
    try:
        csv_rows = parse_csv(content)
    except CsvError as exc:
        return templates.TemplateResponse(
            request, "error.html",
            {"message": f"CSV 校验失败：{exc}"}, status_code=400)

    filename = Path(file.filename or "upload.csv").name
    saved = UPLOAD_DIR / f"{filename}"
    stem, suffix = saved.stem, saved.suffix
    n = 1
    while saved.exists():
        saved = UPLOAD_DIR / f"{stem}-{n}{suffix}"
        n += 1
    saved.write_bytes(content)

    conn = db.connect(DB_PATH)
    try:
        batch_id = db.create_batch(conn, saved.name)
        for r in csv_rows:
            db.add_row(conn, batch_id, r.seq, r.sample_no, r.project, r.result)
    finally:
        conn.close()
    return RedirectResponse(url=f"/batches/{batch_id}", status_code=303)


def _get_batch_or_none(conn, batch_id: int):
    batch = db.get_batch(conn, batch_id)
    if batch is None:
        return None, None
    rows = [_row_view(conn, r) for r in db.get_rows(conn, batch_id)]
    return batch, rows


@app.get("/batches/{batch_id}", response_class=HTMLResponse)
def batch_detail(request: Request, batch_id: int):
    conn = db.connect(DB_PATH)
    try:
        batch, rows = _get_batch_or_none(conn, batch_id)
        if batch is None:
            return templates.TemplateResponse(
                request, "error.html",
                {"message": f"批次 {batch_id} 不存在"}, status_code=404)
        lock = db.current_lock(conn)
        return templates.TemplateResponse(
            request, "batch.html",
            {
                "b": batch, "rows": rows,
                "counts": db.row_counts(conn, batch_id),
                "status_labels": db.ROW_STATUS_LABELS,
                "busy": manager.busy,
                "lock_batch": lock["batch_id"] if lock else None,
                "actionable": db.ACTIONABLE,
            },
        )
    finally:
        conn.close()


@app.post("/batches/{batch_id}/start")
def start_batch(request: Request, batch_id: int, resume: str = Form("0")):
    do_resume = resume == "1"
    conn = db.connect(DB_PATH)
    try:
        batch = db.get_batch(conn, batch_id)
        if batch is None:
            return templates.TemplateResponse(
                request, "error.html",
                {"message": f"批次 {batch_id} 不存在"}, status_code=404)
        if manager.busy or db.current_lock(conn):
            return templates.TemplateResponse(
                request, "error.html",
                {"message": "已有报送任务在运行，每批同一时刻只允许一份；"
                            "请停止当前任务后再操作。"}, status_code=409)
        if batch["status"] == db.BATCH_DONE and not do_resume:
            return RedirectResponse(url=f"/batches/{batch_id}", status_code=303)
        if do_resume:
            pending_rows = [r for r in db.get_rows(conn, batch_id)
                            if r["status"] in db.ACTIONABLE]
            if not pending_rows:
                return RedirectResponse(url=f"/batches/{batch_id}",
                                        status_code=303)
    finally:
        conn.close()

    try:
        manager.start(make_worker(), batch_id, do_resume)
    except RuntimeError as exc:
        return templates.TemplateResponse(
            request, "error.html", {"message": str(exc)}, status_code=409)
    return RedirectResponse(url=f"/batches/{batch_id}", status_code=303)


@app.post("/batches/{batch_id}/stop")
def stop_batch(batch_id: int):
    manager.stop_current()
    return RedirectResponse(url=f"/batches/{batch_id}", status_code=303)


@app.post("/batches/{batch_id}/rows/{row_id}/recheck")
def recheck_row(request: Request, batch_id: int, row_id: int):
    """人工在旧站处理完冲突后，把行重置为待核实，续跑时仍会先查询再决定。"""
    conn = db.connect(DB_PATH)
    try:
        row = db.get_row(conn, row_id)
        if row is None or row["batch_id"] != batch_id:
            return templates.TemplateResponse(
                request, "error.html",
                {"message": "行记录不存在"}, status_code=404)
        if manager.busy or db.current_lock(conn):
            return templates.TemplateResponse(
                request, "error.html",
                {"message": "报送任务运行中，请先停止再重置行状态。"},
                status_code=409)
        if row["status"] != db.CONFLICT:
            return templates.TemplateResponse(
                request, "error.html",
                {"message": f"仅冲突行可重置，当前状态 {row['status']}"},
                status_code=400)
        db.update_row(conn, row_id, status=db.UNVERIFIED, remote_id=None,
                      screenshot=None,
                      message="人工处理后重置为待核实，续跑将先查询核实")
        db.add_event(conn, row_id, "RESET_FOR_RECHECK",
                     "冲突行经人工处理，重置为待核实")
        db.set_batch_status(
            conn, batch_id, db.BATCH_STOPPED,
            note=f"[{db.now()}] 行 {row['seq']} 重置待核实，可续跑；")
    finally:
        conn.close()
    return RedirectResponse(url=f"/batches/{batch_id}", status_code=303)


@app.get("/api/batches/{batch_id}")
def api_batch(batch_id: int):
    conn = db.connect(DB_PATH)
    try:
        batch, rows = _get_batch_or_none(conn, batch_id)
        if batch is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        snap = manager.worker.snapshot() if manager.worker else {}
        return JSONResponse({
            "batch": dict(batch),
            "busy": manager.busy,
            "current_seq": snap.get("row_seq") if manager.busy else None,
            "counts": db.row_counts(conn, batch_id),
            "rows": rows,
        })
    finally:
        conn.close()


@app.get("/batches/{batch_id}/rows/{row_id}/events", response_class=HTMLResponse)
def row_events(request: Request, batch_id: int, row_id: int):
    conn = db.connect(DB_PATH)
    try:
        row = db.get_row(conn, row_id)
        if row is None or row["batch_id"] != batch_id:
            return templates.TemplateResponse(
                request, "error.html",
                {"message": "行记录不存在"}, status_code=404)
        events = db.get_events(conn, row_id)
        return templates.TemplateResponse(
            request, "events.html",
            {"b": db.get_batch(conn, batch_id), "row": row,
             "events": events,
             "status_label": db.ROW_STATUS_LABELS.get(row["status"],
                                                     row["status"])},
        )
    finally:
        conn.close()
