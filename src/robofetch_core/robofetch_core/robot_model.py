"""Robot condition model - battery, temperature and wear (proposal §3.3 C1, §10).

Deliberately free of ROS imports so it can be unit-tested with plain pytest, and so the
disposable ML data generator in tools/ml/ can import the SAME equations that run on the live
robot. That shared definition is the point: a model trained on synthetic data is only
meaningful if the synthetic data was produced by the physics the real system uses.

Three quantities evolve together and feed back into each other:

    energy      rises with payload mass and distance, and with heat and wear
    temperature rises with motor load and payload, and decays toward ambient
    condition   degrades with energy throughput and with time spent overheated,
                and degraded condition then raises energy draw

That mutual coupling is what makes this a model rather than three independent counters.
"""
from dataclasses import dataclass, asdict

# ------------------------------------------------------------------ battery / energy
# Sized so a charge is worth about THREE deliveries. A larger pack would be more realistic for
# a real warehouse robot, but then battery would never bind inside a demo session and the
# accept/refuse decision would have nothing to refuse on: the whole point of C2 is visible only
# if the robot actually runs low while someone is watching.
#
# This constant is load-bearing well beyond this file. The ML training label depends on how much
# of the pack an order consumes, so CHANGING IT INVALIDATES model.joblib - regenerate and
# retrain (tools/ml/generate.py then tools/ml/train.py) whenever it moves.
CAPACITY_WH = 22.0           # nominal usable pack energy
E_BASE = 0.35                # Wh per metre, unloaded
E_LOAD = 0.08                # Wh per metre per kg of payload
T_REF = 25.0                 # temperature at which efficiency is nominal
ALPHA_TEMP = 0.004           # efficiency loss per degree above T_REF
BETA_HEALTH = 0.30           # extra draw of a fully worn robot vs a new one

# ------------------------------------------------------------------------- thermal
T_AMBIENT = 22.0
# Tuned so that payload weight genuinely decides whether the robot overheats. Steady-state
# temperature is T_AMBIENT + K_HEAT*load*(1 + GAMMA_PAYLOAD*m)/K_COOL, which at 70% load gives
# about 65 C for the lightest product and about 74 C for the heaviest - straddling T_MAX. A
# long haul with a heavy item therefore gets refused while the same trip with a light one does
# not, which is the relationship the classifier has to learn.
K_HEAT = 1.2                 # degC per second at full motor load
K_COOL = 0.02                # per second, Newtonian cooling toward ambient
GAMMA_PAYLOAD = 0.05         # extra heating per kg carried
T_WARN = 55.0                # above this, wear accelerates
T_MAX = 70.0                 # above this, the robot must stop and cool down
T_RESUME = 45.0              # cooled enough to work again

# ---------------------------------------------------------------------------- wear
W_ENERGY = 0.010             # condition % lost per Wh delivered
W_HEAT = 0.0005              # condition % lost per second per degC above T_WARN

# ------------------------------------------------------------------------ charging
CHARGE_RATE = 1.0            # battery % per second while docked
COOL_AT_STATION = 1.5        # cooling is more effective when parked and idle

# ------------------------------------------------------------------------- motion
# Effective speed INCLUDING planning, acceleration and cornering - measured from real runs,
# not the controller's peak velocity, because estimates are compared against wall-clock.
EFFECTIVE_SPEED = 0.18       # m/s
FIXED_OVERHEAD_S = 8.0       # grab + release settling per order
DRIVE_LOAD = 0.7             # typical motor load while driving, 0..1

# -------------------------------------------------------------- admission thresholds
RESERVE_PERCENT = 15.0       # charge that must remain AFTER returning to the station
CONDITION_MIN = 30.0         # below this the robot needs maintenance, not more work


def efficiency_penalty(temperature_c, condition_percent):
    """How much more energy the same work costs, given heat and wear.

    Both effects are multiplicative and never reduce the cost below nominal: a cold, new
    robot is the best case, everything else is worse.
    """
    thermal = 1.0 + ALPHA_TEMP * max(0.0, temperature_c - T_REF)
    health = 1.0 + BETA_HEALTH * max(0.0, 1.0 - condition_percent / 100.0)
    return thermal * health


