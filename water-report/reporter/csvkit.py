"""CSV 解析与校验。

要求三列：样本号、项目、结果（结果为非负十进制数）；前两列组成业务键。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

REQUIRED_HEADERS = ["样本号", "项目", "结果"]
HEADER_ALIASES = {
    "样本号": ("样本号", "sample_no", "sample", "样品号"),
    "项目": ("项目", "project", "test_item", "检测项目"),
    "结果": ("结果", "result", "value"),
}


@dataclass
class CsvRow:
    seq: int
    sample_no: str
    project: str
    result: str


class CsvError(ValueError):
    def __init__(self, message: str, line: int | None = None):
        super().__init__(message)
        self.line = line


def _map_headers(raw: list[str]) -> dict[str, int]:
    normalized = [h.strip().lstrip("﻿") for h in raw]
    mapping: dict[str, int] = {}
    for canonical, aliases in HEADER_ALIASES.items():
        for idx, name in enumerate(normalized):
            if name.lower() in (a.lower() for a in aliases):
                mapping[canonical] = idx
                break
    missing = [h for h in REQUIRED_HEADERS if h not in mapping]
    if missing:
        raise CsvError(f"CSV 表头缺少必需列：{ '、'.join(missing) }，"
                       f"实际表头：{ '、'.join(normalized) or '(空)' }")
    return mapping


def parse_csv(content: bytes) -> list[CsvRow]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = content.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise CsvError("CSV 仅支持 UTF-8 或 GB18030 编码") from exc

    reader = csv.reader(io.StringIO(text))
    rows_raw = [r for r in reader]
    if not rows_raw:
        raise CsvError("CSV 文件为空")
    mapping = _map_headers(rows_raw[0])

    out: list[CsvRow] = []
    seen: set[tuple[str, str]] = set()
    for line_no, raw in enumerate(rows_raw[1:], start=2):
        if not raw or all(not c.strip() for c in raw):
            continue  # 跳过空行
        if len(raw) < len(mapping):
            raise CsvError(f"第 {line_no} 行列数不足，应为 3 列", line_no)
        sample_no = raw[mapping["样本号"]].strip()
        project = raw[mapping["项目"]].strip()
        result = raw[mapping["结果"]].strip()
        if not sample_no:
            raise CsvError(f"第 {line_no} 行样本号为空", line_no)
        if not project:
            raise CsvError(f"第 {line_no} 行项目为空", line_no)
        try:
            value = Decimal(result)
        except InvalidOperation:
            raise CsvError(
                f"第 {line_no} 行结果 {result!r} 不是十进制数", line_no
            )
        if value < 0 or not value.is_finite():
            raise CsvError(
                f"第 {line_no} 行结果 {result!r} 必须是非负有限十进制数", line_no
            )
        key = (sample_no, project)
        if key in seen:
            raise CsvError(
                f"第 {line_no} 行业务键（{sample_no}, {project}）在文件内重复",
                line_no,
            )
        seen.add(key)
        out.append(CsvRow(len(out) + 1, sample_no, project, str(value)))

    if not out:
        raise CsvError("CSV 没有任何数据行")
    return out
