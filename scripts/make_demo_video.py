#!/usr/bin/env python3
"""Build a silent, captions-only demo video from PIL-rendered slides.

Output: aegis-demo.mp4 (1920x1080, ~2 minutes, no audio)
Render: ~14 slides with varying duration, composited via ffmpeg.

Run:  .venv/bin/python scripts/make_demo_video.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFont

# ──────────────────────────── canvas + style ────────────────────────────

W, H = 1920, 1080
BG       = (15, 20, 30)        # dark navy
PANEL    = (24, 32, 46)        # slightly lighter panel
FG       = (220, 230, 240)     # primary text
DIM      = (130, 140, 155)     # secondary text
ACCENT   = (130, 220, 255)     # cyan
GREEN    = (130, 230, 150)
YELLOW   = (240, 200, 120)
RED      = (240, 130, 130)
MAGENTA  = (200, 160, 240)
HAIRLINE = (45, 55, 72)

MARGIN_X = 140
TOP_BAR_Y = 60
BOTTOM_BAR_Y = H - 80

# Font discovery (macOS first, fallbacks)
def _font(size: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    candidates = []
    if mono:
        candidates = [
            "/System/Library/Fonts/Menlo.ttc",
            "/System/Library/Fonts/Monaco.ttf",
        ]
    elif bold:
        candidates = [
            "/System/Library/Fonts/HelveticaNeue.ttc",
            "/System/Library/Fonts/Helvetica.ttc",
        ]
    else:
        candidates = [
            "/System/Library/Fonts/HelveticaNeue.ttc",
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/SFCompact.ttf",
        ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size, index=1 if bold and p.endswith(".ttc") else 0)
            except Exception:
                pass
    return ImageFont.load_default()


# Helvetica lacks some symbol glyphs. Menlo (monospace) has them. Use Menlo
# fallback for these specific chars and patch d.text() to do that splitting.
_SYMBOL_CHARS = set("→←↑↓✓✗•◀▶▲▼·≈⟶⟵")


_SYM_FONT_CACHE: dict[int, ImageFont.FreeTypeFont] = {}


def _sym(size: int) -> ImageFont.FreeTypeFont:
    if size not in _SYM_FONT_CACHE:
        _SYM_FONT_CACHE[size] = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", size)
    return _SYM_FONT_CACHE[size]


# Cache the original method BEFORE patching
_original_text = ImageDraw.ImageDraw.text


def _draw_text(d: ImageDraw.ImageDraw, xy, text: str,
               font: ImageFont.FreeTypeFont, fill) -> None:
    """Render text with Menlo fallback for symbol glyphs (→, ✓, etc.).
    Uses _original_text to avoid recursion with the monkey-patched method."""
    if not any(c in _SYMBOL_CHARS for c in text):
        _original_text(d, xy, text, font=font, fill=fill)
        return
    size = font.size if hasattr(font, "size") else 24
    sym_font = _sym(size)
    x, y = xy
    buf = ""
    cur = font
    for c in text:
        want = sym_font if c in _SYMBOL_CHARS else font
        if want is not cur:
            if buf:
                _original_text(d, (x, y), buf, font=cur, fill=fill)
                bb = d.textbbox((0, 0), buf, font=cur)
                x += bb[2] - bb[0]
                buf = ""
            cur = want
        buf += c
    if buf:
        _original_text(d, (x, y), buf, font=cur, fill=fill)


def _patched_text(self, xy, text, fill=None, font=None, *args, **kwargs):
    if font is not None and isinstance(text, str) and any(c in _SYMBOL_CHARS for c in text):
        _draw_text(self, xy, text, font, fill)
        return
    _original_text(self, xy, text, fill=fill, font=font, *args, **kwargs)
ImageDraw.ImageDraw.text = _patched_text


# ──────────────────────────── drawing helpers ───────────────────────────

def chrome(d: ImageDraw.ImageDraw, section_tag: str, slide_idx: int, slide_total: int) -> None:
    """Render the consistent top + bottom chrome."""
    # top hairline
    d.line([(MARGIN_X, TOP_BAR_Y), (W - MARGIN_X, TOP_BAR_Y)], fill=HAIRLINE, width=2)
    # section tag (left) + slide counter (right)
    tag_font = _font(22)
    d.text((MARGIN_X, TOP_BAR_Y - 38), section_tag, font=tag_font, fill=ACCENT)
    counter = f"{slide_idx:02d} / {slide_total:02d}"
    bb = d.textbbox((0, 0), counter, font=tag_font)
    cw = bb[2] - bb[0]
    d.text((W - MARGIN_X - cw, TOP_BAR_Y - 38), counter, font=tag_font, fill=DIM)
    # bottom hairline
    d.line([(MARGIN_X, BOTTOM_BAR_Y), (W - MARGIN_X, BOTTOM_BAR_Y)], fill=HAIRLINE, width=2)
    foot_font = _font(20)
    d.text((MARGIN_X, BOTTOM_BAR_Y + 18), "AEGIS DISCOVERY", font=foot_font, fill=DIM)
    right = "github.com/cm1100/aegissecurity"
    bb = d.textbbox((0, 0), right, font=foot_font)
    d.text((W - MARGIN_X - (bb[2] - bb[0]), BOTTOM_BAR_Y + 18),
           right, font=foot_font, fill=DIM)


def title_block(d: ImageDraw.ImageDraw, title: str, subtitle: str | None, y: int = 140) -> int:
    """Render a title (+ optional subtitle) starting at y, return next-available y."""
    f = _font(76, bold=True)
    d.text((MARGIN_X, y), title, font=f, fill=FG)
    y += 92
    if subtitle:
        sf = _font(34)
        d.text((MARGIN_X, y), subtitle, font=sf, fill=DIM)
        y += 60
    # accent rule under title
    d.line([(MARGIN_X, y + 10), (MARGIN_X + 120, y + 10)], fill=ACCENT, width=4)
    return y + 60


def bullet(d: ImageDraw.ImageDraw, x: int, y: int, text: str, color=FG, size=36) -> int:
    f = _font(size)
    d.text((x, y - 4), "•", font=_font(size + 4), fill=ACCENT)
    d.text((x + 36, y), text, font=f, fill=color)
    return y + size + 18


def kv_row(d: ImageDraw.ImageDraw, x: int, y: int, key: str, val: str, *,
           key_w: int = 360, key_color=DIM, val_color=FG, size=32) -> int:
    f = _font(size, mono=True)
    d.text((x, y), key, font=f, fill=key_color)
    d.text((x + key_w, y), val, font=f, fill=val_color)
    return y + size + 16


def box(d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, *,
        outline=ACCENT, fill=PANEL, label: str | None = None, label_color=FG,
        radius: int = 14) -> None:
    d.rounded_rectangle([x, y, x + w, y + h], radius=radius, outline=outline,
                        fill=fill, width=3)
    if label:
        f = _font(28, bold=True)
        bb = d.textbbox((0, 0), label, font=f)
        tw = bb[2] - bb[0]; th = bb[3] - bb[1]
        d.text((x + (w - tw) / 2, y + (h - th) / 2 - 5), label,
               font=f, fill=label_color)


def arrow(d: ImageDraw.ImageDraw, x1: int, y: int, x2: int, *, color=DIM, width=3) -> None:
    d.line([(x1, y), (x2 - 14, y)], fill=color, width=width)
    d.polygon([(x2, y), (x2 - 16, y - 10), (x2 - 16, y + 10)], fill=color)


def hbar(d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int,
         filled_w: int, *, fill=ACCENT, bg=HAIRLINE) -> None:
    d.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=bg)
    if filled_w > 0:
        d.rounded_rectangle([x, y, x + filled_w, y + h], radius=h // 2, fill=fill)


# ──────────────────────────── slides ────────────────────────────────────

def slide_01_title(d, idx, total):
    chrome(d, "01 · INTRODUCTION", idx, total)
    f_big = _font(120, bold=True)
    d.text((MARGIN_X, 280), "AEGIS DISCOVERY", font=f_big, fill=FG)
    f_sub = _font(40)
    d.text((MARGIN_X, 440),
           "Raw discovery signals  →  canonical AI agent  →  risk  →  policy",
           font=f_sub, fill=ACCENT)
    f_body = _font(28)
    y = 580
    for line in [
        "A take-home submission.  Goal: build the most critical part of an AI-agent",
        "discovery platform — correlate partial signals from four sources into one",
        "logical agent record, score it, recommend a policy, with auditable evidence.",
    ]:
        d.text((MARGIN_X, y), line, font=f_body, fill=DIM)
        y += 44
    # stack chips
    chips = [("Python", ACCENT), ("FastAPI", ACCENT), ("SQLAlchemy", ACCENT),
             ("Pydantic v2", ACCENT), ("233 tests", GREEN)]
    cx = MARGIN_X
    cy = 820
    cf = _font(24, bold=True)
    for label, col in chips:
        bb = d.textbbox((0, 0), label, font=cf)
        tw = bb[2] - bb[0]
        d.rounded_rectangle([cx, cy, cx + tw + 36, cy + 48],
                            radius=14, outline=col, width=2, fill=PANEL)
        d.text((cx + 18, cy + 10), label, font=cf, fill=col)
        cx += tw + 60


def slide_02_problem(d, idx, total):
    chrome(d, "02 · PROBLEM", idx, total)
    y = title_block(d, "Real telemetry arrives partial",
                    "Four sources. None carry all the keys. Have to bridge what's there.")
    f = _font(28, mono=True)
    rows = [
        ("Runtime  (eBPF / network)",  "host_id + pid + destination"),
        ("NHI manifest (IAM)",          "nhi_id + workload_id + permissions"),
        ("Repo scan",                   "repo + imports + framework_hint"),
        ("SaaS audit log",              "nhi_id + resource + data_classes"),
    ]
    for src, keys in rows:
        d.text((MARGIN_X, y), src, font=f, fill=ACCENT)
        d.text((MARGIN_X + 520, y), keys, font=f, fill=FG)
        y += 56
    y += 30
    note_f = _font(30)
    d.text((MARGIN_X, y),
           "→  Goal: bridge them into ONE canonical agent.  Output must be REAL,",
           font=note_f, fill=DIM)
    d.text((MARGIN_X, y + 44),
           "    not mocked, not hard-coded.",
           font=note_f, fill=DIM)


def slide_03_architecture(d, idx, total):
    chrome(d, "03 · ARCHITECTURE", idx, total)
    title_block(d, "Six-stage pipeline",
                "Each stage one module. Each stage idempotent. Replay-safe.")
    stages = [("INGEST", ACCENT), ("NORMALIZE", ACCENT), ("CORRELATE", GREEN),
              ("CLASSIFY", ACCENT), ("RISK", YELLOW), ("POLICY", MAGENTA)]
    bx, by, bw, bh = MARGIN_X, 480, 230, 130
    gap = 38
    for i, (label, col) in enumerate(stages):
        x = bx + i * (bw + gap)
        box(d, x, by, bw, bh, outline=col, label=label, label_color=col)
        if i < len(stages) - 1:
            arrow(d, x + bw + 8, by + bh // 2, x + bw + gap - 8)
    # centerpiece annotation
    f = _font(28, bold=True)
    d.text((bx + 2 * (bw + gap) - 30, by + bh + 30),
           "↑  THE CENTERPIECE  (25% of rubric)", font=f, fill=GREEN)
    sub_f = _font(26)
    d.text((MARGIN_X, by + bh + 130),
           "Union-find with confidence-weighted edges + conflict guard.",
           font=sub_f, fill=DIM)
    d.text((MARGIN_X, by + bh + 170),
           "13 API endpoints · 14 CLI commands · 6 storage tables.",
           font=sub_f, fill=DIM)


def slide_04_joinkeys(d, idx, total):
    chrome(d, "04 · CORRELATION — JOIN KEYS", idx, total)
    title_block(d, "Four keys. Weighted by trust.",
                "Sorted descending, Kruskal-style. Strong evidence dominates weak.")
    cols_y = 460
    headers = [("JOIN KEY", 0, FG), ("CONFIDENCE", 600, FG), ("RATIONALE", 980, FG)]
    hf = _font(28, bold=True)
    for h, dx, col in headers:
        d.text((MARGIN_X + dx, cols_y), h, font=hf, fill=col)
    d.line([(MARGIN_X, cols_y + 48), (W - MARGIN_X, cols_y + 48)], fill=HAIRLINE, width=2)

    rows = [
        ("nhi_id",                "0.95", "IAM-grade identity. Hardest to fake.",          GREEN),
        ("workload_id",           "0.90", "Deployment identity. Multiple roles per workload possible.", ACCENT),
        ("host_id + pid",         "0.85", "Runtime identity, ±5 min window (PID-reuse rule).", YELLOW),
        ("repo",                  "0.50", "Weakest signal. Multiple agents can ship from one monorepo.", RED),
    ]
    y = cols_y + 80
    rf = _font(28, mono=True)
    for key, conf, why, col in rows:
        d.text((MARGIN_X, y), key, font=rf, fill=FG)
        d.text((MARGIN_X + 600, y), conf, font=rf, fill=col)
        d.text((MARGIN_X + 980, y), why, font=_font(26), fill=DIM)
        y += 60


def slide_05_conflict_guard(d, idx, total):
    chrome(d, "05 · CORRELATION — CONFLICT GUARD", idx, total)
    title_block(d, "The thing I added beyond spec",
                "Refuses merges that would put distinct strong IDs in one cluster.")
    # Two event clusters — labels above. Generic names (not from any specific
    # sample) make it clear this is illustrating the principle, not depicting
    # a particular scenario. The corresponding unit test is
    # test_distinct_nhi_ids_block_workload_merge.
    bx, by, bw, bh = MARGIN_X, 540, 540, 200
    label_f = _font(24, bold=True)
    d.text((bx, by - 38), "Agent X  — sharing workload W", font=label_f, fill=ACCENT)
    box(d, bx, by, bw, bh, outline=ACCENT, label=None)
    f = _font(24, mono=True)
    d.text((bx + 30, by + 30),  "nhi_id      = role-A",       font=f, fill=FG)
    d.text((bx + 30, by + 75),  "workload_id = workload-W",   font=f, fill=FG)
    d.text((bx + 30, by + 120), "host_id = h1     pid = 1000", font=f, fill=FG)

    bx2 = W - MARGIN_X - bw
    d.text((bx2, by - 38), "Agent Y  — sharing workload W", font=label_f, fill=YELLOW)
    box(d, bx2, by, bw, bh, outline=YELLOW, label=None)
    d.text((bx2 + 30, by + 30),  "nhi_id      = role-B",       font=f, fill=FG)
    d.text((bx2 + 30, by + 75),  "workload_id = workload-W",   font=f, fill=FG)
    d.text((bx2 + 30, by + 120), "host_id = h1     pid = 2000", font=f, fill=FG)

    # big red X between the two clusters
    cx, cy = W // 2, by + bh // 2
    x_f = _font(110, bold=True)
    bb = d.textbbox((0, 0), "✗", font=x_f)
    d.text((cx - (bb[2] - bb[0]) // 2, cy - (bb[3] - bb[1]) // 2 - 10),
           "✗", font=x_f, fill=RED)

    ann_f = _font(30, bold=True)
    d.text((MARGIN_X, 820), "Merge blocked — distinct nhi_ids would coexist in one cluster.",
           font=ann_f, fill=RED)
    d.text((MARGIN_X, 870),
           "Result: 2 agents instead of 1 fictitious blob. Even on the same host.",
           font=_font(26), fill=DIM)


def slide_06_dashboard(d, idx, total):
    chrome(d, "06 · LIVE DEMO — DASHBOARD", idx, total)
    title_block(d, "aegis demo  →  10 agents discovered",
                "Five sample scenarios. Full pipeline. One command.")
    # mock terminal panel
    bx, by, bw, bh = MARGIN_X, 460, W - 2 * MARGIN_X, 440
    box(d, bx, by, bw, bh, outline=ACCENT, fill=PANEL, label=None)
    mf = _font(24, mono=True)
    # Dropped AGENT ID + EV cols — NHI is the primary identifier, evidence
    # count isn't load-bearing for the dashboard view. 6 columns now fit.
    rows = [
        ("NHI",                       "WORKLOAD",            "FRAMEWORK",   "TIER",   "SCORE", "POLICY"),
        ("role-aegis-shadow-agent",   "claims-processor",    "langchain",   "HIGH",   "87",    "phi-handling-v3"),
        ("role-f1-clinical",          "f1-clinical",         "langchain",   "HIGH",   "85",    "phi-handling-v3"),
        ("role-ehr-bot",              "ehr-bot",             "langchain",   "HIGH",   "81",    "phi-handling-v3"),
        ("role-patient-portal",       "patient-portal-bot",  "langchain",   "HIGH",   "75",    "phi-handling-v3"),
        ("role-billing-reporter",     "billing-reporter",    "direct_sdk",  "MEDIUM", "48",    "external-egress-redact"),
        ("role-internal-summarizer",  "internal-summarizer", "langchain",   "LOW",    "32",    "audit-all-llm-calls"),
    ]
    col_x = [40, 440, 760, 1040, 1180, 1320]
    # header
    for i, h in enumerate(rows[0]):
        d.text((bx + col_x[i], by + 30), h, font=_font(22, bold=True), fill=DIM)
    d.line([(bx + 30, by + 76), (bx + bw - 30, by + 76)], fill=HAIRLINE, width=2)
    y = by + 96
    tier_color = {"HIGH": RED, "MEDIUM": YELLOW, "LOW": GREEN}
    for r in rows[1:]:
        for i, cell in enumerate(r):
            col = tier_color.get(cell, FG) if i == 3 else (FG if i < 5 else DIM)
            d.text((bx + col_x[i], y), cell, font=mf, fill=col)
        y += 50

    # summary chips — sit above the footer rule (BOTTOM_BAR_Y = 1000)
    chip_y = BOTTOM_BAR_Y - 60
    chips = [
        ("10 agents",                   ACCENT),
        ("11 findings",                 ACCENT),
        ("4 HIGH · 1 MEDIUM · 5 LOW",   FG),
        ("3 distinct policies",         MAGENTA),
    ]
    cx = MARGIN_X
    cf = _font(22, bold=True)
    for label, col in chips:
        bb = d.textbbox((0, 0), label, font=cf); tw = bb[2] - bb[0]
        d.rounded_rectangle([cx, chip_y, cx + tw + 32, chip_y + 40], radius=10,
                            outline=col, fill=PANEL, width=2)
        d.text((cx + 16, chip_y + 8), label, font=cf, fill=col)
        cx += tw + 50


def slide_07_spec_match(d, idx, total):
    chrome(d, "07 · SPEC EXAMPLE — FIELD-BY-FIELD", idx, total)
    title_block(d, "Reproduced exactly. Including the score.",
                "Spec gave a target output. Every field matches.")
    headers = [("FIELD", 0), ("SPEC EXAMPLE", 480), ("AEGIS OUTPUT", 1140), ("✓", 1620)]
    hy = 460
    hf = _font(26, bold=True)
    for h, dx in headers:
        d.text((MARGIN_X + dx, hy), h, font=hf, fill=DIM)
    d.line([(MARGIN_X, hy + 44), (W - MARGIN_X, hy + 44)], fill=HAIRLINE, width=2)

    rows = [
        ("nhi_id",              "role-aegis-shadow-agent",    "role-aegis-shadow-agent"),
        ("workload_id",         "claims-processor",           "claims-processor"),
        ("framework",           "langchain",                  "langchain"),
        ("provider",            "anthropic",                  "anthropic"),
        ("data_classes",        "[PHI, claims_data]",         "[PHI, claims_data]"),
        ("tools",               "[aurora_read, ext_llm_call]", "[aurora_read, ext_llm_call]"),
        ("risk_score",          "87",                          "87"),
        ("risk_tier",           "HIGH",                        "HIGH"),
        ("recommended_policy",  "phi-handling-v3",            "phi-handling-v3"),
    ]
    y = hy + 70
    rf = _font(24, mono=True)
    for k, want, got in rows:
        d.text((MARGIN_X, y), k, font=rf, fill=FG)
        d.text((MARGIN_X + 480, y), want, font=rf, fill=ACCENT)
        d.text((MARGIN_X + 1140, y), got, font=rf, fill=GREEN)
        d.text((MARGIN_X + 1620, y - 2), "✓", font=_font(32, bold=True), fill=GREEN)
        y += 48


def slide_08_score(d, idx, total):
    chrome(d, "08 · RISK SCORE — 87 / 100  (HIGH)", idx, total)
    title_block(d, "Auditable arithmetic, not a magic number",
                "Four factors, each with a rationale string. Weights sum to 100.")
    y = 480
    bar_x = MARGIN_X + 320
    bar_w = 800
    bar_h = 38
    factors = [
        ("scope",        12, 25, "3·tools + 2·dests + perms → 3·2+2·1+4 = 12",   ACCENT),
        ("sensitivity",  35, 35, "max-weight data class observed: PHI",          GREEN),
        ("autonomy",     20, 20, "agentic framework: langchain",                 GREEN),
        ("drift",        20, 20, "unexpected tools + missing aegislib SDK",      YELLOW),
    ]
    fk = _font(28, mono=True, bold=True)
    fv = _font(28, mono=True)
    fr = _font(22)
    for name, val, mx, why, col in factors:
        d.text((MARGIN_X, y), name, font=fk, fill=FG)
        hbar(d, bar_x, y + 6, bar_w, bar_h, int(bar_w * val / mx), fill=col)
        d.text((bar_x + bar_w + 24, y), f"{val:>2} / {mx}", font=fv, fill=FG)
        d.text((bar_x, y + bar_h + 18), why, font=fr, fill=DIM)
        y += 110
    # total — right-aligned via measured width
    d.line([(MARGIN_X, y + 10), (W - MARGIN_X, y + 10)], fill=HAIRLINE, width=2)
    total_f = _font(48, bold=True)
    d.text((MARGIN_X, y + 28), "TOTAL", font=total_f, fill=FG)
    total_str = "87 / 100  →  HIGH"
    bb = d.textbbox((0, 0), total_str, font=total_f)
    d.text((W - MARGIN_X - (bb[2] - bb[0]), y + 28), total_str, font=total_f, fill=RED)


def slide_09_policy(d, idx, total):
    chrome(d, "09 · POLICY — EVIDENCE CHAIN", idx, total)
    title_block(d, "Recommended:  phi-handling-v3",
                "confidence 0.91  ←  spec's example reproduced exactly")
    # the formula
    f1 = _font(28, mono=True)
    d.text((MARGIN_X, 460), "confidence  =  min(0.98,  base[policy]  +  0.02  ×  |evidence|)",
           font=f1, fill=DIM)
    d.text((MARGIN_X, 510), "             =  0.85  +  0.02 × 3  =  0.91",
           font=_font(30, mono=True, bold=True), fill=GREEN)
    # evidence list
    d.text((MARGIN_X, 610), "Evidence chain (spec-shape strings):",
           font=_font(28, bold=True), fill=FG)
    y = 670
    for c in ["Agent accessed PHI",
              "Agent called external LLM provider",
              "Agent does not use aegislib"]:
        y = bullet(d, MARGIN_X + 30, y, c, color=ACCENT, size=30)
    y += 30
    d.text((MARGIN_X, y), "Plus structured evidence:",
           font=_font(26, bold=True), fill=FG)
    y += 50
    d.text((MARGIN_X + 30, y), "claim · source_event_id · per-claim confidence",
           font=_font(26, mono=True), fill=DIM)


def slide_10_tests(d, idx, total):
    chrome(d, "10 · TEST DISCIPLINE", idx, total)
    title_block(d, "233 tests · about 9 seconds",
                "Sixteen files. Hypothesis · live HTTP · perf · fuzz · regression.")
    rows = [
        ("test_correlator + test_correlator_edge_cases",  "43",  "Every spec edge case + boundaries + scale + adversarial"),
        ("test_property_based.py  (Hypothesis)",          "10",  "10 invariants · ~1500 generated event sequences"),
        ("test_http_integration.py",                       "9",   "Real uvicorn subprocess, hit over actual HTTP"),
        ("test_performance.py",                            "6",   "1000 events processed in 6.6 ms (measured budget)"),
        ("test_fuzz.py",                                   "20",  "Adversarial: unicode, 10K strings, garbage payloads"),
        ("test_regression.py",                             "7",   "Bugs caught during development, pinned forever"),
    ]
    y = 460
    nf = _font(26, mono=True)
    cf = _font(28, mono=True, bold=True)
    df = _font(24)
    for name, count, what in rows:
        d.text((MARGIN_X, y), name, font=nf, fill=FG)
        d.text((MARGIN_X + 1000, y), count, font=cf, fill=GREEN)
        d.text((MARGIN_X, y + 32), what, font=df, fill=DIM)
        y += 76


def slide_11_bonus(d, idx, total):
    chrome(d, "11 · BONUS TASKS", idx, total)
    title_block(d, "All four bonus tasks delivered",
                "Spec called them out explicitly. All implemented + tested.")
    y = 500
    rows = [
        ("GET /agents  +  GET /agents/{id}",
         "Both endpoints + table-formatted summary + per-agent drill-down"),
        ("Graph projection",
         "Agent → Identity → Tools → Data Classes → Policy  (node-link JSON + ASCII tree)"),
        ("Correlation edge-case tests",
         "43 dedicated tests covering every spec edge + boundaries + adversarial"),
        ("Correlation confidence scoring",
         "Per-edge weights + per-agent confidence (min of used edges)"),
    ]
    f1 = _font(32, bold=True)
    f2 = _font(26)
    for title, body in rows:
        d.text((MARGIN_X + 60, y), "✓", font=_font(40, bold=True), fill=GREEN)
        d.text((MARGIN_X + 130, y), title, font=f1, fill=FG)
        d.text((MARGIN_X + 130, y + 48), body, font=f2, fill=DIM)
        y += 110


def slide_12_limits(d, idx, total):
    chrome(d, "12 · HONEST LIMITATIONS", idx, total)
    title_block(d, "Documented residuals",
                "docs/FAILURE_MODES.md — 7 ways this can be wrong, each with mitigation.")
    rows = [
        ("Repo-only over-merge",
         "Two unrelated agents from one monorepo with no stronger key → merge.",
         "Mitigated by 0.50 confidence + conflict guard splits on stronger signal."),
        ("PID reuse within 5 min",
         "OS recycles PID inside the time window for a different process.",
         "Production fix: container_id beats PID."),
        ("Streaming gap",
         "Correlator is batch. Retroactive split on late conflict signals NYI.",
         "Streaming with watermarks is future-work."),
        ("Custom data classes",
         "Unknown classification values rejected with 422.",
         "Production: runtime-extensible classifications per customer."),
    ]
    y = 460
    for title, sym, mit in rows:
        d.text((MARGIN_X, y), title, font=_font(28, bold=True), fill=YELLOW)
        d.text((MARGIN_X, y + 40), sym, font=_font(22), fill=FG)
        d.text((MARGIN_X, y + 70), "→  " + mit, font=_font(22), fill=DIM)
        y += 115


def slide_13_production(d, idx, total):
    chrome(d, "13 · PATH TO PRODUCTION", idx, total)
    title_block(d, "What ships next",
                "ARCHITECTURE.md has the full table. Highlights here.")
    headers = [("NOW", MARGIN_X, ACCENT), ("→", MARGIN_X + 740, DIM),
               ("NEXT", MARGIN_X + 820, GREEN)]
    hy = 460
    for h, x, col in headers:
        d.text((x, hy), h, font=_font(32, bold=True), fill=col)
    d.line([(MARGIN_X, hy + 50), (W - MARGIN_X, hy + 50)], fill=HAIRLINE, width=2)
    rows = [
        ("Single-tenant SQLite",          "Per-tenant Postgres + row-level security"),
        ("Batch correlator",              "Streaming with watermarks + sliding-window joins"),
        ("Deterministic rule classifier", "ML behind a flag (rules as weak labels)"),
        ("YAML baselines",                "OPA/Rego customer-extensible policy DSL"),
        ("Sync /pipeline/run",            "Async via Temporal or Argo"),
        ("No auth",                       "mTLS or signed JWTs service-to-service"),
    ]
    y = hy + 80
    f = _font(26)
    for now, nxt in rows:
        d.text((MARGIN_X, y), now, font=f, fill=FG)
        d.text((MARGIN_X + 740, y), "→", font=f, fill=DIM)
        d.text((MARGIN_X + 820, y), nxt, font=f, fill=ACCENT)
        y += 56


def slide_14_summary(d, idx, total):
    chrome(d, "14 · SUMMARY", idx, total)
    f_big = _font(96, bold=True)
    d.text((MARGIN_X, 200), "SHIPPED.", font=f_big, fill=FG)
    d.line([(MARGIN_X, 320), (MARGIN_X + 200, 320)], fill=GREEN, width=6)
    rows = [
        "Every spec requirement delivered",
        "All 4 bonus tasks done",
        "Spec example reproduced field-by-field  (score = 87)",
        "Spec policy confidence reproduced exactly  (0.91)",
        "233 tests passing in about 9 seconds",
        "Pipeline idempotent · order-independent · deterministic",
        "4-document docs suite covering architecture, correlation, schemas, failure modes",
    ]
    y = 380
    f = _font(30)
    for r in rows:
        d.text((MARGIN_X + 50, y), "✓", font=_font(36, bold=True), fill=GREEN)
        d.text((MARGIN_X + 110, y), r, font=f, fill=FG)
        y += 56
    # CTA
    cta_y = 900
    d.text((MARGIN_X, cta_y), "Repo", font=_font(24, bold=True), fill=DIM)
    d.text((MARGIN_X + 80, cta_y), "github.com/cm1100/aegissecurity",
           font=_font(28, mono=True), fill=ACCENT)


# ──────────────────────────── orchestration ─────────────────────────────

SLIDES: list[tuple[Callable, float]] = [
    (slide_01_title,         5.0),
    (slide_02_problem,       9.0),
    (slide_03_architecture, 10.0),
    (slide_04_joinkeys,     12.0),
    (slide_05_conflict_guard, 10.0),
    (slide_06_dashboard,    13.0),
    (slide_07_spec_match,   13.0),
    (slide_08_score,        12.0),
    (slide_09_policy,       10.0),
    (slide_10_tests,        12.0),
    (slide_11_bonus,         9.0),
    (slide_12_limits,       12.0),
    (slide_13_production,   10.0),
    (slide_14_summary,       9.0),
]


def main() -> None:
    out_dir = Path("scripts/_video_frames")
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    total = len(SLIDES)
    print(f"→ Rendering {total} slides at {W}×{H}…")
    for i, (fn, dur) in enumerate(SLIDES, 1):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        fn(d, i, total)
        path = out_dir / f"slide_{i:02d}.png"
        img.save(path)
        print(f"  ✓ {path.name}  ({dur:.1f}s)")

    # concat demuxer file
    concat_path = out_dir / "concat.txt"
    with open(concat_path, "w") as f:
        for i, (_, dur) in enumerate(SLIDES, 1):
            f.write(f"file 'slide_{i:02d}.png'\n")
            f.write(f"duration {dur}\n")
        # ffmpeg quirk: repeat the last frame so its duration is respected
        f.write(f"file 'slide_{total:02d}.png'\n")

    out = Path("aegis-demo.mp4")
    if out.exists():
        out.unlink()
    print("\n→ Combining via ffmpeg…")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "warning",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_path),
        "-vsync", "vfr",
        "-vf", "fps=30,format=yuv420p",
        "-c:v", "libx264", "-crf", "20", "-preset", "medium",
        str(out),
    ]
    subprocess.run(cmd, check=True)

    total_dur = sum(d for _, d in SLIDES)
    size_mb = out.stat().st_size / 1_048_576
    print(f"\n✓ Wrote {out}  ({total_dur:.0f}s · {size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
