#!/usr/bin/env python3
"""Capture screenshots of the running web application for the report.

    ./robofetch_venv/bin/python tools/report/capture_screens.py

Requires the system to be running (./scripts/run.sh) and Playwright's chromium
to be installed. Drives a real browser through the real interface: nothing here
is mocked up, and the refusal screenshot is produced by genuinely draining the
robot rather than by editing the database.

Output goes to report/Figures/shot-*.png.
"""
import pathlib
import sys
import time
import urllib.request
import json

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
FIG = ROOT / "report" / "Figures"
BASE = "http://localhost:8000"
VIEWPORT = {"width": 1180, "height": 900}


def api(path, method="GET", body=None):
    request = urllib.request.Request(
        f"{BASE}{path}", method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Content-Type": "application/json"} if body else {})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read() or b"null")


def wait_for_order(order_id, timeout=420):
    deadline = time.time() + timeout
    while time.time() < deadline:
        order = api(f"/api/orders/{order_id}")
        if order["status"] in ("completed", "failed", "refused", "cancelled"):
            return order
        time.sleep(3)
    return api(f"/api/orders/{order_id}")


def shoot(page, url, name, full=True):
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(400)
    path = FIG / name
    page.screenshot(path=str(path), full_page=full)
    print(f"  {name:34} {path.stat().st_size / 1024:6.1f} kB")


def preview(page, product, delivery, name):
    """Fill the real form and submit it, so the screenshot is of the real page."""
    page.goto(BASE + "/", wait_until="networkidle")
    page.select_option("#product_id", product)
    page.select_option("#delivery_id", delivery)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(300)
    path = FIG / name
    page.screenshot(path=str(path), full_page=True)
    verdict = "refused" if "refuses" in page.content() else "accepted"
    print(f"  {name:34} {path.stat().st_size / 1024:6.1f} kB   ({verdict})")
    return verdict


def main():
    FIG.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=2)

        print("capturing the interface")
        shoot(page, BASE + "/", "shot-order-form.png")
        preview(page, "SKU-3001", "delivery_1", "shot-preview-accepted.png")

        # Run real deliveries, heaviest first so the robot heats up and drains fastest.
        # Each product is ordered ONCE: a delivered parcel stays at the bay, so
        # re-ordering the same one just sends the robot to an empty shelf.
        print("\nrunning real deliveries, heaviest product first")
        plan = [("SKU-3001", "delivery_1"), ("SKU-2001", "delivery_2"),
                ("SKU-1001", "delivery_1"), ("SKU-2002", "delivery_2"),
                ("SKU-1002", "delivery_1"), ("SKU-3002", "delivery_2")]
        for product, destination in plan:
            state = api("/api/robot")
            print(f"  battery {state['battery_percent']:.1f}%  "
                  f"temp {state['temperature_c']:.1f} C  ->  {product}")
            check = api("/api/preview", "POST",
                        {"product_id": product, "delivery_id": destination})
            if check["decision"] == "refused":
                print(f"  REFUSED: {check['reason']}")
                break
            order = api("/api/orders", "POST",
                        {"product_id": product, "delivery_id": destination})
            final = wait_for_order(order["id"])
            print(f"  order {order['id']} ({product}) -> {final['status']}")
        else:
            print("  every product delivered; asking for the heaviest again to")
            print("  provoke a refusal from a hot, part-drained robot")

        preview(page, "SKU-3001", "delivery_1", "shot-preview-refused.png")

        print("\ncapturing the remaining pages")
        shoot(page, BASE + "/orders", "shot-orders-page.png")
        shoot(page, BASE + "/robot", "shot-robot-page.png")
        browser.close()
    print("\ndone")
    return 0


if __name__ == "__main__":
    sys.exit(main())
