"""Exact analysis of CSV and Excel files.

A spreadsheet is never summarised by the model. Instead:

1. At ingestion every sheet is profiled: column names, types, value ranges and,
   for low-cardinality columns, the exact values present.
2. At question time Claude only *plans* a query from that profile, using a
   fixed vocabulary of filters, groupings and aggregate functions. It never
   writes code and never sees the rows.
3. The plan is validated against the real columns and executed by pandas over
   every row, so counts, totals and lookups are exact.

The answer states the filters applied and how many rows they matched, so a
reviewer can check what was computed.
"""

from __future__ import annotations

import csv
import io
import json
import numbers
import os
import re
import sqlite3
import threading
from collections import OrderedDict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from office import decode_text

ROWS_IN_TEXT_LIMIT = 300
ROWS_PER_TEXT_PAGE = 50
MAX_GROUP_COLUMNS = 2
MAX_RESULT_ROWS = 50
DEFAULT_RESULT_ROWS = 20
MAX_LISTED_COLUMNS = 8
MAX_TABLES_IN_PLAN = 12
CATEGORY_VALUE_LIMIT = 25
CACHE_FILES = 4

OPERATORS = ["equals", "not_equals", "greater_than", "greater_or_equal", "less_than", "less_or_equal",
             "contains", "not_contains", "one_of", "is_empty", "is_not_empty"]
FUNCTIONS = ["count", "count_distinct", "sum", "average", "minimum", "maximum", "median"]
NUMERIC_FUNCTIONS = {"sum", "average", "median"}
FUNCTION_LABELS = {
    "count": "Count", "count_distinct": "Distinct", "sum": "Sum", "average": "Average",
    "minimum": "Minimum", "maximum": "Maximum", "median": "Median",
}
OPERATOR_WORDS = {
    "equals": "is", "not_equals": "is not", "greater_than": ">", "greater_or_equal": "≥",
    "less_than": "<", "less_or_equal": "≤", "contains": "contains", "not_contains": "does not contain",
    "one_of": "is one of", "is_empty": "is empty", "is_not_empty": "is not empty",
}
# Only real currency markers may precede a number: a symbol, "Rs", or a
# three-letter code followed by a space ("INR 500"). A bare letter prefix is
# an identifier ("C0042"), not an amount.
NUMBER_PATTERN = re.compile(r"^\s*[-+(]?\s*(?:[$€£₹¥]\s*|Rs\.?\s*|[A-Z]{3}\s+)?[\d,]*\.?\d+\s*\)?\s*%?\s*$")
DATE_PATTERN = re.compile(r"^\s*\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?\s*$")


class TablePlanError(ValueError):
    """A plan that cannot be executed safely against the real columns."""


# ── Loading ──────────────────────────────────────────────────────────────────

def load_sheets(path: Path) -> dict[str, Any]:
    """Read every sheet into a typed DataFrame with a detected header row."""

    extension = path.suffix.lower()
    if extension in {".csv", ".tsv"}:
        text = decode_text(path.read_bytes()).replace("\x00", "")
        delimiter = "\t" if extension == ".tsv" else _sniff_delimiter(text)
        # pandas fixes the column count from the first line, so a title line
        # above the real header would silently drop every wider row. Pad
        # ragged rows instead of discarding them.
        records = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        width = max((len(record) for record in records), default=0)
        frames = {"Sheet1": pd.DataFrame([record + [""] * (width - len(record)) for record in records], dtype=object)}
    else:
        frames = pd.read_excel(path, sheet_name=None, header=None, dtype=object, engine="openpyxl")
    sheets: dict[str, Any] = {}
    for name, raw in frames.items():
        frame = _with_header(raw)
        if frame is not None and len(frame.columns):
            sheets[str(name)] = _typed(frame)
    return sheets


def _sniff_delimiter(text: str) -> str:
    try:
        return csv.Sniffer().sniff(text[:8192], delimiters=",\t;|").delimiter
    except csv.Error:
        return ","


