#!/usr/bin/env python3
"""Generate every chart and drawn diagram used in the report.

    ./robofetch_venv/bin/python tools/report/make_figures.py

All output goes to report/Figures/ as PNG. Nothing here invents numbers: the
inputs are the real telemetry log, the real training set, the recorded
acceptance results and the measured delivery distances, all under
tools/report/data/ or in the workspace.

The UML diagrams are produced separately by make_uml.py from the PlantUML
sources in report/uml/.
"""
import csv
import json

import make_results_table
import pathlib

import matplotlib
matplotlib.use("Agg")                     # headless: no display on this machine
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Rectangle, FancyArrowPatch

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
FIG = ROOT / "report" / "Figures"
DATA = HERE / "data"
FIG.mkdir(parents=True, exist_ok=True)

# ------------------------------------------------------------------ house style
BLUE, GREEN, RED, AMBER, PURPLE, GREY = (
    "#2F6FB5", "#2E8B57", "#C0392B", "#D2891E", "#7D5BA6", "#7F8C8D")
plt.rcParams.update({
    "figure.dpi": 160,
    "savefig.dpi": 160,
    "savefig.bbox": "tight",
    "font.size": 9,
    "axes.titlesize": 10.5,
    "axes.titleweight": "bold",
    "axes.labelsize": 9,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.alpha": 0.25,
    "grid.linewidth": 0.6,
    "legend.frameon": False,
})

STATE_COLOURS = {"charging": GREEN, "working": BLUE, "idle": GREY,
                 "cooldown": RED, "returning": AMBER}

# Physics constants, mirrored from robofetch_core/robot_model.py so the figures
# annotate the same thresholds the running system enforces.
T_WARN, T_MAX, RESERVE_PERCENT = 55.0, 70.0, 15.0


def save(fig, name):
    path = FIG / name
    fig.savefig(path)
    plt.close(fig)
    print(f"  {name:38} {path.stat().st_size / 1024:6.1f} kB")


def telemetry():
    with open(DATA / "session_telemetry.csv") as handle:
        rows = [r for r in csv.DictReader(handle) if r["ts"] != "ts"]
    for r in rows:
        for key in ("battery_percent", "temperature_c", "condition_percent",
                    "payload_kg", "speed", "cumulative_distance_m",
                    "cumulative_energy_wh", "ts"):
            r[key] = float(r[key])
    start = rows[0]["ts"]
    for r in rows:
        r["t"] = r["ts"] - start
    return rows


# ============================================================ session telemetry
def fig_battery_session(rows):
    fig, ax = plt.subplots(figsize=(7.2, 3.1))
    t = [r["t"] / 60 for r in rows]
    ax.plot(t, [r["battery_percent"] for r in rows], color=BLUE, lw=1.8,
            label="battery")
    ax.axhline(RESERVE_PERCENT, color=RED, ls="--", lw=1.1,
               label=f"{RESERVE_PERCENT:.0f}% return reserve")

    # Shade the stretches where the robot was actually carrying a load.
    carrying = False
    for index, r in enumerate(rows):
        if r["payload_kg"] > 0 and not carrying:
            span_start, carrying = r["t"] / 60, True
        elif r["payload_kg"] == 0 and carrying:
            ax.axvspan(span_start, r["t"] / 60, color=AMBER, alpha=0.16, lw=0)
            carrying = False
    ax.plot([], [], color=AMBER, alpha=0.4, lw=8, label="carrying a parcel")

    ax.set_xlabel("elapsed time (minutes)")
    ax.set_ylabel("charge (%)")
    ax.set_ylim(0, 105)
    ax.legend(loc="lower left", ncol=3, fontsize=8)
    save(fig, "fig-battery-session.png")


