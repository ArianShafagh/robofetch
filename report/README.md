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

## Screenshots — all taken

The report contains 19 generated charts, 9 UML diagrams and the 14 screenshots below. Screenshots
of the running system are the one thing that cannot be produced automatically, and **all fourteen
are now in `Figures/`** — every figure the `.tex` references exists on disk, so nothing compiles
as a placeholder box. The table is kept as the record of what each one shows, so a retake captures
the same thing. They are added with the same macro the other figures use:

```latex
\uploadedfigure{Figures/shot-order-form.png}{0.9\textwidth}
{The order form, listing the six catalogue products.}{fig:shotform}
```

| Save as | What to capture | Where it belongs |
|---|---|---|
| `shot-login.png` | The login page, reached by visiting any page signed out | Ch 9, Web Application |
| `shot-order-form.png` | The order form, signed in as `controller` — header condition strip and the stop button alone at the right | Ch 9 |
| `shot-preview-accepted.png` | `SKU-3001` to `delivery_1` from a full battery: accepted, ~35% left | Ch 9 |
| `shot-preview-refused.png` | The same order again after the first completes — refused on the reserve | Ch 11, C2 |
| `shot-preview-reserved.png` | A preview taken while two orders are still running: the "battery at start" row and the "already in progress" reason | Ch 11, C2 |
| `shot-stock-consumed.png` | The order page after a delivery: the delivered item at zero stock, marked not orderable | Ch 11 |
| `shot-orders-page.png` | The order history showing a completed delivery, a refusal and a return trip | Ch 9 |
| `shot-robot-page.png` | The robot condition page, mid-charge | Ch 9 |
| `shot-admin-products.png` | Product maintenance, signed in as `admin`, mid-edit | Ch 9 |
| `shot-admin-db.png` | `/admin/db?table=users`, showing the redacted hash and token columns | Ch 9 |
| `shot-estop.png` | `/orders` straight after pressing the stop: the running order failed, the queued one failed too | Ch 9 |
| `shot-gazebo-warehouse.png` | Gazebo, whole warehouse from above, six parcels on the shelves | Ch 8, Simulated Environment |
| `shot-rviz-costmap.png` | RViz: the laser scan lying on the walls, with costmap inflation | Ch 8 |
| `shot-launch.png` | The launch console at `Localized and ready`, with the default-password warning above it | Ch 13 |

Nine were new and five were retakes, because the originals predated the plain redesign and the
login page and no longer matched the interface described in the text.

**A missing one would not block a compile.** Each slot uses `\screenshotslot`, which renders a
labelled placeholder box when the file is absent. Drop the PNG into `Figures/` under the name above
and it becomes a real figure with no other edit — which is also how a retake is done.

To get the **refused** preview, order the heaviest product (`SKU-3001`) to `delivery_1` twice.
On the current battery one such delivery uses over half the pack, so the second is refused on the
reserve — the refusal message explains itself, which is what makes it a good figure.

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
| `07-database-class.puml` | Database, structural (**not currently used** — see note) | `<<table>>` stereotype, `<<PK>>`/`<<FK>>`, associations with multiplicities |
| `08-component.puml` | Components | Packages, components, dependency arrows |
| `09-deployment.puml` | Deployment | Nodes as processes, artifacts inside them |

The report uses the **ER diagram** for the database, since it shows the columns, the keys and the
cardinalities in one readable picture. The UML class version of the same schema is still available
as `07-database-class.puml` and renders to `Figures/07-database-class.png` if you ever want it —
just add an `\uploadedfigure` line for it. The same applies to `fig-category-balance.png`, which is
generated but no longer referenced.

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
| 13 Environment and challenges | `HANDOVER.md` sections 2 and 5 |
| 15 Limitations | `HANDOVER.md` section 7 |

---

## Data behind the charts

Everything plotted is measured, not invented.

| Chart | Source |
|---|---|
| Battery, temperature, duty states, condition | `tools/report/data/session_telemetry.csv` — a real 313-sample run, every catalogue product delivered |
| Payload against energy | `tools/ml/runs.csv` |
| Acceptance results | `tools/report/data/acceptance_results.json`, written directly by `scripts/acceptance.py --json` |
| Delivery accuracy | Distances measured against the simulator during acceptance runs |
| Test composition | Test counts per suite |
| ML metrics | Output of `tools/ml/train.py` |

Before submitting, check your course's policy on tool assistance, and make sure you can explain
any chapter you are asked about — the table above is there to make that straightforward.
