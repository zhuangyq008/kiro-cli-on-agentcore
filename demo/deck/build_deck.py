"""Build the customer deck: an editable PPTX and a pixel preview of the same layout.

    python3 demo/deck/build_deck.py
        -> build/kiro-cli-on-agentcore.pptx       native text and shapes, editable
        -> build/kiro-cli-on-agentcore.pdf        the same slides, for sending and for checking layout
        -> build/preview/slide-NN.png

Slides are described once (deck_content.py) as boxes, text and images in inches; this file turns
that description into PowerPoint shapes and into absolutely positioned HTML, which headless
Chromium renders. The HTML is how the layout gets checked without PowerPoint.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from playwright.sync_api import sync_playwright
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

from deck_content import build_slides
from deck_model import ACCENT, FONT_EA, FONT_LATIN, FONT_MONO, PX, WHITE, H, Image, Rect, Slide, Text, W

HERE = Path(__file__).parent
BUILD = HERE / "build"
BULLET_INDENT = 0.26  # inches


# ----------------------------------------------------------------------------- PPTX
def _rgb(hex_color: str) -> RGBColor:
    return RGBColor.from_string(hex_color)


def _set_fonts(run, mono: bool) -> None:  # noqa: ANN001
    run.font.name = FONT_MONO if mono else FONT_LATIN
    rpr = run._r.get_or_add_rPr()
    for tag in ("a:ea", "a:cs"):
        el = rpr.find(qn(tag))
        if el is None:
            el = rpr.makeelement(qn(tag), {})
            rpr.append(el)
        el.set("typeface", FONT_EA)


def _pptx_bullet(paragraph) -> None:  # noqa: ANN001
    """A real bullet with a hanging indent, so wrapped lines align with the text."""
    ppr = paragraph._p.get_or_add_pPr()
    ppr.set("marL", str(Inches(BULLET_INDENT)))
    ppr.set("indent", str(-Inches(BULLET_INDENT)))
    color = ppr.makeelement(qn("a:buClr"), {})
    color.append(color.makeelement(qn("a:srgbClr"), {"val": ACCENT}))
    ppr.append(color)
    ppr.append(ppr.makeelement(qn("a:buFont"), {"typeface": "Arial"}))
    ppr.append(ppr.makeelement(qn("a:buChar"), {"char": "•"}))


def _pptx_text(slide, t: Text) -> None:  # noqa: ANN001
    box = slide.shapes.add_textbox(Inches(t.x), Inches(t.y), Inches(t.w), Inches(t.h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = {"top": MSO_ANCHOR.TOP, "middle": MSO_ANCHOR.MIDDLE, "bottom": MSO_ANCHOR.BOTTOM}[t.valign]
    for i, para in enumerate(t.paras):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}[para.align]
        p.line_spacing = t.line_spacing
        p.space_after = Pt(para.space_after)
        if para.bullet:
            _pptx_bullet(p)
        for r in para.runs:
            run = p.add_run()
            run.text = r.text
            run.font.size = Pt(r.size or t.size)
            run.font.bold = r.bold
            run.font.color.rgb = _rgb(r.color or t.color)
            _set_fonts(run, r.mono)


def _pptx_rect(slide, r: Rect) -> None:  # noqa: ANN001
    kind = {"rect": MSO_SHAPE.ROUNDED_RECTANGLE if r.radius else MSO_SHAPE.RECTANGLE,
            "oval": MSO_SHAPE.OVAL, "chevron": MSO_SHAPE.CHEVRON}[r.shape]
    shape = slide.shapes.add_shape(kind, Inches(r.x), Inches(r.y), Inches(r.w), Inches(r.h))
    if r.shape == "rect" and r.radius:
        shape.adjustments[0] = min(0.5, r.radius / min(r.w, r.h))
    shape.shadow.inherit = False
    if r.fill:
        shape.fill.solid()
        shape.fill.fore_color.rgb = _rgb(r.fill)
    else:
        shape.fill.background()
    if r.line:
        shape.line.color.rgb = _rgb(r.line)
        shape.line.width = Pt(1)
    else:
        shape.line.fill.background()


def write_pptx(slides: list[Slide], path: Path) -> None:
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(W), Inches(H)
    blank = prs.slide_layouts[6]
    for s in slides:
        slide = prs.slides.add_slide(blank)
        if s.background != WHITE:
            _pptx_rect(slide, Rect(0, 0, W, H, fill=s.background))
        for el in s.elements:
            if isinstance(el, Text):
                _pptx_text(slide, el)
            elif isinstance(el, Rect):
                _pptx_rect(slide, el)
            elif isinstance(el, Image):
                slide.shapes.add_picture(str(el.path), Inches(el.x), Inches(el.y), Inches(el.w), Inches(el.h))
        if s.notes:
            slide.notes_slide.notes_text_frame.text = s.notes
    prs.save(str(path))


# ----------------------------------------------------------------------------- HTML preview
def _css_box(el) -> str:  # noqa: ANN001
    return f"left:{el.x * PX:.1f}px;top:{el.y * PX:.1f}px;width:{el.w * PX:.1f}px;height:{el.h * PX:.1f}px;"


def _html_text(t: Text) -> str:
    justify = {"top": "flex-start", "middle": "center", "bottom": "flex-end"}[t.valign]
    out = [f'<div class="t" style="{_css_box(t)}justify-content:{justify};color:#{t.color};'
           f'font-size:{t.size * 2}px;line-height:{t.line_spacing * 1.32:.3f};">']
    for para in t.paras:
        spans = "".join(
            f'<span style="{"font-weight:700;" if r.bold else ""}{f"color:#{r.color};" if r.color else ""}'
            f'{"font-family:var(--mono);" if r.mono else ""}{f"font-size:{r.size * 2}px;" if r.size else ""}">'
            f"{html.escape(r.text)}</span>"
            for r in para.runs
        )
        if not any(r.text for r in para.runs):
            spans = "&nbsp;"
        indent = BULLET_INDENT * PX
        bullet = (f'<span style="display:inline-block;width:{indent}px;margin-left:-{indent}px;color:#{ACCENT};'
                  'font-weight:700">•</span>') if para.bullet else ""
        pad = f"padding-left:{indent}px;" if para.bullet else ""
        out.append(f'<p style="text-align:{para.align};margin-bottom:{para.space_after * 2}px;{pad}">{bullet}{spans}</p>')
    out.append("</div>")
    return "".join(out)


def _html_rect(r: Rect) -> str:
    style = _css_box(r)
    style += f"background:#{r.fill};" if r.fill else ""
    style += f"border:2px solid #{r.line};" if r.line else ""
    if r.shape == "oval":
        style += "border-radius:50%;"
    elif r.shape == "chevron":
        style += "clip-path:polygon(0 0,75% 0,100% 50%,75% 100%,0 100%,25% 50%);"
    elif r.radius:
        style += f"border-radius:{r.radius * PX:.1f}px;"
    return f'<div class="r" style="{style}"></div>'


def write_preview(slides: list[Slide], pdf: Path, png_dir: Path) -> None:
    pages = []
    for s in slides:
        body = []
        for el in s.elements:
            if isinstance(el, Text):
                body.append(_html_text(el))
            elif isinstance(el, Rect):
                body.append(_html_rect(el))
            elif isinstance(el, Image):
                body.append(f'<img src="{el.path.resolve().as_uri()}" style="position:absolute;{_css_box(el)}">')
        pages.append(f'<section style="background:#{s.background}">{"".join(body)}</section>')
    doc = f"""<!doctype html><meta charset="utf-8"><style>
      :root {{ --mono: "DejaVu Sans Mono","Noto Sans CJK SC",monospace; }}
      @page {{ size: {W * PX}px {H * PX}px; margin: 0; }}
      * {{ box-sizing: border-box; margin: 0; padding: 0; }}
      body {{ font-family: "Noto Sans CJK SC","Noto Sans",sans-serif; }}
      section {{ position: relative; width: {W * PX}px; height: {H * PX}px; overflow: hidden; page-break-after: always; }}
      .t {{ position: absolute; display: flex; flex-direction: column; overflow: visible; }}
      .t p {{ white-space: pre-wrap; }}
      .r {{ position: absolute; }}
    </style>{"".join(pages)}"""
    page_html = BUILD / "preview.html"
    page_html.write_text(doc)
    png_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": int(W * PX), "height": int(H * PX)})
        page.goto(page_html.as_uri())
        page.pdf(path=str(pdf), width=f"{W * PX}px", height=f"{H * PX}px", print_background=True)
        for i, section in enumerate(page.query_selector_all("section"), start=1):
            section.screenshot(path=str(png_dir / f"slide-{i:02d}.png"))
        # Text that does not fit its box is the layout error this preview exists to catch.
        overflow = page.evaluate(
            """() => [...document.querySelectorAll('section')].flatMap((s, i) =>
                 [...s.querySelectorAll('.t')].filter(t => {
                   const inner = [...t.children].reduce((h, p) => h + p.offsetHeight, 0);
                   return inner > t.clientHeight + 2 || t.scrollWidth > t.clientWidth + 2;
                 }).map(t => `slide ${i + 1}: "${t.innerText.slice(0, 30)}"`))"""
        )
        browser.close()
    for item in overflow:
        print("OVERFLOW", item)


def render_architecture(svg: Path, out: Path) -> None:
    """The video's architecture diagram, restyled for a light background."""
    css = """
      body { margin: 0; background: #fff; }
      svg { width: 1720px; height: 770px; display: block; }
      text { font-family: "Noto Sans CJK SC", sans-serif; fill: #0B1F3A; }
      .box { fill: #F3F6FA; stroke: #C3CCDA; stroke-width: 2; }
      .acct { fill: #FBFCFE; stroke: #FF9900; stroke-width: 3; stroke-dasharray: 10 8; }
      .vm { fill: #FFFFFF; stroke: #0A6EBD; stroke-width: 2.5; }
      .t1 { font-size: 30px; font-weight: 600; } .t2, .t3 { font-size: 23px; fill: #51607A; }
      .lbl { font-size: 22px; fill: #0A6EBD; } .acctlbl { font-size: 26px; fill: #B36B00; font-weight: 600; }
      .arrow { stroke: #0A6EBD; stroke-width: 3; fill: none; marker-end: url(#ah); }
      .arrow.ext { stroke: #FF9900; marker-end: url(#ah2); }
      .ext-box { fill: #FFF6E5; stroke: #FF9900; stroke-width: 2; }
      #ah path { fill: #0A6EBD; }
    """
    doc = f"<!doctype html><meta charset='utf-8'><style>{css}</style>{svg.read_text()}"
    src = BUILD / "architecture.html"
    src.write_text(doc)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1720, "height": 770}, device_scale_factor=2)
        page.goto(src.as_uri())
        page.screenshot(path=str(out))
        browser.close()


def main() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    arch = BUILD / "architecture.png"
    render_architecture(HERE.parent / "video" / "architecture.svg", arch)
    casts_dir = HERE.parent / "video" / "build" / "casts"
    casts = {p.stem: json.loads(p.read_text()) for p in casts_dir.glob("*.json")} if casts_dir.exists() else {}
    slides = build_slides(arch, casts)
    write_pptx(slides, BUILD / "kiro-cli-on-agentcore.pptx")
    write_preview(slides, BUILD / "kiro-cli-on-agentcore.pdf", BUILD / "preview")
    print(f"{len(slides)} slides -> {BUILD}")


if __name__ == "__main__":
    main()