def fig_temperature_session(rows):
    fig, ax = plt.subplots(figsize=(7.2, 3.1))
    t = [r["t"] / 60 for r in rows]
    ax.plot(t, [r["temperature_c"] for r in rows], color=RED, lw=1.8,
            label="motor temperature")
    ax.axhline(T_MAX, color=RED, ls="--", lw=1.1, label=f"{T_MAX:.0f} C limit")
    ax.axhline(T_WARN, color=AMBER, ls=":", lw=1.1,
               label=f"{T_WARN:.0f} C wear threshold")

    cooling = False
    for r in rows:
        if r["state"] == "cooldown" and not cooling:
            span_start, cooling = r["t"] / 60, True
        elif r["state"] != "cooldown" and cooling:
            ax.axvspan(span_start, r["t"] / 60, color=RED, alpha=0.10, lw=0)
            cooling = False
    if cooling:
        ax.axvspan(span_start, t[-1], color=RED, alpha=0.10, lw=0)
    ax.plot([], [], color=RED, alpha=0.25, lw=8, label="cooldown state")

    ax.set_xlabel("elapsed time (minutes)")
    ax.set_ylabel("temperature (C)")
    ax.legend(loc="lower right", ncol=2, fontsize=8)
    save(fig, "fig-temperature-session.png")


def fig_duty_states(rows):
    order = ["charging", "idle", "working", "returning", "cooldown"]
    fig, ax = plt.subplots(figsize=(7.2, 2.5))
    for index, r in enumerate(rows[:-1]):
        state = r["state"]
        if state not in order:
            continue
        ax.barh(order.index(state), (rows[index + 1]["t"] - r["t"]) / 60,
                left=r["t"] / 60, height=0.72,
                color=STATE_COLOURS.get(state, GREY), lw=0)
    ax.set_yticks(range(len(order)))
    ax.set_yticklabels(order)
    ax.set_xlabel("elapsed time (minutes)")
    ax.grid(axis="y", alpha=0)
    save(fig, "fig-duty-states.png")


def fig_condition_wear(rows):
    fig, ax = plt.subplots(figsize=(7.2, 2.7))
    ax.plot([r["t"] / 60 for r in rows], [r["condition_percent"] for r in rows],
            color=PURPLE, lw=1.8)
    ax.set_xlabel("elapsed time (minutes)")
    ax.set_ylabel("condition (%)")
    total = rows[0]["condition_percent"] - rows[-1]["condition_percent"]
    ax.annotate(f"{total:.2f} percentage points lost\nover {rows[-1]['t'] / 60:.0f} minutes",
                xy=(rows[-1]["t"] / 60, rows[-1]["condition_percent"]),
                xytext=(-150, 26), textcoords="offset points", fontsize=8,
                arrowprops=dict(arrowstyle="->", color=GREY, lw=0.9))
    save(fig, "fig-condition-wear.png")


# ================================================================= relationships
def fig_payload_energy():
    with open(ROOT / "tools" / "ml" / "runs.csv") as handle:
        rows = list(csv.DictReader(handle))
    weights = sorted({float(r["payload_kg"]) for r in rows})
    per_metre = {w: [] for w in weights}
    for r in rows:
        w, d, e = float(r["payload_kg"]), float(r["route_distance_m"]), float(r["energy_wh"])
        if d > 0:
            per_metre[w].append(e / d)

    fig, ax = plt.subplots(figsize=(6.4, 3.0))
    means = [float(np.mean(per_metre[w])) for w in weights]
    ax.bar([str(w) for w in weights], means, color=BLUE, width=0.62)
    for x, value in enumerate(means):
        ax.text(x, value, f"{value:.3f}", ha="center", va="bottom", fontsize=8)
    ax.set_xlabel("payload (kg) - the six catalogue weights")
    ax.set_ylabel("energy per metre (Wh/m)")
    save(fig, "fig-payload-energy.png")


def fig_delivery_accuracy():
    # Every delivery distance measured against Gazebo ground truth during testing, all of them
    # taken after the approach-and-face fix to the grab. The tolerance was tightened from 1.1 m
    # to 0.7 m at the same time, because the old one no longer discriminated.
    measured = [0.24, 0.31, 0.44, 0.49, 0.43, 0.38, 0.19, 0.46, 0.33, 0.18]
    tolerance = 0.7
    fig, ax = plt.subplots(figsize=(6.6, 3.0))
    ax.bar(range(1, len(measured) + 1), measured, color=GREEN, width=0.6)
    ax.axhline(tolerance, color=RED, ls="--", lw=1.2,
               label=f"{tolerance} m acceptance tolerance")
    ax.axhline(float(np.mean(measured)), color=BLUE, ls=":", lw=1.2,
               label=f"mean {np.mean(measured):.2f} m")
    ax.set_xlabel("delivery (in the order measured)")
    ax.set_ylabel("distance from the bay (m)")
    ax.set_ylim(0, 0.85)
    ax.legend(fontsize=8)
    save(fig, "fig-delivery-accuracy.png")


