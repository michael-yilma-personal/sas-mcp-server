# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Static configuration for the Visual Analytics report-authoring tools.

The frozen data that drives ``report_authoring_helpers.py``, kept apart from
the functions so the catalog can be read and maintained on its own:

* the endpoint paths for the create / operations / copy / delete / content
  calls (the ETag read deliberately targets the Reports service while the
  operations PUT targets Visual Analytics — the two-path handshake the SAS
  sample notebooks use);
* :data:`REPORT_OBJECT_TYPES` — the registry of every API-addable object and
  its data roles, generated from the VA v8 OpenAPI spec (every object in the
  ``addObjectRequest`` union) with role semantics from the ``vaobj`` docs; the
  single source of truth consumed by discovery, validation, and the example
  builder. Adding or retiring an object when VA changes is a one-line edit to
  :data:`OBJECTS` that the tools pick up automatically;
* the operation catalog (:data:`OPERATIONS`), placement vocabulary
  (:data:`PLACEMENT_GUIDE`), layout recipes, alias / intent maps, and the
  API's honest limits — the material ``describe_report_objects`` surfaces;
* the validation vocabularies (aggregations, classifications, geography
  contexts, the SAS format shape, allowed placement/object-spec keys) checked
  pre-flight because VA rejects unknown values with a whole-batch 400.

Data only — anything that executes lives in ``report_authoring_helpers.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# --- endpoints ------------------------------------------------------------

CREATE_PATH = "/visualAnalytics/reports"
OPERATIONS_PATH = "/visualAnalytics/reports/{report_id}"
COPY_PATH = "/visualAnalytics/reports/{report_id}/copy"
DELETE_PATH = "/visualAnalytics/reports/{report_id}"
# The current ETag comes from the Reports service view of the report, not the
# Visual Analytics operations endpoint (mirrors the SAS sample notebooks).
ETAG_PATH = "/reports/reports/{report_id}"
# The stored report definition (BIRD document) — the only read-back path for a
# report's page/object structure. Requires the vnd Accept type (plain
# application/json is answered with 415).
CONTENT_PATH = "/reports/reports/{report_id}/content"
CONTENT_ACCEPT = "application/vnd.sas.report.content+json"

CONFLICT_VALUES = frozenset({"abort", "rename", "replace"})

# The eight report operations the API accepts in an operations array. Meta keys
# may sit alongside the single operation key on an element.
OPERATION_KEYS = frozenset(
    {
        "addData",
        "updateData",
        "changeData",
        "applyDataView",
        "addPage",
        "addObject",
        "updateObject",
        "setParameterValue",
    }
)
META_OP_KEYS = frozenset({"operationId", "includeObjectInResponse"})

# How an addObject may be placed. Each variant carries a target and/or a
# context/position from a fixed enum. VA has no absolute width/height/x/y in the
# request — objects are auto-sized — so layout is built from placement (relative
# positioning + containers + page assignment), not geometry.
# NOTE: the live report-context enum is snake_case "new_page" (the published
# OpenAPI spec says "newPage", but current Viya rejects that spelling with a
# 400); normalize_operations translates "newPage" for callers that follow the
# spec. Page/report "header" bands accept ONLY control objects — never text.
PLACEMENT_VARIANTS: tuple[str, ...] = ("page", "relativeToObject", "container", "report")
PLACEMENT_TARGET_REQUIRED = frozenset({"page", "relativeToObject", "container"})
PLACEMENT_ENUMS: dict[str, dict[str, frozenset[str]]] = {
    "page": {"context": frozenset({"header", "body"}), "position": frozenset({"start", "end"})},
    "relativeToObject": {"position": frozenset({"before", "after", "left", "right", "top", "bottom"})},
    "container": {"position": frozenset({"start", "end"})},
    "report": {"context": frozenset({"new_page", "header"}), "position": frozenset({"start", "end"})},
}
# VA enforces additionalProperties:false, so unknown placement keys fail the
# whole atomic batch server-side — reject them pre-flight instead.
PLACEMENT_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    "page": frozenset({"target", "context", "position"}),
    "relativeToObject": frozenset({"target", "position"}),
    "container": frozenset({"target", "position"}),
    "report": frozenset({"context", "position", "pageName", "pagePosition"}),
}

