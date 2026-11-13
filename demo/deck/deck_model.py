"""Slide model shared by the content (deck_content.py) and the writers (build_deck.py)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

W, H = 13.333, 7.5  # inches, 16:9
PX = 144  # preview pixels per inch (1920 wide)
FONT_EA = "Microsoft YaHei"
FONT_LATIN = "Arial"
FONT_MONO = "Consolas"

INK, MUTED, ACCENT, BLUE = "0B1F3A", "51607A", "FF9900", "0A6EBD"
PANEL, LINE, WHITE, GREEN, DARK = "F3F6FA", "D5DCE6", "FFFFFF", "1E8E3E", "0B1220"


@dataclass
class Run:
    text: str
    bold: bool = False
    color: str | None = None
    mono: bool = False
    size: float | None = None


@dataclass
class Para:
    runs: list[Run]
    align: str = "left"
    space_after: float = 0.0  # points
    bullet: bool = False


@dataclass
class Text:
    x: float
    y: float
    w: float
    h: float
    paras: list[Para]
    size: float = 16
    color: str = INK
    valign: str = "top"
    line_spacing: float = 1.25


@dataclass
class Rect:
    x: float
    y: float
    w: float
    h: float
    fill: str | None = PANEL
    line: str | None = None
    radius: float = 0.0  # inches; 0 = square corners
    shape: str = "rect"  # rect | oval | chevron


@dataclass
class Image:
    path: Path
    x: float
    y: float
    w: float
    h: float


@dataclass
class Slide:
    elements: list = field(default_factory=list)
    notes: str = ""
    background: str = WHITE

    def add(self, *items) -> Slide:  # noqa: ANN002
        self.elements.extend(items)
        return self


def P(*parts, **kw) -> Para:  # noqa: ANN002, ANN003, N802
    """Paragraph from strings and Runs."""
    return Para([p if isinstance(p, Run) else Run(p) for p in parts], **kw)


def T(x, y, w, h, *paras, **kw) -> Text:  # noqa: ANN001, ANN002, ANN003, N802
    return Text(x, y, w, h, [p if isinstance(p, Para) else P(p) for p in paras], **kw)