def fig_predicted_vs_actual():
    # Pairs recorded during acceptance runs: (predicted route m, measured m).
    predicted = [8.60, 6.89, 10.78, 4.19, 8.60]
    actual = [8.30, 6.70, 10.40, 4.05, 8.45]
    fig, ax = plt.subplots(figsize=(4.6, 4.0))
    ax.scatter(predicted, actual, s=52, color=BLUE, zorder=3)
    limits = [0, 12]
    ax.plot(limits, limits, color=GREY, ls="--", lw=1.1, label="perfect prediction")
    ax.set_xlim(*limits)
    ax.set_ylim(*limits)
    ax.set_xlabel("predicted route length (m)")
    ax.set_ylabel("measured route length (m)")
    ax.legend(fontsize=8, loc="upper left")
    ax.set_aspect("equal")
    save(fig, "fig-predicted-vs-actual.png")


# ========================================================= requirements & tests
def fig_category_balance():
    categories = ["Data / CRUD\n(category A)", "Third-party\n(category B)",
                  "Complex, ours\n(category C)"]
    counts = [9, 8, 3]
    fig, ax = plt.subplots(figsize=(5.6, 3.0))
    bars = ax.bar(categories, counts, color=[BLUE, PURPLE, GREEN], width=0.55)
    for bar, value in zip(bars, counts):
        ax.text(bar.get_x() + bar.get_width() / 2, value, str(value),
                ha="center", va="bottom", fontweight="bold")
    ax.set_ylabel("number of functionalities")
    ax.set_ylim(0, 11)
    save(fig, "fig-category-balance.png")


def fig_test_composition():
    suites = ["Condition model\n(unit)", "Admission policy\n(unit)",
              "Database\n(unit)", "Login & roles\n(integration)",
              "API integration\n(integration)"]
    counts = [4, 4, 5, 2, 1]
    fig, ax = plt.subplots(figsize=(6.4, 3.0))
    bars = ax.bar(suites, counts, color=[GREEN, GREEN, GREEN, BLUE, BLUE], width=0.58)
    for bar, value in zip(bars, counts):
        ax.text(bar.get_x() + bar.get_width() / 2, value, str(value),
                ha="center", va="bottom", fontweight="bold")
    ax.set_ylabel("tests")
    # The gate test is parameterised over the eight gated pages, so 16 tests run as 23 cases.
    ax.set_ylim(0, 7)
    save(fig, "fig-test-composition.png")


def fig_acceptance_results():
    with open(DATA / "acceptance_results.json") as handle:
        results = json.load(handle)
    labels = [r["requirement"] for r in results]
    passed = [1 if r["passed"] else 0 for r in results]
    fig, ax = plt.subplots(figsize=(7.2, 2.9))
    ax.bar(range(len(labels)), passed,
           color=[GREEN if p else RED for p in passed], width=0.62)
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_yticks([0, 1])
    ax.set_yticklabels(["fail", "pass"])
    ax.set_ylim(0, 1.35)
    save(fig, "fig-acceptance-results.png")


def fig_test_pyramid():
    fig, ax = plt.subplots(figsize=(5.6, 3.4))
    # Widths are proportional to the number of checks. The automated suite is deliberately a
    # small representative set, so the physical layer is now the widest - the shape inverts.
    tiers = [("Acceptance\n26 checks, live simulator", 0.96, GREEN),
             ("Integration\n3 tests, faked ROS and AI", 0.28, BLUE),
             ("Unit\n13 tests, pure logic", 0.52, PURPLE)]
    for index, (label, width, colour) in enumerate(tiers):
        ax.add_patch(Rectangle((0.5 - width / 2, index * 0.9), width, 0.78,
                               facecolor=colour, alpha=0.82, edgecolor="white", lw=2))
        ax.text(0.5, index * 0.9 + 0.39, label, ha="center", va="center",
                color="white", fontsize=9, fontweight="bold")
    ax.text(0.5, 2.85, "slower, more realistic  <-->  faster, more isolated",
            ha="center", fontsize=8, color=GREY)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 3.05)
    ax.axis("off")
    save(fig, "fig-test-pyramid.png")


