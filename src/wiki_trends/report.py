"""Paginated A4 PDF report builder (1-5 pages).

Deliberate separation of concerns: everything under "Дані" is generated
straight from `stats.TrendResult` (deterministic, testable, cannot drift
from the numbers). The "Висновки та рекомендації" block is the only place
free text is allowed in, and it is rendered in a visually distinct box
directly below the data it must be justified by - so whoever wrote that
text (typically the calling agent) is checkable against the same report.

Layout approach: everything is placed by a "cursor" that walks down the
page in inches, converted to figure-fraction only at the point of drawing.
An earlier version sized sections via nested, hand-tuned fractional
gridspec ratios; that broke as soon as content size varied because a
ratio-of-a-ratio has no fixed physical meaning. Tracking one absolute
cursor in inches is what a real layout engine does, and it generalizes to
pagination for free: before drawing a block, `_Doc.ensure(h)` checks
whether `h` inches still fit above the bottom margin and, if not, starts a
new page. Blocks that can exceed a page on their own (the stats table,
the recommendation text) are drawn in chunks, each chunk `ensure`-d.

Page budget: up to `SOFT_MAX_PAGES` (3) is the recommended length and
builds silently; up to `HARD_MAX_PAGES` (5) builds with a warning; beyond
that no PDF is written and `ReportTooLongError` is raised - a 6+ page
"short report" is a sign the study should be split, not printed.
"""
from __future__ import annotations

import textwrap
import warnings
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import Rectangle

PAGE_W, PAGE_H = 8.27, 11.69  # A4 portrait, inches
MARGIN_X = 0.55
MARGIN_TOP = 0.5
MARGIN_BOTTOM = 0.6  # leaves room for the page-number footer
CONTENT_W = PAGE_W - 2 * MARGIN_X
CONFIDENCE_UA = {"high": "Висока", "medium": "Середня", "low": "Низька", "none": "Немає даних"}

SOFT_MAX_PAGES = 3
HARD_MAX_PAGES = 5


class ReportTooLongError(ValueError):
    """Raised when the report would exceed HARD_MAX_PAGES; no PDF is written."""

    def __init__(self, n_pages: int):
        self.n_pages = n_pages
        super().__init__(
            f"Report would be {n_pages} pages, over the {HARD_MAX_PAGES}-page "
            f"maximum (recommended: <= {SOFT_MAX_PAGES}). No PDF was written. "
            f"Split the study into fewer languages per report, or shorten "
            f"--recommendation."
        )


# Approximate line height (inches) for a given fontsize, incl. leading.
def _line_h_in(fontsize: float) -> float:
    return fontsize / 72.0 * 1.35


class _Doc:
    """Tracks a downward-moving cursor (in inches from the page top) across
    one or more A4 figures, drawing figure-fraction text/shapes relative to
    it, and starting a new page whenever the next block would not fit."""

    def __init__(self):
        self.figs: list = []
        self.new_page()

    @property
    def fig(self):
        return self.figs[-1]

    def new_page(self):
        self.figs.append(plt.figure(figsize=(PAGE_W, PAGE_H), dpi=150))
        self.y = PAGE_H - MARGIN_TOP

    @property
    def room(self) -> float:
        return self.y - MARGIN_BOTTOM

    @property
    def at_page_top(self) -> bool:
        return self.y >= PAGE_H - MARGIN_TOP - 1e-6

    def ensure(self, inches: float):
        """Start a new page unless `inches` fit below the cursor (a block
        taller than a whole page is placed at a page top and left to the
        caller to chunk)."""
        if inches > self.room and not self.at_page_top:
            self.new_page()

    def frac_y(self, y_in: float) -> float:
        return y_in / PAGE_H

    def frac_x(self, x_in: float) -> float:
        return x_in / PAGE_W

    def text(self, x_in: float, text: str, fontsize: float, **kwargs):
        self.fig.text(self.frac_x(x_in), self.frac_y(self.y), text,
                      fontsize=fontsize, va="top", **kwargs)

    def advance(self, inches: float):
        self.y -= inches

    def wrapped_block(self, x_in: float, text: str, fontsize: float, width_chars: int, **kwargs):
        lines = _wrap_lines(text, width_chars)
        self.ensure(len(lines) * _line_h_in(fontsize))
        self.text(x_in, "\n".join(lines), fontsize, **kwargs)
        self.advance(len(lines) * _line_h_in(fontsize))
        return len(lines)

    def add_axes(self, height_in: float, x_in: float = MARGIN_X, width_in: float = CONTENT_W):
        """Reserves `height_in` below the cursor (on a new page if needed)
        and returns an Axes spanning exactly that box."""
        self.ensure(height_in)
        top = self.y
        ax = self.fig.add_axes([
            self.frac_x(x_in), self.frac_y(top - height_in),
            width_in / PAGE_W, height_in / PAGE_H,
        ])
        self.advance(height_in)
        return ax


