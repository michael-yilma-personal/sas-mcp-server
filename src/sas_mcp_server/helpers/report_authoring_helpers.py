# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Helpers for the Visual Analytics report-authoring tools.

Backs ``create_report``, ``apply_report_operations``, ``describe_report_objects``,
and ``get_report_outline`` (all Tier 3, in ``tools/reports.py``), keeping each
``@mcp.tool`` a thin wrapper — matching the ``helpers/`` pattern used by
``report_export_helpers.py``.

The SAS Visual Analytics REST API has a single authoring shape: you POST a new
report or PUT an existing one with an ordered ``operations`` array, and the
service applies every operation atomically (all succeed or nothing persists).
Rather than one tool per chart type, the authoring tools pass that native array
straight through — so a new VA object type needs no new tool — and this module
supplies the *validation* and *discovery* that make the generic surface safe.
The frozen configuration these functions consult (the object registry, the
operation catalog, placement and validation vocabularies) lives in
``report_authoring_registry.py``; this module holds only the functions:

* :func:`validate_operations` — structured, pre-flight checks (known object
  type, addable/updatable gating, data-role names + arity) returning an
  actionable error dict *before* any HTTP call, so the LLM self-corrects.
* :func:`describe` — progressive-disclosure discovery over the registry.
* :func:`execute_operations` — the GET-etag → ``If-Match`` → PUT round-trip
  (with a transparent 412 retry) that the caller never has to manage, plus an
  optional *save-as* mode (``resultReportName``/``resultFolder``) that applies
  the batch to a new report and leaves the source untouched.
* :func:`execute_outline` — the read-back path: reduces the stored report
  definition (BIRD content) to the page → object names/labels that placement,
  updateObject, and export_report consume.

