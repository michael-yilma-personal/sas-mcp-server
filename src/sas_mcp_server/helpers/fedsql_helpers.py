# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Helpers for the FedSQL ``query_data`` tool (Tier 1, ``tools/discovery.py``).

One SQL surface over both storage tiers. FedSQL is the dialect because it is the
only one that speaks to compute librefs *and* CAS caslibs, and because its
``LIMIT`` clause caps rows server-side — a query against a billion-row CAS table
returns the page asked for, not the table.

Everything runs through the **compute** session (never a direct CAS REST
session): compute is the only route that reaches both tiers, and it reuses the
warm per-user session the other compute tools already share.

Three live-verified constraints shape this module:

* **The caller's ``LIMIT`` cannot be trusted.** CAS silently *discards* a
  malformed one (``limit 'two'`` returns the whole table), so :func:`build_query_code`
  ignores any limit in the query text and wraps it in its own derived table with
  an injected integer cap. One row beyond the page is fetched to detect
  truncation.
* **Cross-tier joins are impossible in a single statement.** ``SESSREF=`` makes
  PROC FEDSQL ignore the LIBS option, so a caslib table and a WORK table cannot
  be joined in one statement on any route. ``target`` selects the namespace;
  joining across tiers means staging one side first.
* **A formatted value serialises as a padded *string*** (``"   $1,234.57"``),
  and a formatted missing as ``"         ."`` rather than null. The generated
  code therefore materialises a format-stripped copy for the row read, while
  column metadata (and thus the formats) is read from the unstripped table —
  which is how :func:`convert_rows` can turn date serials back into ISO text.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any

# --- dialect facts -----------------------------------------------------------

# Statement forms FedSQL accepts that this tool deliberately does not: it runs
# SELECTs and returns rows, so anything that writes is refused up front (the
# real boundary is the write classification in tools/_access.py — this is the
# helpful error, not the security control).
_LEADING_SELECT = re.compile(r"^\s*(?:select|\(\s*select)\b", re.IGNORECASE)
# Line (--) and block (/* */) comments, and quoted strings, stripped before the
# structural checks so a ';' inside a comment or literal doesn't false-trigger.
_COMMENTS_AND_STRINGS = re.compile(
    r"--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"", re.DOTALL
)
# Comments and SINGLE-quoted strings only. SAS resolves macro triggers inside
# double quotes but not single quotes, so the macro scan must still see
# double-quoted text while ignoring 'like ''%son%''' patterns.
_COMMENTS_AND_SQ_STRINGS = re.compile(r"--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'", re.DOTALL)
# SAS name literals ('my table'n) are not FedSQL; they parse as a string
# followed by a stray token and produce a confusing error.
_NAME_LITERAL = re.compile(r"'(?:[^']|'')*'\s*n\b", re.IGNORECASE)
# The query is embedded in a SAS program, so the MACRO PROCESSOR sees it before
# FedSQL does: a %macro call or &var reference would expand — and execute —
# outside SQL entirely. This is the real injection channel, and it is closed
# regardless of how the tool is classified.
_MACRO_TRIGGER = re.compile(r"[%&][A-Za-z_]")
# Statement verbs that write. The leading-SELECT rule already rejects them at
# the start; this catches one smuggled deeper (e.g. behind a union) and, more
# usefully, answers with the specific reason rather than a generic one.
_WRITE_VERBS = (
    "insert", "update", "delete", "create", "drop", "alter", "truncate", "merge",
    "grant", "revoke", "execute", "call", "commit", "rollback", "replace", "load",
)
_WRITE_VERB_RE = re.compile(r"\b(" + "|".join(_WRITE_VERBS) + r")\b", re.IGNORECASE)

# FedSQL reserved words that regularly appear as column names. Used only to
# sharpen a syntax-error message with the "double-quote it" remedy.
RESERVED_WORDS: frozenset[str] = frozenset(
    {
        "case", "cast", "date", "day", "end", "group", "having", "hour", "index",
        "join", "key", "left", "level", "match", "merge", "minute", "month",
        "order", "outer", "right", "row", "rows", "select", "table", "time",
        "timestamp", "type", "user", "value", "values", "when", "where", "year",
    }
)

# Format-name prefixes whose numeric values are SAS date/datetime/time serials.
# Checked longest-first so DATETIME wins over DATE.
_DATETIME_FORMATS = ("DATETIME", "E8601DT", "B8601DT", "NLDATM")
_DATE_FORMATS = (
    "DATE", "DDMMYY", "MMDDYY", "YYMMDD", "YYMMDDD", "JULIAN", "JULDAY", "MONYY",
    "WORDDATE", "WEEKDATE", "E8601DA", "B8601DA", "NLDATE", "DOWNAME", "MONTH", "YEAR",
)
_TIME_FORMATS = ("TIME", "HHMM", "TOD", "E8601TM", "B8601TM", "NLTIME")
# SAS counts days (and seconds) from 1960-01-01.
_SAS_EPOCH = _dt.date(1960, 1, 1)