# =================================================================== ML figures
def fig_ml_class_balance():
    with open(ROOT / "tools" / "ml" / "runs.csv") as handle:
        rows = list(csv.DictReader(handle))
    feasible = sum(1 for r in rows if r["completed"] == "1")
    fig, ax = plt.subplots(figsize=(4.4, 3.0))
    ax.bar(["feasible\n(label 1)", "infeasible\n(label 0)"],
           [feasible, len(rows) - feasible], color=[GREEN, RED], width=0.52)
    for x, value in enumerate([feasible, len(rows) - feasible]):
        ax.text(x, value, f"{value}\n({value / len(rows):.0%})", ha="center",
                va="bottom", fontsize=8.5)
    ax.set_ylabel("training examples")
    ax.set_ylim(0, feasible * 1.25)
    save(fig, "fig-ml-class-balance.png")


def fig_ml_confusion():
    matrix = np.array([[639, 10], [21, 330]])        # held-out test split
    fig, ax = plt.subplots(figsize=(4.2, 3.6))
    ax.imshow(matrix, cmap="Blues")
    for i in range(2):
        for j in range(2):
            ax.text(j, i, str(matrix[i, j]), ha="center", va="center",
                    fontsize=15, fontweight="bold",
                    color="white" if matrix[i, j] > 500 else "#1B2631")
    ax.set_xticks([0, 1]); ax.set_xticklabels(["refuse", "accept"])
    ax.set_yticks([0, 1]); ax.set_yticklabels(["refuse", "accept"])
    ax.set_xlabel("predicted"); ax.set_ylabel("actual")
    ax.grid(False)
    save(fig, "fig-ml-confusion.png")


def fig_ml_importance():
    features = ["battery_percent", "route_distance_m", "condition_percent",
                "payload_kg", "temperature_c"]
    importance = [0.608, 0.195, 0.088, 0.072, 0.037]
    fig, ax = plt.subplots(figsize=(6.0, 2.9))
    ax.barh(features[::-1], importance[::-1], color=BLUE, height=0.6)
    for index, value in enumerate(importance[::-1]):
        ax.text(value + 0.008, index, f"{value:.3f}", va="center", fontsize=8.5)
    ax.set_xlabel("relative importance")
    ax.set_xlim(0, 0.78)
    save(fig, "fig-ml-importance.png")


def fig_ml_pipeline():
    fig, ax = plt.subplots(figsize=(7.4, 2.2))
    boxes = [("tools/ml/generate.py\nsynthetic runs", 0.5, AMBER, "disposable"),
             ("runs.csv\n4000 labelled runs", 2.3, GREY, ""),
             ("tools/ml/train.py\nrandom forest", 4.1, AMBER, "disposable"),
             ("model.joblib", 5.9, PURPLE, ""),
             ("robofetch_ai\nservice :8001", 7.5, GREEN, "permanent")]
    for label, x, colour, tag in boxes:
        ax.add_patch(Rectangle((x, 0.55), 1.45, 0.75, facecolor=colour, alpha=0.20,
                               edgecolor=colour, lw=1.6))
        ax.text(x + 0.72, 0.93, label, ha="center", va="center", fontsize=8)
        if tag:
            ax.text(x + 0.72, 0.40, tag, ha="center", fontsize=7.5,
                    style="italic", color=colour)
    for x in (1.95, 3.75, 5.55, 7.35):
        ax.add_patch(FancyArrowPatch((x, 0.93), (x + 0.32, 0.93),
                                     arrowstyle="-|>", mutation_scale=12, color=GREY))
    ax.text(4.6, 1.62, "the live run logger writes the same CSV schema,\n"
                       "so real and synthetic runs are interchangeable",
            ha="center", fontsize=7.8, color=GREY)
    ax.set_xlim(0.2, 9.2); ax.set_ylim(0.2, 1.9)
    ax.axis("off")
    save(fig, "fig-ml-pipeline.png")