def _blank(value: Any) -> bool:
    """True for None, NaN, NaT, pandas NA and whitespace-only text."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict)):
        return False
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _with_header(raw: Any) -> Any | None:
    """Use the first well-populated text row as the header.

    Exports often begin with a title, a run date or blank lines; treating those
    as the header would name every column "Unnamed".
    """
    frame = raw.loc[~raw.apply(lambda row: all(_blank(value) for value in row), axis=1)]
    frame = frame.loc[:, ~frame.apply(lambda column: all(_blank(value) for value in column))]
    if frame.empty:
        return None
    counts = [sum(not _blank(value) for value in row) for row in frame.head(20).itertuples(index=False)]
    widest = max(counts)
    header_position = 0
    for position, row in enumerate(frame.head(20).itertuples(index=False)):
        values = [value for value in row if not _blank(value)]
        if counts[position] >= max(1, widest * 0.6) and all(isinstance(value, str) for value in values):
            header_position = position
            break
    header = list(frame.iloc[header_position])
    body = frame.iloc[header_position + 1:].reset_index(drop=True)
    names: list[str] = []
    for index, value in enumerate(header, start=1):
        name = " ".join(str(value).split()) if not _blank(value) else f"Column {index}"
        candidate, suffix = name, 2
        while candidate in names:
            candidate, suffix = f"{name} ({suffix})", suffix + 1
        names.append(candidate)
    body.columns = names
    return body


def _to_number(value: Any) -> float | None:
    if _blank(value) or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (datetime, date)):
        return None
    text = str(value).strip()
    if not NUMBER_PATTERN.match(text):
        return None
    negative = text.startswith("-") or (text.startswith("(") and text.endswith(")"))
    digits = re.sub(r"[^\d.]", "", text)
    if not digits or digits.count(".") > 1:
        return None
    number = float(digits)
    return -number if negative else number


def _typed(frame: Any) -> Any:
    """Convert columns that are mostly numbers or dates; keep text as text.

    A column is converted only when at least 95% of its non-empty values parse,
    so an identifier column with a few letters stays text.
    """

    typed = {}
    for column in frame.columns:
        series = frame[column]
        present = series[~series.map(_blank)]
        if present.empty:
            typed[column] = series.map(lambda value: None if _blank(value) else str(value).strip())
            continue
        numbers = present.map(_to_number)
        if numbers.notna().mean() >= 0.95 and not _looks_like_identifier(column, present):
            typed[column] = pd.to_numeric(series.map(_to_number), errors="coerce")
            continue
        dates = _parse_dates(present)
        if dates is not None and dates.notna().mean() >= 0.95:
            typed[column] = _parse_dates(series.map(lambda value: None if _blank(value) else value))
            continue
        typed[column] = series.map(lambda value: None if _blank(value) else " ".join(str(value).split()))
    return pd.DataFrame(typed)


def _looks_like_identifier(column: str, present: Any) -> bool:
    """Account and customer numbers stay text: leading zeros matter and sums are meaningless."""
    name = column.lower()
    if not re.search(r"\b(id|no|number|account|acct|cif|code|phone|mobile|pin|ifsc|pan|aadhaar)\b", name):
        return False
    sample = present.head(200).map(lambda value: str(value).strip())
    return bool(sample.map(lambda value: value.startswith("0") and len(value) > 1).any()) or bool(sample.map(len).min() >= 6)


def _parse_dates(series: Any) -> Any | None:

    sample = series.dropna().head(200)
    if sample.empty:
        return None
    if not all(isinstance(value, (datetime, date, pd.Timestamp)) or (isinstance(value, str) and DATE_PATTERN.match(value)) for value in sample):
        return None
    strings = [value for value in sample if isinstance(value, str)]
    day_first = any(int(re.split(r"[-/.]", value.strip())[0]) > 12 for value in strings if re.match(r"^\s*\d{1,2}[-/.]", value))
    return pd.to_datetime(series, errors="coerce", format="mixed", dayfirst=day_first)


def column_type(series: Any) -> str:

    if pd.api.types.is_datetime64_any_dtype(series):
        return "date"
    if pd.api.types.is_numeric_dtype(series):
        return "number"
    return "text"


# ── Profiling and searchable text ────────────────────────────────────────────

def _display(value: Any) -> str:
    if _blank(value):
        return ""
    if hasattr(value, "isoformat") and not isinstance(value, str):
        text = value.isoformat()
        return text[:10] if text.endswith("T00:00:00") or len(text) == 10 else text
    if isinstance(value, numbers.Integral) and not isinstance(value, bool):
        return f"{int(value):,}"
    if isinstance(value, numbers.Real) and not isinstance(value, bool):
        number = float(value)
        if number.is_integer() and abs(number) < 1e15:
            return f"{int(number):,}"
        return f"{number:,.2f}"
    return str(value)


def profile_sheet(frame: Any) -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = []
    for name in frame.columns:
        series = frame[name]
        present = series.dropna()
        kind = column_type(series)
        info: dict[str, Any] = {
            "name": name, "type": kind, "nonEmpty": int(present.size),
            "distinct": int(present.nunique()),
        }
        if kind in {"number", "date"} and not present.empty:
            info["minimum"] = _display(present.min())
            info["maximum"] = _display(present.max())
        if kind == "text" and 0 < info["distinct"] <= CATEGORY_VALUE_LIMIT:
            counts = present.value_counts()
            info["values"] = [{"value": str(value), "count": int(count)} for value, count in counts.items()]
        else:
            info["examples"] = [_display(value) for value in present.drop_duplicates().head(5)]
        columns.append(info)
    return columns


def sheet_text(file_name: str, sheets: dict[str, Any]) -> tuple[str, list[str], list[dict[str, Any]]]:
    """Searchable text pages, their labels, and per-sheet profiles.

    Every sheet gets an overview page so passage search can find the file by
    its columns. Small sheets also include their rows; large sheets are only
    answerable through exact analysis, which reads every row.
    """
    pages: list[str] = []
    labels: list[str] = []
    profiles: list[dict[str, Any]] = []
    multiple = len(sheets) > 1
    for sheet_name, frame in sheets.items():
        columns = profile_sheet(frame)
        profiles.append({"sheet": sheet_name, "rowCount": int(len(frame)), "columns": columns})
        prefix = f"Sheet {sheet_name} · " if multiple else ""
        described = []
        for column in columns:
            detail = ", ".join(item["value"] for item in column.get("values", [])[:8]) or ", ".join(column.get("examples", [])[:3])
            described.append(f"- {column['name']} ({column['type']}): {detail}")
        pages.append(
            f"Spreadsheet {file_name}{f', sheet {sheet_name}' if multiple else ''}: "
            f"{len(frame):,} rows and {len(columns)} columns.\nColumns:\n" + "\n".join(described)
        )
        labels.append(f"{prefix}Overview")
        if len(frame) <= ROWS_IN_TEXT_LIMIT:
            header = " | ".join(frame.columns)
            for start in range(0, len(frame), ROWS_PER_TEXT_PAGE):
                block = frame.iloc[start:start + ROWS_PER_TEXT_PAGE]
                rows = [" | ".join(_display(value) for value in row) for row in block.itertuples(index=False)]
                pages.append(header + "\n" + "\n".join(rows))
                labels.append(f"{prefix}Rows {start + 1:,}–{start + len(block):,}")
    text = "\n\n".join(f"--- Page {index} ---\n{body}" for index, body in enumerate(pages, start=1))
    return text, labels, profiles


# ── Storage ──────────────────────────────────────────────────────────────────

def initialise(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS document_tables (
            upload_id TEXT NOT NULL, sheet TEXT NOT NULL, position INTEGER NOT NULL,
            row_count INTEGER NOT NULL, columns_json TEXT NOT NULL,
            PRIMARY KEY (upload_id, sheet),
            FOREIGN KEY (upload_id) REFERENCES uploads(id)
        )"""
    )