MAX_LIMIT = 10_000


# --- request screening -------------------------------------------------------


def screen_query(query: str, limit: int, start: int) -> dict[str, Any] | None:
    """Return a structured error for an unusable request, else ``None``.

    Rejects anything that is not a single SELECT: the tool returns rows and
    never creates objects, and a stray ``;`` would otherwise let arbitrary SAS
    ride along behind ``quit;``.
    """
    if not query or not query.strip():
        return _invalid("query is required — pass a single FedSQL SELECT statement.")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return _invalid(f"limit must be a positive integer (got {limit!r}).")
    if limit > MAX_LIMIT:
        return _invalid(
            f"limit must be <= {MAX_LIMIT} (got {limit}). Page through larger "
            "results with start=, or aggregate in the query."
        )
    if not isinstance(start, int) or isinstance(start, bool) or start < 0:
        return _invalid(f"start must be a non-negative integer (got {start!r}).")

    # Quote balance first — every later mask depends on quoting being sane, and
    # an unterminated literal is the one input that HANGS rather than fails: SAS
    # keeps consuming, looking for the closing quote, so the job never returns
    # and the warm compute session is wedged for every later call (including
    # execute_sas_code). Verified live.
    structural = _COMMENTS_AND_STRINGS.sub(lambda m: " " * len(m.group()), query)
    if "'" in structural or '"' in structural:
        return _invalid(
            "unbalanced quote — every string literal and quoted identifier must "
            "be closed. An unterminated quote would hang the compute session "
            "rather than fail. Escape a quote inside a literal by doubling it: "
            "'O''Brien'."
        )

    # Macro scan next: it is the only check guarding a channel *outside* SQL,
    # so its message must not be pre-empted by a syntax complaint.
    macro_masked = _COMMENTS_AND_SQ_STRINGS.sub(lambda m: " " * len(m.group()), query)
    if _MACRO_TRIGGER.search(macro_masked):
        return _invalid(
            "SAS macro triggers (% and &) are not allowed — the macro processor "
            "would expand them before FedSQL runs, outside SQL entirely. Use "
            "single quotes for LIKE patterns ('%son%'), which the macro processor "
            "leaves alone."
        )

    # Remaining structural checks run against the same masked copy, so a ';' or
    # a paren inside a literal or comment can't trip them.
    masked = structural
    if masked.count("/*") != masked.count("*/"):
        return _invalid(
            "unterminated block comment — an open /* would swallow the rest of "
            "the generated program."
        )
    if not _LEADING_SELECT.match(masked):
        verb = _WRITE_VERB_RE.search(masked)
        if verb:
            return _invalid(
                f"'{verb.group(1).upper()}' is not allowed — this tool runs a single "
                "read-only SELECT and never modifies data. Use execute_sas_code for "
                "DDL/DML. (FedSQL has no MERGE; express a merge as a join.)"
            )
        return _invalid(
            "query must be a single FedSQL SELECT statement. This tool only reads "
            "rows — use execute_sas_code for DDL/DML, and describe the result you "
            "want as a SELECT (joins, GROUP BY, and subqueries are supported)."
        )
    # A write verb *after* a valid SELECT start means it was smuggled deeper —
    # behind a set operator or an unbalanced paren.
    verb = _WRITE_VERB_RE.search(masked)
    if verb:
        return _invalid(
            f"'{verb.group(1).upper()}' is not allowed anywhere in the query — this "
            "tool runs a single read-only SELECT. Use execute_sas_code for DDL/DML."
        )
    if ";" in masked.rstrip().rstrip(";"):
        return _invalid(
            "pass exactly one SELECT statement — remove the ';' separators "
            "(multiple statements are not accepted)."
        )
    if masked.count("(") != masked.count(")"):
        return _invalid(
            "unbalanced parentheses — the query is wrapped in a derived table, so "
            "an extra ')' would break out of it."
        )
    if _NAME_LITERAL.search(query):
        return _invalid(
            "SAS name literals ('my table'n) are not FedSQL. Quote irregular "
            'identifiers with double quotes instead: "my table".'
        )
    return None


def _invalid(message: str) -> dict[str, Any]:
    return {"status": "invalid_query", "message": message}


# --- code generation ---------------------------------------------------------


def build_view_sql(query: str, view_name: str) -> str:
    """The ``CREATE VIEW`` text for *query* — returned to the caller, never run.

    CAS accepts only SELECT / CTAS / DROP TABLE, so this is compute-dialect
    FedSQL the caller can run themselves (e.g. via ``execute_sas_code``) to
    persist the query as a view.
    """
    return f"create view {view_name} as\n{query.strip().rstrip(';')};"


