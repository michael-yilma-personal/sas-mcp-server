# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Static vocabularies for the FedSQL ``query_data`` tool.

The frozen data that drives ``fedsql_helpers.py``, kept apart from the functions
so each table can be read, audited, and extended on its own:

* the screening patterns — what a submitted statement must look like, and the
  masks that let the structural checks ignore comments and literals;
* :data:`WRITE_VERBS` / :data:`WRITE_VERB_RE` — the statement verbs this tool
  refuses, and :data:`RESERVED_WORDS`, used to sharpen a syntax error with the
  "double-quote it" remedy;
* the SAS format-name prefixes that mark a numeric column as a date, datetime,
  or time serial, plus the epoch those serials count from;
* :data:`ERROR_RULES` — the log-line → (status, remediation) table that turns a
  raw FedSQL failure into something a model can act on, and :data:`ERROR_NOISE`,
  the trailing lines every failure appends.

Data only — anything that executes lives in ``fedsql_helpers.py``. Adding a
dialect fact (a new reserved word, another date format, one more error the
service emits) is a one-line edit here that the screen, the result shaping, and
the error mapping all pick up.
"""

from __future__ import annotations

import datetime as _dt
import re

# --- request screening -------------------------------------------------------

# NOTE (live-verified): PROC FEDSQL does NOT honour a libref's
# ``access=readonly``. A DATA step writing to such a libref is denied ("Write
# access to member ... is denied"), but FedSQL's BASE driver talks to the
# underlying directory and happily runs CREATE TABLE, UPDATE, DELETE and DROP
# against it. So there is no library-level backstop behind this screen: on the
# compute tier the screen is the *only* thing standing between a submitted
# statement and a write. (CAS is different — caslib access is enforced by Viya
# authorisation against the caller's identity, which FedSQL cannot bypass.)
LEADING_SELECT = re.compile(r"^\s*(?:select|\(\s*select)\b", re.IGNORECASE)
# Line (--) and block (/* */) comments, and quoted strings, stripped before the
# structural checks so a ';' inside a comment or literal doesn't false-trigger.
COMMENTS_AND_STRINGS = re.compile(
    r"--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"", re.DOTALL
)
# Comments and SINGLE-quoted strings only. SAS resolves macro triggers inside
# double quotes but not single quotes, so the macro scan must still see
# double-quoted text while ignoring 'like ''%son%''' patterns.
COMMENTS_AND_SQ_STRINGS = re.compile(r"--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'", re.DOTALL)
# SAS name literals ('my table'n) are not FedSQL; they parse as a string
# followed by a stray token and produce a confusing error.
NAME_LITERAL = re.compile(r"'(?:[^']|'')*'\s*n\b", re.IGNORECASE)
# The query is embedded in a SAS program, so the MACRO PROCESSOR sees it before
# FedSQL does: a %macro call or &var reference would expand — and execute —
# outside SQL entirely. This is the real injection channel, and it is closed
# regardless of how the tool is classified.
MACRO_TRIGGER = re.compile(r"[%&][A-Za-z_]")

# Statement verbs that write. The leading-SELECT rule already rejects them at
# the start; this catches one smuggled deeper (e.g. behind a union) and, more
# usefully, answers with the specific reason rather than a generic one.
WRITE_VERBS: tuple[str, ...] = (
    "insert", "update", "delete", "create", "drop", "alter", "truncate", "merge",
    "grant", "revoke", "execute", "call", "commit", "rollback", "replace", "load",
)
WRITE_VERB_RE = re.compile(r"\b(" + "|".join(WRITE_VERBS) + r")\b", re.IGNORECASE)

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

# Upper bound on the rows one call may return; the tool clamps ``limit`` to it.
MAX_LIMIT = 10_000


# --- value conversion --------------------------------------------------------

# Format-name prefixes whose numeric values are SAS date/datetime/time serials.
# Checked longest-first so DATETIME wins over DATE.
DATETIME_FORMATS: tuple[str, ...] = ("DATETIME", "E8601DT", "B8601DT", "NLDATM")
DATE_FORMATS: tuple[str, ...] = (
    "DATE", "DDMMYY", "MMDDYY", "YYMMDD", "YYMMDDD", "JULIAN", "JULDAY", "MONYY",
    "WORDDATE", "WEEKDATE", "E8601DA", "B8601DA", "NLDATE", "DOWNAME", "MONTH", "YEAR",
)
TIME_FORMATS: tuple[str, ...] = ("TIME", "HHMM", "TOD", "E8601TM", "B8601TM", "NLTIME")
# SAS counts days (and seconds) from 1960-01-01.
SAS_EPOCH = _dt.date(1960, 1, 1)


# --- error mapping -----------------------------------------------------------

# Log-line patterns -> (status, remediation). Ordered: the first match wins, so
# specific patterns precede generic ones. Every message names the next action,
# because the model reading it has to fix the query without seeing the log.
ERROR_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
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
ERROR_NOISE = re.compile(
    r"^ERROR:\s*(The action stopped due to errors|The FedSQL action was not successful"
    r"|Fatal error|Execution error)\.?\s*$",
    re.I,
)