# ============================================================ drawn diagrams
def fig_warehouse_layout():
    fig, ax = plt.subplots(figsize=(7.0, 5.4))
    ax.add_patch(Rectangle((-4.05, -3.05), 8.10, 6.10, facecolor="#F7F9FC",
                           edgecolor="#2C3E50", lw=2.4))
    for cx, cy, sx, sy, name in [(-2.5, 2.25, 2.0, 1.4, "shelf_1"),
                                 (1.5, 2.25, 1.5, 1.4, "shelf_2"),
                                 (3.6, -1.0, 0.5, 2.0, "shelf_3")]:
        ax.add_patch(Rectangle((cx - sx / 2, cy - sy / 2), sx, sy,
                               facecolor="#8B6F47", alpha=0.75, edgecolor="#5D4A31"))
        # shelf_3 is narrow and against the east wall, so its label is rotated to fit.
        ax.text(cx, cy, name, ha="center", va="center", color="white",
                fontsize=8, fontweight="bold", rotation=90 if sy > sx else 0)

    for x, y, label in [(-3.0, -2.2, "delivery_1"), (2.6, -2.2, "delivery_2")]:
        ax.add_patch(Rectangle((x - 0.6, y - 0.5), 1.2, 1.0, facecolor=GREEN,
                               alpha=0.28, edgecolor=GREEN, lw=1.6))
        ax.text(x, y - 0.72, label, ha="center", fontsize=7.5, color=GREEN)
    ax.add_patch(Rectangle((-0.45, -2.65), 0.9, 0.9, facecolor=BLUE, alpha=0.28,
                           edgecolor=BLUE, lw=1.6))
    ax.text(0.0, -2.88, "station_1", ha="center", fontsize=7.5, color=BLUE)

    parcels = [(-3.0, 1.35, "1001"), (-2.0, 1.35, "1002"), (1.05, 1.35, "2001"),
               (1.95, 1.35, "2002"), (3.15, -1.5, "3001"), (3.15, -0.5, "3002")]
    for x, y, sku in parcels:
        ax.add_patch(Rectangle((x - 0.09, y - 0.09), 0.18, 0.18,
                               facecolor=AMBER, edgecolor="#6E4B10"))
        ax.text(x, y + 0.22, sku, ha="center", fontsize=6.6, color="#6E4B10")

    picks = [(-3.0, 0.95), (-2.0, 0.95), (1.05, 0.95), (1.95, 0.95),
             (2.75, -1.5), (2.75, -0.5)]
    for x, y in picks:
        ax.plot(x, y, "o", ms=6, mfc="none", mec="#2C3E50", mew=1.3)
    ax.plot([], [], "o", ms=6, mfc="none", mec="#2C3E50", mew=1.3, label="pick point")
    ax.plot([], [], "s", ms=7, color=AMBER, label="parcel (0.09 m cube)")

    ax.set_xlim(-4.6, 4.6); ax.set_ylim(-3.6, 3.5)
    ax.set_xlabel("x (m)"); ax.set_ylabel("y (m)")
    ax.set_aspect("equal")
    ax.legend(loc="upper center", ncol=2, fontsize=8)
    save(fig, "fig-warehouse-layout.png")