The ETag is read from the generic Reports service resource
(``/reports/reports/{id}``) while the operations PUT targets the Visual
Analytics service (``/visualAnalytics/reports/{id}``) — the two-path handshake
the SAS sample notebooks use.
"""

from __future__ import annotations

import contextlib
import difflib
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from sas_mcp_server.config import VIYA_ENDPOINT
from sas_mcp_server.helpers.report_authoring_registry import (
    AGGREGATIONS,
    ALIASES,
    API_LIMITS,
    CATEGORIES,
    CLASSIFICATIONS,
    CONFLICT_VALUES,
    CONTENT_ACCEPT,
    CONTENT_PATH,
    COPY_PATH,
    CREATE_PATH,
    DELETE_PATH,
    ETAG_PATH,
    GEO_NAME_CODE_CONTEXTS,
    GEO_SOURCE_KEYS,
    INTENT_MAP,
    LAYOUT_RECIPES,
    META_OP_KEYS,
    NORMALIZED_NOT_ADDABLE,
    NORMALIZED_TYPES,
    NOT_ADDABLE,
    OBJECT_NOTES,
    OBJECT_SPEC_ALLOWED_KEYS,
    OBJECTS,
    OPERATION_KEYS,
    OPERATIONS,
    OPERATIONS_PATH,
    PLACEMENT_ALLOWED_KEYS,
    PLACEMENT_ENUMS,
    PLACEMENT_GUIDE,
    PLACEMENT_TARGET_REQUIRED,
    PLACEMENT_VARIANTS,
    REPORT_OBJECT_TYPES,
    SAS_FORMAT_RE,
    VaObject,
    normalize_key,
)

# --- discovery ------------------------------------------------------------


def _example_for(obj: VaObject) -> dict[str, Any]:
    """Build a minimal, copy-paste ``addObject`` payload for *obj*.

    The role-less content objects get their real payloads (probing showed the
    generic dataSource template is rejected or useless for them), and every
    other object carries an inline ``options.object.title`` — titled-at-add-time
    is the single cheapest polish an authoring agent can apply.
    """
    if obj.schema_key == "text":
        body: dict[str, Any] = {"options": {"content": "Your narrative text here."}}
    elif obj.schema_key == "image":
        body = {"options": {"url": "https://example.com/logo.png"}}
    elif obj.schema_key == "standardContainer":
        body = {}
    else:
        seed = obj.commonly_required or (obj.role_names[:1] if obj.role_names else ())
        roles: dict[str, Any] = {}
        for name in seed:
            spec = next((r for r in obj.roles if r.name == name), None)
            placeholder = f"<{name}Column>"
            roles[name] = [placeholder] if (spec and spec.multi) else placeholder
        body = {"dataSource": "<dataSourceName>"}
        if roles:
            body["dataRoles"] = roles
        # keyValue tiles render their measure's label prominently — a title
        # just duplicates it; name the measure well via dataItems instead.
        if obj.schema_key != "keyValue":
            body["options"] = {"object": {"title": "<Meaningful chart title>"}}
    return {"addObject": {"object": {obj.schema_key: body}, "placement": {"page": {"target": "<pageName>"}}}}


def describe(
    object_type: str | None = None,
    category: str | None = None,
    operation: str | None = None,
) -> dict[str, Any]:
    """Return the object/operation catalog, one object's contract, or one operation's shape.

    * No args → an index: the eight operations, every object (schema key,
      purpose, category, addable/updatable), the placement guide, layout
      recipes, an intent→object map, and the API's honest limits.
    * ``category`` → the index filtered to that category.
    * ``object_type`` → one object's roles (name + whether it takes a list),
      the commonly-required roles, common options, and a ready-to-send example
      payload; or a ``not_addable`` / ``unknown_object_type`` redirect.
      Colloquial aliases (``kpi``, ``choropleth``, ...) resolve to the nearest
      schema key.
    * ``operation`` → that operation's full entry (purpose, required keys,
      worked example, notes).
    """
    if object_type:
        return _describe_one(object_type)
    if operation:
        return _describe_operation(operation)

    objects = [
        {
            "schema_key": o.schema_key,
            "purpose": o.purpose,
            "category": o.category,
            "addable": o.addable,
            "updatable": o.updatable,
        }
        for o in OBJECTS
        if category is None or o.category == category
    ]
    result: dict[str, Any] = {
        "operations": [{"key": op["key"], "purpose": op["purpose"]} for op in OPERATIONS],
        "categories": list(CATEGORIES),
        "objects": objects,
        "intent_map": dict(INTENT_MAP),
        "placement": list(PLACEMENT_GUIDE),
        "layout_recipes": list(LAYOUT_RECIPES),
        "limits": list(API_LIMITS),
        "hint": (
            "Call describe_report_objects(object_type='barChart') for one object's data roles and "
            "an example payload, describe_report_objects(operation='addData') for an operation's "
            "full shape (addData's dataItems is where formats/aggregations/geography live), and "
            "get_castable_columns to map columns to roles. Use intent_map to pick the right object "
            "and the placement variants + layout_recipes to arrange objects instead of stacking them."
        ),
    }
    if category is not None and not objects:
        result["note"] = f"No objects in category '{category}'. Valid categories: {list(CATEGORIES)}."
    return result


def _describe_operation(operation: str) -> dict[str, Any]:
    for op in OPERATIONS:
        if op["key"] == operation:
            return dict(op)
    return {
        "status": "unknown_operation",
        "operation": operation,
        "valid_operations": sorted(OPERATION_KEYS),
        "did_you_mean": difflib.get_close_matches(operation, OPERATION_KEYS, n=3, cutoff=0.5),
    }


def _describe_one(object_type: str) -> dict[str, Any]:
    resolved_from: str | None = None
    normalized = normalize_key(object_type)
    obj = REPORT_OBJECT_TYPES.get(object_type)
    if obj is None:
        target = NORMALIZED_TYPES.get(normalized) or ALIASES.get(normalized)
        if target:
            resolved_from = object_type
            obj = REPORT_OBJECT_TYPES[target]
    if obj is not None:
        detail: dict[str, Any] = {
            "schema_key": obj.schema_key,
            "purpose": obj.purpose,
            "category": obj.category,
            "addable": obj.addable,
            "updatable": obj.updatable,
            "data_roles": [
                {
                    "name": r.name,
                    "takes": "list" if r.multi else "single",
                    "commonly_required": r.name in obj.commonly_required,
                }
                for r in obj.roles
            ],
            "commonly_required": list(obj.commonly_required),
        }
        if resolved_from:
            detail["resolved_from_alias"] = resolved_from
        if obj.addable:
            detail["example"] = _example_for(obj)
            detail["placement_hint"] = (
                "The example uses page placement. To arrange it precisely, swap placement for a "
                "relativeToObject / container variant — see describe_report_objects() placement + layout_recipes."
            )
        else:
            detail["note"] = f"'{obj.schema_key}' can be updated (updateObject) but not added via the API."
        if obj.schema_key == "standardContainer":
            detail["common_options"] = {
                "note": OBJECT_NOTES["standardContainer"]["options_note"],
            }
        else:
            detail["common_options"] = {
                "shape": {"options": {"object": {"title": "<title>", "alternativeText": "<alt text>"}}},
                "note": (
                    "Accepted inline at add time — title every visual meaningfully instead of "
                    "relying on VA's auto-labels."
                ),
            }
        detail.update(OBJECT_NOTES.get(obj.schema_key, {}))
        if obj.render_required:
            detail["render_required"] = [list(group) for group in obj.render_required]
        if obj.category == "Geo Maps":
            detail["precondition"] = (
                "Geo objects need their column classified as geography first — in the same batch is "
                "fine: addData with dataItems [{'dataItem': 'State', 'properties': {'classification': "
                "'geography', 'geographyDataSource': {'geographyNameCodeContext': 'USStateNames'}}}]. "
                "For raw lat/long point data use geographyCoordinates instead: {'geographyDataSource': "
                "{'geographyCoordinates': {'latitudeDataItem': 'LATITUDE', 'longitudeDataItem': "
                "'LONGITUDE'}}}. See describe_report_objects(operation='addData') for the full shape."
            )
        return detail

    not_addable_key = object_type if object_type in NOT_ADDABLE else NORMALIZED_NOT_ADDABLE.get(normalized)
    if not_addable_key:
        alt = NOT_ADDABLE[not_addable_key]
        return {
            "status": "not_addable",
            "object_type": object_type,
            "nearest": alt,
            "message": f"'{object_type}' is a VA UI object with no report-API support. Use '{alt}' instead.",
        }

    candidates = list(REPORT_OBJECT_TYPES) + list(ALIASES) + list(NOT_ADDABLE)
    matches = difflib.get_close_matches(object_type, candidates, n=3, cutoff=0.5)
    suggestions = list(dict.fromkeys(ALIASES.get(m, m) for m in matches))
    return {
        "status": "unknown_object_type",
        "object_type": object_type,
        "did_you_mean": suggestions,
        "hint": "Call describe_report_objects() with no arguments to list every object type.",
    }


# --- normalisation --------------------------------------------------------


def normalize_operations(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a copy of *operations* with tolerant, non-semantic coercions.

    * Coerces an integer ``addPage.pagePosition`` to the string index that
      operation expects (report-placement ``pagePosition`` is the opposite: a
      number — a digit string there is coerced to int).
    * Translates the spec spelling ``newPage`` to the ``new_page`` the live
      report-placement enum actually accepts.
    * Wraps a bare column string in a list for array-valued (``multi``) data
      roles, so ``measures="MSRP"`` works as well as ``measures=["MSRP"]``.
    * Expands an ``addPage`` convenience ``title`` into a text object placed at
      the top of that page's body (VA rejects text in page headers) — the
      addPage keeps its ``pageName`` and the title text is appended as a
      following ``addObject``.

    Never raises — malformed input is caught by :func:`validate_operations`.
    """
    try:
        ops = json.loads(json.dumps(operations))
    except (TypeError, ValueError):
        return operations
    if not isinstance(ops, list):
        return operations
    out: list[dict[str, Any]] = []
    title_ops: list[dict[str, Any]] = []
    for op in ops:
        if not isinstance(op, dict):
            out.append(op)
            continue
        page = op.get("addPage")
        if isinstance(page, dict):
            position = page.get("pagePosition")
            if isinstance(position, int) and not isinstance(position, bool):
                page["pagePosition"] = str(position)
            # Expand a page title into a body text band, but only when the
            # page is named (the text object must target the page by name).
            # Always strip the tool-only 'title' key — VA rejects unknown
            # properties — and skip the text op for an empty title.
            if "title" in page and page.get("pageName"):
                title = page.pop("title")
                if title:
                    title_ops.append(_page_title_object(page["pageName"], str(title)))
        add = op.get("addObject")
        if isinstance(add, dict):
            _normalize_roles(add.get("object"))
            _normalize_placement(add.get("placement"))
        out.append(op)
    # Synthesized title ops go at the END of the batch so the caller's
    # operation indices survive normalization — op_index in validation errors
    # and VA's failed-at-index messages keep pointing at the caller's array.
    # position "start" still renders the text at the top of the page body
    # regardless of when in the batch it is applied.
    out.extend(title_ops)
    return out


def _page_title_object(page_name: str, title: str) -> dict[str, Any]:
    """Build an addObject op putting *title* text at the top of *page_name*'s body.

    Page (and report) headers accept only control objects — a text placed with
    ``context: "header"`` fails the whole atomic batch — so a page title is a
    text band at the start of the body.
    """
    return {
        "addObject": {
            "object": {"text": {"options": {"content": title}}},
            "placement": {"page": {"target": page_name, "context": "body", "position": "start"}},
        }
    }


def _normalize_placement(placement: Any) -> None:
    """In-place report-placement coercions: enum spelling and pagePosition typing."""
    if not isinstance(placement, dict):
        return
    report = placement.get("report")
    if not isinstance(report, dict):
        return
    if report.get("context") == "newPage":  # published-spec spelling; live enum is snake_case
        report["context"] = "new_page"
    position = report.get("pagePosition")
    if isinstance(position, str):
        # A non-numeric string is left for validation to reject with the typed message.
        with contextlib.suppress(ValueError):
            report["pagePosition"] = int(position)