def store_profiles(connection: sqlite3.Connection, upload_id: str, profiles: list[dict[str, Any]]) -> None:
    connection.execute("DELETE FROM document_tables WHERE upload_id=?", (upload_id,))
    for position, profile in enumerate(profiles):
        connection.execute(
            "INSERT INTO document_tables(upload_id, sheet, position, row_count, columns_json) VALUES (?, ?, ?, ?, ?)",
            (upload_id, profile["sheet"], position, profile["rowCount"], json.dumps(profile["columns"])),
        )


def delete_document(connection: sqlite3.Connection, upload_id: str) -> None:
    connection.execute("DELETE FROM document_tables WHERE upload_id=?", (upload_id,))
    _cache.evict(upload_id)


def tables_in_scope(connection: sqlite3.Connection, document_ids: list[str] | None) -> list[sqlite3.Row]:
    sql = """SELECT t.*, u.original_name, u.stored_name FROM document_tables t
             JOIN uploads u ON u.id=t.upload_id"""
    params: list[Any] = []
    if document_ids is not None:
        if not document_ids:
            return []
        sql += f" WHERE t.upload_id IN ({','.join('?' for _ in document_ids)})"
        params.extend(document_ids)
    return connection.execute(sql + " ORDER BY u.uploaded_at DESC, t.position", params).fetchall()