def build_pdf(
    out_path: str,
    title: str,
    date_range_label: str,
    chart_png_path: str,
    stats_rows: list[dict],
    findings: list[str],
    caveats: list[str],
    recommendation: str = "",
    generated_by: str = "wikipedia-trend-insights skill",
    horizon_rows: list[dict] | None = None,
) -> int:
    """`stats_rows`: [{"label", "confidence", "growth_pct", "n_points",
    "confidence_reason"}], one per compared language/article.

    Returns the number of pages written. Raises `ReportTooLongError`
    (writing nothing) if the content needs more than HARD_MAX_PAGES."""
    doc = _Doc()
    try:
        _layout(doc, title, date_range_label, chart_png_path, stats_rows,
                findings, caveats, recommendation, generated_by, horizon_rows or [])
        n_pages = len(doc.figs)
        if n_pages > HARD_MAX_PAGES:
            raise ReportTooLongError(n_pages)
        if n_pages > SOFT_MAX_PAGES:
            warnings.warn(
                f"Report is {n_pages} pages (recommended <= {SOFT_MAX_PAGES}, "
                f"max {HARD_MAX_PAGES}). Consider fewer languages per report "
                f"or a shorter --recommendation.",
                stacklevel=2,
            )
        with PdfPages(out_path) as pdf:
            for i, fig in enumerate(doc.figs, start=1):
                if n_pages > 1:
                    fig.text(1 - MARGIN_X / PAGE_W, 0.3 / PAGE_H, f"стор. {i} / {n_pages}",
                             fontsize=7, color="#AAAAAA", ha="right", va="bottom")
                pdf.savefig(fig)
        return n_pages
    finally:
        for fig in doc.figs:
            plt.close(fig)


def _layout(doc: _Doc, title, date_range_label, chart_png_path, stats_rows,
            findings, caveats, recommendation, generated_by, horizon_rows) -> None:
    # -- header ------------------------------------------------------------
    doc.wrapped_block(MARGIN_X, title, 16, width_chars=70, fontweight="bold")
    doc.advance(0.04)
    doc.wrapped_block(MARGIN_X, date_range_label, 10, width_chars=90, color="#555555")
    doc.wrapped_block(
        MARGIN_X, "* зростання = середнє перших 3 vs останніх 3 періодів; тренд без сезонності = зміна згладженого тренду STL за весь період (пунктир на графіку; потрібно >= 24 місяці)",
        6.5, width_chars=110, color="#888888",
    )
    doc.advance(0.12)

    # -- chart ---------------------------------------------------------------
    ax_chart = doc.add_axes(3.1)
    ax_chart.axis("off")
    ax_chart.imshow(plt.imread(chart_png_path), aspect="auto")
    doc.advance(0.18)

    # -- stats table (split across pages, header repeated) -----------------
    _draw_table(doc, stats_rows)
    doc.advance(0.18)

    # -- multi-horizon table (only when the user gave no dates) ------------
    if horizon_rows:
        doc.ensure(_line_h_in(11) + 0.05 + 0.34 + 0.5)
        doc.text(MARGIN_X, "Різні періоди", 11, fontweight="bold")
        doc.advance(_line_h_in(11) + 0.05)
        _draw_grid(
            doc, ["Мова/розділ", "3 міс.\n(тижні)", "1 рік", "3 роки", "Підсумок"],
            [[_wrap(r["label"], 22), r["cells"]["3m_weekly"], r["cells"]["1y"],
              r["cells"]["3y"], _wrap(r["summary"], 50)] for r in horizon_rows],
            [0.19, 0.11, 0.11, 0.11, 0.48],
        )
        doc.advance(0.12)
        doc.wrapped_block(
            MARGIN_X, "Зміна за 1 рік і 3 роки - тренд без сезонності (STL), якщо є >= 24 місяці; "
            "за 3 місяці - перші 3 тижні vs останні 3. Після % - довіра до тренду на цьому періоді.",
            6.5, width_chars=110, color="#888888")
        doc.advance(0.18)

    # -- findings (auto-generated, data-derived) --------------------------
    doc.ensure(_line_h_in(11) + 0.05 + 2 * _line_h_in(8.5))  # keep heading with first item
    doc.text(MARGIN_X, "Спостереження за даними", 11, fontweight="bold")
    doc.advance(_line_h_in(11) + 0.05)
    for item in (findings or ["(немає спостережень)"]):
        doc.wrapped_block(MARGIN_X, "• " + item, 8.5, width_chars=100)
    doc.advance(0.15)

    # -- recommendation (agent-authored, clearly separated) ---------------
    _draw_recommendation(doc, recommendation)
    doc.advance(0.35)

    # -- footer / caveats --------------------------------------------------
    caveat_text = " · ".join(caveats) if caveats else "Обмежень не виявлено."
    doc.wrapped_block(MARGIN_X, "Припущення й обмеження: " + caveat_text, 6.5,
                      width_chars=115, color="#888888")
    doc.advance(0.06)
    doc.wrapped_block(
        MARGIN_X,
        f"{generated_by} · сформовано {datetime.now():%Y-%m-%d %H:%M} · "
        f"джерело даних: Wikimedia Pageviews API",
        6.5, width_chars=130, color="#AAAAAA",
    )