def _normalize_roles(obj: Any) -> None:
    if not isinstance(obj, dict) or len(obj) != 1:
        return
    (type_key,) = obj
    vo = REPORT_OBJECT_TYPES.get(type_key)
    spec = obj[type_key]
    if vo is None or not isinstance(spec, dict):
        return
    roles = spec.get("dataRoles")
    if not isinstance(roles, dict):
        return
    multi = {r.name for r in vo.roles if r.multi}
    for name, value in list(roles.items()):
        if name in multi and isinstance(value, str):
            roles[name] = [value]


# --- validation -----------------------------------------------------------


def _err(status: str, index: int, message: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "op_index": index, "message": message, **extra}


def validate_operations(operations: Any) -> dict[str, Any] | None:
    """Return a structured error dict for the invalid operations, else None.

    Enforces the rules the VA endpoints impose *before* any HTTP call: exactly
    one known operation per array element; for ``addObject`` a known + addable
    object type with only the spec-allowed keys, whose ``dataRoles`` are a
    subset of that type's roles with the right list/single arity, and a valid
    placement; for ``updateObject`` a known + updatable type with a ``name``;
    and the required blocks for the data operations. Every invalid operation is
    reported at once (the batch is atomic, so serial fix-one-resend loops are
    expensive): the returned dict is the first error, carrying ``all_errors``
    when more than one operation failed.
    """
    if not isinstance(operations, list) or not operations:
        return {"status": "invalid_operation", "message": "operations must be a non-empty list of operation objects."}
    errors = [err for i, op in enumerate(operations) for err in [_validate_one(op, i)] if err is not None]
    if not errors:
        return None
    if len(errors) == 1:
        return errors[0]
    return {**errors[0], "error_count": len(errors), "all_errors": errors}


def _validate_one(op: Any, i: int) -> dict[str, Any] | None:
    if not isinstance(op, dict):
        return _err("invalid_operation", i, "each operation must be an object.")
    keys = [k for k in op if k in OPERATION_KEYS]
    if not keys:
        unknown = [k for k in op if k not in META_OP_KEYS]
        return _err(
            "invalid_operation",
            i,
            f"no known operation key. Expected one of {sorted(OPERATION_KEYS)}.",
            unknown_keys=unknown,
        )
    if len(keys) > 1:
        return _err("invalid_operation", i, f"exactly one operation per array element; got {sorted(keys)}.")
    stray = [k for k in op if k not in OPERATION_KEYS and k not in META_OP_KEYS]
    if stray:
        return _err(
            "invalid_operation",
            i,
            f"unknown key(s) {sorted(stray)} alongside '{keys[0]}' — VA rejects unrecognised "
            f"properties; allowed meta keys: {sorted(META_OP_KEYS)}.",
            unknown_keys=sorted(stray),
        )
    key = keys[0]
    val = op[key]
    if key == "addObject":
        return _validate_add_object(val, i)
    if key == "updateObject":
        return _validate_update_object(val, i)
    if key == "addData":
        err = _require_keys(val, i, "addData", ("cas",), nested={"cas": ("library", "table")})
        # The OpenAPI spec marks cas.server optional, but live Viya rejects the
        # whole batch without it ("Missing the required property: server").
        if err is None and isinstance(val.get("cas"), dict) and not val["cas"].get("server"):
            err = _err(
                "invalid_operation",
                i,
                "'addData.cas' requires 'server' (the CAS server name — list_cas_servers returns "
                "it; typically 'cas-shared-default').",
            )
        return err if err is not None else _validate_data_items(val, i, "addData")
    if key == "changeData":
        return _require_keys(val, i, "changeData", ("originalData", "replacementData"))
    if key == "applyDataView":
        return _require_keys(val, i, "applyDataView", ("targetData", "dataView"))
    if key == "setParameterValue":
        return _require_keys(val, i, "setParameterValue", ("name", "value"))
    if key == "addPage":
        return _validate_add_page(val, i)
    if key == "updateData":
        err = _require_keys(val, i, "updateData", ("data",))
        if err is not None:
            return err
        data = val.get("data")
        if not isinstance(data, dict):
            return _err("invalid_operation", i, "'updateData.data' must be an object.")
        return _validate_data_items(data, i, "updateData")
    return None


def _validate_add_page(val: Any, i: int) -> dict[str, Any] | None:
    if not isinstance(val, dict):
        return _err("invalid_operation", i, "'addPage' must be an object.")
    # A title becomes a body text object targeting the page by name, so the
    # page must be named. (When pageName is present, normalize strips the title
    # before validation, so this only fires on the unnamed case.)
    if "title" in val and not val.get("pageName"):
        return _err(
            "invalid_operation",
            i,
            "'addPage.title' requires 'pageName' — a page title is a text object placed at the "
            "top of the named page's body.",
        )
    position = val.get("pagePosition")
    if position is not None and (isinstance(position, bool) or not isinstance(position, (str, int))):
        return _err(
            "invalid_operation",
            i,
            "'addPage.pagePosition' must be a string index like '0' (an int is coerced for you) — "
            "unlike the numeric report-placement pagePosition.",
        )
    return None


def _validate_data_items(val: Any, i: int, op: str) -> dict[str, Any] | None:
    """Validate the optional ``dataItems`` polish block against the spec enums."""
    items = val.get("dataItems")
    if items is None:
        return None
    if not isinstance(items, list):
        return _err("invalid_operation", i, f"'{op}.dataItems' must be a list of {{dataItem, properties}} objects.")
    for item in items:
        if not isinstance(item, dict) or not item.get("dataItem"):
            return _err(
                "invalid_operation",
                i,
                f"each '{op}.dataItems' entry needs 'dataItem' (the column name) plus 'properties'.",
            )
        props = item.get("properties")
        if props is None:
            continue
        if not isinstance(props, dict):
            return _err("invalid_operation", i, f"'{op}.dataItems[].properties' must be an object.")
        aggregation = props.get("aggregation")
        if aggregation is not None and aggregation not in AGGREGATIONS:
            return _err(
                "invalid_operation",
                i,
                f"unknown aggregation '{aggregation}' for data item '{item['dataItem']}'.",
                valid_values=sorted(AGGREGATIONS),
            )
        fmt = props.get("format")
        if fmt is not None and (not isinstance(fmt, str) or not SAS_FORMAT_RE.match(fmt)):
            return _err(
                "invalid_operation",
                i,
                f"format '{fmt}' is not a named SAS format — VA rejects bare numeric w.d forms "
                f"(and the whole atomic batch with them). Use e.g. DOLLAR12.2, COMMA10., "
                f"PERCENT8.1, DATE9.",
            )
        classification = props.get("classification")
        if classification is not None and classification not in CLASSIFICATIONS:
            return _err(
                "invalid_operation",
                i,
                f"unknown classification '{classification}' for data item '{item['dataItem']}'.",
                valid_values=sorted(CLASSIFICATIONS),
            )
        geo = props.get("geographyDataSource")
        if geo is not None:
            if classification != "geography":
                return _err(
                    "invalid_operation",
                    i,
                    "'geographyDataSource' is only allowed together with classification 'geography'.",
                )
            if not isinstance(geo, dict):
                return _err(
                    "invalid_operation",
                    i,
                    "'geographyDataSource' must be an object, e.g. "
                    "{'geographyNameCodeContext': 'USStateNames'}.",
                )
            if isinstance(geo, dict):
                unknown = sorted(set(geo) - GEO_SOURCE_KEYS)
                if unknown:
                    return _err(
                        "invalid_operation",
                        i,
                        f"unknown geographyDataSource key(s) {unknown} — for lat/long point data "
                        f"use geographyCoordinates: {{'latitudeDataItem': '<col>', "
                        f"'longitudeDataItem': '<col>'}}.",
                        valid_keys=sorted(GEO_SOURCE_KEYS),
                    )
                context = geo.get("geographyNameCodeContext")
                if context is not None and context not in GEO_NAME_CODE_CONTEXTS:
                    return _err(
                        "invalid_operation",
                        i,
                        f"unknown geographyNameCodeContext '{context}'.",
                        valid_values=sorted(GEO_NAME_CODE_CONTEXTS),
                    )
                coordinates = geo.get("geographyCoordinates")
                if coordinates is not None:
                    if context is not None:
                        return _err(
                            "invalid_operation",
                            i,
                            "geographyDataSource takes geographyNameCodeContext OR "
                            "geographyCoordinates, not both.",
                        )
                    if not isinstance(coordinates, dict) or not (
                        coordinates.get("latitudeDataItem") and coordinates.get("longitudeDataItem")
                    ):
                        return _err(
                            "invalid_operation",
                            i,
                            "'geographyCoordinates' requires 'latitudeDataItem' and "
                            "'longitudeDataItem' (column names or labels).",
                        )
    return None


