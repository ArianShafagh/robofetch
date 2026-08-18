"""FastAPI service: the web tier of RoboFetch v2 (proposal §5, §9).

Two faces on the same application:

  HTML pages (server-rendered, no JavaScript anywhere; all require a signed-in session)
      GET  /login       sign in                                          UC12
      POST /logout      sign out
      GET  /            order form                                       UC1
      POST /preview     cost + accept/refuse verdict, before committing  UC2, UC9
      POST /confirm     place the order                                  UC3
      GET  /orders      order history                                    UC4
      GET  /robot       robot condition                                  UC7
      GET  /return      confirm sending the robot home                   UC10
      POST /return      queue the trip home
      POST /estop       abandon all work and halt - one click, no confirmation  UC11

  HTML pages, administrators only
      GET/POST /admin[/products|/delivery-points]   catalogue CRUD       UC13
      GET      /admin/db                            read-only db browser
      POST     /admin/users/password|delete         account management

  REST (same logic, for scripts and the acceptance suite)
      GET    /api/products[?all]      orderable catalogue, or all of it
      GET    /api/delivery-points     destinations
      POST   /api/preview             {product_id, delivery_id} -> estimate + verdict
      POST   /api/orders              submit; refuses if admission says no
      POST   /api/orders/return       send the robot home; queues, never refused
      GET    /api/orders[/{id}]       list / track
      DELETE /api/orders/{id}         cancel a pending order
      POST   /api/estop               abandon all work and halt the robot
      GET    /api/robot               current condition
      GET    /api/analytics           aggregate statistics                UC6
      GET    /health                  is this API wired to a robot?

The order of operations matters: an order is admitted BEFORE it is stored, so the database
never contains a job the robot was never going to attempt. Refused orders are still recorded -
with status `refused` and the reason - because "what did we turn away" is exactly the kind of
question the analytics are for.
"""
import os

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from robofetch_bridge import admission
from robofetch_bridge.db import Database, KIND_RETURN, ROLE_ADMIN, ROLES
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
    """An order changed state on the robot: persist it, and close the books when it ends.

    Reaching a terminal status also RELEASES the order's reservation, simply by leaving the
    set of statuses `Database.committed_load` counts - the ledger is derived from the orders
    table rather than kept alongside it, so it cannot drift out of step with reality.
    """
    order = db.update_order(payload["id"], status=payload.get("status"),
                            attempts=payload.get("attempts"),
                            detail=payload.get("detail"))
    # A return-to-station task reports through this same path, but it delivered nothing: it has
    # no parcel to take out of stock and belongs in no delivery history. Its cost still leaves
    # the reservation ledger, which happens automatically via its status.
    if order and order.get("kind") == KIND_RETURN:
        return
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
    if order and payload.get("status") == "completed":
        # The parcel is now at the delivery bay, not on its shelf. Ordering it again would send
        # the robot to an empty pick point to fail three grab attempts, so take it out of
        # stock - admission gate 1 turns that into a clean refusal, and the order form stops
        # offering it at all.
        db.consume_stock(order["product_id"])


def _on_telemetry(sample):
    """Robot condition arrived: keep the current row fresh and append to the time-series."""
    db.update_robot_state(**sample)
    db.record_telemetry(sample)


ros = RosThread(on_status=_on_status, on_telemetry=_on_telemetry)


@app.on_event("startup")
async def _startup():
    # Every launch restores the same Gazebo world - six parcels on their shelves, the robot
    # docked and charged - so the run starts from a clean history to match. Doing it here
    # rather than in a launch script means it happens however the system is started.
    db.reset_session()
    # The pages carry no advice, so this is the only place a default password gets mentioned.
    # Console rather than UI: it is for whoever launched the system, not for whoever is
    # looking at it, and printing account names on a web page helps the wrong reader.
    still_default = db.uses_default_passwords()
    if still_default:
        print(f"WARNING: these accounts still have their default password: "
              f"{', '.join(still_default)}. Change them on the admin page, or set "
              f"ROBOFETCH_ADMIN_PASSWORD / ROBOFETCH_CONTROLLER_PASSWORD before first launch.",
              flush=True)
    ros.start()


@app.on_event("shutdown")
async def _shutdown():
    ros.stop()


# ------------------------------------------------------------------- authentication
# Only the HTML pages are gated. The REST API is deliberately left open, because the acceptance
# suite and the helper scripts in scripts/ are unauthenticated clients of it and this is a
# single-user simulator on a developer's own machine. That is a real limitation and it is
# documented as one in README "Known limitations" - if this ever faced a network, the API would
# need the same session check, and every script would need a token.
SESSION_COOKIE = "robofetch_session"


