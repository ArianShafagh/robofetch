"""Order feasibility prediction service (proposal §5, §10.4).

A small FastAPI service that loads the classifier trained by tools/ml/train.py and answers one
question:

    POST /predict {battery_percent, temperature_c, condition_percent,
                   payload_kg, route_distance_m}
      -> {feasible: bool, confidence: float, model_loaded: bool}

It runs as a **separate process** on port 8001, on purpose. The main API must survive this
service being down, restarted, or having no model at all - admission control falls back to the
deterministic model in that case (NFR2). Keeping the AI behind a network boundary is what makes
that failure mode obvious and testable rather than theoretical.

    ./robofetch_venv/bin/python -m uvicorn robofetch_ai.service:app --port 8001
"""
import os

from fastapi import FastAPI
from pydantic import BaseModel

MODEL_PATH = os.environ.get(
    "ROBOFETCH_MODEL",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "model.joblib"))

app = FastAPI(title="RoboFetch AI", version="1.0.0")

_bundle = None
_load_error = None


def _load():
    """Load the model once, lazily, and remember why if it fails.

    A missing model is not a crash: the service stays up and reports `model_loaded: false`,
    so the caller can distinguish "the AI says no" from "there is no AI", which are very
    different things to show an operator.
    """
    global _bundle, _load_error
    if _bundle is not None or _load_error is not None:
        return
    try:
        import joblib
        _bundle = joblib.load(MODEL_PATH)
    except Exception as exc:                                       # noqa: BLE001
        _load_error = str(exc)


class Features(BaseModel):
    battery_percent: float
    temperature_c: float
    condition_percent: float
    payload_kg: float
    route_distance_m: float


@app.get("/health")
def health():
    _load()
    return {"status": "ok", "model_loaded": _bundle is not None,
            "model_path": MODEL_PATH, "error": _load_error}


@app.post("/predict")
def predict(features: Features):
    _load()
    if _bundle is None:
        # 200 with model_loaded false rather than an error status: the caller is expected to
        # carry on without us, and an exception here would just be noise in its logs.
        return {"feasible": None, "confidence": None, "model_loaded": False,
                "error": _load_error}

    model = _bundle["model"]
    order = _bundle["features"]
    values = features.model_dump()
    row = [[values[name] for name in order]]

    prediction = bool(model.predict(row)[0])
    try:
        # Confidence in the class that was actually predicted, not always class 1.
        probabilities = model.predict_proba(row)[0]
        confidence = float(max(probabilities))
    except AttributeError:
        confidence = 1.0

    return {"feasible": prediction, "confidence": confidence, "model_loaded": True}


def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("ROBOFETCH_AI_PORT", 8001)))


if __name__ == "__main__":
    main()