class _SheetCache:
    """A few recently used files, so follow-up questions do not re-read large exports."""

    def __init__(self) -> None:
        self._items: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, upload_id: str, path: Path) -> dict[str, Any]:
        with self._lock:
            if upload_id in self._items:
                self._items.move_to_end(upload_id)
                return self._items[upload_id]
        sheets = load_sheets(path)
        with self._lock:
            self._items[upload_id] = sheets
            while len(self._items) > CACHE_FILES:
                self._items.popitem(last=False)
        return sheets

    def evict(self, upload_id: str) -> None:
        with self._lock:
            self._items.pop(upload_id, None)


_cache = _SheetCache()


def remember(upload_id: str, sheets: dict[str, Any]) -> None:
    """Seed the cache with sheets just loaded during ingestion."""
    with _cache._lock:
        _cache._items[upload_id] = sheets
        while len(_cache._items) > CACHE_FILES:
            _cache._items.popitem(last=False)


# ── Planning ─────────────────────────────────────────────────────────────────

PLAN_TOOL = {
    "name": "plan_table_query",
    "description": "Plan an exact query over one spreadsheet sheet, or decline if the question is not about these tables.",
    "input_schema": {
        "type": "object",
        "properties": {
            "useTable": {"type": "boolean", "description": "false when the question is not answerable from these columns."},
            "tableId": {"type": "string", "description": "The id of the one table to query."},
            "filters": {"type": "array", "items": {"type": "object", "properties": {
                "column": {"type": "string"}, "operator": {"type": "string", "enum": OPERATORS},
                "value": {"description": "A string, number, or for one_of a list of strings."},
            }, "required": ["column", "operator"], "additionalProperties": False}},
            "groupBy": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_GROUP_COLUMNS},
            "metrics": {"type": "array", "items": {"type": "object", "properties": {
                "function": {"type": "string", "enum": FUNCTIONS}, "column": {"type": "string"},
            }, "required": ["function"], "additionalProperties": False}, "maxItems": 4},
            "columns": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_LISTED_COLUMNS,
                        "description": "Columns to show when listing matching rows (no metrics)."},
            "sortBy": {"type": "string", "description": "A column name, or a metric label such as 'Count' or 'Sum of Balance'."},
            "sortDirection": {"type": "string", "enum": ["ascending", "descending"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULT_ROWS},
        },
        "required": ["useTable"],
        "additionalProperties": False,
    },
}

PLAN_SYSTEM = (
    "You plan exact spreadsheet queries for a banking operations analyst. You see only table schemas, never rows; "
    "a program executes your plan over every row. Set useTable=false when the question is about narrative "
    "documents, policies or anything these columns cannot answer. Otherwise choose exactly one table. "
    "Use only column names exactly as listed. For text filters on a column whose values are listed, use one of "
    "those exact values. Use count for 'how many', sum/average/minimum/maximum/median for amounts, groupBy for "
    "'per', 'by' or 'which X has the most' questions, and columns (with no metrics) to list or look up matching "
    "records such as one account or customer. For 'top N' or 'which has the most', sort by the metric descending "
    "and set limit. Dates must be written as YYYY-MM-DD. Never guess a value that is not implied by the question."
)