def _require_keys(
    val: Any, i: int, op: str, keys: tuple[str, ...], nested: dict[str, tuple[str, ...]] | None = None
) -> dict[str, Any] | None:
    if not isinstance(val, dict):
        return _err("invalid_operation", i, f"'{op}' must be an object.")
    for k in keys:
        if k not in val:
            return _err("invalid_operation", i, f"'{op}' requires '{k}'.")
    for parent, children in (nested or {}).items():
        block = val.get(parent)
        if not isinstance(block, dict):
            return _err("invalid_operation", i, f"'{op}.{parent}' must be an object.")
        for c in children:
            if c not in block:
                return _err("invalid_operation", i, f"'{op}.{parent}' requires '{c}'.")
    return None


def _single_type(container: Any, i: int, op: str) -> tuple[str, Any] | dict[str, Any]:
    """Extract the single ``{<type>: spec}`` pair, or an error dict."""
    if not isinstance(container, dict) or len(container) != 1:
        return _err(
            "invalid_operation", i, f"'{op}.object' must name exactly one object type, e.g. {{'barChart': {{...}}}}."
        )
    (type_key,) = container
    return type_key, container[type_key]


def _resolve_type(type_key: str, i: int, *, for_update: bool) -> dict[str, Any] | None:
    """Return an error dict if *type_key* can't be added/updated, else None."""
    obj = REPORT_OBJECT_TYPES.get(type_key)
    if obj is None:
        if type_key in NOT_ADDABLE:
            alt = NOT_ADDABLE[type_key]
            return _err(
                "not_addable",
                i,
                f"'{type_key}' is a VA UI object with no report-API support. Use '{alt}'.",
                object_type=type_key,
                nearest=alt,
            )
        return _err(
            "unknown_object_type",
            i,
            f"unknown object type '{type_key}'.",
            object_type=type_key,
            did_you_mean=difflib.get_close_matches(type_key, REPORT_OBJECT_TYPES, n=3, cutoff=0.5),
        )
    if for_update and not obj.updatable:
        return _err(
            "not_updatable", i, f"'{type_key}' cannot be updated via the API (it is add-only).", object_type=type_key
        )
    if not for_update and not obj.addable:
        return _err(
            "not_addable", i, f"'{type_key}' cannot be added via the API (it is update-only).", object_type=type_key
        )
    return None


def _validate_object_spec_keys(type_key: str, spec: Any, i: int) -> dict[str, Any] | None:
    if not isinstance(spec, dict):
        return None  # shape errors are reported by _validate_roles
    if type_key == "standardContainer":
        if spec:
            return _err(
                "invalid_object_spec",
                i,
                "standardContainer accepts no properties at add time (VA rejects even 'options'); "
                "add it bare ({}) and set its title via a follow-up updateObject using the name "
                "the apply returns.",
                object_type=type_key,
            )
        return None
    extras = sorted(set(spec) - OBJECT_SPEC_ALLOWED_KEYS)
    if extras:
        if "name" in extras:
            return _err(
                "invalid_object_spec",
                i,
                "objects are auto-named by VA — 'name' is not allowed at add time; use the name "
                "the apply result returns (or get_report_outline) to reference the object later.",
                object_type=type_key,
                unknown_keys=extras,
            )
        return _err(
            "invalid_object_spec",
            i,
            f"'{type_key}' does not accept {extras}; allowed keys: "
            f"{sorted(OBJECT_SPEC_ALLOWED_KEYS)}.",
            object_type=type_key,
            unknown_keys=extras,
        )
    return None


def _validate_add_object(val: Any, i: int) -> dict[str, Any] | None:
    if not isinstance(val, dict):
        return _err("invalid_operation", i, "'addObject' must be an object.")
    has_object = "object" in val
    has_report_object = "reportObject" in val
    if has_object and has_report_object:
        return _err("invalid_operation", i, "'addObject' takes object OR reportObject, not both.")
    if not has_object and not has_report_object:
        return _err("invalid_operation", i, "'addObject' requires 'object' or 'reportObject'.")
    if has_report_object:
        # Adding a pre-existing object by reference — the object itself needs
        # no validation, but its placement still does.
        return _validate_placement(val.get("placement"), i)

    extracted = _single_type(val["object"], i, "addObject")
    if isinstance(extracted, dict):
        return extracted
    type_key, spec = extracted
    resolved = _resolve_type(type_key, i, for_update=False)
    if resolved is not None:
        return resolved
    keys_err = _validate_object_spec_keys(type_key, spec, i)
    if keys_err is not None:
        return keys_err
    roles_err = _validate_roles(REPORT_OBJECT_TYPES[type_key], spec, i)
    if roles_err is not None:
        return roles_err
    placement_err = _validate_placement(val.get("placement"), i)
    if placement_err is not None:
        return placement_err
    return _validate_header_placement(type_key, val.get("placement"), i)


def _validate_header_placement(type_key: str, placement: Any, i: int) -> dict[str, Any] | None:
    """Page/report headers accept ONLY control objects — enforce it pre-flight.

    VA rejects anything else with "Only control objects are supported in the
    page header" and rolls back the whole atomic batch.
    """
    if not isinstance(placement, dict):
        return None
    for variant in ("page", "report"):
        inner = placement.get(variant)
        if isinstance(inner, dict) and inner.get("context") == "header":
            obj = REPORT_OBJECT_TYPES.get(type_key)
            if obj is not None and obj.category != "Controls":
                return _err(
                    "invalid_placement",
                    i,
                    f"the {variant} header accepts ONLY control objects (dropdownList, buttonBar, "
                    f"slider, ...); '{type_key}' ({obj.category}) belongs in the body — a title is "
                    "a text object placed with context 'body', position 'start'.",
                    object_type=type_key,
                )
    return None