# The placement vocabulary and layout recipes surfaced by describe() so an agent
# can build structured pages rather than a single auto-flow stack.
PLACEMENT_GUIDE: tuple[dict[str, Any], ...] = (
    {
        "variant": "page",
        "shape": {"page": {"target": "<pageName>", "context": "header | body", "position": "start | end"}},
        "purpose": (
            "Put the object on a page (defaults: body/end). context 'header' is the control band "
            "across the top — it accepts ONLY control objects (dropdownList, buttonBar, ...), never "
            "text or visuals; a title text belongs in the body with position 'start'."
        ),
    },
    {
        "variant": "relativeToObject",
        "shape": {"relativeToObject": {"target": "<objectName>", "position": "left|right|top|bottom|before|after"}},
        "purpose": (
            "Anchor next to an EXISTING object by name — build columns, rows, and grids. "
            "left/right/top/bottom are geometric (VA auto-wraps both objects in a layout container "
            "when the direction crosses the parent's flow); before/after insert in flow order. The "
            "target must already exist when the operation applies — same-batch forward references fail."
        ),
    },
    {
        "variant": "container",
        "shape": {"container": {"target": "<containerName>", "position": "start | end"}},
        "purpose": "Place inside a standardContainer added earlier, to group objects together.",
    },
    {
        "variant": "report",
        "shape": {
            "report": {
                "context": "new_page | header",
                "position": "start | end",
                "pageName": "<name for the new page>",
                "pagePosition": 0,
            }
        },
        "purpose": (
            "Place at the report level. context 'new_page' creates a page as it places the object; "
            "give it a pageName (becomes the page label, targetable by later ops in the SAME batch) "
            "and an optional numeric pagePosition (0 = first — a NUMBER here, unlike the string "
            "addPage.pagePosition). context 'header' is the report-wide control band (controls only)."
        ),
    },
)

LAYOUT_RECIPES: tuple[str, ...] = (
    "Default page skeleton — the page body auto-flows VERTICALLY, so N page-placed objects render "
    "as one tall ugly stack. Structure every page instead: KPI tiles side by side in a "
    "standardContainer at the top, then each chart row built with relativeToObject left/right. "
    "Page placement alone is only right for a page's FIRST object per row.",
    'Page title: give addPage a "title", e.g. {"addPage": {"pageName": "Overview", "title": "Sales '
    'Overview"}} — it expands into a text band at the TOP OF THE PAGE BODY. Page and report headers '
    "accept only control objects, so titles never go in a header.",
    'Chart titles: pass {"options": {"object": {"title": "Revenue by Region"}}} inside the object '
    "spec at add time (every addable type except standardContainer — title a container via a "
    "follow-up updateObject using its returned name). Untitled charts fall back to auto-labels "
    'like "Frequency of Origin".',
    "One-batch multi-page report: create each page with its first object via placement "
    '{"report": {"context": "new_page", "pageName": "Trends", "pagePosition": 1}}, then target that '
    'pageName from later operations in the SAME batch with {"page": {"target": "Trends"}}. Page '
    "names are caller-chosen; object names are not.",
    "Two columns: add chart A in one call, read its returned object name, then add chart B with "
    '{"relativeToObject": {"target": "<A\'s name>", "position": "right"}}. Targets must already '
    "exist — a same-batch forward reference fails the whole atomic batch. before/after insert in "
    "flow order; left/right/top/bottom are geometric side-by-side.",
    "2x2 grid: place obj2 right of obj1, obj3 bottom of obj1, obj4 right of obj3 — chaining the "
    "object names each apply_report_operations call returns.",
    "Grouped strip (e.g. a KPI row of keyValue tiles): add a standardContainer (bare {} — it "
    "accepts no options at add time), then in a follow-up apply add each tile with placement "
    '{"container": {"target": "<container\'s returned name>"}}.',
    "Placement and dataRoles are WRITE-ONCE: updateObject changes only options (title, etc.) and "
    "there is no move/resize/remove operation — get placement right at add time, or rebuild via "
    "save-as/copy.",
    'Creating a report with inline operations leaves VA\'s empty default "Page 1" as the first '
    "page, so whole-report exports can render blank — verify page-by-page with "
    "export_report(..., report_objects=['<page label>']) and inspect structure with "
    "get_report_outline.",
)


# --- object registry ------------------------------------------------------


@dataclass(frozen=True)
class RoleSpec:
    """A single data role a VA object accepts.

    ``multi`` marks an array-valued role (e.g. ``measures``, ``columns``,
    ``variables``) versus a single-column role (e.g. ``category``, ``xAxis``).
    """

    name: str
    multi: bool = False


@dataclass(frozen=True)
class VaObject:
    """One VA report object and the data roles it exposes.

    ``commonly_required`` is a curated heuristic from the SAS ``vaobj``
    documentation (the OpenAPI spec marks *every* role optional), used only for
    non-blocking warnings — never to reject a payload. ``purpose`` is a one-line
    picking hint surfaced by describe() so an agent can choose the right object
    for an analytical intent instead of defaulting to barChart for everything.
    """

    schema_key: str
    category: str
    addable: bool
    updatable: bool
    roles: tuple[RoleSpec, ...]
    commonly_required: tuple[str, ...] = ()
    purpose: str = ""
    # Role groups where at least ONE member must be filled or the object is
    # accepted by VA but RENDERS EMPTY ("required roles not assigned") — the
    # API does not auto-apply Frequency the way the VA UI does. Live-observed.
    render_required: tuple[tuple[str, ...], ...] = ()

    @property
    def role_names(self) -> tuple[str, ...]:
        return tuple(r.name for r in self.roles)