def build_query_code(
    query: str,
    *,
    target: str,
    limit: int,
    start: int,
    uid: str,
    caslib: str = "casuser",
) -> str:
    """Generate the SAS program that materialises one page of *query*.

    Both targets end with two WORK tables: ``_q<uid>`` (formats intact — the
    column-metadata source) and ``_f<uid>`` (formats stripped — the row source,
    where numerics are numbers and missings are null).

    For ``target="cas"`` the CAS session is created *and terminated inside the
    same job*, so nothing outlives the query: the session-scoped result table
    dies with it, and no CAS session can leak past the compute session that
    owns it.

    Two hard-won details make that reliable across a *failed* query:

    * ``options nosyntaxcheck`` — a PROC FEDSQL error otherwise puts SAS into
      syntax-check mode, which silently skips every later statement including
      the ``terminate``, leaking the session.
    * the CAS session name carries *uid*, so even a session that does leak (a
      cancelled job, say) can never collide with the next query's ``cas``
      statement. A fixed name turned one failed query into an unrecoverable
      compute session, with every later query failing to start.
    """
    inner = query.strip().rstrip(";")
    # +1 row beyond the page is the truncation probe.
    cap = start + limit + 1
    select = f"select * from (\n{inner}\n) qwrap limit {cap}"
    preamble = "options nosyntaxcheck;\n"
    if target == "cas":
        return (
            f"{preamble}"
            f"cas _mcpq{uid};\n"
            f'libname _mcpql cas caslib="{caslib}" sessref=_mcpq{uid};\n'
            f"proc fedsql sessref=_mcpq{uid};\n"
            f"  create table {caslib}._q{uid} {{options replace=true}} as\n  {select};\n"
            f"quit;\n"
            f"data work._q{uid}; set _mcpql._q{uid}; run;\n"
            f"data work._f{uid}; set work._q{uid}; format _all_; run;\n"
            f"libname _mcpql clear;\n"
            f"cas _mcpq{uid} terminate;\n"
        )
    return (
        f"{preamble}"
        f"proc fedsql;\n"
        f"  create table work._q{uid} as\n  {select};\n"
        f"quit;\n"
        f"data work._f{uid}; set work._q{uid}; format _all_; run;\n"
    )


def build_cleanup_code(uid: str) -> str:
    """Drop the scratch tables (best effort; WORK dies with the session anyway)."""
    return f"proc datasets library=work nolist nowarn; delete _q{uid} _f{uid}; quit;\n"


# --- result shaping ----------------------------------------------------------


def _format_family(format_name: str | None) -> str | None:
    if not format_name:
        return None
    name = format_name.upper().lstrip("$")
    for prefix in _DATETIME_FORMATS:
        if name.startswith(prefix):
            return "datetime"
    for prefix in _TIME_FORMATS:
        if name.startswith(prefix):
            return "time"
    for prefix in _DATE_FORMATS:
        if name.startswith(prefix):
            return "date"
    return None