def current_user(request: Request):
    """Whoever is logged in, or None. Never raises - callers decide what to do about it."""
    session = db.get_session(request.cookies.get(SESSION_COOKIE))
    return {"username": session["username"], "role": session["role"]} if session else None


def _login_redirect(request: Request):
    """Send an anonymous visitor to the login page, remembering where they wanted to go."""
    target = request.url.path
    suffix = f"?next={target}" if target not in ("/", "/login") else ""
    return RedirectResponse(f"/login{suffix}", status_code=303)


def require_user(request: Request):
    """Any logged-in role. Raises a redirect for anonymous visitors."""
    user = current_user(request)
    if user is None:
        raise _RedirectException(_login_redirect(request))
    return user


def require_admin(request: Request):
    """Administrators only - the catalogue and the database itself."""
    user = require_user(request)
    if user["role"] != ROLE_ADMIN:
        raise HTTPException(
            status_code=403,
            detail="administrator access required - you are signed in as a robot controller")
    return user


class _RedirectException(Exception):
    """Carries a redirect out of a dependency, which cannot simply return one."""

    def __init__(self, response):
        self.response = response


@app.exception_handler(_RedirectException)
async def _redirect_handler(request: Request, exc: _RedirectException):
    return exc.response


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
    # The ledger of work already accepted and not yet finished. Judging this order against the
    # live telemetry alone would let it be admitted on a battery the queue has already spent.
    committed = db.committed_load()
    decision, reason, decided_by, cost = admission.decide(
        product, delivery, station, state, robot_xy=ros.node.robot_xy,
        predictor=predictor, committed=committed)
    return product, delivery, decision, reason, decided_by, cost


def _return_cost():
    """What sending the robot home right now would cost. No verdict: it is always allowed."""
    return admission.estimate_return(db.get_station(), db.get_robot_state(),
                                     robot_xy=ros.node.robot_xy,
                                     committed=db.committed_load())


def _queue_return():
    """Create and dispatch a return task. Returns (order, error_message).

    Refuses to queue a second one while the first is outstanding - a double-clicked button
    would otherwise fill /orders with returns that all complete the moment the robot docks.
    """
    existing = db.active_return()
    if existing is not None:
        return existing, (f"a return to the station is already queued as task "
                          f"{existing['id']} ({existing['status']})")

    order = db.create_return_order(estimate=_return_cost())
    if ros.node.order_pub.get_subscription_count() == 0:
        return db.update_order(
            order["id"],
            detail="no robot is subscribed to /orders/new - task stored but NOT sent"), None

    ros.node.submit_return(order["id"], db.get_station())
    return db.get_order(order["id"]), None


# ------------------------------------------------------------------------ REST API
@app.get("/api/products")
def api_products(all: bool = False):
    """What can be ordered right now.

    Defaults to products still on their shelves, because that is what a client can actually
    ask for. `?all=true` returns the full catalogue including delivered items, which is what
    an administrator - and the acceptance suite - needs to see.
    """
    return db.list_products(available_only=not all)


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


@app.post("/api/orders/return", status_code=201)
def api_create_return():
    """UC10 - send the robot back to its station.

    A task, not a command: it queues behind whatever the robot is already doing. Never refused
    by admission control - see `admission.estimate_return` for why going home is the one job a
    struggling robot must always be allowed to do.
    """
    order, error = _queue_return()
    if error:
        raise HTTPException(status_code=409, detail=error)
    return order


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

# How often the live-state pages reload themselves, via <meta http-equiv="refresh">. Applied to
# /orders and /robot only - /preview is the result of a POST and reloading it would re-submit,
# and "/" is a form the operator is typing into.
#
# 2 s is fast enough that an order visibly moves through navigating -> grabbing -> delivering
# while you watch. It is affordable because every page is a handful of SQLite reads with no
# JavaScript and no polling API behind it.
PAGE_REFRESH_SECONDS = 2


def _css_version():
    """Cache-buster for the stylesheet, taken from its own modification time.

    Browsers hold on to a stylesheet hard, and StaticFiles serves it with a validator they are
    entitled to reuse - so an edit to the CSS could leave the operator staring at the previous
    design and reasonably concluding nothing had changed. Keying the query string to the file's
    mtime means any edit invalidates the cached copy automatically, with nothing to remember.
    """
    try:
        return int(os.path.getmtime(os.path.join(WEB_DIR, "static", "style.css")))
    except OSError:
        return 0


def _page_context(request):
    """Everything every page needs: the robot's condition, and who is looking at it.

    `user` drives which nav links appear, so a robot controller is never shown an Admin link
    they would only get a 403 from.
    """
    return {"request": request, "robot": db.get_robot_state(),
            "user": current_user(request), "css_version": _css_version()}