def _schema_for_planner(tables: list[sqlite3.Row]) -> str:
    lines: list[str] = []
    for row in tables:
        columns = json.loads(row["columns_json"])
        lines.append(f"Table id: {row['upload_id']}::{row['sheet']}\nFile: {row['original_name']} · sheet {row['sheet']} · {row['row_count']:,} rows")
        for column in columns:
            if column.get("values"):
                detail = "values: " + ", ".join(f"{item['value']} ({item['count']})" for item in column["values"])
            elif column["type"] in {"number", "date"} and "minimum" in column:
                detail = f"range {column['minimum']} to {column['maximum']}"
            else:
                detail = "e.g. " + ", ".join(column.get("examples", [])[:3])
            lines.append(f"  - {column['name']} [{column['type']}, {column['distinct']:,} distinct] {detail}")
    return "\n".join(lines)


def plan_query(question: str, tables: list[sqlite3.Row], model: str) -> dict[str, Any]:
    from rag import generation_options

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured on the backend.")
    from anthropic import Anthropic

    message = Anthropic(api_key=key).messages.create(
        **generation_options(model, 900), system=PLAN_SYSTEM,
        messages=[{"role": "user", "content": f"Tables:\n{_schema_for_planner(tables)}\n\nQuestion: {question}"}],
        tools=[PLAN_TOOL], tool_choice={"type": "tool", "name": PLAN_TOOL["name"]},
    )
    plan = next((block.input for block in message.content if block.type == "tool_use"), None)
    return plan if isinstance(plan, dict) else {"useTable": False}


# ── Execution ────────────────────────────────────────────────────────────────

def _metric_label(metric: dict[str, Any]) -> str:
    label = FUNCTION_LABELS[metric["function"]]
    return label if metric["function"] == "count" or not metric.get("column") else f"{label} of {metric['column']}"


def _comparable(series: Any, value: Any, kind: str) -> Any:

    if kind == "number":
        number = _to_number(value)
        if number is None:
            raise TablePlanError(f"'{value}' is not a number.")
        return number
    if kind == "date":
        parsed = pd.to_datetime(str(value), errors="coerce")
        if pd.isna(parsed):
            raise TablePlanError(f"'{value}' is not a date (use YYYY-MM-DD).")
        return parsed
    return str(value).strip().casefold()


def _filter_mask(frame: Any, condition: dict[str, Any]) -> Any:
    column, operator = condition.get("column"), condition.get("operator")
    if column not in frame.columns:
        raise TablePlanError(f"Column '{column}' does not exist.")
    if operator not in OPERATORS:
        raise TablePlanError(f"Unsupported filter operator '{operator}'.")
    series = frame[column]
    if operator == "is_empty":
        return series.isna()
    if operator == "is_not_empty":
        return series.notna()
    kind = column_type(series)
    value = condition.get("value")
    if value is None or (isinstance(value, str) and not value.strip()):
        raise TablePlanError(f"The filter on '{column}' needs a value.")
    text = series.map(lambda item: None if item is None or item != item else str(item).strip().casefold())
    if operator in {"contains", "not_contains"}:
        needle = str(value).strip().casefold()
        found = text.map(lambda item: item is not None and needle in item)
        return found if operator == "contains" else ~found
    if operator == "one_of":
        options = value if isinstance(value, list) else [value]
        if kind == "text":
            wanted = {str(item).strip().casefold() for item in options}
            return text.isin(wanted)
        wanted_values = [_comparable(series, item, kind) for item in options]
        return series.isin(wanted_values)
    target = _comparable(series, value, kind)
    left = text if kind == "text" else series
    if operator == "equals":
        if kind == "date":
            return series.dt.normalize() == target.normalize()
        return left == target
    if operator == "not_equals":
        return ~(left == target)
    if kind == "text":
        raise TablePlanError(f"'{column}' is text, so it cannot be compared with '{operator}'.")
    return {"greater_than": left > target, "greater_or_equal": left >= target,
            "less_than": left < target, "less_or_equal": left <= target}[operator]


