"""Explicit, finite JSON table output; no renderer or model dependencies.

Document v1 is ``{schema_version: 1, tables: [{title, columns, rows}, ...]}``.
There is at least one table and one string column per table. Row widths match
columns exactly; cells are finite JSON values. No row/column truncation occurs.
The normal wire frame budget applies to each detached output snapshot.
"""
from dataclasses import dataclass, field
import json

from .wire import encode_message, _object


def _snapshot(value):
    # Round-trip applies the receiver's duplicate-key rule (e.g. 1 and "1")
    # as well as finite values, UTF-8 encoding and the existing frame budget.
    return json.loads(encode_message(value)[4:], object_pairs_hook=_object)


def _table_data(title, columns, rows):
    if not isinstance(title, str):
        raise ValueError("table title must be a string")
    if not isinstance(columns, (list, tuple)) or not columns or any(not isinstance(c, str) for c in columns):
        raise ValueError("table columns must contain at least one string column")
    if not isinstance(rows, (list, tuple)):
        raise ValueError("table rows must be a sequence")
    if any(not isinstance(row, (list, tuple)) or len(row) != len(columns) for row in rows):
        raise ValueError("table row width must match columns")
    return {"title": title, "columns": list(columns), "rows": [list(row) for row in rows]}


@dataclass(frozen=True)
class Table:
    title: str = ""
    columns: object = field(default_factory=tuple)
    rows: object = field(default_factory=tuple)

    def __post_init__(self):
        value = _snapshot(_table_data(self.title, self.columns, self.rows))
        object.__setattr__(self, "columns", value["columns"])
        object.__setattr__(self, "rows", value["rows"])


@dataclass(frozen=True)
class TableOutput:
    result: object
    tables: object = field(default_factory=tuple)

    def __post_init__(self):
        result, document = normalize_table_output(self)
        object.__setattr__(self, "result", result)
        object.__setattr__(self, "tables", tuple(Table(**table) for table in document["tables"]))


def normalize_table_document(value) -> dict:
    """Validate v1 and return an independent finite JSON document snapshot."""
    if (not isinstance(value, dict) or set(value) != {"schema_version", "tables"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not isinstance(value["tables"], list) or not value["tables"]):
        raise ValueError("table document requires schema_version 1 and at least one table")
    data = []
    for table in value["tables"]:
        if not isinstance(table, dict) or set(table) != {"title", "columns", "rows"}:
            raise ValueError("table requires title, columns and rows")
        data.append(_table_data(**table))
    return _snapshot({"schema_version": 1, "tables": data})


def normalize_table_output(output: TableOutput) -> tuple:
    """Detach the original business result and its separate table document."""
    if not isinstance(output, TableOutput):
        raise TypeError("table output must be an explicit TableOutput")
    if (not isinstance(output.tables, (list, tuple)) or not output.tables
            or any(not isinstance(table, Table) for table in output.tables)):
        raise ValueError("TableOutput requires at least one Table")
    document = {"schema_version": 1, "tables": [
        _table_data(table.title, table.columns, table.rows) for table in output.tables]}
    snapshot = _snapshot({"result": output.result, "table_document": document})
    return snapshot["result"], snapshot["table_document"]


def table_cell_text(value) -> str:
    """String cells stay literal; other cells retain their complete JSON text."""
    return value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def table_document_text(document) -> str:
    """Stable row-major TSV projection, with a blank line between tables.

    Titles, columns and string cells remain literal (including newlines/markup).
    Renderers must escape control text. Empty rows display the text ``无记录``.
    """
    document = normalize_table_document(document)
    parts = []
    for table in document["tables"]:
        lines = [table["title"]] if table["title"] else []
        lines.append("\t".join(table["columns"]))
        lines.extend("\t".join(table_cell_text(cell) for cell in row) for row in table["rows"])
        if not table["rows"]:
            lines.append("无记录")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