# Registry generated from the SAS Visual Analytics v8 OpenAPI spec (every object
# in the ``addObjectRequest`` union) and role semantics from the ``vaobj`` docs.
# Adding or retiring an object when VA changes is a one-line edit here that
# describe, validation, and the example builder all pick up automatically.
_R = RoleSpec
_O = VaObject
OBJECTS: tuple[VaObject, ...] = (
    _O(
        "crosstab",
        "Tables",
        True,
        True,
        (_R("rows", multi=True), _R("columns", multi=True), _R("measures", multi=True)),
        (),
        purpose="Pivot table — measures at row x column category intersections.",
    ),
    _O(
        "listTable",
        "Tables",
        True,
        True,
        (_R("columns", multi=True),),
        ("columns",),
        purpose="Detail rows, spreadsheet-style — one row per record.",
    ),
    _O(
        "buttonBar",
        "Controls",
        True,
        True,
        (_R("category"), _R("measure")),
        (),
        purpose="Row of buttons picking one category value (prompt control; not auto-wired to filter).",
    ),
    _O(
        "dropdownList",
        "Controls",
        True,
        True,
        (_R("category"), _R("measure")),
        (),
        purpose="Compact dropdown picking a category value (prompt control; not auto-wired to filter).",
    ),
    _O(
        "list",
        "Controls",
        True,
        True,
        (_R("category"), _R("measure")),
        (),
        purpose="Scrollable multi-select list of category values (prompt control).",
    ),
    _O(
        "slider",
        "Controls",
        True,
        True,
        (_R("measure"),),
        (),
        purpose="Numeric range slider (prompt control).",
    ),
    _O(
        "textInput",
        "Controls",
        True,
        True,
        (_R("category"), _R("measure")),
        (),
        purpose="Free-text search box (prompt control).",
    ),
    _O(
        "standardContainer",
        "Containers",
        True,
        True,
        (),
        (),
        purpose="Groups objects into one auto-arranged block — the KPI-row / panel building block.",
    ),
    _O(
        "dataDrivenContent",
        "Content",
        True,
        True,
        (_R("variables", multi=True),),
        (),
        purpose="Embeds a custom third-party visualization fed by report data (its URL is NOT settable here).",
    ),
    _O(
        "image",
        "Content",
        True,
        True,
        (),
        (),
        purpose="Static image from a URL or a Viya folder — logos and branding.",
    ),
    _O(
        "text",
        "Content",
        True,
        True,
        (),
        (),
        purpose="Static narrative text — title bands, section intros, footnotes.",
    ),
    _O(
        "geoBubble",
        "Geo Maps",
        True,
        True,
        (_R("geography"), _R("size"), _R("color")),
        ("geography",),
        purpose="Map with bubbles sized/colored by measures at locations.",
    ),
    _O(
        "geoCluster",
        "Geo Maps",
        True,
        False,
        (_R("geography"), _R("size"), _R("color")),
        ("geography",),
        purpose="Map clustering dense point locations.",
    ),
    _O(
        "geoContour",
        "Geo Maps",
        True,
        True,
        (_R("geography"), _R("color")),
        ("geography",),
        purpose="Map with density contours over locations.",
    ),
    _O(
        "geoCoordinate",
        "Geo Maps",
        True,
        True,
        (_R("geography"), _R("size"), _R("color")),
        ("geography",),
        purpose="Map plotting individual coordinate points.",
    ),
    _O(
        "geoLine",
        "Geo Maps",
        True,
        True,
        (_R("geography"), _R("width"), _R("color"), _R("pattern")),
        ("geography",),
        purpose="Map drawing lines/routes between geo points.",
    ),
    _O(
        "geoLineCoordinate",
        "Geo Maps",
        True,
        True,
        (
            _R("geographyLine"),
            _R("widthLine"),
            _R("colorLine"),
            _R("patternLine"),
            _R("geographyScatter"),
            _R("sizeScatter"),
            _R("colorScatter"),
        ),
        (),
        purpose="Map combining a line layer with a coordinate-point layer.",
    ),
    _O(
        "geoNetwork",
        "Geo Maps",
        True,
        True,
        (_R("source"), _R("target"), _R("size"), _R("color"), _R("dataLabel")),
        (),
        purpose="Map of source-to-target links (flows) between locations.",
    ),
    _O(
        "geoPie",
        "Geo Maps",
        True,
        True,
        (_R("geography"), _R("size"), _R("response"), _R("group")),
        ("geography",),
        purpose="Map with pie markers at locations.",
    ),
    _O(
        "geoRegion",
        "Geo Maps",
        True,
        True,
        (_R("geography"), _R("color")),
        ("geography",),
        purpose="Choropleth — regions filled by a measure.",
    ),
    _O(
        "geoRegionCoordinate",
        "Geo Maps",
        True,
        True,
        (_R("geographyRegion"), _R("colorRegion"), _R("geographyScatter"), _R("sizeScatter"), _R("colorScatter")),
        (),
        purpose="Choropleth plus a coordinate-point overlay.",
    ),
    _O(
        "automatedExplanation",
        "Analytics",
        True,
        True,
        (_R("response"), _R("underlyingFactors", multi=True)),
        (),
        purpose="Auto-generated narrative explaining what drives a measure.",
    ),
    _O(
        "forecasting",
        "Analytics",
        True,
        True,
        (_R("timeAxis"), _R("measures", multi=True), _R("underlyingFactors", multi=True)),
        ("timeAxis",),
        purpose="Time-series forecast with confidence bands.",
    ),
    _O(
        "networkAnalysis",
        "Analytics",
        True,
        True,
        (_R("source"), _R("target"), _R("size"), _R("color"), _R("linkWidth"), _R("linkColor")),
        (),
        purpose="Node-link network diagram of relationships.",
    ),
    _O(
        "pathAnalysis",
        "Analytics",
        True,
        True,
        (_R("event"), _R("sequenceOrder"), _R("transactionId"), _R("weight")),
        (),
        purpose="Sankey-style flow of event sequences (journeys, funnels).",
    ),
    _O(
        "cluster",
        "Statistics",
        True,
        True,
        (_R("variables", multi=True),),
        (),
        purpose="Cluster analysis grouping observations across variables.",
    ),
    _O(
        "linearRegression",
        "Statistics",
        False,
        True,
        (_R("response"), _R("continuousEffects", multi=True), _R("classificationEffects", multi=True)),
        (),
        purpose="Linear regression fit summary (update-only via the API).",
    ),
    _O(
        "logisticRegression",
        "Statistics",
        True,
        False,
        (_R("response"), _R("continuousEffects", multi=True)),
        (),
        purpose="Logistic regression fit summary.",
    ),
    _O(
        "nonparametricLogisticRegression",
        "Statistics",
        True,
        True,
        (_R("response"), _R("splineEffects", multi=True)),
        (),
        purpose="Spline-based (GAM-like) logistic regression.",
    ),
    _O(
        "bayesianNetwork",
        "Machine Learning",
        True,
        True,
        (_R("response"), _R("predictors", multi=True)),
        (),
        purpose="Bayesian network model of a response and predictors.",
    ),
    _O(
        "factorizationMachine",
        "Machine Learning",
        True,
        True,
        (_R("response"), _R("predictors", multi=True)),
        (),
        purpose="Factorization machine model (sparse interactions).",
    ),
    _O(
        "forest",
        "Machine Learning",
        True,
        True,
        (_R("response"), _R("predictors", multi=True)),
        (),
        purpose="Random-forest model assessment.",
    ),
    _O(
        "gradientBoosting",
        "Machine Learning",
        True,
        True,
        (_R("response"), _R("predictors", multi=True)),
        (),
        purpose="Gradient-boosting model assessment (nearest to a decision tree).",
    ),
    _O(
        "neuralNetwork",
        "Machine Learning",
        True,
        True,
        (_R("response"), _R("predictors", multi=True)),
        (),
        purpose="Neural network model assessment.",
    ),
    _O(
        "supportVectorMachine",
        "Machine Learning",
        True,
        True,
        (_R("response"), _R("predictors", multi=True)),
        (),
        purpose="Support vector machine model assessment.",
    ),
    _O(
        "barChart",
        "Graphs",
        True,
        True,
        (_R("category"), _R("measures", multi=True), _R("frequency")),
        ("category",),
        purpose="Bars comparing measures across categories — the general workhorse.",
        render_required=(("measures", "frequency"),),
    ),
    _O(
        "boxPlot",
        "Graphs",
        True,
        True,
        (_R("category"), _R("measures", multi=True)),
        ("measures",),
        purpose="Distribution quartiles and outliers per category.",
    ),
    _O(
        "bubbleChangePlot",
        "Graphs",
        True,
        True,
        (_R("xStart"), _R("xEnd"), _R("yStart"), _R("yEnd"), _R("sizeStart"), _R("sizeEnd"), _R("group")),
        (),
        purpose="Bubbles showing movement between a start and an end state.",
    ),
    _O(
        "bubblePlot",
        "Graphs",
        True,
        True,
        (_R("xAxis"), _R("yAxis"), _R("size"), _R("group")),
        ("xAxis", "yAxis", "size"),
        purpose="Scatter with a third measure encoded as bubble size.",
    ),
    _O(
        "butterflyChart",
        "Graphs",
        True,
        True,
        (_R("category"), _R("measureBar"), _R("measureBar2")),
        (),
        purpose="Two measures diverging left/right from a shared category axis.",
    ),
    _O(
        "comparativeTimeSeriesPlot",
        "Graphs",
        True,
        True,
        (_R("timeAxis"), _R("measureTimeSeries1"), _R("measureTimeSeries2")),
        (),
        purpose="Two stacked time-series panels over the same time axis.",
    ),
    _O(
        "correlationMatrix",
        "Graphs",
        True,
        True,
        (_R("measures", multi=True),),
        ("measures",),
        purpose="Heat-colored pairwise correlations between measures.",
    ),
    _O(
        "dotPlot",
        "Graphs",
        True,
        True,
        (_R("category"), _R("measure")),
        ("category",),
        purpose="Dots marking a measure per category — lighter than bars.",
    ),
    _O(
        "dualAxisBarChart",
        "Graphs",
        True,
        True,
        (_R("category"), _R("measureBar"), _R("measureBar2")),
        (),
        purpose="Bars for two measures on independent Y axes.",
    ),
    _O(
        "dualAxisBarLineChart",
        "Graphs",
        True,
        True,
        (_R("category"), _R("measureBar"), _R("measureLine")),
        (),
        purpose="Bars plus a line on independent Y axes — volume vs rate.",
    ),
    _O(
        "dualAxisLineChart",
        "Graphs",
        True,
        True,
        (_R("category"), _R("measureLine"), _R("measureLine2")),
        (),
        purpose="Two lines on independent Y axes.",
    ),
    _O(
        "dualAxisTimeSeriesPlot",
        "Graphs",
        True,
        True,
        (_R("timeAxis"), _R("measureLine"), _R("measureLine2")),
        (),
        purpose="Two time series on independent Y axes.",
    ),
    _O(
        "gauge",
        "Graphs",
        True,
        True,
        (_R("measure"), _R("target"), _R("group")),
        ("measure",),
        purpose="KPI dial of a measure against a target.",
    ),
    _O(
        "heatMap",
        "Graphs",
        True,
        True,
        (_R("axisItems", multi=True), _R("color")),
        ("axisItems",),
        purpose="Grid cells colored by a measure.",
    ),
    _O(
        "histogram",
        "Graphs",
        True,
        True,
        (_R("measure"), _R("frequency")),
        ("measure",),
        purpose="Distribution of a single measure.",
    ),
    _O(
        "keyValue",
        "Graphs",
        True,
        True,
        (_R("measure"), _R("latticeCategory")),
        ("measure",),
        purpose="Big-number KPI tile — one headline value.",
    ),
    _O(
        "lineChart",
        "Graphs",
        True,
        True,
        (_R("category"), _R("measures", multi=True), _R("frequency")),
        ("category",),
        purpose="Lines across ordered categories — trends over a non-date axis.",
        render_required=(("measures", "frequency"),),
    ),
    _O(
        "needlePlot",
        "Graphs",
        True,
        True,
        (_R("xAxis"), _R("yAxis"), _R("group")),
        (),
        purpose="Vertical needles from a baseline — sparse event values.",
    ),
    _O(
        "numericSeriesPlot",
        "Graphs",
        True,
        True,
        (_R("xAxis"), _R("yAxis"), _R("group")),
        (),
        purpose="Line over a numeric (non-date) X axis.",
    ),
    _O(
        "parallelCoordinatePlot",
        "Graphs",
        True,
        True,
        (_R("variables", multi=True),),
        (),
        purpose="Profile lines across many variables at once.",
    ),
    _O(
        "pieChart",
        "Graphs",
        True,
        True,
        (_R("category"), _R("measures", multi=True), _R("frequency")),
        ("category",),
        purpose="Part-to-whole share per category (keep to a few slices).",
        render_required=(("measures", "frequency"),),
    ),
    _O(
        "scatterPlot",
        "Graphs",
        True,
        True,
        (_R("measures", multi=True), _R("color")),
        ("measures",),
        purpose="Point cloud relating two or more measures.",
    ),
    _O(
        "scheduleChart",
        "Graphs",
        True,
        True,
        (_R("task"), _R("start"), _R("finish"), _R("group")),
        (),
        purpose="Gantt-style bars of tasks over start/finish times.",
    ),
    _O(
        "stepPlot",
        "Graphs",
        True,
        True,
        (_R("xAxis"), _R("yAxis"), _R("group")),
        (),
        purpose="Stepped line — values holding constant between changes.",
    ),
    _O(
        "targetedBarChart",
        "Graphs",
        True,
        True,
        (_R("category"), _R("measure"), _R("target")),
        (),
        purpose="Bars with target markers — actual vs goal.",
    ),
    _O(
        "timeSeriesPlot",
        "Graphs",
        True,
        True,
        (_R("timeAxis"), _R("measure"), _R("group")),
        ("timeAxis",),
        purpose="Trend of a measure over a date/time axis.",
    ),
    _O(
        "treeMap",
        "Graphs",
        True,
        True,
        (_R("category"), _R("measure")),
        ("category",),
        purpose="Nested rectangles sized by a measure — hierarchical part-to-whole.",
    ),
    _O(
        "vectorPlot",
        "Graphs",
        True,
        True,
        (_R("xAxis"), _R("yAxis"), _R("xOrigin"), _R("yOrigin"), _R("color")),
        (),
        purpose="Arrows from origin to point — direction and magnitude of change.",
    ),
    _O(
        "waterfallChart",
        "Graphs",
        True,
        True,
        (_R("category"), _R("response")),
        (),
        purpose="Cumulative running total across categories.",
    ),
    _O(
        "wordCloud",
        "Graphs",
        True,
        True,
        (_R("word"), _R("size"), _R("color")),
        (),
        purpose="Words sized by frequency or a measure.",
        render_required=(("size",),),
    ),
)
del _R, _O