def energy_wh(distance_m, payload_kg, temperature_c=T_REF, condition_percent=100.0):
    """Energy needed to move `distance_m` carrying `payload_kg`.

    This is the deterministic formula of proposal §10.2. It is used for the cost shown in the
    order preview, and it is also the fallback decision rule when the AI service cannot be
    reached (NFR2).
    """
    if distance_m <= 0:
        return 0.0
    base = E_BASE + E_LOAD * max(0.0, payload_kg)
    return distance_m * base * efficiency_penalty(temperature_c, condition_percent)


def duration_s(distance_m):
    """Wall-clock estimate for a route, including the fixed pick-and-place overhead."""
    return distance_m / EFFECTIVE_SPEED + FIXED_OVERHEAD_S


def battery_percent_for(energy, capacity_wh=CAPACITY_WH):
    """Convert Wh into percentage points of the pack."""
    return 100.0 * energy / capacity_wh


@dataclass
class RobotCondition:
    """The robot's physical condition, integrated forward one tick at a time."""

    battery_percent: float = 100.0
    temperature_c: float = T_AMBIENT
    condition_percent: float = 100.0
    cumulative_distance_m: float = 0.0
    cumulative_energy_wh: float = 0.0
    uptime_s: float = 0.0

    def step(self, dt, distance_m=0.0, payload_kg=0.0, motor_load=0.0, docked=False):
        """Advance the model by `dt` seconds.

        `motor_load` is 0..1. `docked` means parked at the station, where the robot charges
        and cools faster because the drive is off.
        """
        if dt <= 0:
            return self
        self.uptime_s += dt

        # --- energy drawn for the distance actually covered this tick
        used = energy_wh(distance_m, payload_kg, self.temperature_c, self.condition_percent)
        self.cumulative_distance_m += distance_m
        self.cumulative_energy_wh += used

        if docked:
            self.battery_percent = min(100.0, self.battery_percent + CHARGE_RATE * dt)
        else:
            self.battery_percent = max(0.0, self.battery_percent
                                       - battery_percent_for(used))

        # --- temperature: heating from the motors, Newtonian cooling toward ambient
        cooling = K_COOL * (COOL_AT_STATION if docked else 1.0)
        heating = 0.0 if docked else K_HEAT * motor_load * (1.0 + GAMMA_PAYLOAD * payload_kg)
        self.temperature_c += dt * (heating - cooling * (self.temperature_c - T_AMBIENT))
        self.temperature_c = max(T_AMBIENT, self.temperature_c)

        # --- wear: energy throughput plus time spent above the warning temperature
        overheat = max(0.0, self.temperature_c - T_WARN)
        self.condition_percent = max(
            0.0, self.condition_percent - (W_ENERGY * used + W_HEAT * overheat * dt))
        return self

    def as_dict(self):
        return asdict(self)


def simulate_route(condition, distance_m, payload_kg, dt=1.0, motor_load=DRIVE_LOAD):
    """Forward-simulate the condition model over a proposed route.

    Admission control needs more than "how many Wh": it needs to know whether the robot would
    OVERHEAT part-way, which a single energy number cannot tell you because temperature is a
    trajectory, not a total. Stepping the same model the live robot runs gives the peak.

    Returns (predicted_condition_at_the_end, peak_temperature_reached).
    """
    sim = RobotCondition(**condition.as_dict())
    total = duration_s(distance_m)
    speed = distance_m / total if total > 0 else 0.0
    peak = sim.temperature_c
    elapsed = 0.0
    while elapsed < total:
        step = min(dt, total - elapsed)
        sim.step(step, distance_m=speed * step, payload_kg=payload_kg,
                 motor_load=motor_load)
        peak = max(peak, sim.temperature_c)
        elapsed += step
    return sim, peak


def route_legs(robot_xy, pick_xy, delivery_xy, station_xy):
    """The three legs admission control reasons about.

    The return leg matters as much as the outward one: accepting a job the robot cannot come
    back from strands it, so it is costed even though the customer never sees it.
    """
    def dist(a, b):
        return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5

    to_pick = dist(robot_xy, pick_xy)
    to_delivery = dist(pick_xy, delivery_xy)
    to_station = dist(delivery_xy, station_xy)
    return {
        "to_pick_m": to_pick,
        "to_delivery_m": to_delivery,
        "return_m": to_station,
        "order_m": to_pick + to_delivery,          # what the customer's job costs
        "total_m": to_pick + to_delivery + to_station,
    }
