# tools/ml — disposable ML pipeline

**This directory is meant to be deleted once the model is trained.** Nothing in the running
system imports it; the deployed service depends only on the exported `model.joblib`. That is
requirement NFR6 in the proposal, and it is why this lives in `tools/` rather than in `src/` —
it is outside the colcon build entirely.

## What it does

```
generate.py  →  runs.csv  →  train.py  →  model.joblib  →  robofetch_ai service
```

`generate.py` fabricates labelled runs by simulating them through **the same equations the live
robot uses** — it imports `robofetch_core.robot_model` rather than reimplementing the physics.
A model trained here is therefore at least consistent with the system it is deployed into.

The output schema is identical to the per-run CSV that `robot_state_node` writes during real
operation, so the two files are interchangeable and can be concatenated.

## Running it

```bash
source install/setup.bash          # needed: generate.py imports robofetch_core

./robofetch_venv/bin/python tools/ml/generate.py --runs 6000 --out tools/ml/runs.csv
./robofetch_venv/bin/python tools/ml/train.py --data tools/ml/runs.csv \
    --out src/robofetch_ai/robofetch_ai/models/model.joblib
```

`generate.py` warns if the classes come out worse than 20/80 — a badly unbalanced set trains a
model that just predicts the majority class and looks accurate while being useless.

## What the model is and is not

The classifier answers one question: *given the robot's battery, temperature and condition, the
payload weight and the route length, will this order complete within limits?*

`train.py` deliberately excludes `battery_after_percent` and `peak_temperature_c` from the
features, even though they are in the CSV. Those are computed by the same formula that produced
the label, so including them would let the model read the answer off its own input and score
~100% — a textbook leak. It only sees quantities knowable **before** the order runs, which is
exactly what admission control has at decision time.

**State this plainly in the report:** the model learns this generator's assumptions, not real
robot physics. Its accuracy figure says something about the generator, not about the robot. The
contribution being claimed is the decision *workflow* that wraps it — the feature construction,
the route computation, and the policy gates that can overrule the model — not the model itself.

## Deleting it

```bash
rm -rf tools/ml
```

The system keeps working. To prove it, the AI service reports `model_loaded` on
`GET localhost:8001/health`, and the web tier falls back to the deterministic energy formula
whenever the service cannot be reached — which is covered by
`test_admission.py::test_system_still_decides_when_the_ai_is_unreachable`.