REPORT_OBJECT_TYPES: dict[str, VaObject] = {o.schema_key: o for o in OBJECTS}

# Objects that appear in the VA UI but have NO schema in the report API. Mapping
# each to the nearest addable alternative lets the tools redirect an agent
# instead of letting it fail with an opaque VA error.
NOT_ADDABLE: dict[str, str] = {
    "textTopics": "wordCloud",
    "decisionTree": "gradientBoosting",
    "generalizedAdditiveModel": "nonparametricLogisticRegression",
    "generalizedLinearModel": "logisticRegression",
}

CATEGORIES: tuple[str, ...] = (
    "Tables",
    "Controls",
    "Containers",
    "Content",
    "Graphs",
    "Geo Maps",
    "Analytics",
    "Statistics",
    "Machine Learning",
)

# Colloquial names an agent is likely to try, mapped to real schema keys —
# consulted before difflib so describe('kpi') lands on keyValue instead of
# nothing (difflib alone scores kpi->keyValue below its cutoff).
ALIASES: dict[str, str] = {
    "kpi": "keyValue",
    "tile": "keyValue",
    "bignumber": "keyValue",
    "indicator": "keyValue",
    "map": "geoRegion",
    "choropleth": "geoRegion",
    "table": "listTable",
    "datatable": "listTable",
    "pivot": "crosstab",
    "pivottable": "crosstab",
    "filter": "dropdownList",
    "dropdown": "dropdownList",
    "donut": "pieChart",
    "gantt": "scheduleChart",
    "sankey": "pathAnalysis",
    "trend": "timeSeriesPlot",
    "sparkline": "timeSeriesPlot",
    "container": "standardContainer",
    "textbox": "text",
    "label": "text",
    "logo": "image",
    "funnel": "pathAnalysis",
}