def _validate_placement(placement: Any, i: int) -> dict[str, Any] | None:
    """Validate an addObject ``placement`` block, or None if valid/absent."""
    if placement is None:
        return None
    if not isinstance(placement, dict) or len(placement) != 1:
        return _err(
            "invalid_placement",
            i,
            f"placement must name exactly one of {list(PLACEMENT_VARIANTS)}.",
            valid_variants=list(PLACEMENT_VARIANTS),
        )
    (variant,) = placement
    if variant not in PLACEMENT_VARIANTS:
        return _err(
            "invalid_placement",
            i,
            f"unknown placement variant '{variant}'.",
            valid_variants=list(PLACEMENT_VARIANTS),
        )
    inner = placement[variant]
    if not isinstance(inner, dict):
        return _err("invalid_placement", i, f"placement.{variant} must be an object.")
    extras = sorted(set(inner) - PLACEMENT_ALLOWED_KEYS[variant])
    if extras:
        return _err(
            "invalid_placement",
            i,
            f"placement.{variant} does not accept {extras}; allowed keys: "
            f"{sorted(PLACEMENT_ALLOWED_KEYS[variant])}.",
            unknown_keys=extras,
        )
    if variant in PLACEMENT_TARGET_REQUIRED and not inner.get("target"):
        return _err(
            "invalid_placement",
            i,
            f"placement.{variant} requires 'target' (the name of the {variant} to place against).",
        )
    for field_name, allowed in PLACEMENT_ENUMS.get(variant, {}).items():
        value = inner.get(field_name)
        if value is not None and value not in allowed:
            return _err(
                "invalid_placement",
                i,
                f"placement.{variant}.{field_name} must be one of {sorted(allowed)}; got '{value}'.",
                valid_values=sorted(allowed),
            )
    if variant == "report":
        page_name = inner.get("pageName")
        if page_name is not None and (not isinstance(page_name, str) or not page_name.strip()):
            return _err("invalid_placement", i, "placement.report.pageName must be a non-empty string.")
        page_position = inner.get("pagePosition")
        if page_position is not None and (
            isinstance(page_position, bool) or not isinstance(page_position, (int, float))
        ):
            return _err(
                "invalid_placement",
                i,
                "placement.report.pagePosition must be a NUMBER (0 puts the new page first) — "
                "unlike addPage.pagePosition, which is a string.",
            )
    return None


def _validate_update_object(val: Any, i: int) -> dict[str, Any] | None:
    if not isinstance(val, dict) or "object" not in val:
        return _err("invalid_operation", i, "'updateObject' requires 'object'.")
    extracted = _single_type(val["object"], i, "updateObject")
    if isinstance(extracted, dict):
        return extracted
    type_key, spec = extracted
    resolved = _resolve_type(type_key, i, for_update=True)
    if resolved is not None:
        return resolved
    if not isinstance(spec, dict) or not spec.get("name"):
        return _err(
            "invalid_operation",
            i,
            f"'updateObject.object.{type_key}' requires 'name' (the existing object's name or label).",
        )
    extras = sorted(set(spec) - {"name", "options"})
    if extras:
        return _err(
            "invalid_object_spec",
            i,
            f"'updateObject.object.{type_key}' does not accept {extras} — updates can change only "
            "'options' (placement and dataRoles are write-once; there is no move/re-role).",
            object_type=type_key,
            unknown_keys=extras,
        )
    return None


def _validate_roles(obj: VaObject, spec: Any, i: int) -> dict[str, Any] | None:
    if not isinstance(spec, dict):
        return _err("invalid_operation", i, f"'{obj.schema_key}' must be an object.")
    roles = spec.get("dataRoles")
    if roles is None:
        return None
    if not isinstance(roles, dict):
        return _err(
            "invalid_roles",
            i,
            f"'{obj.schema_key}.dataRoles' must be an object of role -> column.",
            object_type=obj.schema_key,
            valid_roles=list(obj.role_names),
        )
    by_name = {r.name: r for r in obj.roles}
    for name, value in roles.items():
        spec_role = by_name.get(name)
        if spec_role is None:
            return _err(
                "invalid_roles",
                i,
                f"'{name}' is not a role of {obj.schema_key}.",
                object_type=obj.schema_key,
                valid_roles=list(obj.role_names),
            )
        if not spec_role.multi and isinstance(value, list):
            return _err(
                "invalid_roles",
                i,
                f"role '{name}' on {obj.schema_key} takes a single column, not a list.",
                object_type=obj.schema_key,
            )
    return None


def warn_operations(operations: list[dict[str, Any]]) -> list[str]:
    """Return non-blocking warnings for render risks the API accepts silently.

    The OpenAPI spec marks every data role optional, so these are advisory: an
    object can pass validation (and the VA PUT) yet render blank without its
    usual roles, and multiple content-bearing texts can collapse onto one
    element on affected Viya builds.
    """
    warnings: list[str] = []
    if not isinstance(operations, list):
        return warnings
    content_texts = 0
    page_stacked: dict[str, int] = {}
    for op in operations:
        if not isinstance(op, dict):
            continue
        add = op.get("addObject")
        if not isinstance(add, dict) or "object" not in add:
            continue
        placement = add.get("placement")
        if isinstance(placement, dict):
            page = placement.get("page")
            report = placement.get("report")
            if isinstance(page, dict) and page.get("context") != "header":
                target = str(page.get("target"))
                page_stacked[target] = page_stacked.get(target, 0) + 1
            elif isinstance(report, dict) and report.get("pageName"):
                target = str(report["pageName"])
                page_stacked[target] = page_stacked.get(target, 0) + 1
        container = add["object"]
        if not isinstance(container, dict) or len(container) != 1:
            continue
        (type_key,) = container
        spec = container[type_key]
        if type_key == "text" and isinstance(spec, dict):
            options = spec.get("options")
            if isinstance(options, dict) and options.get("content"):
                content_texts += 1
        obj = REPORT_OBJECT_TYPES.get(type_key)
        if obj is None:
            continue
        provided = set()
        if isinstance(spec, dict) and isinstance(spec.get("dataRoles"), dict):
            provided = set(spec["dataRoles"])
        missing = [r for r in obj.commonly_required if r not in provided]
        if missing:
            warnings.append(f"{type_key} usually needs role(s) {missing}; none provided — it may render empty.")
        for group in obj.render_required:
            if not any(role in provided for role in group):
                warnings.append(
                    f"{type_key} renders EMPTY without one of {list(group)} — the API does not "
                    f"auto-apply Frequency the way the VA UI does; add a measure (a count/flag "
                    f"column works)."
                )
    if content_texts >= 2:
        warnings.append(
            f"This batch creates {content_texts} content-bearing text objects; some Viya builds "
            f"misroute text options.content onto the report's FIRST text element (add and update "
            f"alike). Prefer one content-bearing text per report — e.g. title at most one page — "
            f"and check the result's text_content_warning / get_report_outline after applying."
        )
    for target, count in page_stacked.items():
        if count >= 4:
            warnings.append(
                f"{count} objects are page-placed onto '{target}' — the page body auto-flows "
                f"VERTICALLY, so they will render as one tall stack. Put KPI tiles side by side "
                f"in a standardContainer and arrange charts in rows with relativeToObject "
                f"left/right (see describe_report_objects layout_recipes)."
            )
    return warnings