def execute(sheets: dict[str, Any], sheet: str, plan: dict[str, Any]) -> dict[str, Any]:
    """Run a validated plan with pandas. Raises TablePlanError for unsafe plans."""

    if sheet not in sheets:
        raise TablePlanError(f"Sheet '{sheet}' no longer exists in this file.")
    frame = sheets[sheet]
    mask = pd.Series(True, index=frame.index)
    filters = plan.get("filters") or []
    for condition in filters:
        mask &= _filter_mask(frame, condition).fillna(False).astype(bool)
    matched = frame[mask]
    group_by = list(dict.fromkeys(plan.get("groupBy") or []))[:MAX_GROUP_COLUMNS]
    for column in group_by:
        if column not in frame.columns:
            raise TablePlanError(f"Column '{column}' does not exist.")
    metrics = plan.get("metrics") or []
    excluded: dict[str, int] = {}
    for metric in metrics:
        function, column = metric.get("function"), metric.get("column")
        if function not in FUNCTIONS:
            raise TablePlanError(f"Unsupported function '{function}'.")
        if function != "count" and column not in frame.columns:
            raise TablePlanError(f"Column '{column}' does not exist.")
        if function in NUMERIC_FUNCTIONS and column_type(frame[column]) != "number":
            raise TablePlanError(f"'{column}' is not a numeric column, so it cannot be summed or averaged.")
        if function != "count" and column in frame.columns:
            missing = int(matched[column].isna().sum())
            if missing:
                excluded[column] = missing
    limit = max(1, min(int(plan.get("limit") or DEFAULT_RESULT_ROWS), MAX_RESULT_ROWS))
    direction_descending = plan.get("sortDirection", "descending") != "ascending"

    def aggregate(group: Any) -> list[Any]:
        values: list[Any] = []
        for metric in metrics:
            function, column = metric["function"], metric.get("column")
            if function == "count":
                values.append(int(len(group)) if not column else int(group[column].notna().sum()))
            elif function == "count_distinct":
                values.append(int(group[column].nunique()))
            else:
                series = group[column].dropna()
                if series.empty:
                    values.append(None)
                    continue
                values.append({"sum": series.sum, "average": series.mean, "minimum": series.min,
                               "maximum": series.max, "median": series.median}[function]())
        return values

    result_columns: list[str]
    rows: list[list[Any]]
    total_rows: int
    if metrics:
        labels = [_metric_label(metric) for metric in metrics]
        if group_by:
            grouped = matched.groupby(group_by, dropna=False, sort=False)
            rows = [[*(key if isinstance(key, tuple) else (key,)), *aggregate(group)] for key, group in grouped]
            result_columns = [*group_by, *labels]
        else:
            rows = [aggregate(matched)]
            result_columns = labels
        sort_by = plan.get("sortBy") if plan.get("sortBy") in result_columns else (labels[0] if group_by else None)
        if sort_by:
            index = result_columns.index(sort_by)
            # Empty values always sort last, whichever the direction.
            present = [row for row in rows if not _blank(row[index])]
            absent = [row for row in rows if _blank(row[index])]
            try:
                present.sort(key=lambda row: row[index], reverse=direction_descending)
            except TypeError:
                present.sort(key=lambda row: str(row[index]), reverse=direction_descending)
            rows = present + absent
        total_rows = len(rows)
        rows = rows[:limit]
    else:
        columns = [column for column in (plan.get("columns") or []) if column in frame.columns]
        unknown = [column for column in (plan.get("columns") or []) if column not in frame.columns]
        if unknown:
            raise TablePlanError(f"Column '{unknown[0]}' does not exist.")
        result_columns = list(dict.fromkeys([*group_by, *columns])) or list(frame.columns[:MAX_LISTED_COLUMNS])
        listing = matched
        sort_by = plan.get("sortBy")
        if sort_by in frame.columns:
            listing = listing.sort_values(sort_by, ascending=not direction_descending, na_position="last")
        total_rows = int(len(listing))
        rows = [list(row) for row in listing[result_columns].head(limit).itertuples(index=False)]
    return {
        "columns": result_columns,
        "rows": [[_display(value) for value in row] for row in rows],
        "totalResultRows": total_rows,
        "matchedRows": int(len(matched)),
        "sheetRows": int(len(frame)),
        "filters": filters,
        "groupBy": group_by,
        "metrics": metrics,
        "excludedEmpty": excluded,
        "isAggregate": bool(metrics),
    }