def normalize_key(value: str) -> str:
    """Case/spacing-insensitive lookup form: 'Decision Tree' -> 'decisiontree'."""
    return value.strip().lower().replace(" ", "").replace("-", "").replace("_", "")


NORMALIZED_TYPES: dict[str, str] = {normalize_key(k): k for k in REPORT_OBJECT_TYPES}
NORMALIZED_NOT_ADDABLE: dict[str, str] = {normalize_key(k): k for k in NOT_ADDABLE}


# Analytical intent -> the object types that serve it, surfaced in the describe
# index so an agent picks variety deliberately instead of barChart-for-everything.
INTENT_MAP: dict[str, list[str]] = {
    "single KPI number": ["keyValue", "gauge"],
    "KPI row of tiles": ["standardContainer", "keyValue"],
    "trend over time": ["timeSeriesPlot", "lineChart"],
    "compare categories": ["barChart", "dotPlot"],
    "actual vs target": ["targetedBarChart", "gauge"],
    "part-to-whole": ["pieChart", "treeMap"],
    "distribution": ["histogram", "boxPlot"],
    "relationship between measures": ["scatterPlot", "bubblePlot", "heatMap"],
    "geographic distribution": ["geoRegion", "geoBubble"],
    "detail rows": ["listTable"],
    "pivot / cross-tab": ["crosstab"],
    "filter control": ["dropdownList", "buttonBar", "slider"],
    "narrative text / title band": ["text"],
    "logo / branding": ["image"],
    "forecast": ["forecasting"],
    "what drives a measure": ["automatedExplanation"],
    "flows / sequences": ["pathAnalysis", "networkAnalysis"],
}