# ------------------------------------------------------------------- login / logout
@app.get("/login", response_class=HTMLResponse)
def page_login(request: Request, next: str = "/", error: str = None):
    context = {"request": request, "robot": db.get_robot_state(), "user": None,
               "next": next, "error": error, "css_version": _css_version()}
    return templates.TemplateResponse(request, "login.html", context, headers=NO_CACHE)


@app.post("/login")
def do_login(username: str = Form(...), password: str = Form(...),
             next: str = Form("/")):
    user = db.verify_user(username, password)
    if user is None:
        # One message for both a bad username and a bad password, so the form cannot be used to
        # find out which accounts exist.
        return RedirectResponse("/login?error=Wrong+username+or+password", status_code=303)
    token = db.create_session(user["username"], user["role"])
    response = RedirectResponse(next or "/", status_code=303)
    # httponly: no script can read it - and there is no script here anyway. samesite=lax is
    # enough to stop another site POSTing the emergency stop on the operator's behalf.
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", path="/")
    return response


@app.post("/logout")
def do_logout(request: Request):
    db.delete_session(request.cookies.get(SESSION_COOKIE))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/app")
@app.get("/app/{path:path}")
def legacy_dashboard(path: str = ""):
    """The v1 dashboard lived under /app. Send old links and bookmarks to the new UI."""
    return RedirectResponse("/", status_code=308)


@app.get("/", response_class=HTMLResponse)
def page_order_form(request: Request, user: dict = Depends(require_user)):
    """UC1 - the order form.

    Two lists on purpose: `products` is what the dropdown may offer, `catalogue` is everything.
    A delivered parcel therefore vanishes from the choices but stays visible in the table at
    zero stock, which answers "where did SKU-1001 go?" without the operator having to guess.
    """
    context = _page_context(request)
    context.update(products=db.list_products(available_only=True),
                   catalogue=db.list_products(),
                   deliveries=db.list_delivery_points())
    return templates.TemplateResponse(request, "order.html", context, headers=NO_CACHE)


@app.post("/preview", response_class=HTMLResponse)
def page_preview(request: Request, product_id: str = Form(...),
                 delivery_id: str = Form(...),
                 user: dict = Depends(require_user)):
    """UC2 - the verdict is shown BEFORE the order is committed."""
    product, delivery, decision, reason, decided_by, cost = _admit(product_id, delivery_id)
    context = _page_context(request)
    context.update(product=product, delivery=delivery, estimate=cost,
                   decision=decision, reason=reason, decided_by=decided_by,
                   accepted=decision == admission.ACCEPTED)
    return templates.TemplateResponse(request, "preview.html", context, headers=NO_CACHE)


@app.post("/confirm")
def page_confirm(product_id: str = Form(...), delivery_id: str = Form(...),
                 user: dict = Depends(require_user)):
    api_create_order(OrderRequest(product_id=product_id, delivery_id=delivery_id))
    return RedirectResponse("/orders", status_code=303)


@app.get("/return", response_class=HTMLResponse)
def page_return_confirm(request: Request, user: dict = Depends(require_user)):
    """Ask before moving the robot. GET shows the question, POST does it.

    Split that way on purpose: a GET can be reached from a plain link and can be reloaded or
    bookmarked harmlessly, while the thing that actually moves a robot is a POST that a browser
    will never replay on refresh. Same reason /preview exists before /confirm.
    """
    context = _page_context(request)
    context.update(estimate=_return_cost(), pending=db.active_return())
    return templates.TemplateResponse(request, "return.html", context, headers=NO_CACHE)


@app.post("/return")
def page_return(request: Request, user: dict = Depends(require_user)):
    """The operator said yes. Queue it and show them the queue."""
    _queue_return()
    return RedirectResponse("/orders", status_code=303)


# ------------------------------------------------------------------ emergency stop
# One button and one action, with an end. It abandons the running task, clears the queue and
# halts the robot where it stands - and then it is over. Nothing latches, so there is nothing to
# switch off afterwards and nothing that outlives the session that pressed it.
#
# No confirmation page, unlike /return: an emergency stop that asks "are you sure?" is not an
# emergency stop. The protection against pressing it by accident is where it sits on the page,
# not an extra click.
@app.post("/estop")
def page_estop(request: Request, user: dict = Depends(require_user)):
    ros.node.publish_estop("stop")
    return RedirectResponse("/orders", status_code=303)


@app.post("/api/estop")
def api_estop():
    """UC11 - abandon all work and halt the robot.

    `robot_listening` matters: publishing to a topic nobody subscribes to succeeds silently, so
    a stop button that reached no robot would look identical to one that worked.
    """
    listeners = ros.node.publish_estop("stop")
    return {"stopped": True, "robot_listening": listeners > 0}


