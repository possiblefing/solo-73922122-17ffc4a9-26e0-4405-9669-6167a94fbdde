"""端到端实测（驱动真实 HTTP 服务 + 真实 Chromium）：

覆盖：
1. CSV 校验（负数/非数字/重复键/坏表头）
2. 正常上传报送：查询无记录 -> 新增 -> SAVED + 远端编号
3. 重复报送同值：ALREADY_SENT，旧站不产生重复行
4. 异值：CONFLICT 停批，页面能拿到双方值
5. 旧站允许重复键（直接验证旧站行为）
6. 保存后断连：UNVERIFIED；关闭断连续跑 -> 核实为已 SAVED，不重复入库
7. 查询中断（保存前断连）：QUERY_FAILED，绝不被当成不存在
8. 每批单实例：运行中再次启动被拒
9. 重启恢复：SAVING 残留 -> UNVERIFIED，锁被清理
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTER = os.environ.get("REPORTER_URL", "http://127.0.0.1:8010")
LEGACY = os.environ.get("LEGACY_URL", "http://127.0.0.1:8011")
RDB = Path(os.environ.get("REPORTER_DB", ROOT / "data/reporter.db"))
LDB = Path(os.environ.get("LEGACY_DB", ROOT / "data/legacy.db"))

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def http(method: str, url: str, data: bytes | None = None,
         content_type: str | None = None, allow_errors: bool = False):
    req = urllib.request.Request(url, data=data, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        if allow_errors:
            return exc.code, exc.read()
        raise


def upload_batch(csv_path: Path) -> int:
    boundary = "----t" + str(time.time_ns())
    content = csv_path.read_bytes()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{csv_path.name}"\r\n'
        "Content-Type: text/csv\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler)
    req = urllib.request.Request(
        f"{REPORTER}/batches", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with opener.open(req, timeout=30) as resp:
        return int(resp.url.rstrip("/").split("/")[-1])


def upload_expect_error(csv_path: Path) -> tuple[int, bytes]:
    boundary = "----t" + str(time.time_ns())
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{csv_path.name}"\r\n'
        "Content-Type: text/csv\r\n\r\n"
    ).encode() + csv_path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    return http("POST", f"{REPORTER}/batches", body,
                f"multipart/form-data; boundary={boundary}",
                allow_errors=True)


def post_form(path: str, fields: dict[str, str]):
    data = urllib.parse.urlencode(fields).encode()
    opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler)
    req = urllib.request.Request(REPORTER + path, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with opener.open(req, timeout=30) as resp:
        return resp.status, resp.url


def post_form_expect_error(path: str, fields: dict[str, str]):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(REPORTER + path, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        urllib.request.urlopen(req, timeout=30)
        return 200, b""
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def set_disconnect(mode: str) -> None:
    """mode: off / save（仅保存后断连）/ all（全站断连）。"""
    data = urllib.parse.urlencode({"mode": mode}).encode()
    req = urllib.request.Request(f"{LEGACY}/__admin/toggle-disconnect",
                                 data=data, method="POST")
    urllib.request.urlopen(req, timeout=15).read()


def wait_batch(batch_id: int, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = http("GET", f"{REPORTER}/api/batches/{batch_id}")
        data = json.loads(body)
        if not data["busy"]:
            return data
        time.sleep(0.5)
    raise TimeoutError(f"batch {batch_id} did not finish")


def legacy_count(sample_no: str, project: str) -> int:
    q = urllib.parse.urlencode({"sample_no": sample_no, "project": project})
    # 只用浏览器页面路径不方便计数；这里通过旧站 records.json（演示核对接口）
    _, body = http("GET", f"{LEGACY}/records.json")
    rows = json.loads(body)
    return sum(1 for r in rows
               if r["sample_no"] == sample_no and r["project"] == project)


def legacy_results(sample_no: str, project: str) -> list[str]:
    _, body = http("GET", f"{LEGACY}/records.json")
    rows = json.loads(body)
    return [r["result"] for r in rows
            if r["sample_no"] == sample_no and r["project"] == project]


def write_csv(name: str, text: str) -> Path:
    p = ROOT / "data" / name
    p.write_text(text, encoding="utf-8")
    return p


def main() -> int:
    print("== 1. CSV 校验 ==")
    bad_neg = write_csv("t-neg.csv", "样本号,项目,结果\nA,pH,-1\n")
    code, body = upload_expect_error(bad_neg)
    check("负数结果被拒(400)", code == 400 and "非负".encode() in body, str(code))
    bad_num = write_csv("t-num.csv", "样本号,项目,结果\nA,pH,abc\n")
    code, body = upload_expect_error(bad_num)
    check("非数字被拒", code == 400 and "十进制".encode() in body, str(code))
    bad_dup = write_csv("t-dup.csv", "样本号,项目,结果\nA,pH,1\nA,pH,2\n")
    code, body = upload_expect_error(bad_dup)
    check("文件内重复键被拒", code == 400 and "重复".encode() in body, str(code))
    bad_hdr = write_csv("t-hdr.csv", "foo,bar,baz\nA,pH,1\n")
    code, body = upload_expect_error(bad_hdr)
    check("坏表头被拒", code == 400 and "表头".encode() in body, str(code))

    print("== 2. 正常报送（先查无 -> 新增） ==")
    b1 = upload_batch(ROOT / "data/sample-batch-a.csv")
    post_form(f"/batches/{b1}/start", {"resume": "0"})
    d = wait_batch(b1)
    statuses = {r["seq"]: r["status"] for r in d["rows"]}
    check("5 行全部 SAVED",
          all(s == "SAVED" for s in statuses.values()), str(statuses))
    check("每行有远端记录编号",
          all(r["remote_id"].isdigit() for r in d["rows"]),
          str([r["remote_id"] for r in d["rows"]]))
    check("批次 DONE", d["batch"]["status"] == "DONE", d["batch"]["status"])
    check("旧站恰好 5 条相关记录",
          legacy_count("W2026-001", "pH") == 1
          and legacy_count("W2026-003", "COD") == 1)

    print("== 3. 同值重报 -> ALREADY_SENT，不重复入库 ==")
    b2 = upload_batch(ROOT / "data/sample-batch-a.csv")
    post_form(f"/batches/{b2}/start", {"resume": "0"})
    d = wait_batch(b2)
    check("全部 ALREADY_SENT",
          all(r["status"] == "ALREADY_SENT" for r in d["rows"]),
          str({r["seq"]: r["status"] for r in d["rows"]}))
    check("远端编号与首次一致",
          {r["sample_no"] + r["project"]: r["remote_id"] for r in d["rows"]}
          == {x["sample_no"] + x["project"]: x["remote_id"]
              for x in wait_batch(b1)["rows"]})
    check("旧站无重复行",
          legacy_count("W2026-001", "pH") == 1
          and legacy_count("W2026-002", "氨氮") == 1)

    print("== 4. 异值 -> CONFLICT 停批，含双方值 ==")
    conflict_csv = write_csv(
        "t-conflict.csv",
        "样本号,项目,结果\nW2026-001,pH,9.99\nW2026-002,氨氮,0.85\n")
    b3 = upload_batch(conflict_csv)
    post_form(f"/batches/{b3}/start", {"resume": "0"})
    d = wait_batch(b3)
    r0 = d["rows"][0]
    check("首行 CONFLICT", r0["status"] == "CONFLICT", r0["status"])
    check("消息同时含本地值与远端值",
          "9.99" in r0["message"] and "7.2" in r0["message"], r0["message"])
    check("批次停批", d["batch"]["status"] == "STOPPED", d["batch"]["status"])
    check("冲突后第二行未被处理（仍 PENDING）",
          d["rows"][1]["status"] == "PENDING", d["rows"][1]["status"])
    check("旧站未被写入异值", legacy_results("W2026-001", "pH") == ["7.2"])

    print("== 5. 旧站本身允许重复键 ==")
    # 直接走旧站网页保存两次同键
    for val in ("5.55", "5.55"):
        data = urllib.parse.urlencode({
            "sample_no": "DUP-KEY", "project": "pH", "result": val}).encode()
        req = urllib.request.Request(f"{LEGACY}/save", data=data, method="POST")
        try:
            urllib.request.urlopen(req, timeout=15).read()
        except urllib.error.HTTPError:
            pass
    check("旧站允许同键两行", legacy_count("DUP-KEY", "pH") == 2)

    print("== 6. 保存后断连：UNVERIFIED -> 续跑核实，不重复入库 ==")
    b4_csv = write_csv("t-disconnect.csv",
                       "样本号,项目,结果\nW2026-301,pH,8.1\nW2026-301,浊度,2.2\n")
    b4 = upload_batch(b4_csv)
    set_disconnect("save")
    try:
        post_form(f"/batches/{b4}/start", {"resume": "0"})
        d = wait_batch(b4)
        check("断连时两行均 UNVERIFIED",
              all(r["status"] == "UNVERIFIED" for r in d["rows"]),
              str([r["status"] for r in d["rows"]]))
        check("断连期间旧站实际已落库 2 行",
              legacy_count("W2026-301", "pH") == 1
              and legacy_count("W2026-301", "浊度") == 1)
    finally:
        set_disconnect("off")
    post_form(f"/batches/{b4}/start", {"resume": "1"})
    d = wait_batch(b4)
    check("续跑核实后全部 SAVED",
          all(r["status"] == "SAVED" for r in d["rows"]),
          str([(r["status"], r["message"]) for r in d["rows"]]))
    check("核实拿到的是真实远端编号",
          all(r["remote_id"].isdigit() for r in d["rows"]))
    check("核实未产生重复入库",
          legacy_count("W2026-301", "pH") == 1
          and legacy_count("W2026-301", "浊度") == 1)

    print("== 7. 查询期断连：QUERY_FAILED，不当作不存在 ==")
    b5_csv = write_csv("t-qfail.csv",
                       "样本号,项目,结果\nW2026-401,pH,6.6\nW2026-402,pH,7.7\n")
    b5 = upload_batch(b5_csv)
    set_disconnect("all")
    try:
        post_form(f"/batches/{b5}/start", {"resume": "0"})
        d = wait_batch(b5, timeout=120)
        check("查询失败行标记 QUERY_FAILED",
              all(r["status"] == "QUERY_FAILED" for r in d["rows"]),
              str([r["status"] for r in d["rows"]]))
        check("失败消息明确不得视为不存在",
              all("不得视为不存在" in r["message"] for r in d["rows"]))
        check("查询失败绝未写入旧站",
              legacy_count("W2026-401", "pH") == 0
              and legacy_count("W2026-402", "pH") == 0)
    finally:
        set_disconnect("off")
    post_form(f"/batches/{b5}/start", {"resume": "1"})
    d = wait_batch(b5)
    check("恢复后续跑成功 SAVED",
          all(r["status"] == "SAVED" for r in d["rows"]),
          str([r["status"] for r in d["rows"]]))

    print("== 8. 每批单实例锁 ==")
    b6 = upload_batch(write_csv(
        "t-lock.csv",
        "样本号,项目,结果\n"
        + "\n".join(f"L-{i},pH,1.{i}" for i in range(8))
        + "\n"))
    post_form(f"/batches/{b6}/start", {"resume": "0"})
    time.sleep(1)
    code, body = post_form_expect_error(f"/batches/{b6}/start",
                                        {"resume": "1"})
    check("运行中再次启动被拒(409)", code == 409, str(code))
    wait_batch(b6)

    print("== 9. 重启恢复：SAVING 残留 -> UNVERIFIED，锁清理 ==")
    conn = sqlite3.connect(RDB)
    conn.execute("INSERT INTO worker_lock(id,worker_id,batch_id,acquired_at)"
                 " VALUES(1,'dead-worker',?,'2026-01-01T00:00:00')", (b6,))
    conn.execute("UPDATE rows SET status='SAVING' WHERE batch_id=? "
                 "AND seq=1", (b6,))
    conn.commit()
    conn.close()
    # 在独立进程里执行启动恢复逻辑
    env = dict(os.environ)
    env.update({
        "REPORTER_DB": str(RDB),
        "UPLOAD_DIR": str(ROOT / "data/uploads"),
        "SCREENSHOT_DIR": str(ROOT / "data/screenshots"),
    })
    out = subprocess.run(
        [sys.executable, "-c",
         "from reporter import db; c=db.connect('" + str(RDB) + "');"
         "print(db.recover_stale_runs(c)); c.close()"],
        cwd=str(ROOT), env=env, capture_output=True, text=True)
    check("恢复执行无异常", out.returncode == 0, out.stderr)
    conn = sqlite3.connect(RDB)
    lock = conn.execute("SELECT COUNT(*) FROM worker_lock").fetchone()[0]
    st = conn.execute(
        "SELECT status FROM rows WHERE batch_id=? AND seq=1", (b6,)).fetchone()[0]
    conn.close()
    check("残留锁被清理", lock == 0, str(lock))
    check("SAVING 恢复为 UNVERIFIED", st == "UNVERIFIED", st)

    print("== 10. 冲突人工处理后重置待核实，续跑核实 ==")
    b7_csv = write_csv("t-recheck.csv",
                       "样本号,项目,结果\nW2026-501,pH,8.88\n")
    b7 = upload_batch(b7_csv)
    post_form(f"/batches/{b7}/start", {"resume": "0"})
    d = wait_batch(b7)
    rid = d["rows"][0]["id"]
    check("首次报送 SAVED", d["rows"][0]["status"] == "SAVED",
          d["rows"][0]["status"])
    # 同键异值 -> 冲突停批
    b8 = upload_batch(write_csv(
        "t-recheck2.csv", "样本号,项目,结果\nW2026-501,pH,3.33\n"))
    post_form(f"/batches/{b8}/start", {"resume": "0"})
    d = wait_batch(b8)
    check("异值行 CONFLICT", d["rows"][0]["status"] == "CONFLICT",
          d["rows"][0]["status"])
    # 非冲突行不能重置
    b8_row = d["rows"][0]["id"]
    code, _ = post_form_expect_error(
        f"/batches/{b8}/rows/{b8_row + 999}/recheck", {})
    check("不存在行重置被拒(404)", code == 404, str(code))
    # 模拟人工在旧站按本地正确值补录一条（旧站允许同键多行）
    data = urllib.parse.urlencode(
        {"sample_no": "W2026-501", "project": "pH", "result": "3.33"}).encode()
    req = urllib.request.Request(f"{LEGACY}/save", data=data, method="POST")
    urllib.request.urlopen(req, timeout=15).read()
    # 重置冲突行为待核实
    status, _ = post_form(
        f"/batches/{b8}/rows/{b8_row}/recheck", {})
    check("重置请求成功", status == 200)
    d = json.loads(http("GET", f"{REPORTER}/api/batches/{b8}")[1])
    check("行已转 UNVERIFIED", d["rows"][0]["status"] == "UNVERIFIED",
          d["rows"][0]["status"])
    # 续跑：查询命中同值记录 -> SAVED，且不新增
    post_form(f"/batches/{b8}/start", {"resume": "1"})
    d = wait_batch(b8)
    check("续跑核实后 SAVED", d["rows"][0]["status"] == "SAVED",
          str(d["rows"][0]))
    check("拿到补录记录的远端编号",
          d["rows"][0]["remote_id"].isdigit())
    check("远端共 2 条同键记录（旧值+人工补录），后台未再新增",
          legacy_count("W2026-501", "pH") == 2)

    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