# --- request shaping ------------------------------------------------------


@dataclass
class CreateReportRequest:
    """A normalised ``create_report`` invocation."""

    name: str
    folder: str | None = None
    on_conflict: str = "rename"
    operations: list[dict[str, Any]] | None = field(default=None)


def validate_create(req: CreateReportRequest) -> dict[str, Any] | None:
    """Validate a create request (name, conflict policy, any inline operations)."""
    if not req.name or not str(req.name).strip():
        return {"status": "invalid_request", "message": "create_report requires a non-empty name."}
    if req.on_conflict not in CONFLICT_VALUES:
        return {
            "status": "invalid_request",
            "message": f"on_conflict must be one of {sorted(CONFLICT_VALUES)}.",
            "on_conflict": req.on_conflict,
        }
    if req.operations:
        return validate_operations(req.operations)
    return None


def build_create_body(req: CreateReportRequest) -> dict[str, Any]:
    """Build the POST /visualAnalytics/reports request body."""
    body: dict[str, Any] = {"resultReportName": req.name, "resultNameConflict": req.on_conflict}
    if req.folder:
        body["resultFolder"] = req.folder
    if req.operations:
        body["operations"] = req.operations
    return body


def summarize_created(operations: list[dict[str, Any]], response: dict[str, Any]) -> dict[str, Any]:
    """Summarise what an operations batch created, from the request (+ response).

    Reads the requested pages/objects/data sources from *operations* so the
    result is deterministic regardless of the PUT response shape, then merges
    the per-operation response entries **by index** — the VA response
    ``operations`` array is a strict 1:1 mapping of the request array (failed
    entries keep their slot). Each entry carries ``name`` (the handle placement
    targets and updateObject consume, e.g. ``ve15``), ``label`` (the display
    label ``export_report`` consumes, e.g. ``Text 2``), and ``status``.
    """
    pages: list[dict[str, Any]] = []
    objects: list[dict[str, Any]] = []
    data_sources: list[dict[str, Any]] = []
    slots: list[dict[str, Any] | None] = []
    for op in operations or []:
        record: dict[str, Any] | None = None
        if isinstance(op, dict):
            if "addPage" in op:
                page = op["addPage"] if isinstance(op["addPage"], dict) else {}
                # The response name is the internal section name (vi*); the
                # label is the requested pageName — what page placement targets.
                record = {"label": page.get("pageName")}
                pages.append(record)
            elif "addData" in op and isinstance(op["addData"], dict):
                add = op["addData"]
                cas = add.get("cas", {}) if isinstance(add.get("cas"), dict) else {}
                record = {"name": add.get("name") or cas.get("table")}
                data_sources.append(record)
            elif "addObject" in op and isinstance(op["addObject"], dict):
                container = op["addObject"].get("object")
                if isinstance(container, dict) and len(container) == 1:
                    (type_key,) = container
                    record = {
                        "type": type_key,
                        "page": _placement_page(op["addObject"].get("placement")),
                        "placement": op["addObject"].get("placement"),
                    }
                    objects.append(record)
        slots.append(record)
    _merge_response_results(slots, response)
    return {"pages": pages, "objects": objects, "dataSources": data_sources}


def _placement_page(placement: Any) -> str | None:
    if not isinstance(placement, dict):
        return None
    page = placement.get("page")
    if isinstance(page, dict):
        return page.get("target")
    report = placement.get("report")
    if isinstance(report, dict):
        return report.get("pageName")
    return None


def _merge_response_results(slots: list[dict[str, Any] | None], response: dict[str, Any]) -> None:
    """Merge per-operation response entries onto the request records by index."""
    if not isinstance(response, dict):
        return
    results = response.get("operations") or response.get("operationResponses")
    if not isinstance(results, list):
        return
    # Index-aligned 1:1 with the request array; strict=False tolerates a
    # response the server truncated.
    for record, entry in zip(slots, results, strict=False):
        if record is None or not isinstance(entry, dict):
            continue
        for key in ("name", "label", "status"):
            # Request-provided values win, but a pre-seeded None (e.g. an
            # unnamed addPage's label) must not block the response value.
            if entry.get(key) is not None and record.get(key) is None:
                record[key] = entry[key]
        if entry.get("messages"):
            record["messages"] = entry["messages"]