# --------------------------------------------------------------------------- admin
@app.get("/admin", response_class=HTMLResponse)
def page_admin(request: Request, user: dict = Depends(require_admin)):
    context = _page_context(request)
    context.update(tables=db.table_summary(), users=db.list_users(),
                   stats=db.analytics())
    return templates.TemplateResponse(request, "admin.html", context, headers=NO_CACHE)


@app.get("/admin/products", response_class=HTMLResponse)
def page_admin_products(request: Request, edit: str = None, error: str = None,
                        user: dict = Depends(require_admin)):
    context = _page_context(request)
    context.update(products=db.list_products(), shelves=db.list_shelves(),
                   pick_points=db.list_pick_points(),
                   editing=db.get_product(edit) if edit else None, error=error)
    return templates.TemplateResponse(request, "admin_products.html", context,
                                      headers=NO_CACHE)


@app.post("/admin/products")
def do_admin_product(product_id: str = Form(...), name: str = Form(...),
                     category: str = Form(""), weight_kg: float = Form(...),
                     stock: int = Form(1), shelf_id: str = Form(...),
                     pick_point_id: str = Form(...), model_name: str = Form(...),
                     user: dict = Depends(require_admin)):
    """Create or update - the same form does both, which is why db.upsert_product exists."""
    db.upsert_product(product_id.strip(), name.strip(), category.strip(), weight_kg, stock,
                      shelf_id, pick_point_id, model_name.strip())
    return RedirectResponse("/admin/products", status_code=303)


@app.post("/admin/products/delete")
def do_admin_product_delete(product_id: str = Form(...),
                            user: dict = Depends(require_admin)):
    db.delete_product(product_id)
    return RedirectResponse("/admin/products", status_code=303)


@app.get("/admin/delivery-points", response_class=HTMLResponse)
def page_admin_points(request: Request, edit: str = None,
                      user: dict = Depends(require_admin)):
    context = _page_context(request)
    context.update(points=db.list_delivery_points(),
                   editing=db.get_delivery_point(edit) if edit else None)
    return templates.TemplateResponse(request, "admin_points.html", context,
                                      headers=NO_CACHE)


@app.post("/admin/delivery-points")
def do_admin_point(delivery_id: str = Form(...), name: str = Form(...),
                   x: float = Form(...), y: float = Form(...),
                   user: dict = Depends(require_admin)):
    db.upsert_delivery_point(delivery_id.strip(), name.strip(), x, y)
    return RedirectResponse("/admin/delivery-points", status_code=303)


@app.post("/admin/delivery-points/delete")
def do_admin_point_delete(delivery_id: str = Form(...),
                          user: dict = Depends(require_admin)):
    db.delete_delivery_point(delivery_id)
    return RedirectResponse("/admin/delivery-points", status_code=303)


@app.get("/admin/db", response_class=HTMLResponse)
def page_admin_db(request: Request, table: str = None, offset: int = 0,
                  user: dict = Depends(require_admin)):
    """Read-only browse of every table. Writes go through the CRUD pages, not this."""
    context = _page_context(request)
    context.update(tables=db.table_summary(), selected=None, offset=offset, limit=100)
    if table:
        try:
            context.update(selected=db.table_page(table, limit=100, offset=offset),
                           total=db.table_count(table))
        except ValueError as exc:
            context.update(error=str(exc))
    return templates.TemplateResponse(request, "admin_db.html", context, headers=NO_CACHE)


@app.post("/admin/users/password")
def do_admin_password(username: str = Form(...), password: str = Form(...),
                      role: str = Form(...), user: dict = Depends(require_admin)):
    if role not in ROLES or len(password) < 4:
        raise HTTPException(status_code=400,
                            detail="pick a valid role and a password of at least 4 characters")
    db.create_user(username.strip(), password, role)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/users/delete")
def do_admin_user_delete(username: str = Form(...),
                         user: dict = Depends(require_admin)):
    if username == user["username"]:
        raise HTTPException(status_code=400,
                            detail="you cannot delete the account you are signed in as")
    db.delete_user(username)
    return RedirectResponse("/admin", status_code=303)


@app.get("/orders", response_class=HTMLResponse)
def page_orders(request: Request, user: dict = Depends(require_user)):
    context = _page_context(request)
    context.update(orders=list(reversed(db.list_orders())),
                   stats=db.analytics(),
                   refresh_seconds=PAGE_REFRESH_SECONDS)
    return templates.TemplateResponse(request, "orders.html", context, headers=NO_CACHE)


@app.get("/robot", response_class=HTMLResponse)
def page_robot(request: Request, user: dict = Depends(require_user)):
    context = _page_context(request)
    context.update(stats=db.analytics(), telemetry=db.list_telemetry(limit=20),
                   ai=predictor.status(), refresh_seconds=PAGE_REFRESH_SECONDS)
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