def fig_layered_architecture():
    fig, ax = plt.subplots(figsize=(6.8, 4.4))
    layers = [
        ("Robot platform  -  Nav2, Gazebo, ros_gz_bridge", "third party", PURPLE),
        ("Robot logic  -  task manager, condition model, gripper", "ours", GREEN),
        ("AI service  -  feasibility classifier (:8001)", "third-party model,\nour boundary", AMBER),
        ("Application  -  FastAPI, admission workflow, SQLite", "ours", BLUE),
        ("Presentation  -  server-rendered HTML and CSS", "ours", "#5D6D7E"),
    ]
    for index, (label, tag, colour) in enumerate(layers):
        ax.add_patch(Rectangle((0.06, index * 0.9), 0.72, 0.72, facecolor=colour,
                               alpha=0.22, edgecolor=colour, lw=1.8))
        ax.text(0.42, index * 0.9 + 0.36, label, ha="center", va="center", fontsize=9)
        ax.text(0.86, index * 0.9 + 0.36, tag, ha="center", va="center",
                fontsize=7.5, style="italic", color=colour)
    ax.annotate("", xy=(0.03, 0.15), xytext=(0.03, 4.35),
                arrowprops=dict(arrowstyle="-|>", color=GREY, lw=1.4))
    ax.text(-0.02, 2.25, "dependencies point downward", rotation=90, va="center",
            ha="center", fontsize=8, color=GREY)
    ax.set_xlim(-0.06, 1.0); ax.set_ylim(-0.12, 4.7)
    ax.axis("off")
    save(fig, "fig-layered-architecture.png")


def fig_dependency_direction():
    fig, ax = plt.subplots(figsize=(7.2, 3.4))
    # Positions are (label, left edge); every box is 2.0 wide.
    pure = {"robot_model.py\ncondition physics": 1.3, "admission.py\npolicy gates": 4.2}
    impure = {"robot_state_node\n(ROS node)": 0.2, "app.py\n(HTTP handlers)": 3.0,
              "generate.py\n(disposable tool)": 5.8}
    for label, x in pure.items():
        ax.add_patch(Rectangle((x, 0.2), 2.0, 0.74, facecolor=GREEN, alpha=0.20,
                               edgecolor=GREEN, lw=1.6))
        ax.text(x + 1.0, 0.57, label, ha="center", va="center", fontsize=8)
    for label, x in impure.items():
        ax.add_patch(Rectangle((x, 2.0), 2.0, 0.74, facecolor=BLUE, alpha=0.18,
                               edgecolor=BLUE, lw=1.6))
        ax.text(x + 1.0, 2.37, label, ha="center", va="center", fontsize=8)

    # Real import edges, taken from the source files.
    edges = [(impure["robot_state_node\n(ROS node)"] + 1.0, pure["robot_model.py\ncondition physics"] + 0.7),
             (impure["generate.py\n(disposable tool)"] + 1.0, pure["robot_model.py\ncondition physics"] + 1.5),
             (impure["app.py\n(HTTP handlers)"] + 1.0, pure["admission.py\npolicy gates"] + 0.7)]
    for x0, x1 in edges:
        ax.add_patch(FancyArrowPatch((x0, 1.98), (x1, 0.96), arrowstyle="-|>",
                                     mutation_scale=12, color=GREY, lw=1.2,
                                     connectionstyle="arc3,rad=0.12"))
    # admission.py itself imports the physics: pure depending only on pure.
    ax.add_patch(FancyArrowPatch((pure["admission.py\npolicy gates"] + 0.05, 0.57),
                                 (pure["robot_model.py\ncondition physics"] + 2.05, 0.57),
                                 arrowstyle="-|>", mutation_scale=12, color=GREEN, lw=1.2))

    ax.text(4.0, 3.02, "depend on ROS, HTTP or the database", ha="center",
            fontsize=8.5, color=BLUE)
    ax.text(4.0, -0.18,
            "pure Python: no ROS, no HTTP, no database - unit-testable in milliseconds",
            ha="center", fontsize=8.5, color=GREEN)
    ax.set_xlim(0.0, 8.0); ax.set_ylim(-0.5, 3.3)
    ax.axis("off")
    save(fig, "fig-dependency-direction.png")


