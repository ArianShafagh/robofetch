# RoboFetch report

LaTeX source for the Software Engineering report, plus every figure and the editable diagram
sources.

```
report/
  RoboFetch_Software_Engineering_Report.tex   the report (single file)
  Figures/                                    28 PNGs - 19 charts + 9 UML diagrams
  uml/                                        PlantUML sources you can edit
tools/report/
  make_figures.py                             regenerates the charts from real project data
  make_uml.py                                 renders uml/*.puml to Figures/*.png
  data/                                       the measured results the charts are built from
```

---

## Building it

LaTeX is **not** installed in this workspace, so the document has never been compiled here. It is
written for **Overleaf** and uses only widely available packages — no TikZ, no pgfplots, no
`minted` — so there is nothing that can fail to install.

1. Zip the `report/` folder (the `.tex` and the `Figures/` directory together).
2. On Overleaf: **New Project → Upload Project** and select the zip.
3. Set the main document to `RoboFetch_Software_Engineering_Report.tex` if it is not detected.
4. Compile with **pdfLaTeX**. Run it **twice** so the table of contents, the list of figures and
   all cross-references resolve.

If you prefer to build locally you need a TeX distribution (`texlive-full` on Ubuntu), then:

```bash
pdflatex RoboFetch_Software_Engineering_Report.tex
pdflatex RoboFetch_Software_Engineering_Report.tex
```

**Expected result:** no missing-figure warnings and no `??` in place of cross-references. Every
reference was checked by script before hand-off, but only a real compile proves it.

---

## Screenshots you still need to take

The report currently contains 28 generated figures. Screenshots of the running system are the one
thing that cannot be produced automatically. Take these, save them into `Figures/` with the names
below, and add them with the same macro the other figures use:

```latex
\uploadedfigure{Figures/shot-order-form.png}{0.9\textwidth}
{The order form, listing the six catalogue products.}{fig:shotform}
```

| Save as | What to capture | Where it belongs |
|---|---|---|
| `shot-gazebo-warehouse.png` | Gazebo, whole warehouse, six parcels visible on the shelves | Chapter 7, The Simulated Environment |
| `shot-rviz-costmap.png` | RViz showing the laser scan and the costmap | Chapter 7 |
| `shot-order-form.png` | The order form at `localhost:8000` | Chapter 8, Implementation |
| `shot-preview-accepted.png` | A preview showing **accepted**, with the cost figures | Chapter 8 or 9 |
| `shot-preview-refused.png` | A preview showing **refused**, with the reason visible | Chapter 9, next to the activity diagram |
| `shot-orders-page.png` | The order history with a few finished orders | Chapter 8 |
| `shot-robot-page.png` | The robot condition page | Chapter 8 |
| `shot-order-script.png` | A terminal running `./scripts/order.sh`, showing the verdict and the ground-truth check at the end | Chapter 12, Testing |
| `shot-acceptance.png` | A terminal showing the acceptance suite output, 15/15 | Chapter 12 |

To get the **refused** preview, order the heaviest product (`SKU-3001`) repeatedly until the
battery falls below the reserve — the refusal message explains itself, which is what makes it a
good figure.

---

## Regenerating the figures

Charts, from the real telemetry logs, the training set and the recorded test results:

```bash
./robofetch_venv/bin/python tools/report/make_figures.py
```

UML, from the PlantUML sources:

```bash
./robofetch_venv/bin/python tools/report/make_uml.py
```

Both write into `report/Figures/`. Filenames use hyphens rather than underscores on purpose:
underscores in a `\includegraphics` path are a common cause of compile errors.

---

## Editing the UML

`uml/` holds one commented PlantUML source per diagram. These are the originals — the PNGs are
generated from them.

Three ways to edit:

- **Online**: open <https://www.plantuml.com/plantuml>, paste the file, edit, download the PNG.
- **VS Code**: install the *PlantUML* extension, open the file, `Alt+D` to preview.
- **Locally**: edit the file and re-run `make_uml.py`.

| File | Diagram | Notation used |
|---|---|---|
| `01-use-case.puml` | Use cases | Actors, use cases, `<<include>>` for a step that always happens as part of another |
| `02-domain-class.puml` | Domain classes | Runtime objects, with `*--` composition and `..>` dependency |
| `03-sequence-order.puml` | Order lifecycle | Lifelines, synchronous messages, a `loop` fragment for the retry |
| `04-state-duty-cycle.puml` | Robot duty cycle | States and transitions labelled with their trigger condition |
| `05-activity-admission.puml` | Admission workflow | Actions and decision nodes; the branch order is the point of the diagram |
| `06-er-diagram.puml` | Database, conceptual | Crow's-foot cardinality |
| `07-database-class.puml` | Database, structural | `<<table>>` stereotype, `<<PK>>`/`<<FK>>`, associations with multiplicities |
| `08-component.puml` | Components | Packages, components, dependency arrows |
| `09-deployment.puml` | Deployment | Nodes as processes, artifacts inside them |

Two views of the database are included on purpose. The class diagram shows the *structure* —
columns, types, keys. The ER diagram shows the *concepts* and their cardinality. The report
explains why both are given.

---

## Where each chapter comes from

Useful if you are asked to walk through a section and want to open the code beside it.

| Chapter | Source it describes |
|---|---|
| 2 Requirements and categorization | `docs/proposal.md` |
| 4 Requirements engineering | `docs/proposal.md` sections 2 and 4 |
| 5 Analysis and domain model | `src/robofetch_bridge/robofetch_bridge/db.py` |
| 6 Software architecture | the package layout, `delivery.launch.py`, `ros_link.py`, `predictor.py` |
| 7 Principles and quality | `robot_model.py`, `admission.py`, the test suites |
| 8 Simulated environment | `warehouse.sdf`, `robofetch.urdf.xacro`, `generate_map.py` |
| 9 Implementation | `task_manager.py`, `gripper_node.py`, `robot_state_node.py`, `app.py` |
| 10 Complex functionality | `robot_model.py`, `admission.py`, `task_manager.py` |
| 11 Machine learning | `tools/ml/`, `src/robofetch_ai/robofetch_ai/service.py` |
| 12 Testing | `src/*/test/`, `scripts/acceptance.py` |
| 14 Limitations | `HANDOVER.md` section 7 |

---

## Data behind the charts

Everything plotted is measured, not invented.

| Chart | Source |
|---|---|
| Battery, temperature, duty states, condition | `tools/report/data/session_telemetry.csv` — a real 610-sample run |
| Payload against energy | `tools/ml/runs.csv` |
| Acceptance results | `tools/report/data/acceptance_results.json` |
| Delivery accuracy | Distances measured against the simulator during acceptance runs |
| Test composition | Test counts per suite |
| ML metrics | Output of `tools/ml/train.py` |

Before submitting, check your course's policy on tool assistance, and make sure you can explain
any chapter you are asked about — the table above is there to make that straightforward.