def parse_failure(response_text: str) -> dict[str, Any]:
    """Extract structured per-operation failure details from a VA error body.

    A rejected batch echoes the ``operations`` array with the failing entry at
    its request index carrying ``status: Failed/Invalid`` plus ``messages``;
    top-level ``messages`` name the failing index. Returns ``{}`` when the body
    is not parseable JSON in that shape.
    """
    try:
        data = json.loads(response_text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    results = data.get("operations") or data.get("operationResponses")
    if isinstance(results, list):
        failed = [
            {"op_index": i, "status": entry.get("status"), "messages": entry.get("messages")}
            for i, entry in enumerate(results)
            if isinstance(entry, dict) and entry.get("status") not in (None, "Success")
        ]
        if failed:
            out["failed_operations"] = failed
            out["failed_operation_index"] = failed[0]["op_index"]
    if isinstance(data.get("messages"), list):
        out["viya_messages"] = data["messages"]
    return out


# --- execution ------------------------------------------------------------


def viewer_url(report_id: Any) -> str | None:
    """A ready-to-open VA viewer deep link — the deliverable of every build."""
    if not report_id:
        return None
    return f"{VIYA_ENDPOINT}/SASVisualAnalytics/?reportUri=/reports/reports/{report_id}"


def _expected_text_pairs(
    operations: list[dict[str, Any]], created_objects: list[dict[str, Any]]
) -> list[tuple[str, str]]:
    """(created ve-name, intended content) for each content-bearing text op.

    Paired op-by-op: every text op consumes one created text record (matching
    summarize_created's selection), whether or not it carries content — two
    independently filtered lists would shift against each other whenever a
    content-less text precedes a content-bearing one, producing false
    misroute warnings.
    """
    text_records = iter([o for o in created_objects if o.get("type") == "text"])
    pairs: list[tuple[str, str]] = []
    for op in operations or []:
        if not isinstance(op, dict) or not isinstance(op.get("addObject"), dict):
            continue
        container = op["addObject"].get("object")
        if not isinstance(container, dict) or len(container) != 1 or "text" not in container:
            continue
        record = next(text_records, None)
        if record is None:
            break
        spec = container["text"]
        options = spec.get("options") if isinstance(spec, dict) else None
        content = options.get("content") if isinstance(options, dict) else None
        if isinstance(content, str) and record.get("name"):
            pairs.append((record["name"], content))
    return pairs


async def check_text_contents(
    report_id: Any,
    operations: list[dict[str, Any]],
    created_objects: list[dict[str, Any]],
    client: httpx.AsyncClient,
) -> str | None:
    """Best-effort post-apply check that text contents landed on their elements.

    Some Viya builds misroute text ``options.content`` onto the report's FIRST
    text element (on add AND update), silently shipping wrong titles. One cheap
    content GET catches it; returns a warning string on mismatch, else None.
    Never raises.
    """
    try:
        pairs = _expected_text_pairs(operations, created_objects)
        if not pairs or not report_id:
            return None
        resp = await client.get(
            f"{VIYA_ENDPOINT}{CONTENT_PATH.format(report_id=report_id)}",
            headers={"Accept": CONTENT_ACCEPT},
        )
        if resp.status_code >= 400:
            return None
        outline = reduce_content_outline(_response_json_dict(resp))
        actual = {
            obj["name"]: obj.get("text")
            for page in outline.get("pages", [])
            for obj in page.get("objects", [])
            if obj.get("name")
        }
        mismatched = [
            f"'{content}' was meant for {name}, which holds {actual.get(name)!r}"
            for name, content in pairs
            if (actual.get(name) or "").strip() != content.strip()
        ]
        if mismatched:
            return (
                "Text content check FAILED — this Viya build misroutes text options.content onto "
                "the report's first text element: " + "; ".join(mismatched) + ". Keep one "
                "content-bearing text per report (title at most one page), or set contents via "
                "the report content endpoint."
            )
        return None
    except Exception:  # noqa: BLE001 - verification must never break the apply
        return None


def _response_json_dict(resp: httpx.Response) -> dict[str, Any]:
    """The response body as a dict — {} for empty, non-JSON, or non-object bodies."""
    if not resp.content:
        return {}
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _verify_hint(report_id: Any, created: dict[str, Any]) -> str | None:
    """A next-step suggestion so authoring agents close the see-what-you-built loop."""
    if not created.get("pages") and not created.get("objects"):
        return None
    labels = [p.get("label") or p.get("name") for p in created.get("pages", [])]
    page = next((label for label in labels if label), "<page label>")
    return (
        f"To see the result: export_report('{report_id}', 'png', report_objects=['{page}'], "
        "image_size='1200px,800px') — export page-by-page (whole-report png can render blank); "
        "get_report_outline returns the structure with the names/labels to target in follow-ups."
    )


async def execute_create(req: CreateReportRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    """POST a create request and return ``{status, id, name}`` (or a failure dict)."""
    body = build_create_body(req)
    resp = await client.post(
        f"{VIYA_ENDPOINT}{CREATE_PATH}",
        content=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    if resp.status_code >= 400:
        return {
            "status": "create_failed",
            "name": req.name,
            "http_status": resp.status_code,
            "message": (
                f"Viya rejected the report creation (HTTP {resp.status_code}). A name "
                f"conflict policy of '{req.on_conflict}' with an existing name can cause this. "
                f"Viya said: {resp.text[:400] or '(no response body)'}"
            ),
            **parse_failure(resp.text),
        }
    data = _response_json_dict(resp)
    report_id = data.get("resultReportId") or data.get("id")
    result: dict[str, Any] = {
        "status": "created",
        "id": report_id,
        "name": data.get("resultReportName") or req.name,
    }
    url = viewer_url(report_id)
    if url:
        result["open_url"] = url
    if req.operations:
        created = summarize_created(req.operations, data)
        result["created"] = created
        if created.get("pages"):
            result["note"] = (
                'VA prepends an empty default "Page 1" before pages added at creation, so '
                "whole-report exports can render blank — verify page-by-page."
            )
        hint = _verify_hint(report_id, created)
        if hint:
            result["verify_hint"] = hint
        text_warning = await check_text_contents(report_id, req.operations, created.get("objects", []), client)
        if text_warning:
            result["text_content_warning"] = text_warning
    return result


async def _read_etag(report_id: str, client: httpx.AsyncClient) -> str:
    resp = await client.get(
        f"{VIYA_ENDPOINT}{ETAG_PATH.format(report_id=report_id)}",
        headers={"Accept": "application/json"},
    )
    resp.raise_for_status()
    return resp.headers.get("etag", "")


async def execute_operations(
    report_id: str,
    operations: list[dict[str, Any]],
    client: httpx.AsyncClient,
    response_format: str = "concise",
    retries: int = 1,
    result_report_name: str | None = None,
    result_folder: str | None = None,
    result_name_conflict: str = "rename",
) -> dict[str, Any]:
    """Apply a *validated* operations array with the ETag round-trip + 412 retry.

    Reads the report's current ETag, PUTs the operations with ``If-Match``, and
    on a 412 (a concurrent edit moved the ETag) transparently re-reads and
    retries once. HTTP >= 400 is surfaced as a structured ``apply_failed`` dict
    (with the per-operation failure details parsed out of the VA body) rather
    than raised; a missing report as ``not_found``.

    Passing ``result_report_name`` and/or ``result_folder`` switches the PUT to
    *save-as* mode: the operations are applied to a NEW report (HTTP 201) and
    the source report is left untouched — atomic template instantiation.
    """
    body: dict[str, Any] = {"operations": operations}
    save_as = bool(result_report_name or result_folder)
    if save_as:
        if result_report_name:
            body["resultReportName"] = result_report_name
        if result_folder:
            body["resultFolder"] = result_folder
        body["resultNameConflict"] = result_name_conflict
    payload = json.dumps(body).encode()
    url = f"{VIYA_ENDPOINT}{OPERATIONS_PATH.format(report_id=report_id)}"
    attempt = 0
    while True:
        try:
            etag = await _read_etag(report_id, client)
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return {"status": "not_found", "report_id": report_id, "message": f"No report with id '{report_id}'."}
            raise
        resp = await client.put(
            url,
            content=payload,
            headers={
                "If-Match": etag,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        if resp.status_code == 412 and attempt < retries:
            attempt += 1
            continue
        break

    if resp.status_code >= 400:
        return {
            "status": "apply_failed",
            "report_id": report_id,
            "http_status": resp.status_code,
            "message": (
                f"Viya rejected the operations (HTTP {resp.status_code}). The whole batch is "
                f"atomic, so the report is unchanged. See failed_operations for the failing "
                f"index and Viya's reasons; fix those operations and resend the batch. "
                f"Viya said: {resp.text[:400] or '(no response body)'}"
            ),
            **parse_failure(resp.text),
        }

    data = _response_json_dict(resp)
    created = summarize_created(operations, data)
    result: dict[str, Any] = {
        "status": "applied",
        "report_id": report_id,
        "created": created,
    }
    verify_target: Any = report_id
    if save_as:
        result["saved_as"] = {
            "id": data.get("resultReportId"),
            "name": data.get("resultReportName") or result_report_name,
        }
        result["message"] = "Save-as: operations were applied to a NEW report; the source report is unchanged."
        verify_target = result["saved_as"]["id"] or report_id
        saved_url = viewer_url(result["saved_as"]["id"])
        if saved_url:
            result["saved_as"]["open_url"] = saved_url
    url = viewer_url(verify_target)
    if url:
        result["open_url"] = url
    hint = _verify_hint(verify_target, created)
    if hint:
        result["verify_hint"] = hint
    text_warning = await check_text_contents(verify_target, operations, created.get("objects", []), client)
    if text_warning:
        result["text_content_warning"] = text_warning
    if response_format == "detailed":
        result["response"] = data
    return result


# --- report outline (read-back) --------------------------------------------


def reduce_content_outline(content: dict[str, Any]) -> dict[str, Any]:
    """Reduce a BIRD report-content document to ``pages -> objects`` handles.

    Returns exactly what the authoring tools consume: per page the internal
    section name (``vi*``) and its label, and per object the visual-element
    name (``ve*`` — the relativeToObject/container/updateObject target), its
    label (the ``export_report`` handle), its type, and any text content.
    """
    elements: dict[str, dict[str, Any]] = {}
    for element in content.get("visualElements") or []:
        if isinstance(element, dict) and element.get("name"):
            elements[element["name"]] = element
    pages: list[dict[str, Any]] = []
    view = content.get("view")
    sections = view.get("sections") if isinstance(view, dict) else None
    for section in sections or []:
        if not isinstance(section, dict):
            continue
        page: dict[str, Any] = {"name": section.get("name"), "label": section.get("label"), "objects": []}
        seen: set[str] = set()
        queue: list[tuple[str, str | None]] = []
        # Walk the whole section (header band + body) so header controls
        # appear in the outline too.
        _collect_refs(section, queue)
        while queue:
            ref, parent = queue.pop(0)
            if ref in seen:
                continue
            seen.add(ref)
            obj: dict[str, Any] = {"name": ref}
            element = elements.get(ref)
            if isinstance(element, dict):
                obj["type"] = element.get("@element")
                obj["label"] = element.get("labelAttribute")
                text = _text_content(element)
                if text:
                    obj["text"] = text
                # Containers hold their children in their own element entry.
                _collect_refs(element, queue, parent=ref)
            if parent:
                obj["container"] = parent
            page["objects"].append(obj)
        pages.append(page)
    return {"pages": pages}


def _collect_refs(node: Any, out: list[tuple[str, str | None]], parent: str | None = None) -> None:
    """Collect (ref, enclosing-container-ref) pairs from a layout tree, in order.

    Children of an implicit layout container (VA auto-creates one for geometric
    relativeToObject placements) are nested INSIDE the Container entry of the
    section body — the walk must descend through every entry, not just the top
    ``containedElementList`` — and carrying the enclosing ref lets the outline
    show which container each object sits in.
    """
    if isinstance(node, dict):
        ref = node.get("ref") if node.get("@element") in ("Visual", "Container") else None
        if ref:
            out.append((ref, parent))
        for value in node.values():
            _collect_refs(value, out, parent=ref or parent)
    elif isinstance(node, list):
        for value in node:
            _collect_refs(value, out, parent=parent)


def _text_content(element: dict[str, Any]) -> str | None:
    """Join the TextString fragments of a Text element's paragraphList."""
    texts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            text = node.get("text")
            if isinstance(text, str):
                texts.append(text)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(element.get("paragraphList"))
    return " ".join(texts) if texts else None


async def execute_outline(report_id: str, client: httpx.AsyncClient) -> dict[str, Any]:
    """GET the stored report definition and return the compact page/object outline."""
    resp = await client.get(
        f"{VIYA_ENDPOINT}{CONTENT_PATH.format(report_id=report_id)}",
        headers={"Accept": CONTENT_ACCEPT},
    )
    if resp.status_code == 404:
        return {"status": "not_found", "report_id": report_id, "message": f"No report with id '{report_id}'."}
    if resp.status_code >= 400:
        return {
            "status": "outline_failed",
            "report_id": report_id,
            "http_status": resp.status_code,
            "message": (
                f"Viya rejected the content read (HTTP {resp.status_code}). "
                f"Viya said: {resp.text[:400] or '(no response body)'}"
            ),
        }
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return {"status": "outline_failed", "report_id": report_id, "message": "content endpoint returned non-JSON."}
    outline = reduce_content_outline(data if isinstance(data, dict) else {})
    result = {
        "status": "ok",
        "report_id": report_id,
        **outline,
        "hint": (
            "Object 'name' (ve*) is the handle for relativeToObject/container placement and "
            "updateObject; 'label' is what export_report report_objects takes; a page's 'label' "
            "is the page-placement target; 'container' names the enclosing container element."
        ),
    }
    url = viewer_url(report_id)
    if url:
        result["open_url"] = url
    return result


def validate_copy(name: str | None, on_conflict: str) -> dict[str, Any] | None:
    """Validate a copy request (optional new name, conflict policy)."""
    if name is not None and not str(name).strip():
        return {"status": "invalid_request", "message": "copy_report name, if given, must be non-empty."}
    if on_conflict not in CONFLICT_VALUES:
        return {
            "status": "invalid_request",
            "message": f"on_conflict must be one of {sorted(CONFLICT_VALUES)}.",
            "on_conflict": on_conflict,
        }
    return None


def build_copy_body(name: str | None, folder: str | None, on_conflict: str) -> dict[str, Any]:
    """Build the PUT /visualAnalytics/reports/{id}/copy request body."""
    body: dict[str, Any] = {"resultNameConflict": on_conflict}
    if name:
        body["resultReportName"] = name
    if folder:
        body["resultFolder"] = folder
    return body


async def execute_copy(
    report_id: str,
    name: str | None,
    folder: str | None,
    on_conflict: str,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    """Copy a report and return ``{status, id, name, source_report_id}``.

    The copy endpoint mints a new report, so no ETag/If-Match is needed. HTTP
    errors surface as structured ``not_found`` / ``copy_failed`` dicts.
    """
    body = build_copy_body(name, folder, on_conflict)
    resp = await client.put(
        f"{VIYA_ENDPOINT}{COPY_PATH.format(report_id=report_id)}",
        content=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    if resp.status_code == 404:
        return {"status": "not_found", "report_id": report_id,
                "message": f"No report with id '{report_id}' to copy."}
    if resp.status_code >= 400:
        return {
            "status": "copy_failed",
            "source_report_id": report_id,
            "http_status": resp.status_code,
            "message": (
                f"Viya rejected the copy (HTTP {resp.status_code}). A name conflict policy "
                f"of '{on_conflict}' with an existing name can cause this. "
                f"Viya said: {resp.text[:400] or '(no response body)'}"
            ),
        }
    data = _response_json_dict(resp)
    copy_id = data.get("resultReportId") or data.get("id")
    result = {
        "status": "copied",
        "source_report_id": report_id,
        "id": copy_id,
        "name": data.get("resultReportName") or name,
    }
    url = viewer_url(copy_id)
    if url:
        result["open_url"] = url
    return result


async def execute_delete(report_id: str, client: httpx.AsyncClient) -> dict[str, Any]:
    """Delete a report and its content, returning a structured result."""
    resp = await client.delete(f"{VIYA_ENDPOINT}{DELETE_PATH.format(report_id=report_id)}")
    if resp.status_code == 404:
        return {"status": "not_found", "report_id": report_id,
                "message": f"No report with id '{report_id}'."}
    if resp.status_code >= 400:
        return {
            "status": "delete_failed",
            "report_id": report_id,
            "http_status": resp.status_code,
            "message": (
                f"Viya rejected the delete (HTTP {resp.status_code}). "
                f"Viya said: {resp.text[:400] or '(no response body)'}"
            ),
        }
    return {"status": "deleted", "report_id": report_id}