def fig_incremental():
    """Pressman's incremental model, drawn with this project's own three increments.

    The point the picture has to make is that the phases are not a single pass: each increment
    runs the whole sequence and ends in something delivered. Drawing them staggered rather than
    stacked is what shows that increment 2 starts from a working system rather than from a
    document.
    """
    phases = ["Req.", "Analysis", "Design", "Impl.", "Testing", "Deploy"]
    increments = [
        ("Increment 1  Core delivery system", BLUE),
        ("Increment 2  Decision layer", GREEN),
        ("Increment 3  Operational control", PURPLE),
    ]
    box_w, box_h, gap = 0.92, 0.42, 0.06
    row_drop, row_shift = 1.02, 0.72

    fig, ax = plt.subplots(figsize=(7.6, 4.2))
    for row, (label, colour) in enumerate(increments):
        y = (len(increments) - row - 1) * row_drop
        x0 = row * row_shift
        ax.text(x0 - 0.12, y + box_h + 0.20, label, fontsize=8.6,
                fontweight="bold", color=colour, va="bottom")
        for index, phase in enumerate(phases):
            x = x0 + index * (box_w + gap)
            ax.add_patch(Rectangle((x, y), box_w, box_h, facecolor=colour, alpha=0.20,
                                   edgecolor=colour, lw=1.4))
            ax.text(x + box_w / 2, y + box_h / 2, phase, ha="center", va="center", fontsize=7.6)
            if index < len(phases) - 1:
                ax.add_patch(FancyArrowPatch((x + box_w, y + box_h / 2),
                                             (x + box_w + gap, y + box_h / 2),
                                             arrowstyle="-|>", mutation_scale=8, color=GREY))
        # the delivery that closes the increment - the whole reason the row exists
        end_x = x0 + len(phases) * (box_w + gap)
        ax.add_patch(FancyArrowPatch((end_x - gap, y + box_h / 2), (end_x + 0.24, y + box_h / 2),
                                     arrowstyle="-|>", mutation_scale=11, color=colour))
        ax.text(end_x + 0.34, y + box_h / 2, "delivered\nand verified", fontsize=7.4,
                color=colour, va="center", style="italic")

    # Maintenance is NOT a box at the end of a row. The moment increment 1 is delivered, every
    # later change to it is maintenance, so it runs as a band from the first delivery onwards and
    # does not stop at the last increment. Drawing it as a phase box would say the opposite.
    first_delivery_x = len(phases) * (box_w + gap) + 0.24
    band_y, band_h, band_end = -0.78, 0.36, 9.20
    ax.add_patch(Rectangle((first_delivery_x, band_y), band_end - first_delivery_x, band_h,
                           facecolor=GREY, alpha=0.16, edgecolor=GREY, lw=1.1))
    ax.text(first_delivery_x + 0.22, band_y + band_h / 2, "Maintenance",
            fontsize=8.4, color="#3E4B4A", va="center", style="italic")
    ax.add_patch(FancyArrowPatch((band_end, band_y + band_h / 2),
                                 (band_end + 0.45, band_y + band_h / 2),
                                 arrowstyle="-|>", mutation_scale=10, color=GREY))
    # a short tick, not a full riser: it marks where maintenance starts without
    # drawing a line through the increments above it
    ax.plot([first_delivery_x, first_delivery_x], [band_y + band_h, band_y + band_h + 0.22],
            ls=":", lw=1.1, color=GREY)
    ax.text(first_delivery_x, band_y + band_h + 0.30, "begins at the first delivery",
            fontsize=7.2, color=GREY, ha="left", va="bottom")

    ax.set_xlim(-0.35, 10.1)
    ax.set_ylim(-1.00, 3.15)
    ax.axis("off")
    save(fig, "fig-incremental-model.png")


# ========================================================================= main
def main():
    print(f"writing figures to {FIG}")
    rows = telemetry()
    fig_battery_session(rows)
    fig_temperature_session(rows)
    fig_duty_states(rows)
    fig_condition_wear(rows)
    fig_payload_energy()
    fig_delivery_accuracy()
    fig_predicted_vs_actual()
    fig_category_balance()
    fig_test_composition()
    fig_acceptance_results()
    fig_test_pyramid()
    fig_ml_class_balance()
    fig_ml_confusion()
    fig_ml_importance()
    fig_ml_pipeline()
    fig_warehouse_layout()
    fig_layered_architecture()
    fig_dependency_direction()
    fig_incremental()

    # The acceptance table is generated from the same JSON as fig_acceptance_results, and is
    # refreshed here so the figure and the table beside it can never describe different runs.
    make_results_table.main()
    print("done")


if __name__ == "__main__":
    main()
