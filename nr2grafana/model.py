"""New Relic dashboard model.

Normalizes the two wire formats — NerdGraph entity reads and UI
"Copy JSON" exports — into one structure. They share the same schema except
for read-only fields (guids, ids, timestamps) and
linkedEntities-vs-linkedEntityGuids, all of which we treat as optional.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class NRWidget:
    title: str = ""
    viz_id: str = ""
    layout: Dict[str, int] = field(default_factory=dict)  # column,row,width,height
    raw_configuration: Dict[str, Any] = field(default_factory=dict)

    @property
    def nrql_queries(self) -> List[Dict[str, Any]]:
        return self.raw_configuration.get("nrqlQueries") or []


@dataclass
class NRPage:
    name: str = ""
    description: str = ""
    widgets: List[NRWidget] = field(default_factory=list)


@dataclass
class NRVariable:
    name: str = ""
    title: str = ""
    type: str = "STRING"  # NRQL | ENUM | STRING
    nrql_query: Optional[Dict[str, Any]] = None  # {accountIds, query}
    items: List[Dict[str, Any]] = field(default_factory=list)
    default_values: List[str] = field(default_factory=list)
    is_multi: bool = False
    replacement_strategy: str = "DEFAULT"


@dataclass
class NRDashboard:
    name: str = ""
    description: str = ""
    permissions: str = ""
    pages: List[NRPage] = field(default_factory=list)
    variables: List[NRVariable] = field(default_factory=list)
    guid: str = ""
    account_id: Optional[int] = None

    def widget_count(self) -> int:
        return sum(len(p.widgets) for p in self.pages)


def parse_nr_dashboard(data: Dict[str, Any]) -> NRDashboard:
    """Parse a NerdGraph read or UI export into an NRDashboard."""
    if not isinstance(data, dict):
        raise ValueError("dashboard JSON must be an object")
    # Some tools wrap in {"dashboard": {...}}; tolerate it.
    if "dashboard" in data and isinstance(data["dashboard"], dict) \
            and "pages" in data["dashboard"]:
        data = data["dashboard"]
    if "pages" not in data:
        raise ValueError(
            "not a New Relic dashboard JSON (missing 'pages'). Expected the "
            "NerdGraph export or the dashboard UI 'Copy JSON' format.")
    if not isinstance(data.get("pages"), list):
        raise ValueError("'pages' must be a list")

    dash = NRDashboard(
        name=data.get("name") or "Untitled",
        description=data.get("description") or "",
        permissions=data.get("permissions") or "",
        guid=data.get("guid") or "",
    )

    for page in data.get("pages") or []:
        if not isinstance(page, dict):
            raise ValueError("each page must be an object, got %r"
                             % type(page).__name__)
        widgets = page.get("widgets") or []
        if not isinstance(widgets, list):
            raise ValueError("page 'widgets' must be a list")
        p = NRPage(name=page.get("name") or "Page",
                   description=page.get("description") or "")
        for w in widgets:
            if not isinstance(w, dict):
                raise ValueError("each widget must be an object")
            viz = (w.get("visualization") or {}).get("id") or ""
            layout = w.get("layout")
            raw = w.get("rawConfiguration")
            widget = NRWidget(
                title=w.get("title") or "",
                viz_id=viz,
                layout=layout if isinstance(layout, dict) else {},
                raw_configuration=raw if isinstance(raw, dict) else {},
            )
            p.widgets.append(widget)
            if dash.account_id is None:
                for nq in widget.nrql_queries:
                    acct = nq.get("accountId") or \
                        (nq.get("accountIds") or [None])[0]
                    if acct:
                        dash.account_id = acct
                        break
        dash.pages.append(p)

    variables = data.get("variables") or []
    if not isinstance(variables, list):
        variables = []
    for v in variables:
        if not isinstance(v, dict):
            continue
        defaults: List[str] = []
        for dv in v.get("defaultValues") or []:
            val = (dv or {}).get("value") or {}
            if isinstance(val, dict) and val.get("string") is not None:
                defaults.append(str(val["string"]))
            elif isinstance(val, str):
                defaults.append(val)
        dash.variables.append(NRVariable(
            name=v.get("name") or "",
            title=v.get("title") or "",
            type=(v.get("type") or "STRING").upper(),
            nrql_query=v.get("nrqlQuery"),
            items=v.get("items") or [],
            default_values=defaults,
            is_multi=bool(v.get("isMultiSelection")),
            replacement_strategy=(v.get("replacementStrategy") or "DEFAULT").upper(),
        ))
    return dash
