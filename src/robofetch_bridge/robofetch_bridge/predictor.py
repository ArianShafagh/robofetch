"""Client for the AI feasibility service (proposal §5, NFR2).

Thin HTTP wrapper with one job beyond calling the endpoint: **never let the AI take the system
down with it.** If the service is stopped, slow, has no model loaded, or returns something
unexpected, this returns `None` and admission control carries on using the deterministic model
alone. That is a hard requirement (NFR2), not a nicety - the robot must keep working when the
AI does not.

Uses urllib rather than a client library so the web tier gains no new dependency for what is a
single POST.
"""
import json
import urllib.error
import urllib.request

TIMEOUT_S = 1.5     # generous for localhost; the preview must still meet NFR1's 2 s budget


class Predictor:
    def __init__(self, base_url, timeout=TIMEOUT_S):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._last_error = None
        self._reachable = None

    def status(self):
        """What the last call did - surfaced on /health and the robot page."""
        return {"url": self.base_url, "reachable": self._reachable,
                "error": self._last_error}

    def __call__(self, features):
        """Return (feasible, confidence, source) or None if the model could not be consulted.

        `source` is "model" or "fallback", and it is recorded against the order so the report
        can say how each decision was actually made.
        """
        request = urllib.request.Request(
            f"{self.base_url}/predict",
            data=json.dumps(features).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            self._reachable = False
            self._last_error = str(exc)
            return None

        self._reachable = True
        self._last_error = None

        # The service answers 200 with feasible=None when it has no model, so that "the AI
        # says no" stays distinguishable from "there is no AI".
        if not body.get("model_loaded") or body.get("feasible") is None:
            self._last_error = body.get("error") or "no model loaded"
            return None

        return bool(body["feasible"]), float(body.get("confidence") or 0.0), "model"
