"""Playwright 浏览器报送后台。

铁律：
1. 只通过真实浏览器访问旧站的查询页 / 录入页，不调用任何旧站接口或数据库；
2. 每条业务键先查询：查询失败（含断连错误页、超时、异常）一律记 QUERY_FAILED，
   绝不能当成"远端不存在"；
3. 远端已有同值 -> ALREADY_SENT；有异值 -> CONFLICT 并停止整批，页面展示双方值；
4. 新增保存后若拿不到回执（错误页/超时/异常/进程退出）-> UNVERIFIED，
   不做第二次保存；续跑时先重新查询核实，再决定是否仍需补填；
5. 远端保存（旧站事务）与本地进度更新（本地事务）是两个独立提交。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    Page,
    Playwright,
    TimeoutError as PWTimeout,
    sync_playwright,
)

from . import db

QUERY_RETRIES = 2          # 查询失败时的额外尝试次数（每次均为真实页面操作）
DEFAULT_TIMEOUT_MS = 15000
RETRY_BACKOFF_SECONDS = 2


class BrowserError(RuntimeError):
    pass


@dataclass
class RemoteRecord:
    remote_id: str
    result: Decimal
    raw_result: str


@dataclass
class QueryOutcome:
    ok: bool                      # 查询本身是否成功（False=查询失败，不是无结果）
    records: list[RemoteRecord]
    error: str = ""
    screenshot: str | None = None


def _norm(value: str) -> Decimal | None:
    try:
        return Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        return None


class ReportingWorker:
    """一个 worker 对应一条后台线程、一个浏览器实例。"""

    def __init__(self, base_url: str, db_path: str, screenshot_dir: str,
                 headless: bool = True, slow_mo_ms: int = 0,
                 channel: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.db_path = db_path
        self.shot_dir = Path(screenshot_dir)
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.slow_mo = slow_mo_ms
        # 默认 None = Playwright 自带 headless shell；
        # 本机缺 shell 时可用 channel="chromium" 走完整 Chromium
        self.channel = channel or None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._current: dict[str, Any] = {"batch_id": None, "row_seq": None}

    # ---------- 生命周期 ----------
    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def stop(self) -> None:
        self._stop.set()

    def start(self, batch_id: int, resume: bool) -> None:
        if self.alive:
            raise RuntimeError("已有浏览器报送任务在运行")
        self._stop.clear()
        self._current = {"batch_id": batch_id, "row_seq": None}
        self._thread = threading.Thread(
            target=self._run_guarded,
            args=(batch_id, resume),
            name=f"reporting-worker-b{batch_id}",
            daemon=True,
        )
        self._thread.start()

    def snapshot(self) -> dict[str, Any]:
        return dict(self._current)

    def _run_guarded(self, batch_id: int, resume: bool) -> None:
        conn = db.connect(self.db_path)
        worker_id = None
        pw: Playwright | None = None
        try:
            try:
                worker_id = db.acquire_lock(conn, batch_id)
            except db.LockBusy as exc:
                db.set_batch_status(conn, batch_id, db.BATCH_STOPPED,
                                    note=f"[{db.now()}] {exc}；")
                return
            db.mark_batch_running(conn, batch_id, resume)
            with sync_playwright() as pw_ctx:
                pw = pw_ctx
                browser = pw.chromium.launch(
                    headless=self.headless, slow_mo=self.slow_mo,
                    channel=self.channel,
                    args=["--no-sandbox"] if not self.channel else None,
                )
                context = browser.new_context()
                context.set_default_timeout(DEFAULT_TIMEOUT_MS)
                page = context.new_page()
                self._process(conn, worker_id, page, batch_id)
                context.close()
                browser.close()
        except Exception as exc:  # 浏览器启动失败等：批次安全落地为暂停
            try:
                db.set_batch_status(
                    conn, batch_id, db.BATCH_STOPPED,
                    note=f"[{db.now()}] 浏览器任务异常：{exc}；",
                )
            except Exception:
                pass
        finally:
            if worker_id:
                try:
                    db.release_lock(conn, worker_id)
                except Exception:
                    pass
            conn.close()

    # ---------- 主流程 ----------
    def _process(self, conn, worker_id: str, page: Page, batch_id: int) -> None:
        rows = db.get_rows(conn, batch_id)
        conflict = False
        for row in rows:
            if self._stop.is_set():
                db.set_batch_status(
                    conn, batch_id, db.BATCH_STOPPED,
                    note=f"[{db.now()}] 收到停止请求，剩余行待续跑；",
                )
                return
            if row["status"] not in db.ACTIONABLE:
                continue  # 终态行（含 CONFLICT）跳过
            self._current["row_seq"] = row["seq"]
            if conflict:
                # 冲突停批后，剩余可处理行保持原状态，批次结束等待人工。
                break

            if row["status"] == db.UNVERIFIED:
                outcome = self._verify_unverified(conn, page, row)
                if outcome is None:
                    conflict = True
                continue

            # PENDING / QUERY_FAILED：先查后填
            outcome = self._query_with_retry(conn, page, row)
            if not outcome.ok:
                db.update_row(
                    conn, row["id"], status=db.QUERY_FAILED,
                    message=f"查询失败（不得视为不存在）：{outcome.error}",
                    screenshot=outcome.screenshot,
                    attempts=row["attempts"] + 1,
                )
                db.add_event(conn, row["id"], "QUERY_FAILED", outcome.error)
                continue

            local_value = Decimal(row["result"])
            decision = self._decide(outcome.records, local_value)
            if decision == "absent":
                self._create_new(conn, page, row)
            elif decision == "same":
                rec = next(r for r in outcome.records if r.result == local_value)
                db.update_row(
                    conn, row["id"], status=db.ALREADY_SENT,
                    remote_id=rec.remote_id,
                    message="远端已存在同值记录，记为已送，不重复入库",
                    screenshot=outcome.screenshot,
                    attempts=row["attempts"] + 1,
                )
                db.add_event(conn, row["id"], "ALREADY_SENT",
                             f"remote_id={rec.remote_id}, result={rec.raw_result}")
            else:  # conflict
                remote_summary = "; ".join(
                    f"编号{r.remote_id} 值{r.raw_result}" for r in outcome.records
                )
                msg = (f"异值冲突停批：本地 {row['result']}，"
                       f"远端 [{remote_summary}]")
                db.update_row(
                    conn, row["id"], status=db.CONFLICT,
                    remote_id=",".join(r.remote_id for r in outcome.records),
                    message=msg, screenshot=outcome.screenshot,
                    attempts=row["attempts"] + 1,
                )
                db.add_event(conn, row["id"], "CONFLICT", msg)
                conflict = True

        self._current["row_seq"] = None
        if conflict:
            db.set_batch_status(
                conn, batch_id, db.BATCH_STOPPED,
                note=f"[{db.now()}] 因异值冲突停批，页面展示双方值，"
                     "处理冲突后续跑；",
            )
        elif self._stop.is_set():
            db.set_batch_status(
                conn, batch_id, db.BATCH_STOPPED,
                note=f"[{db.now()}] 已停止，待续跑；",
            )
        else:
            remaining = [
                r for r in db.get_rows(conn, batch_id)
                if r["status"] in db.ACTIONABLE
            ]
            if remaining:
                db.set_batch_status(
                    conn, batch_id, db.BATCH_STOPPED,
                    note=f"[{db.now()}] 本轮结束，仍有 "
                    f"{len(remaining)} 行查询失败/待核实，可续跑；",
                )
            else:
                db.set_batch_status(conn, batch_id, db.BATCH_DONE,
                                    note=f"[{db.now()}] 全部行处理完成；")

    # ---------- 页面动作：查询 ----------
    def _query_with_retry(self, conn, page: Page, row) -> QueryOutcome:
        last: QueryOutcome | None = None
        for attempt in range(QUERY_RETRIES + 1):
            if self._stop.is_set():
                return QueryOutcome(False, [], "任务已停止")
            last = self._query_once(page, row)
            if last.ok:
                return last
            db.add_event(conn, row["id"], "QUERY_ATTEMPT_FAILED",
                         f"第{attempt + 1}次：{last.error}")
            if attempt < QUERY_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS)
        return last  # type: ignore[return-value]

    def _query_once(self, page: Page, row) -> QueryOutcome:
        shot: str | None = None
        try:
            page.goto(f"{self.base_url}/search", wait_until="domcontentloaded")
            page.fill("#q-sample-no", row["sample_no"])
            page.fill("#q-project", row["project"])
            with page.expect_navigation(wait_until="domcontentloaded",
                                        timeout=DEFAULT_TIMEOUT_MS):
                page.click("#search-submit")

            if page.locator("#error-page").count() > 0:
                shot = self._shot(page, row, "query-error")
                return QueryOutcome(False, [], "旧站返回错误页（可能断连）", shot)

            records: list[RemoteRecord] = []
            for tr in page.locator("#result-table tr.result-row").all():
                rid = tr.locator(".remote-id").inner_text().strip()
                raw = tr.locator(".remote-result").inner_text().strip()
                value = _norm(raw)
                if value is None:
                    return QueryOutcome(
                        False, [], f"远端结果无法解析：{raw!r}",
                        self._shot(page, row, "query-parse-error"),
                    )
                records.append(RemoteRecord(rid, value, raw))
            shot = self._shot(page, row,
                              "query-hit" if records else "query-empty")
            return QueryOutcome(True, records, screenshot=shot)
        except PWTimeout as exc:
            return QueryOutcome(False, [], f"页面超时：{exc}",
                                self._safe_shot(page, row, "query-timeout"))
        except Exception as exc:  # 导航失败、浏览器断连等
            return QueryOutcome(False, [], f"浏览器异常：{exc}",
                                self._safe_shot(page, row, "query-exception"))

    @staticmethod
    def _decide(records: list[RemoteRecord], local: Decimal) -> str:
        if not records:
            return "absent"
        if any(r.result == local for r in records):
            return "same"
        return "conflict"

    # ---------- 页面动作：新增保存 ----------
    def _create_new(self, conn, page: Page, row) -> None:
        # SAVING 单独落一次本地提交：若进程在此之后退出，
        # 重启恢复会把它转成 UNVERIFIED 而不是盲目重填。
        db.update_row(conn, row["id"], status=db.SAVING,
                      attempts=row["attempts"] + 1,
                      message="正在通过浏览器提交保存")
        db.add_event(conn, row["id"], "SAVING",
                     f"sample={row['sample_no']}, project={row['project']}, "
                     f"result={row['result']}")
        try:
            page.goto(f"{self.base_url}/new", wait_until="domcontentloaded")
            page.fill("#f-sample-no", row["sample_no"])
            page.fill("#f-project", row["project"])
            page.fill("#f-result", row["result"])
            with page.expect_navigation(wait_until="domcontentloaded",
                                        timeout=DEFAULT_TIMEOUT_MS):
                page.click("#entry-submit")
        except PWTimeout as exc:
            self._save_unverified(conn, row, f"提交后导航超时：{exc}",
                                  self._safe_shot(page, row, "save-timeout"))
            return
        except Exception as exc:
            self._save_unverified(conn, row, f"提交时浏览器异常：{exc}",
                                  self._safe_shot(page, row, "save-exception"))
            return

        # 回执判定：只有明确的成功页才算保存成功
        if page.locator("#saved-record-id").count() > 0:
            remote_id = page.locator("#saved-record-id").inner_text().strip()
            shot = self._shot(page, row, "saved")
            db.update_row(conn, row["id"], status=db.SAVED,
                          remote_id=remote_id, screenshot=shot,
                          message=f"已新增，远端记录编号 {remote_id}")
            db.add_event(conn, row["id"], "SAVED", f"remote_id={remote_id}")
            return

        # 错误页（典型：保存后断连，数据可能已落库）或任何非预期页面
        reason = "未拿到保存成功回执"
        if page.locator("#error-page").count() > 0:
            reason = "旧站返回断连/错误页，数据可能已保存，必须重新查询核实"
        self._save_unverified(conn, row, reason,
                              self._safe_shot(page, row, "save-no-receipt"))

    def _save_unverified(self, conn, row, reason: str,
                         screenshot: str | None) -> None:
        db.update_row(conn, row["id"], status=db.UNVERIFIED,
                      remote_id=None, screenshot=screenshot,
                      message=f"保存后回执丢失，待核实：{reason}")
        db.add_event(conn, row["id"], "UNVERIFIED", reason)

    # ---------- 续跑核实 ----------
    def _verify_unverified(self, conn, page: Page, row) -> bool | None:
        """返回 True=核实并处理完成；False=None 语义：出现冲突（停批）。"""
        outcome = self._query_with_retry(conn, page, row)
        local_value = Decimal(row["result"])
        if not outcome.ok:
            db.update_row(
                conn, row["id"], status=db.UNVERIFIED,
                message=f"续跑核实查询失败，保持待核实：{outcome.error}",
                screenshot=outcome.screenshot,
                attempts=row["attempts"] + 1,
            )
            db.add_event(conn, row["id"], "VERIFY_QUERY_FAILED", outcome.error)
            return True

        decision = self._decide(outcome.records, local_value)
        if decision == "same":
            rec = next(r for r in outcome.records if r.result == local_value)
            db.update_row(
                conn, row["id"], status=db.SAVED,
                remote_id=rec.remote_id, screenshot=outcome.screenshot,
                message=f"续跑核实：上次保存实际已成功，远端记录编号 {rec.remote_id}",
                attempts=row["attempts"] + 1,
            )
            db.add_event(conn, row["id"], "VERIFIED_SAVED",
                         f"remote_id={rec.remote_id}")
            return True
        if decision == "conflict":
            remote_summary = "; ".join(
                f"编号{r.remote_id} 值{r.raw_result}" for r in outcome.records
            )
            msg = f"续跑核实发现异值：本地 {row['result']}，远端 [{remote_summary}]"
            db.update_row(
                conn, row["id"], status=db.CONFLICT,
                remote_id=",".join(r.remote_id for r in outcome.records),
                message=msg, screenshot=outcome.screenshot,
                attempts=row["attempts"] + 1,
            )
            db.add_event(conn, row["id"], "CONFLICT", msg)
            return None

        # 查询成功且确认远端仍无记录：现在才允许重新填写
        db.add_event(conn, row["id"], "VERIFY_ABSENT",
                     "续跑核实确认远端不存在，重新填写")
        self._create_new(conn, page, row)
        return True

    # ---------- 截图 --------
    def _shot(self, page: Page, row, tag: str) -> str:
        path = self.shot_dir / f"b{row['batch_id']}-r{row['id']}-{tag}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
            return str(path.name)
        except Exception:
            return ""

    def _safe_shot(self, page: Page, row, tag: str) -> str | None:
        try:
            return self._shot(page, row, tag)
        except Exception:
            return None


class WorkerManager:
    """进程内单 worker 管理；配合 db.worker_lock 形成双保险。"""

    def __init__(self):
        self.worker: ReportingWorker | None = None
        self._lock = threading.Lock()

    def start(self, worker: ReportingWorker, batch_id: int,
              resume: bool) -> None:
        with self._lock:
            if self.worker and self.worker.alive:
                raise RuntimeError("当前已有报送任务在运行，每批只运行一份")
            worker.start(batch_id, resume)
            self.worker = worker

    def stop_current(self) -> None:
        with self._lock:
            if self.worker and self.worker.alive:
                self.worker.stop()

    @property
    def busy(self) -> bool:
        return bool(self.worker and self.worker.alive)
