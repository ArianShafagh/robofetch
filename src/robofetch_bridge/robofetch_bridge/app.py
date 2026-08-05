"""FastAPI service: the web tier of RoboFetch (proposal 4 and 5.2).

REST
    POST   /orders              submit a delivery order            (UC1)
    GET    /orders              list orders
    GET    /orders/{id}         one order's status                 (UC2)
    DELETE /orders/{id}         cancel an order that is still pending
    GET    /locations           the waypoint registry              (UC5)
    POST   /locations           add or update a waypoint           (UC5, admin)
    DELETE /locations/{name}    remove a waypoint                  (UC5, admin)
    GET    /analytics           aggregate statistics               (UC6, admin)
    GET    /robot               last known robot state

WebSocket
    /telemetry                  robot position + order state changes, pushed live (NFR1)

The process owns an rclpy node on a background thread, so an HTTP handler can publish an
order straight onto a ROS topic. Orders are stored in SQLite first and only then handed
to the robot: the database is the record of what was asked for, the robot reports back
what actually happened.
"""
import asyncio
import os

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from robofetch_bridge.db import Database
from robofetch_bridge.ros_link import RosThread

DB_PATH = os.environ.get("ROBOFETCH_DB", os.path.expanduser("~/robofetch_ws/robofetch.db"))
WEB_DIR = os.environ.get("ROBOFETCH_WEB", "")

app = FastAPI(title="RoboFetch", version="0.1.0")
db = Database(DB_PATH)


class OrderRequest(BaseModel):
    item: str
    point_a: str        # pickup waypoint NAME, e.g. "shelf_1"
    point_b: str        # drop-off waypoint NAME, e.g. "delivery_1"


class LocationRequest(BaseModel):
    name: str
    x: float
    y: float


class Telemetry:
    """Fan-out of live updates to every connected WebSocket client."""

    def __init__(self):
        self.clients = set()
        self.loop = None            # set once FastAPI's event loop is running

    def register_loop(self):
        self.loop = asyncio.get_running_loop()

    async def connect(self, ws):
        await ws.accept()
        self.clients.add(ws)

    def disconnect(self, ws):
        self.clients.discard(ws)

    def publish(self, message):
        """Called from the ROS thread, so hop onto the event loop to send."""
        if self.loop is None or not self.clients:
            return
        asyncio.run_coroutine_threadsafe(self._broadcast(message), self.loop)

    async def _broadcast(self, message):
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(message)
            except Exception:                                  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


telemetry = Telemetry()


def _on_status(payload):
    """An order changed state on the robot: persist it, then push it to clients."""
    db.update_order(payload["id"], status=payload.get("status"),
                    retries=payload.get("retries"), detail=payload.get("detail"))
    if payload.get("status") in ("completed", "failed"):
        order = db.get_order(payload["id"])
        if order and order.get("completed_at") and order.get("created_at"):
            db.record_history(order["id"],
                              duration=order["completed_at"] - order["created_at"],
                              distance=None, outcome=order["status"])
    db.update_robot_state(status=payload.get("status"))
    telemetry.publish({"type": "order", **payload})


def _on_pose(pose):
    db.update_robot_state(x=pose["x"], y=pose["y"])
    telemetry.publish({"type": "pose", **pose})


def _on_item(item):
    """A parcel moved in Gazebo. Not persisted - this is live ground truth, not a record."""
    telemetry.publish({"type": "item", **item})


ros = RosThread(on_status=_on_status, on_pose=_on_pose, on_item=_on_item)


@app.on_event("startup")
async def _startup():
    telemetry.register_loop()
    ros.start()


@app.on_event("shutdown")
async def _shutdown():
    ros.stop()


# ----------------------------------------------------------------------- orders
@app.post("/orders", status_code=201)
def create_order(request: OrderRequest):
    """UC1 - submit a delivery order."""
    try:
        order = db.create_order(request.item, request.point_a, request.point_b)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    pickup = db.get_location(request.point_a)
    dropoff = db.get_location(request.point_b)
    # Waypoint names are resolved to coordinates here so the robot side stays numeric.
    ros.node.submit_order(order["id"], order["item"],
                          (pickup["x"], pickup["y"]), (dropoff["x"], dropoff["y"]))

    # Publishing to a topic nobody subscribes to succeeds silently, so an order sent to a
    # robot that is not running looks exactly like one that was accepted. The database is
    # still the record of what was ASKED FOR - so keep the order, but say plainly that it
    # went nowhere instead of leaving it at 'pending' with an empty detail.
    if ros.node.order_pub.get_subscription_count() == 0:
        order = db.update_order(
            order["id"],
            detail="no robot is subscribed to /orders/new - order stored but NOT sent")

    telemetry.publish({"type": "order", **order})
    return order


