#!/usr/bin/env python3
"""Render captured terminal output as figures for the report.

    ./robofetch_venv/bin/python tools/report/make_terminal_figures.py

The text rendered here is real output, captured from actual runs and stored
under tools/report/data/. Rendering it rather than photographing a terminal
keeps the figures sharp at print resolution and readable in a PDF.
"""
import pathlib
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

HERE = pathlib.Path(__file__).resolve().parent
FIG = HERE.parent.parent / "report" / "Figures"
DATA = HERE / "data"

BG, FG = "#1B222B", "#D6DEE7"
ACCENT = {"PASS": "#4CD07D", "FAIL": "#F5766B", "VERIFIED": "#4CD07D",
          "ACCEPTED": "#4CD07D", "REFUSED": "#F5766B", "completed": "#4CD07D",
          "failed": "#F5766B", "NOT": "#F5766B"}


def colour_for(line):
    for key, colour in ACCENT.items():
        if key in line:
            return colour
    if line.strip().startswith("[order]") or line.strip().startswith("  "):
        return FG
    return FG


def render(text, name, title, width=9.4, fontsize=7.6):
    lines = text.rstrip().splitlines()
    height = 0.34 + len(lines) * 0.135
    fig = plt.figure(figsize=(width, height), dpi=170)
    fig.patch.set_facecolor(BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor(BG)
    ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    ax.axis("off")

    # A small title bar, so the figure reads as a terminal window.
    ax.add_patch(Rectangle((0, 0.965), 1, 0.035, facecolor="#2B3542", lw=0))
    for index, colour in enumerate(("#F5766B", "#E8C25B", "#4CD07D")):
        ax.add_patch(plt.Circle((0.012 + index * 0.014, 0.9825), 0.0045,
                                facecolor=colour, lw=0, transform=ax.transAxes))
    ax.text(0.5, 0.9825, title, color="#8FA0B4", fontsize=6.8,
            ha="center", va="center", family="monospace")

    top = 0.945
    step = (top - 0.015) / max(1, len(lines))
    for index, line in enumerate(lines):
        ax.text(0.012, top - index * step, line.rstrip(), color=colour_for(line),
                fontsize=fontsize, family="monospace", va="top", ha="left")

    path = FIG / name
    fig.savefig(path, facecolor=BG)
    plt.close(fig)
    print(f"  {name:34} {path.stat().st_size / 1024:6.1f} kB  ({len(lines)} lines)")


def main():
    FIG.mkdir(parents=True, exist_ok=True)
    print("rendering terminal figures")
    for source, name, title in [
        ("order_run.txt", "shot-order-script.png", "scripts/order.sh SKU-3001 delivery_1"),
        ("acceptance_run.txt", "shot-acceptance.png", "scripts/acceptance.py"),
    ]:
        path = DATA / source
        if not path.exists():
            print(f"  skipped {name}: {path} not found")
            continue
        render(path.read_text(), name, title)
    return 0


if __name__ == "__main__":
    sys.exit(main())