# Honest boundaries of the operations API, surfaced by describe() so an agent
# does not burn round-trips hunting for capabilities that do not exist.
API_LIMITS: tuple[str, ...] = (
    "Controls are added UNWIRED: no operation creates filters, actions, or links between objects "
    "— interactive wiring needs the VA UI (or raw report-content editing).",
    "setParameterValue only sets EXISTING report parameters; parameters cannot be created here.",
    "Calculated items, hierarchies, and custom sorts cannot be created via operations — save a "
    "data view once in the VA UI, then import it with applyDataView.",
    "No theme/page-numbering/footer operation exists; retheming lives in the SAS Report "
    "Transforms API.",
    "Placement and dataRoles are write-once: updateObject changes only options, and there is no "
    "move/resize/remove operation (delete_report or save-as/copy and rebuild instead).",
    "Objects are auto-named and auto-sized: no width/height/x/y anywhere, and VA rejects a "
    "caller-supplied object 'name' at add time — chain layouts on the names each apply returns.",
    "Page and report headers accept ONLY control objects — titles are text objects placed at the "
    "top of the page body.",
)

# dataItems vocabularies from the VA v8 OpenAPI spec (dataItemProperties),
# validated pre-flight because VA rejects unknown values with a whole-batch 400.
AGGREGATIONS: frozenset[str] = frozenset(
    {
        "sum",
        "average",
        "min",
        "max",
        "count",
        "median",
        "variance",
        "numberMissing",
        "standardDeviation",
        "standardError",
        "firstQuartile",
        "thirdQuartile",
        "skewness",
        "kurtosis",
        "coefficientOfVariation",
        "correctedSumOfSquares",
        "uncorrectedSumOfSquares",
        "tStatistic",
        "pValue",
    }
)
CLASSIFICATIONS: frozenset[str] = frozenset({"category", "measure", "geography"})
# The geographyDataSource union from the spec: named-region contexts OR raw
# lat/long coordinates (geographyCoordinates) — exactly one of the two.
GEO_SOURCE_KEYS: frozenset[str] = frozenset(
    {
        "geographyNameCodeContext",
        "geographyCountryRegion",
        "geographyDataProvider",
        "geographyCoordinates",
    }
)
# Named SAS format: optional $, leading letter, optional width, dot, optional
# decimals (DOLLAR12.2, COMMA10., PERCENT8.1, DATE9., $CHAR20.). VA rejects
# bare numeric w.d forms like "8.1" — and the whole atomic batch with them.
SAS_FORMAT_RE = re.compile(r"^\$?[A-Za-z][A-Za-z0-9_]*\.\d*$")
GEO_NAME_CODE_CONTEXTS: frozenset[str] = frozenset(
    {
        "CountryRegionNames",
        "CountryRegionISO2LetterCodes",
        "CountryRegionISO3LetterCodes",
        "CountryRegionISONumericCodes",
        "CountryRegionSASMapIdValues",
        "SubdivisionNames",
        "SubdivisionSASMapIdValues",
        "USStateNames",
        "USStateAbbreviations",
        "USZipCodes",
    }
)