def describe_columns(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reduce the compute ``/columns`` payload to name/type/format triples."""
    columns: list[dict[str, Any]] = []
    for item in items:
        fmt = item.get("format")
        format_name = fmt.get("name") if isinstance(fmt, dict) else fmt
        columns.append(
            {
                "name": item.get("name"),
                "type": item.get("type"),
                "format": format_name,
            }
        )
    return columns


def convert_rows(
    row_items: list[dict[str, Any]], columns: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Zip raw cells onto column names, restoring dates as ISO text.

    Rows come from the format-stripped table, so a date arrives as its SAS
    serial (``24107``). The formats survive on the *unstripped* table's column
    metadata, which is what lets these become ``"2026-01-01"`` again — readable
    without reintroducing the padded-string/`"."`-missing problems that reading
    formatted values would bring back.
    """
    names = [c["name"] for c in columns]
    families = [_format_family(c.get("format")) for c in columns]
    rows: list[dict[str, Any]] = []
    for item in row_items:
        cells = item.get("cells", [])
        row: dict[str, Any] = {}
        for name, family, value in zip(names, families, cells, strict=False):
            row[name] = _convert_cell(value, family)
        rows.append(row)
    return rows


def _convert_cell(value: Any, family: str | None) -> Any:
    if family is None or not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    try:
        if family == "date":
            return (_SAS_EPOCH + _dt.timedelta(days=int(value))).isoformat()
        if family == "datetime":
            return (
                _dt.datetime(1960, 1, 1) + _dt.timedelta(seconds=float(value))
            ).isoformat(sep=" ")
        if family == "time":
            return str(_dt.timedelta(seconds=float(value)))
    except (OverflowError, ValueError):
        return value
    return value


# --- error mapping -----------------------------------------------------------

# Log-line patterns -> (status, remediation). Ordered: the first match wins, so
# specific patterns precede generic ones. Every message names the next action,
# because the model reading it has to fix the query without seeing the log.
_ERROR_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r'Table "?([\w.]+)"? does not exist or cannot be accessed', re.I),
        "table_not_found",
        "Table {0} was not found. Check the qualifier: on target='cas' tables are "
        "caslib.table (list_castables); on target='compute' they are libref.table "
        "(list_compute_tables). Session-scoped tables vanish with their session.",
    ),
    (
        re.compile(r"BASE driver, schema name (\w+) was not found", re.I),
        "schema_not_found",
        "Libref '{0}' is not visible to FedSQL. Two causes: (1) wrong tier — "
        "caslibs are reachable only with target='cas' and librefs only with "
        "target='compute', and the two cannot be joined in one statement; "
        "(2) it is a CONCATENATED libref (several directories under one name, "
        "as SASHELP and MAPS are). FedSQL's BASE driver maps one schema to one "
        "directory, so it skips concatenated librefs entirely — copy the table "
        "into WORK first (data work.t; set sashelp.cars; run;) and query WORK.",
    ),
    (
        re.compile(r"The caslib (\w+) does not exist", re.I),
        "schema_not_found",
        "Caslib '{0}' does not exist or is not assigned. list_caslibs shows the "
        "available ones.",
    ),
    (
        re.compile(r'Column reference "?([\w.]+)"? is ambiguous', re.I),
        "ambiguous_column",
        "Column '{0}' is ambiguous — qualify it with its table alias "
        "(e.g. a.{0}).",
    ),
    (
        re.compile(r'Column "?([\w.]+)"? not found or cannot be accessed', re.I),
        "column_not_found",
        "Column '{0}' was not found. get_castable_columns / list_compute_columns "
        "list the real names. Note FedSQL reports this same error when the FROM "
        "clause is missing entirely.",
    ),
    (
        re.compile(r'Syntax error at or near "?([\w]+)"?', re.I),
        "syntax_error",
        "Syntax error at '{0}'.",
    ),
    (
        re.compile(r"Unsupported SQL statement", re.I),
        "unsupported_statement",
        "That statement is not supported here. This tool runs a single SELECT; "
        "FedSQL has no MERGE — express the merge as a join (a full join with "
        "COALESCE emulates an upsert view of two tables).",
    ),
)

# Trailing noise every FedSQL failure appends; dropped so the caller sees the
# one line that names the problem.
_NOISE = re.compile(
    r"^ERROR:\s*(The action stopped due to errors|The FedSQL action was not successful"
    r"|Fatal error|Execution error)\.?\s*$",
    re.I,
)


def error_lines(log: str) -> list[str]:
    """The meaningful ``ERROR`` lines of a SAS log, noise removed."""
    return [
        line.strip()
        for line in log.split("\n")
        if line.lstrip().startswith("ERROR") and not _NOISE.match(line.strip())
    ]


def map_error(log: str) -> dict[str, Any] | None:
    """Map a failed run's log to a structured, actionable error dict.

    Returns ``None`` when the log carries no error. **A job's state alone is not
    a success signal** — a discarded ``LIMIT`` produces an ``ERROR`` line while
    the job still reports ``completed`` — so callers must consult this on every
    run, not only on a non-completed state.
    """
    errors = error_lines(log)
    if not errors:
        return None
    first = errors[0]
    for pattern, status, template in _ERROR_RULES:
        match = pattern.search(first)
        if not match:
            continue
        message = template.format(*match.groups())
        if status == "syntax_error":
            message = f"{message} {_syntax_hint(match.group(1))}"
        return {"status": status, "message": message, "sas_errors": errors[:3]}
    return {
        "status": "query_failed",
        "message": f"FedSQL rejected the query: {first}",
        "sas_errors": errors[:3],
    }


def _syntax_hint(token: str) -> str:
    lowered = token.lower()
    if lowered == "with":
        return (
            "FedSQL has no WITH/CTE — rewrite it as a derived table: "
            'select ... from (select ...) "t".'
        )
    if lowered == "merge":
        return (
            "FedSQL has no MERGE statement — express it as a join (full join "
            "with COALESCE for upsert semantics)."
        )
    if lowered in ("top", "rownum"):
        return "Row capping uses the LIMIT clause; pass the tool's limit= instead."
    if lowered in RESERVED_WORDS:
        return (
            f'"{token}" is a FedSQL reserved word — double-quote it to use it as '
            f'an identifier: "{token}".'
        )
    return "Check the clause order: SELECT ... FROM ... WHERE ... GROUP BY ... HAVING ... ORDER BY."