@app.get("/orders")
def list_orders(status: str = None):
    return db.list_orders(status=status)


@app.get("/orders/{order_id}")
def get_order(order_id: int):
    """UC2 - track one order."""
    order = db.get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="no such order")
    return order


@app.delete("/orders/{order_id}")
def cancel_order(order_id: int):
    """Cancel an order that has not started yet."""
    order, error = db.cancel_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=error)
    if error:
        raise HTTPException(status_code=409, detail=error)
    telemetry.publish({"type": "order", **order})
    return order


# -------------------------------------------------------------------- locations
@app.get("/locations")
def list_locations():
    return db.list_locations()


@app.post("/locations", status_code=201)
def upsert_location(request: LocationRequest):
    """UC5 - the administrator maintains the waypoint registry."""
    return db.upsert_location(request.name, request.x, request.y)


@app.delete("/locations/{name}")
def delete_location(name: str):
    if not db.delete_location(name):
        raise HTTPException(status_code=404, detail="no such location")
    return {"deleted": name}


# --------------------------------------------------------------- state & analytics
@app.get("/robot")
def robot_state():
    state = db.get_robot_state()
    state.update(ros.node.robot_pose)      # freshest position, straight from AMCL
    return state


@app.get("/analytics")
def analytics():
    """UC6 - aggregate delivery statistics."""
    return db.analytics()


@app.get("/health")
def health():
    """Is this API actually wired to a robot?

    "I submitted an order and nothing happened" has several causes that look identical
    from the client: the order comes back as valid JSON either way. The most confusing is
    a STALE api left holding port 8000 - it serves HTTP perfectly while being connected to
    a different database and no running robot, so the real launch's API silently died with
    "address already in use" and every order disappears into the old process.

    The live subscriber count on /orders/new distinguishes them in one request: it is
    non-zero only when a task manager is really listening to THIS process.
    """
    subscribers = ros.node.order_pub.get_subscription_count()
    return {
        "status": "ok",
        "database": DB_PATH,
        "robot_connected": subscribers > 0,
        "orders_new_subscribers": subscribers,
    }


# ------------------------------------------------------------------- telemetry
@app.websocket("/telemetry")
async def telemetry_socket(ws: WebSocket):
    """Live robot position and order updates (NFR1: within 1 s of a change)."""
    await telemetry.connect(ws)
    try:
        # One snapshot on connect means the page renders a complete picture immediately,
        # instead of staying blank until the next thing happens to change.
        await ws.send_json({"type": "snapshot",
                            "orders": db.list_orders(),
                            "robot": db.get_robot_state(),
                            "locations": db.list_locations(),
                            "items": ros.node.item_poses})
        while True:
            await ws.receive_text()        # kept open; clients need not send anything
    except WebSocketDisconnect:
        telemetry.disconnect(ws)
    except Exception:                                          # noqa: BLE001
        telemetry.disconnect(ws)


# The dashboard (M8) is served from here when the directory exists.
if WEB_DIR and os.path.isdir(WEB_DIR):
    # follow_symlink=True is REQUIRED here, not optional tidiness.
    #
    # We build with `colcon build --symlink-install`, which installs every file in
    # share/robofetch_web/web as a SYMLINK back to src/. Starlette's StaticFiles defaults
    # to resolving each request through os.path.realpath and rejecting anything that lands
    # outside the mounted directory - a sensible guard against path traversal, but it also
    # rejects our symlinks, because their real paths live in src/.
    #
    # The failure is quietly misleading: "/" still works (FileResponse does no such check)
    # so the page loads, while every /app/*.css and /app/*.js returns 404 - a completely
    # unstyled, non-functioning dashboard that looks like a frontend bug.
    app.mount("/app", StaticFiles(directory=WEB_DIR, html=True, follow_symlink=True),
              name="web")

    @app.get("/")
    def index():
        return FileResponse(os.path.join(WEB_DIR, "index.html"))


def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("ROBOFETCH_PORT", 8000)))


if __name__ == "__main__":
    main()