# The eight operations, with a worked example each, for describe(operation=...)
# and the tool docstrings. The index lists key+purpose; the full entry (example,
# notes) is returned by describe_report_objects(operation='addData') etc.
OPERATIONS: tuple[dict[str, Any], ...] = (
    {
        "key": "addData",
        "purpose": (
            "Bind a CAS table as a data source; dataItems is where reports get polish — "
            "readable labels, formats, aggregations, geography."
        ),
        "required": ["cas.{server, library, table}"],
        "example": {
            "addData": {
                "cas": {"server": "cas-shared-default", "library": "Public", "table": "SALES"},
                "dataItems": [
                    {
                        "dataItem": "Revenue",
                        "properties": {"name": "Revenue (USD)", "format": "DOLLAR12.2", "aggregation": "average"},
                    },
                    {
                        "dataItem": "State",
                        "properties": {
                            "classification": "geography",
                            "geographyDataSource": {"geographyNameCodeContext": "USStateNames"},
                        },
                    },
                ],
            }
        },
        "notes": [
            "addObject.dataSource references the addData 'name', defaulting to cas.table.",
            "After a rename, dataRoles must use the NEW name (e.g. 'Revenue (USD)').",
            f"aggregation: one of {sorted(AGGREGATIONS)}.",
            "format must be a NAMED SAS format (DOLLAR12.2, PERCENT8.1, COMMA10., DATE9.) — "
            "bare numeric forms like '8.1' are rejected.",
            "classification: category | measure | geography — geography classification is the "
            "precondition for every Geo Map object.",
            f"geographyNameCodeContext: one of {sorted(GEO_NAME_CODE_CONTEXTS)}.",
            "Raw lat/long point data: {'classification': 'geography', 'geographyDataSource': "
            "{'geographyCoordinates': {'latitudeDataItem': 'LATITUDE', 'longitudeDataItem': "
            "'LONGITUDE'}}} — coordinates OR a name-code context, never both.",
        ],
    },
    {
        "key": "addPage",
        "purpose": (
            "Add a page; a 'title' becomes a text band at the top of the page body; reference the "
            "page by pageName when placing objects."
        ),
        "required": [],
        "example": {"addPage": {"pageName": "Overview", "title": "Sales Overview", "pagePosition": "0"}},
        "notes": [
            "pagePosition is a STRING here ('0' = first) — unlike the numeric "
            "report-placement pagePosition.",
            "The title text lands in the page body (page headers accept only controls).",
        ],
    },
    {
        "key": "addObject",
        "purpose": "Add a visual/control/content object, titled and placed.",
        "required": ["object.<type>  (or reportObject)"],
        "example": {
            "addObject": {
                "object": {
                    "barChart": {
                        "dataSource": "SALES",
                        "dataRoles": {"category": "Region", "measures": ["Revenue (USD)"]},
                        "options": {"object": {"title": "Revenue by Region"}},
                    }
                },
                "placement": {"page": {"target": "Overview"}},
            }
        },
        "notes": [
            "Allowed object-spec keys: dataSource, dataRoles, options (VA rejects anything else, "
            "including a caller-supplied 'name').",
            "options.object.title/alternativeText work at add time on every type except "
            "standardContainer.",
        ],
    },
    {
        "key": "updateObject",
        "purpose": "Change an existing object's options (title, etc.) — never its placement or data roles.",
        "required": ["object.<type>.name"],
        "example": {
            "updateObject": {
                "object": {"barChart": {"name": "ve15", "options": {"object": {"title": "MSRP by Region"}}}}
            }
        },
        "notes": ["'name' is the object name (ve*) or label returned by a previous apply or get_report_outline."],
    },
    {
        "key": "setParameterValue",
        "purpose": "Set an EXISTING report parameter's value (parameters cannot be created here).",
        "required": ["name", "value"],
        "example": {"setParameterValue": {"name": "originFilter", "value": "Asia"}},
    },
    {
        "key": "updateData",
        "purpose": "Update an existing data source's items in place.",
        "required": ["data"],
        "example": {"updateData": {"data": {"name": "SALES"}}},
    },
    {
        "key": "changeData",
        "purpose": "Swap a data source for a different CAS table (copy-and-replace).",
        "required": ["originalData", "replacementData"],
        "example": {
            "changeData": {
                "originalData": {"cas": {"server": "cas-shared-default", "library": "Public", "table": "SALES"}},
                "replacementData": {
                    "cas": {"server": "cas-shared-default", "library": "Public", "table": "SALES_2025"}
                },
            }
        },
    },
    {
        "key": "applyDataView",
        "purpose": (
            "Import a data view saved in the VA UI — the sanctioned route to calculated items, "
            "hierarchies, and custom sorts."
        ),
        "required": ["targetData", "dataView"],
        "example": {
            "applyDataView": {
                "dataItemConflictResolution": "createDuplicate",
                "targetData": {"name": "SALES"},
                "dataView": {"name": "SALES View 1"},
            }
        },
        "notes": [
            "dataItemConflictResolution: abort (default) | createDuplicate | replaceExisting | "
            "keepExisting | dataMapping.",
            "dataView takes {'name': ...} or {'uri': ...}; there is no API to create or list data "
            "views — save one in the VA UI first.",
        ],
    },
)


