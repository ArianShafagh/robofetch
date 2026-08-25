"""Unit tests for the robot condition model (proposal C1, §10.2-10.3).

These pin the *relationships* the system is built on, not just arithmetic: a heavier payload
must cost more energy, driving must drain the battery, a docked robot must recover, and the
charge must never leave 0-100%. If any of those invert, the admission decisions built on top
of them become nonsense while still looking plausible.

A deliberately small, representative set - see the testing chapter of the report.
"""
import pytest

from robofetch_core.robot_model import RobotCondition, energy_wh


# --------------------------------------------------------------------- energy


def test_heavier_payload_costs_more():
    light = energy_wh(10.0, 0.3)
    heavy = energy_wh(10.0, 4.5)
    assert heavy > light


# -------------------------------------------------------------------- dynamics
def test_driving_drains_the_battery():
    condition = RobotCondition()
    condition.step(1.0, distance_m=1.0, payload_kg=2.0, motor_load=0.8)
    assert condition.battery_percent < 100.0
    assert condition.cumulative_distance_m == pytest.approx(1.0)


def test_docked_robot_charges_and_cools():
    condition = RobotCondition(battery_percent=50.0, temperature_c=60.0)
    condition.step(10.0, docked=True)
    assert condition.battery_percent > 50.0
    assert condition.temperature_c < 60.0


def test_battery_never_leaves_its_bounds():
    empty = RobotCondition(battery_percent=0.5)
    empty.step(5.0, distance_m=50.0, payload_kg=4.5, motor_load=1.0)
    assert empty.battery_percent == 0.0

    full = RobotCondition(battery_percent=99.9)
    full.step(60.0, docked=True)
    assert full.battery_percent == 100.0