def _draw_table(doc: _Doc, stats_rows: list[dict]) -> None:
    col_labels = ["Мова/розділ", "Зростання*", "Тренд без\nсезонн.*", "К-сть\nточок", "Довіра", "Причина"]
    cell_text = []
    for row in stats_rows:
        growth = "н/д" if row["growth_pct"] is None else f"{row['growth_pct']:+.1f}%"
        trend = row.get("trend_change_pct")
        cell_text.append([
            _wrap(row["label"], 22), growth, "н/д" if trend is None else f"{trend:+.1f}%",
            str(row["n_points"]),
            CONFIDENCE_UA.get(row["confidence"], row["confidence"]),
            _wrap(row.get("confidence_reason", ""), 48),
        ])
    if not cell_text:
        cell_text = [["(немає даних)", "", "", "", "", ""]]

    _draw_grid(doc, col_labels, cell_text, [0.19, 0.10, 0.09, 0.07, 0.09, 0.46])


def _draw_grid(doc: _Doc, col_labels: list[str], cell_text: list[list[str]],
               col_widths: list[float]) -> None:
    """Table split across pages (header repeated), each row as tall as its
    tallest cell (2 lines fit the 0.42in minimum), so a long cell can't
    spill into the next row."""
    header_h = 0.34
    heights = [max(0.42, 0.15 + max(c.count("\n") + 1 for c in row) * _line_h_in(7.5))
               for row in cell_text]
    remaining = list(zip(cell_text, heights))
    while remaining:
        doc.ensure(header_h + remaining[0][1])
        chunk, used = [], header_h
        while remaining and (not chunk or used + remaining[0][1] <= doc.room):
            used += remaining[0][1]
            chunk.append(remaining.pop(0))
        table_h = used
        ax = doc.add_axes(table_h)
        ax.axis("off")
        table = ax.table(
            cellText=[row for row, _ in chunk], colLabels=col_labels, loc="upper left",
            cellLoc="left", colWidths=col_widths,
        )
        table.auto_set_font_size(False)
        table.set_fontsize(7.5)
        # Heights as a fraction of the axes box keep physical sizes fixed
        # regardless of how many rows this chunk has.
        for (r, _c), cell in table.get_celld().items():
            cell.set_height((header_h if r == 0 else chunk[r - 1][1]) / table_h)
            cell.set_text_props(va="center")
            if r == 0:
                cell.set_text_props(fontweight="bold")


def _draw_recommendation(doc: _Doc, recommendation: str) -> None:
    """Draws the shaded recommendation box, continuing it onto following
    pages (with a "(продовження)" heading) if the text is long."""
    lines = _wrap_lines(recommendation.strip() or "(не надано)", 100)
    body_h = _line_h_in(8.5)
    head_h = _line_h_in(11) + 0.08
    pad = 0.18
    heading = "Висновки та рекомендації"
    while lines:
        doc.ensure(2 * pad + head_h + min(len(lines), 3) * body_h)
        fit = max(1, int((doc.room - 2 * pad - head_h) // body_h))
        chunk, lines = lines[:fit], lines[fit:]
        box_h = 2 * pad + head_h + len(chunk) * body_h
        box_top = doc.y
        doc.fig.add_artist(Rectangle(
            (doc.frac_x(MARGIN_X), doc.frac_y(box_top - box_h)),
            CONTENT_W / PAGE_W, box_h / PAGE_H,
            transform=doc.fig.transFigure, facecolor="#F5F7FA", edgecolor="#D0D7DE", zorder=0,
        ))
        doc.advance(pad)
        doc.text(MARGIN_X + 0.15, heading, 11, fontweight="bold")
        doc.advance(head_h)
        doc.text(MARGIN_X + 0.15, "\n".join(chunk), 8.5)
        doc.y = box_top - box_h
        heading = "Висновки та рекомендації (продовження)"


def _wrap_lines(text: str, width: int) -> list[str]:
    """Wraps each paragraph separately so explicit newlines survive."""
    out: list[str] = []
    for para in text.split("\n"):
        out.extend(textwrap.wrap(para, width=width) or [""])
    return out or [text]


def _wrap(text: str, width: int) -> str:
    return "\n".join(textwrap.wrap(text, width=width)) or text