def describe(result: dict[str, Any], file_name: str, sheet: str, multiple_sheets: bool) -> str:
    """A deterministic account of what was computed. No model writes this text."""
    location = f"{file_name}{f' (sheet {sheet})' if multiple_sheets else ''}"
    lines: list[str] = []
    if result["isAggregate"] and not result["groupBy"]:
        values = result["rows"][0] if result["rows"] else []
        lines.append("; ".join(f"{label}: {value or 'no values'}" for label, value in zip(result["columns"], values)) + ".")
    elif result["isAggregate"]:
        shown = len(result["rows"])
        total = result["totalResultRows"]
        lines.append(
            f"Results by {' and '.join(result['groupBy'])}"
            + (f", showing {shown} of {total:,} groups." if total > shown else f" ({total:,} group{'s' if total != 1 else ''}).")
        )
    else:
        shown = len(result["rows"])
        total = result["totalResultRows"]
        if total == 0:
            lines.append("No rows match.")
        else:
            lines.append(f"{total:,} matching row{'s' if total != 1 else ''}" + (f", showing the first {shown}." if total > shown else "."))
    if result["filters"]:
        described = []
        for condition in result["filters"]:
            value = condition.get("value")
            quote = lambda item: str(item) if isinstance(item, (int, float)) and not isinstance(item, bool) else f'"{item}"'
            value_text = "" if condition["operator"] in {"is_empty", "is_not_empty"} else (
                ", ".join(quote(item) for item in value) if isinstance(value, list) else quote(value))
            described.append(f"{condition['column']} {OPERATOR_WORDS[condition['operator']]} {value_text}".strip())
        lines.append("Filters: " + "; ".join(described) + ".")
    lines.append(f"Computed over {result['matchedRows']:,} of {result['sheetRows']:,} rows in {location}.")
    for column, missing in result["excludedEmpty"].items():
        lines.append(f"{missing:,} matching row{'s' if missing != 1 else ''} with an empty {column} {'were' if missing != 1 else 'was'} left out of that calculation.")
    return "\n".join(lines)


def answer(connection: sqlite3.Connection, question: str, document_ids: list[str] | None,
           uploads_dir: Path, model: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Answer from spreadsheets in scope, or return None to fall through.

    Returns (result, trace). ``result`` carries the answer text, a source, and
    the result table for display.
    """
    tables = tables_in_scope(connection, document_ids)
    trace: dict[str, Any] = {"tablesInScope": len(tables)}
    if not tables:
        trace["reason"] = "No spreadsheets in scope."
        return None, trace
    if len(tables) > MAX_TABLES_IN_PLAN:
        trace["tablesTruncated"] = f"Planned over the {MAX_TABLES_IN_PLAN} most recent of {len(tables)} sheets."
        tables = tables[:MAX_TABLES_IN_PLAN]
    plan = plan_query(question, tables, model)
    trace["plan"] = plan
    # The model sometimes omits the flag on an otherwise complete plan, so only
    # an explicit refusal or a plan without a table counts as declining.
    if plan.get("useTable") is False or not plan.get("tableId"):
        trace["reason"] = "The planner judged this question is not answerable from the spreadsheets."
        return None, trace
    by_id = {f"{row['upload_id']}::{row['sheet']}": row for row in tables}
    table = by_id.get(str(plan.get("tableId")))
    if table is None:
        trace["reason"] = "The planner chose a table outside this scope."
        return None, trace
    path = uploads_dir / table["stored_name"]
    if not path.is_file():
        trace["reason"] = "The spreadsheet file is missing."
        return None, trace
    sheets = _cache.get(table["upload_id"], path)
    try:
        result = execute(sheets, table["sheet"], plan)
    except TablePlanError as error:
        trace["reason"] = f"The query plan could not be executed safely: {error}"
        return None, trace
    multiple = len(sheets) > 1
    trace["execution"] = {key: result[key] for key in ("matchedRows", "sheetRows", "totalResultRows", "excludedEmpty")}
    sheet_label = f"Sheet {table['sheet']} · " if multiple else ""
    return {
        "answer": describe(result, table["original_name"], table["sheet"], multiple),
        "sources": [{
            "documentId": table["upload_id"], "filename": table["original_name"], "pageNumber": 1,
            "pageLabel": f"{sheet_label}{result['matchedRows']:,} of {result['sheetRows']:,} rows",
            "chunkId": f"table:{table['upload_id']}:{table['sheet']}",
        }],
        "table": {"columns": result["columns"], "rows": result["rows"], "totalRows": result["totalResultRows"]},
    }, trace
