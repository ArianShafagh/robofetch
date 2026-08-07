"""FastAPI service: the web tier of RoboFetch v2 (proposal §5, §9).

Two faces on the same application:

  HTML pages (server-rendered, no JavaScript anywhere)
      GET  /            order form                                       UC1
      POST /preview     cost + accept/refuse verdict, before committing  UC2, UC9
      POST /confirm     place the order                                  UC3
      GET  /orders      order history                                    UC4
      GET  /robot       robot condition                                  UC7

  REST (same logic, for scripts and the acceptance suite)
      GET    /api/products            the catalogue
      GET    /api/delivery-points     destinations
      POST   /api/preview             {product_id, delivery_id} -> estimate + verdict
      POST   /api/orders              submit; refuses if admission says no
      GET    /api/orders[/{id}]       list / track
      DELETE /api/orders/{id}         cancel a pending order
      GET    /api/robot               current condition
      GET    /api/analytics           aggregate statistics                UC6
      GET    /health                  is this API wired to a robot?

The order of operations matters: an order is admitted BEFORE it is stored, so the database
never contains a job the robot was never going to attempt. Refused orders are still recorded -
with status `refused` and the reason - because "what did we turn away" is exactly the kind of
question the analytics are for.
"""
import os

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from robofetch_bridge import admission
from robofetch_bridge.db import Database
from robofetch_bridge.predictor import Predictor
from robofetch_bridge.ros_link import RosThread

DB_PATH = os.environ.get("ROBOFETCH_DB", os.path.expanduser("~/robofetch_ws/robofetch.db"))
WEB_DIR = os.environ.get("ROBOFETCH_WEB", "")
AI_URL = os.environ.get("ROBOFETCH_AI_URL", "http://localhost:8001")

app = FastAPI(title="RoboFetch", version="2.0.0")
db = Database(DB_PATH)
predictor = Predictor(AI_URL)

templates = Jinja2Templates(directory=os.path.join(WEB_DIR, "templates")) if WEB_DIR else None


class OrderRequest(BaseModel):
    product_id: str
    delivery_id: str


class PreviewRequest(BaseModel):
    product_id: str
    delivery_id: str


def _on_status(payload):
    """An order changed state on the robot: persist it, and close the books when it ends."""
    order = db.update_order(payload["id"], status=payload.get("status"),
                            attempts=payload.get("attempts"),
                            detail=payload.get("detail"))
    if order and payload.get("status") in ("completed", "failed"):
        duration = ((order.get("completed_at") or 0) - order["created_at"]) or None
        db.record_history(
            order["id"],
            duration_s=duration,
            distance_m=order.get("estimated_distance_m"),
            # Charge the predicted energy only for orders that actually ran to completion;
            # a failed order did not consume a full delivery's worth.
            energy_wh=(order.get("estimated_energy_wh")
                       if payload["status"] == "completed" else None),
            payload_kg=order.get("weight_kg"),
            outcome=order["status"])


def _on_telemetry(sample):
    """Robot condition arrived: keep the current row fresh and append to the time-series."""
    db.update_robot_state(**sample)
    db.record_telemetry(sample)


ros = RosThread(on_status=_on_status, on_telemetry=_on_telemetry)


@app.on_event("startup")
async def _startup():
    ros.start()


@app.on_event("shutdown")
async def _shutdown():
    ros.stop()


# ------------------------------------------------------------------ admission core
def _admit(product_id, delivery_id):
    """Resolve, cost and judge an order. Raises HTTPException for unknown inputs."""
    product = db.get_product(product_id)
    if product is None:
        raise HTTPException(status_code=404, detail=f"unknown product '{product_id}'")
    delivery = db.get_delivery_point(delivery_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail=f"unknown destination '{delivery_id}'")

    station = db.get_station()
    state = db.get_robot_state()
    decision, reason, decided_by, cost = admission.decide(
        product, delivery, station, state,
        robot_xy=ros.node.robot_xy, predictor=predictor)
    return product, delivery, decision, reason, decided_by, cost


# ------------------------------------------------------------------------ REST API
@app.get("/api/products")
def api_products():
    return db.list_products()


@app.get("/api/delivery-points")
def api_delivery_points():
    return db.list_delivery_points()


@app.post("/api/preview")
def api_preview(request: PreviewRequest):
    """UC2/UC9 - what would this cost, and would the robot take it?"""
    product, delivery, decision, reason, decided_by, cost = _admit(
        request.product_id, request.delivery_id)
    return {"product": product, "delivery": delivery, "estimate": cost,
            "decision": decision, "reason": reason, "decided_by": decided_by}


