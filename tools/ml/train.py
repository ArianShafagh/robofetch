#!/usr/bin/env python3
"""Train the order-feasibility classifier - DISPOSABLE (proposal §10.4, NFR6).

    ./robofetch_venv/bin/python tools/ml/train.py --data tools/ml/runs.csv \
        --out src/robofetch_ai/robofetch_ai/models/model.joblib

One binary classifier. Given the robot's condition and the size of the job, does the order
complete within battery and thermal limits?

    features: battery_percent, temperature_c, condition_percent, payload_kg, route_distance_m
    label   : completed (1/0)

Note what is deliberately NOT a feature: `battery_after_percent` and `peak_temperature_c`.
Those are computed by the same formula that generated the label, so including them would let
the model score ~100% by reading the answer off its own input - a textbook leak that would
make the evaluation meaningless. The model only sees quantities that are knowable BEFORE the
order runs, which is the same information admission control has at decision time.

Like generate.py, this file is meant to be deleted once model.joblib exists. The running
system never imports it.
"""
import argparse

import joblib
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.model_selection import cross_val_score, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

FEATURES = ["battery_percent", "temperature_c", "condition_percent",
            "payload_kg", "route_distance_m"]
LABEL = "completed"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", default="tools/ml/runs.csv")
    parser.add_argument("--out", default="src/robofetch_ai/robofetch_ai/models/model.joblib")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    frame = pd.read_csv(args.data)
    print(f"{len(frame)} rows, {frame[LABEL].mean():.1%} feasible")

    X, y = frame[FEATURES], frame[LABEL]
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.25, random_state=args.seed, stratify=y)

    # Scaler + forest. The scaler is not strictly needed by a tree, but keeping it in the
    # pipeline means the exported artefact is self-contained: the service hands it raw
    # physical units and never has to remember a preprocessing step.
    model = Pipeline([
        ("scale", StandardScaler()),
        ("forest", RandomForestClassifier(n_estimators=200, min_samples_leaf=2,
                                          random_state=args.seed, n_jobs=-1)),
    ])

    scores = cross_val_score(model, X_train, y_train, cv=5, scoring="f1")
    print(f"5-fold CV F1 on the training split: {scores.mean():.3f} (+/- {scores.std():.3f})")

    model.fit(X_train, y_train)
    predictions = model.predict(X_test)

    print("\nHeld-out test set")
    print(classification_report(y_test, predictions, target_names=["refuse", "accept"]))
    print("confusion matrix (rows = actual, cols = predicted)")
    print(confusion_matrix(y_test, predictions))

    importances = sorted(zip(FEATURES, model.named_steps["forest"].feature_importances_),
                         key=lambda pair: -pair[1])
    print("\nfeature importance")
    for name, weight in importances:
        print(f"  {name:22} {weight:.3f}")

    joblib.dump({"model": model, "features": FEATURES}, args.out)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