# VA enforces additionalProperties:false on every object spec: an unknown key
# (including a caller-supplied 'name') fails the whole atomic batch with an HTTP
# 400 — catch it pre-flight instead. standardContainer is stricter still: it
# accepts NO properties at add time, not even 'options'.
OBJECT_SPEC_ALLOWED_KEYS = frozenset({"dataSource", "dataRoles", "options"})


# Extra guidance for objects whose real-world contract probing showed to be
# surprising; merged into the describe() detail.
OBJECT_NOTES: dict[str, dict[str, str]] = {
    "text": {
        "content_note": (
            "Set the text via options.content (a plain string — no markup). Caveat: some Viya "
            "builds misroute options.content to the report's FIRST text object on both add and "
            "update, so keep one content-bearing text per report, or write additional texts via "
            "the report content endpoint (PUT /reports/reports/{id}/content)."
        ),
    },
    "image": {
        "options_note": (
            "options is a oneOf directly under it (no wrapper): {'url': 'https://.../logo.png'} "
            "for a web image, OR {'imageName': 'logo.png', 'imageFolder': '/folders/folders/"
            "{folderId}'} for a repository image (imageFolder is the folders-service URI, not a "
            "display path). URL extension must look like an image (.png/.jpg); reachability is "
            "NOT checked at add time, repository existence IS."
        ),
    },
    "standardContainer": {
        "options_note": (
            "Accepts NO properties at add time — VA rejects even 'options'. Add it bare ({}), "
            "then title it via a follow-up updateObject using the name the apply returned. "
            "Containers auto-arrange their children; there is no layout/direction knob."
        ),
    },
    "dataDrivenContent": {
        "options_note": (
            "The content URL is NOT settable via the operations API — a dataDrivenContent added "
            "here renders empty until the URL is set in the VA UI."
        ),
    },
    "histogram": {
        "data_note": (
            "A dataItem carrying a preset aggregation breaks histogram binning (the rendered "
            "object shows 'missing data item'). Point the histogram's measure at the raw, "
            "unaggregated column — or use a boxPlot for aggregated comparisons."
        ),
    },
    "keyValue": {
        "title_note": (
            "Skip options.object.title on KPI tiles — the tile already renders its measure's "
            "label prominently, so a title duplicates it. Give the measure a good display name "
            "via addData dataItems (e.g. 'Avg Loan (USD)') instead, and place tiles side by side "
            "in a standardContainer, never stacked on the page."
        ),
    },
}
