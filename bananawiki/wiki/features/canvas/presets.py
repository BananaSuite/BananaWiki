"""Starting layouts for new canvases: flowchart, mind map, retrospective and SWOT.

:func:`document` builds a template's document with its labels in the current
language; the result goes through :func:`.model.clean_document` like any
other stored document, so templates obey the same rules as user data.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ...i18n import t
from . import model

GREEN, ORANGE, BLUE, RED, PURPLE, YELLOW = "#4caf50", "#ff9800", "#2196f3", "#e91e63", "#9c27b0", "#ffeb3b"


def _node(node_id: str, x: int, y: int, width: int, height: int, **fields: Any) -> dict[str, Any]:
    return {"id": node_id, "type": "text", "x": x, "y": y, "width": width, "height": height, "layer": 1,
            "text_size": 14, **fields}


def _edge(edge_id: str, start: str, end: str, **fields: Any) -> dict[str, Any]:
    return {"id": edge_id, "from": start, "to": end, "label": "", **fields}


def _flowchart() -> dict[str, Any]:
    nodes = [
        _node("start", -100, 0, 200, 70, shape="pill", color=GREEN, content=t("canvas.preset.flow.start")),
        _node("step", -110, 140, 220, 90, shape="rounded", content=t("canvas.preset.flow.step")),
        _node("decision", -120, 300, 240, 150, shape="diamond", color=YELLOW,
              content=t("canvas.preset.flow.decision")),
        _node("io", 240, 335, 240, 80, shape="parallelogram", color=BLUE, content=t("canvas.preset.flow.io")),
        _node("end", -100, 530, 200, 70, shape="pill", color=RED, content=t("canvas.preset.flow.end")),
    ]
    edges = [
        _edge("e1", "start", "step"),
        _edge("e2", "step", "decision"),
        _edge("e3", "decision", "end", label=t("canvas.preset.flow.yes")),
        _edge("e4", "decision", "io", label=t("canvas.preset.flow.no")),
        # The loop back leaves the top of the input step and enters the side of the first step,
        # so it does not run along the main line.
        _edge("e5", "io", "step", route="elbow", from_side="top", to_side="right"),
    ]
    return {"nodes": nodes, "edges": edges}


def _mind_map() -> dict[str, Any]:
    nodes = [_node("center", -130, -65, 260, 130, shape="ellipse", color=PURPLE, text_size=18,
                   content=t("canvas.preset.mind.center"))]
    edges = []
    places = ((-480, -230, GREEN), (260, -230, BLUE), (-480, 160, ORANGE), (260, 160, RED))
    for number, (x, y, color) in enumerate(places, start=1):
        nodes.append(_node(f"idea{number}", x, y, 220, 70, shape="pill", color=color,
                           content=t("canvas.preset.mind.idea", number=number)))
        edges.append(_edge(f"e{number}", "center", f"idea{number}", arrow="none", route="straight"))
    return {"nodes": nodes, "edges": edges}


def _columns(titles: list[tuple[str, str]], width: int, height: int, columns: int) -> list[dict[str, Any]]:
    """Locked frames laid out in a grid, each with one sticky note inside."""
    nodes = []
    for index, (title, color) in enumerate(titles):
        x = (index % columns) * (width + 40)
        y = (index // columns) * (height + 40)
        nodes.append(_node(f"frame{index + 1}", x, y, width, height, layer=0, locked=True, color=color,
                           display_text=title, content=""))
        nodes.append(_node(f"note{index + 1}", x + 30, y + 70, width - 60, 110, layer=2, shape="note",
                           content=t("canvas.preset.sticky")))
    return nodes


def _retrospective() -> dict[str, Any]:
    titles = [(t("canvas.preset.retro.well"), GREEN), (t("canvas.preset.retro.improve"), ORANGE),
              (t("canvas.preset.retro.actions"), BLUE)]
    return {"nodes": _columns(titles, 340, 600, 3), "edges": []}


def _swot() -> dict[str, Any]:
    titles = [(t("canvas.preset.swot.strengths"), GREEN), (t("canvas.preset.swot.weaknesses"), ORANGE),
              (t("canvas.preset.swot.opportunities"), BLUE), (t("canvas.preset.swot.threats"), RED)]
    return {"nodes": _columns(titles, 420, 320, 2), "edges": []}


TEMPLATES: dict[str, Callable[[], dict[str, Any]]] = {
    "flowchart": _flowchart,
    "mind_map": _mind_map,
    "retrospective": _retrospective,
    "swot": _swot,
}


def names() -> tuple[str, ...]:
    return tuple(TEMPLATES)


def document(name: str | None) -> dict[str, Any] | None:
    """The clean document of template *name*, or ``None`` for an unknown or empty name."""
    build = TEMPLATES.get(name or "")
    return model.clean_document(build()) if build else None
