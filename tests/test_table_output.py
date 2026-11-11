"""Explicit table wrappers preserve JSON business results and finite snapshots."""
import json
from types import SimpleNamespace

import pytest

from rc_policy_sdk import tables
from rc_policy_sdk.command_runtime import CommandExecutor
from rc_policy_sdk.runtime import PolicyRuntime
from rc_policy_sdk.wire import MAX_MESSAGE_BYTES, ProtocolError


def test_table_output_is_public_and_preserves_false_zero_null_and_multiple_tables():
    from rc_policy_sdk import Table, TableOutput
    raw = {"values": [False, 0, None]}
    output = TableOutput(result=raw, tables=[
        Table(title="[bold]结果[/bold]", columns=["a", "b"], rows=[[False, {"x": [0, None]}]]),
        Table(columns=["empty"], rows=[]),
    ])
    result, document = tables.normalize_table_output(output)
    assert result == raw
    assert document == {"schema_version": 1, "tables": [
        {"title": "[bold]结果[/bold]", "columns": ["a", "b"], "rows": [[False, {"x": [0, None]}]]},
        {"title": "", "columns": ["empty"], "rows": []},
    ]}
    assert tables.table_document_text(document) == '[bold]结果[/bold]\na\tb\nfalse\t{"x":[0,null]}\n\nempty\n无记录'


def test_empty_rows_remain_readable_but_empty_tables_and_columns_are_rejected():
    table = tables.Table(title="No data", columns=["value"], rows=[])
    document = tables.normalize_table_output(tables.TableOutput(result=None, tables=[table]))[1]
    assert tables.table_document_text(document) == "No data\nvalue\n无记录"
    with pytest.raises(ValueError, match="column"):
        tables.Table()
    with pytest.raises(ValueError, match="Table"):
        tables.TableOutput(result=None)
    with pytest.raises(TypeError):
        tables.TableOutput()


@pytest.mark.parametrize("invalid", [
    {"title": 0}, {"columns": [1]}, {"columns": "name"},
    {"columns": ["a"], "rows": [[1, 2]]}, {"columns": ["a"], "rows": ["x"]},
    {"columns": ["a"], "rows": [[float("nan")]]},
    {"columns": ["a"], "rows": [[float("inf")]]},
    {"columns": ["a"], "rows": [[object()]]},
    {"columns": ["a"], "rows": [[{1: "one", "1": "duplicate"}]]},
])
def test_table_rejects_invalid_shape_and_nonfinite_or_duplicate_json(invalid):
    with pytest.raises((TypeError, ValueError)):
        tables.Table(**invalid)


def test_snapshot_detaches_all_mutable_data_and_enforces_frame_budget():
    values = {"items": [1]}
    rows = [[{"nested": [2]}]]
    table = tables.Table(columns=["data"], rows=rows)
    output = tables.TableOutput(result=values, tables=[table])
    result, document = tables.normalize_table_output(output)
    values["items"].append(9)
    rows[0][0]["nested"].append(8)
    table.rows[0][0]["nested"].append(7)
    output.result["items"].append(6)
    assert result == {"items": [1]}
    assert document["tables"][0]["rows"] == [[{"nested": [2]}]]
    with pytest.raises(ProtocolError, match="exceeds"):
        tables.TableOutput(result="x" * MAX_MESSAGE_BYTES, tables=[table])
    with pytest.raises(ProtocolError, match="duplicate"):
        tables.TableOutput(result={1: "one", "1": "duplicate"}, tables=[table])


@pytest.mark.parametrize("document", [
    {"schema_version": True, "tables": []}, {"schema_version": 2, "tables": []},
    {"schema_version": 1, "tables": [], "unknown": 1},
    {"schema_version": 1, "tables": [{"title": "", "columns": [], "rows": [[1]]}]},
])
def test_document_rejects_unversioned_or_ambiguous_shapes(document):
    with pytest.raises(ValueError):
        tables.normalize_table_document(document)


def invoke_result(value, *, enabled=True):
    packets = []
    channel = SimpleNamespace(send=packets.append, table_output_v1=enabled)
    executor = CommandExecutor(channel, None, {}, {"call": lambda _: value}, 1)
    try:
        return executor._invoke({"command_id": "cmd", "config_version": 1, "name": "call", "args": []})
    finally:
        executor.close()


def test_command_unwraps_only_explicit_wrapper_and_preserves_original_result():
    plain = {"table": {}, "tables": [], "table_document": {"rows": []}}
    assert invoke_result(plain)["result"] == plain
    assert "table_document" not in invoke_result(plain)
    result = invoke_result(tables.TableOutput(result=False, tables=[tables.Table(columns=["ok"], rows=[[False]])]))
    assert result["type"] == "command_result"
    assert result["result"] is False
    assert result["table_document"]["tables"][0]["rows"] == [[False]]
    with pytest.raises(RuntimeError, match="upgrade"):
        invoke_result(tables.TableOutput(result=0, tables=[tables.Table(columns=["x"])]), enabled=False)


def test_runtime_logs_snapshot_current_context_version_and_request_without_control_calls():
    packets = []
    channel = SimpleNamespace(context_id="ctx", config_version=2, table_output_v1=True,
                              send_log=lambda message: packets.append(message) or True)
    runtime = PolicyRuntime(channel, {"context_id": "ctx", "config_version": 1, "request_id": 4, "generation": 8})
    output = tables.TableOutput(result={"ok": 0}, tables=[tables.Table(columns=["ok"], rows=[[0]])])
    assert runtime.log_table(output, level="warning") is True
    output.result["ok"] = 9
    assert packets[0]["result"] == {"ok": 0}
    assert {key: packets[0][key] for key in ("context_id", "config_version", "request_id", "generation")} == {
        "context_id": "ctx", "config_version": 2, "request_id": 4, "generation": 8}
    assert packets[0]["level"] == "warning"
    assert packets[0]["message"] == "ok\n0"
    channel.context_id = "replacement"
    with pytest.raises(RuntimeError, match="no longer active"):
        runtime.log_table(output)
    channel.context_id = "ctx"
    channel.table_output_v1 = False
    with pytest.raises(RuntimeError, match="upgrade"):
        runtime.log_table(output)


def test_runtime_rejects_non_wrapper_and_invalid_levels():
    runtime = PolicyRuntime(SimpleNamespace(context_id=None, table_output_v1=True, send_log=lambda _: True))
    with pytest.raises(TypeError):
        runtime.log_table({"result": 1, "tables": []})
    with pytest.raises(ValueError):
        runtime.log_table(tables.TableOutput(result=1, tables=[tables.Table(columns=["x"])]), level="surprise")


def test_log_table_complete_record_obeys_frame_budget_and_no_row_truncation():
    from rc_policy_sdk.wire import encode_message
    packets = []
    def send(message):
        encode_message(message)
        packets.append(message)
        return True
    runtime = PolicyRuntime(SimpleNamespace(context_id=None, table_output_v1=True, send_log=send))
    output = tables.TableOutput(result=False, tables=[tables.Table(columns=['n'], rows=[[n] for n in range(10000)])])
    assert runtime.log_table(output)
    assert len(packets[0]['table_document']['tables'][0]['rows']) == 10000
    large = tables.TableOutput(result=None, tables=[tables.Table(columns=['text'], rows=[['x' * (MAX_MESSAGE_BYTES//2)]])])
    with pytest.raises(ProtocolError, match='exceeds'):
        runtime.log_table(large)
    assert len(packets) == 1
