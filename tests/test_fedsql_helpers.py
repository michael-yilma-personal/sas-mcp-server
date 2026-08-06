# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the FedSQL helpers — pure logic, no MCP or network.

Cover the query screen (the injection boundary), the generated program, the
result shaping that turns stripped cells back into typed rows, and the log →
structured-error mapping.
"""

import pytest

from sas_mcp_server.helpers.fedsql_helpers import (
    build_cleanup_code,
    build_query_code,
    build_view_sql,
    convert_rows,
    describe_columns,
    error_lines,
    map_error,
    screen_query,
)

# --- screening: accepted ------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "select * from Public.HMEQ",
        "  SELECT a, b FROM t WHERE x > 1 ",
        "select a from t union select b from u",
        'select t.n from (select count(*) as n from x) t',
        "select JOB from t where JOB like '%ther%'",  # % inside single quotes is safe
        "select a from t -- trailing comment",
        "select a from t /* block ; comment */",
        "select a from t where s = 'a;b'",  # ; inside a literal
        "select a from t;",  # a single trailing ; is tolerated
    ],
)
def test_screen_accepts_selects(query):
    assert screen_query(query, 10, 0) is None


# --- screening: rejected ------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "fragment"),
    [
        ("update t set x=1", "UPDATE"),
        ("delete from t", "DELETE"),
        ("create table t as select 1 from x", "CREATE"),
        ("drop table t", "DROP"),
        ("merge into t using s on 1=1", "MERGE"),
        ("insert into t values (1)", "INSERT"),
        ("select 1 from t union all update t set x=1", "UPDATE"),
        ("select * from (delete from t) x", "DELETE"),
        ("select 1 from t; quit; data x; run;", "';'"),
        ("select a from t) qwrap limit 5 --", "parenthes"),
        ("select a from t /* oops", "block comment"),
        ("select a from 'my table'n", "name literal"),
        ("with t as (select 1 from x) select * from t", "single FedSQL SELECT"),
        ("", "required"),
        ("   ", "required"),
    ],
)
def test_screen_rejects(query, fragment):
    result = screen_query(query, 10, 0)
    assert result is not None
    assert result["status"] == "invalid_query"
    assert fragment.lower() in result["message"].lower()


@pytest.mark.parametrize(
    "query",
    [
        "select a from t where id = 'abc",
        'select a from t where id = "abc',
        "select a from t where id = 'it''s",
        "select 'a' , 'b from t",
    ],
)
def test_screen_rejects_unbalanced_quotes(query):
    """An unterminated literal HANGS the compute session (SAS keeps looking for
    the closing quote), wedging it for every later call — so it must never be
    submitted."""
    result = screen_query(query, 10, 0)
    assert result is not None
    assert "unbalanced quote" in result["message"]


@pytest.mark.parametrize(
    "query",
    [
        "select a from t where name = 'O''Brien'",
        "select a from t where id = 'U1001'' or ''1''=''1'",
        "select \"odd name\" from t where x = 'a'",
    ],
)
def test_screen_accepts_correctly_escaped_literals(query):
    """Doubled quotes are the SQL escape — a tautology payload is just a value."""
    assert screen_query(query, 10, 0) is None


@pytest.mark.parametrize(
    "query",
    [
        'select a from t where u = "&sysuserid"',  # resolves inside double quotes
        "select a from t where x = %sysfunc(getoption(work))",
        "select &col from t",
        "%put hello; select a from t",
    ],
)
def test_screen_blocks_macro_triggers(query):
    """The macro processor expands % and & before FedSQL ever runs — the real
    injection channel, and the reason a SELECT-shaped string is not enough."""
    result = screen_query(query, 10, 0)
    assert result is not None
    assert "macro" in result["message"].lower()


@pytest.mark.parametrize(("limit", "start"), [(0, 0), (-1, 0), (10_001, 0), (10, -1)])
def test_screen_rejects_out_of_range_paging(limit, start):
    assert screen_query("select 1 from t", limit, start)["status"] == "invalid_query"


@pytest.mark.parametrize("limit", [True, 1.5, "10", None])
def test_screen_requires_integer_limit(limit):
    """limit is the server-side row cap, so a non-integer must never reach the SQL."""
    assert screen_query("select 1 from t", limit, 0)["status"] == "invalid_query"


# --- generated code -----------------------------------------------------------


def test_compute_code_wraps_with_injected_limit():
    code = build_query_code("select a from WORK.t limit 999", target="compute", limit=5, start=0, uid="ab12")
    # The caller's LIMIT survives only inside the derived table; the cap that
    # decides how much comes back is ours, one row beyond the page.
    assert "limit 6" in code
    assert "create table work._qab12 as" in code
    assert "data work._fab12; set work._qab12; format _all_; run;" in code
    assert "options nosyntaxcheck;" in code
    assert "cas " not in code


def test_cas_code_creates_and_terminates_a_unique_session():
    code = build_query_code("select a from Public.T", target="cas", limit=3, start=2, uid="ff99")
    assert "cas _mcpqff99;" in code
    assert "cas _mcpqff99 terminate;" in code  # nothing outlives the query
    assert "sessref=_mcpqff99" in code
    assert "limit 6" in code  # start(2) + limit(3) + 1
    assert 'caslib="casuser"' in code


def test_cas_session_name_is_unique_per_call():
    """A fixed name made one failed query poison every later one."""
    a = build_query_code("select 1 from t", target="cas", limit=1, start=0, uid="aaaa")
    b = build_query_code("select 1 from t", target="cas", limit=1, start=0, uid="bbbb")
    assert "_mcpqaaaa" in a and "_mcpqaaaa" not in b


def test_build_view_sql_is_text_only():
    sql = build_view_sql("select a from t;", "myview")
    assert sql == "create view myview as\nselect a from t;"


def test_cleanup_drops_both_scratch_tables():
    assert "_qab12 _fab12" in build_cleanup_code("ab12")


# --- result shaping -----------------------------------------------------------


def test_describe_columns_extracts_format_names():
    items = [
        {"name": "amt", "type": "FLOAT", "format": {"name": "DOLLAR12.2", "decimals": 2}},
        {"name": "nm", "type": "CHAR"},
    ]
    assert describe_columns(items) == [
        {"name": "amt", "type": "FLOAT", "format": "DOLLAR12.2"},
        {"name": "nm", "type": "CHAR", "format": None},
    ]


def test_convert_rows_restores_dates_and_keeps_nulls():
    """Rows come from the format-stripped table (raw serials, real nulls); the
    formats from the unstripped twin turn the serials back into ISO text."""
    columns = [
        {"name": "d", "type": "FLOAT", "format": "DATE9."},
        {"name": "dt", "type": "FLOAT", "format": "DATETIME20."},
        {"name": "amt", "type": "FLOAT", "format": "DOLLAR12.2"},
        {"name": "nm", "type": "CHAR", "format": None},
    ]
    rows = convert_rows(
        [{"cells": [24107, 2082844800, 1234.5678, "ann"]}, {"cells": [None, None, None, ""]}],
        columns,
    )
    assert rows[0]["d"] == "2026-01-01"
    assert rows[0]["dt"].startswith("2026-01-01")
    assert rows[0]["amt"] == 1234.5678  # a currency format must not become text
    assert rows[0]["nm"] == "ann"
    assert rows[1] == {"d": None, "dt": None, "amt": None, "nm": ""}


def test_convert_rows_tolerates_short_cell_lists():
    rows = convert_rows([{"cells": [1]}], [{"name": "a", "type": "FLOAT", "format": None},
                                           {"name": "b", "type": "CHAR", "format": None}])
    assert rows == [{"a": 1}]


# --- error mapping ------------------------------------------------------------


def test_map_error_returns_none_without_errors():
    assert map_error("NOTE: PROCEDURE FEDSQL used") is None


@pytest.mark.parametrize(
    ("log", "status", "fragment"),
    [
        ('ERROR: Table "PUBLIC.NOPE" does not exist or cannot be accessed', "table_not_found", "PUBLIC.NOPE"),
        ("ERROR: BASE driver, schema name PCAS was not found", "schema_not_found", "PCAS"),
        (
            "ERROR: BASE driver, schema name SASHELP was not found",
            "schema_not_found",
            "CONCATENATED",
        ),
        ("ERROR: The caslib NopeLib does not exist", "schema_not_found", "NopeLib"),
        ('ERROR: Column "X" not found or cannot be accessed', "column_not_found", "X"),
        ('ERROR: Column reference "id" is ambiguous', "ambiguous_column", "alias"),
        ("ERROR: Unsupported SQL statement", "unsupported_statement", "MERGE"),
        ("ERROR: something entirely new", "query_failed", "something entirely new"),
    ],
)
def test_map_error_statuses(log, status, fragment):
    mapped = map_error(log)
    assert mapped["status"] == status
    assert fragment in mapped["message"]


def test_table_not_found_message_has_no_stray_quote():
    mapped = map_error('ERROR: Table "PUBLIC.NOPE" does not exist or cannot be accessed')
    assert 'PUBLIC.NOPE"' not in mapped["message"]


@pytest.mark.parametrize(
    ("token", "hint"),
    [("with", "derived table"), ("merge", "join"), ("top", "LIMIT"), ("value", "reserved word")],
)
def test_syntax_hints_are_dialect_specific(token, hint):
    mapped = map_error(f'ERROR: Syntax error at or near "{token}"')
    assert mapped["status"] == "syntax_error"
    assert hint.lower() in mapped["message"].lower()


def test_error_lines_drops_trailing_noise():
    log = (
        'ERROR: Table "T" does not exist or cannot be accessed\n'
        "ERROR: The action stopped due to errors.\n"
        "ERROR: The FedSQL action was not successful.\n"
    )
    assert error_lines(log) == ['ERROR: Table "T" does not exist or cannot be accessed']


def test_map_error_reports_the_first_real_error():
    log = "NOTE: fine\nERROR: BASE driver, schema name ZZ was not found\nERROR: Fatal error.\n"
    mapped = map_error(log)
    assert mapped["status"] == "schema_not_found"
    assert len(mapped["sas_errors"]) == 1
