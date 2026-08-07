"""Unit tests for the robot condition model (proposal C1, §10.2-10.3).

These pin the *relationships* the system is built on, not just arithmetic: heavier payloads
must cost more energy and run hotter, heat and wear must make the same job more expensive, and
a docked robot must recover. If any of these invert, the admission decisions built on top of
them become nonsense while still looking plausible.
"""
import pytest

from robofetch_core.robot_model import (CAPACITY_WH, RobotCondition, T_AMBIENT, T_MAX, T_REF,
                                        battery_percent_for, duration_s, efficiency_penalty,
                                        energy_wh, route_legs, simulate_route)


# --------------------------------------------------------------------- energy
def test_energy_scales_with_distance():
    a = energy_wh(5.0, 1.0)
    b = energy_wh(10.0, 1.0)
    assert b == pytest.approx(2 * a)


def test_heavier_payload_costs_more():
    light = energy_wh(10.0, 0.3)
    heavy = energy_wh(10.0, 4.5)
    assert heavy > light


def test_zero_distance_is_free():
    assert energy_wh(0.0, 4.5) == 0.0
    assert energy_wh(-3.0, 4.5) == 0.0


def test_heat_and_wear_both_make_work_more_expensive():
    nominal = energy_wh(10.0, 1.0, temperature_c=T_REF, condition_percent=100.0)
    hot = energy_wh(10.0, 1.0, temperature_c=60.0, condition_percent=100.0)
    worn = energy_wh(10.0, 1.0, temperature_c=T_REF, condition_percent=40.0)
    assert hot > nominal
    assert worn > nominal


def test_efficiency_penalty_never_rewards_heat_or_wear():
    """A cold, new robot is the best case; nothing may cost less than nominal."""
    assert efficiency_penalty(T_REF, 100.0) == pytest.approx(1.0)
    assert efficiency_penalty(0.0, 100.0) == pytest.approx(1.0)      # colder than ref: no bonus
    assert efficiency_penalty(80.0, 10.0) > 1.0


def test_battery_percent_conversion():
    assert battery_percent_for(CAPACITY_WH) == pytest.approx(100.0)


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


def test_temperature_rises_under_load_and_never_drops_below_ambient():
    condition = RobotCondition()
    for _ in range(30):
        condition.step(1.0, distance_m=0.2, payload_kg=1.0, motor_load=1.0)
    assert condition.temperature_c > T_AMBIENT

    cooling = RobotCondition(temperature_c=T_AMBIENT)
    cooling.step(30.0)
    assert cooling.temperature_c >= T_AMBIENT


def test_heavier_payload_runs_hotter():
    """The relationship the classifier has to learn - it must exist in the model first."""
    light, heavy = RobotCondition(), RobotCondition()
    for _ in range(60):
        light.step(1.0, distance_m=0.2, payload_kg=0.3, motor_load=1.0)
        heavy.step(1.0, distance_m=0.2, payload_kg=4.5, motor_load=1.0)
    assert heavy.temperature_c > light.temperature_c


def test_condition_degrades_and_never_goes_negative():
    condition = RobotCondition(condition_percent=0.05)
    for _ in range(20):
        condition.step(1.0, distance_m=1.0, payload_kg=4.5, motor_load=1.0)
    assert condition.condition_percent == 0.0


def test_a_zero_length_tick_changes_nothing():
    condition = RobotCondition()
    before = condition.as_dict()
    condition.step(0.0, distance_m=5.0, payload_kg=4.5)
    assert condition.as_dict() == before


# -------------------------------------------------------------- route helpers
def test_simulate_route_reports_a_peak_at_least_as_hot_as_the_start():
    start = RobotCondition(temperature_c=50.0)
    _, peak = simulate_route(start, 10.0, 1.0)
    assert peak >= 50.0


def test_simulate_route_does_not_mutate_the_input():
    """Admission control simulates hypothetical orders; it must not age the real robot."""
    start = RobotCondition(battery_percent=80.0)
    before = start.as_dict()
    simulate_route(start, 10.0, 4.5)
    assert start.as_dict() == before


def test_long_heavy_hauls_can_exceed_the_thermal_limit():
    """Without this the temperature gate could never fire and would be dead code."""
    _, peak = simulate_route(RobotCondition(temperature_c=60.0), 12.0, 4.5)
    assert peak >= T_MAX


def test_route_legs_add_up():
    legs = route_legs((0, 0), (3, 4), (3, 0), (0, 0))
    assert legs["to_pick_m"] == pytest.approx(5.0)
    assert legs["to_delivery_m"] == pytest.approx(4.0)
    assert legs["return_m"] == pytest.approx(3.0)
    assert legs["order_m"] == pytest.approx(9.0)
    assert legs["total_m"] == pytest.approx(12.0)


def test_duration_includes_the_pick_and_place_overhead():
    assert duration_s(0.0) > 0.0
    assert duration_s(10.0) > duration_s(5.0)