@app.post("/api/orders", status_code=201)
def api_create_order(request: OrderRequest):
    """UC1/UC3 - submit an order. A refused order is stored, not executed."""
    product, delivery, decision, reason, decided_by, cost = _admit(
        request.product_id, request.delivery_id)

    order = db.create_order(request.product_id, request.delivery_id, estimate=cost,
                            decision=decision, reason=reason, decided_by=decided_by)

    if decision != admission.ACCEPTED:
        db.update_order(order["id"], status="refused", detail=reason)
        return db.get_order(order["id"])

    # Publishing to a topic nobody subscribes to succeeds silently, so an order sent to a
    # robot that is not running looks exactly like one that worked. Say so instead.
    if ros.node.order_pub.get_subscription_count() == 0:
        return db.update_order(
            order["id"],
            detail="no robot is subscribed to /orders/new - order stored but NOT sent")

    ros.node.submit_order(order["id"], product,
                          (product["pick_x"], product["pick_y"]),
                          (delivery["x"], delivery["y"]))
    return db.get_order(order["id"])


@app.get("/api/orders")
def api_list_orders(status: str = None):
    return db.list_orders(status=status)


@app.get("/api/orders/{order_id}")
def api_get_order(order_id: int):
    order = db.get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="no such order")
    return order


@app.delete("/api/orders/{order_id}")
def api_cancel_order(order_id: int):
    order, error = db.cancel_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=error)
    if error:
        raise HTTPException(status_code=409, detail=error)
    return order


@app.get("/api/robot")
def api_robot():
    return db.get_robot_state()


@app.get("/api/analytics")
def api_analytics():
    return db.analytics()


@app.get("/health")
def health():
    """Is this API actually wired to a robot?

    "I submitted an order and nothing happened" has several causes that look identical from
    the client. The most confusing is a STALE api left holding port 8000: it serves HTTP
    perfectly while connected to a different database and no running robot. The live
    subscriber count on /orders/new distinguishes them in one request.
    """
    subscribers = ros.node.order_pub.get_subscription_count()
    return {
        "status": "ok",
        "database": DB_PATH,
        "robot_connected": subscribers > 0,
        "orders_new_subscribers": subscribers,
        "ai_service": predictor.status(),
    }


# --------------------------------------------------------------------- HTML pages
# Every page is a snapshot of live state - orders in flight, battery, temperature - so it must
# never be served from the browser cache. This also matters for the v1 -> v2 upgrade: the old
# dashboard was a static file at "/", which browsers cache aggressively, so without this a
# returning user keeps seeing the old page and reasonably concludes nothing changed.
NO_CACHE = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}


def _page_context(request):
    return {"request": request, "robot": db.get_robot_state()}


@app.get("/app")
@app.get("/app/{path:path}")
def legacy_dashboard(path: str = ""):
    """The v1 dashboard lived under /app. Send old links and bookmarks to the new UI."""
    return RedirectResponse("/", status_code=308)


@app.get("/", response_class=HTMLResponse)
def page_order_form(request: Request):
    context = _page_context(request)
    context.update(products=db.list_products(),
                   deliveries=db.list_delivery_points())
    return templates.TemplateResponse(request, "order.html", context, headers=NO_CACHE)


@app.post("/preview", response_class=HTMLResponse)
def page_preview(request: Request, product_id: str = Form(...),
                 delivery_id: str = Form(...)):
    """UC2 - the verdict is shown BEFORE the order is committed."""
    product, delivery, decision, reason, decided_by, cost = _admit(product_id, delivery_id)
    context = _page_context(request)
    context.update(product=product, delivery=delivery, estimate=cost,
                   decision=decision, reason=reason, decided_by=decided_by,
                   accepted=decision == admission.ACCEPTED)
    return templates.TemplateResponse(request, "preview.html", context, headers=NO_CACHE)


@app.post("/confirm")
def page_confirm(product_id: str = Form(...), delivery_id: str = Form(...)):
    api_create_order(OrderRequest(product_id=product_id, delivery_id=delivery_id))
    return RedirectResponse("/orders", status_code=303)


@app.get("/orders", response_class=HTMLResponse)
def page_orders(request: Request):
    context = _page_context(request)
    context.update(orders=list(reversed(db.list_orders())),
                   stats=db.analytics())
    return templates.TemplateResponse(request, "orders.html", context, headers=NO_CACHE)


@app.get("/robot", response_class=HTMLResponse)
def page_robot(request: Request):
    context = _page_context(request)
    context.update(stats=db.analytics(), telemetry=db.list_telemetry(limit=20),
                   ai=predictor.status())
    return templates.TemplateResponse(request, "robot.html", context, headers=NO_CACHE)


# Static CSS. Templates are rendered above; only the stylesheet is served as a file.
if WEB_DIR and os.path.isdir(os.path.join(WEB_DIR, "static")):
    # follow_symlink=True is REQUIRED: colcon --symlink-install installs these as symlinks
    # back into src/, and StaticFiles rejects anything whose real path leaves the mounted
    # directory. Without it every stylesheet request 404s while the pages still render.
    app.mount("/static",
              StaticFiles(directory=os.path.join(WEB_DIR, "static"), follow_symlink=True),
              name="static")


def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("ROBOFETCH_PORT", 8000)))


if __name__ == "__main__":
    main()
