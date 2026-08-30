# Installing RoboFetch from zero

How to get RoboFetch running on a machine that has nothing on it yet.

This is the **setup** document. Once the system is running, `README.md` explains how to use it and
`HANDOVER.md` explains how it works and every problem hit while building it.

**Expect 60–90 minutes**, most of it waiting for downloads. ROS 2 and Gazebo are large.

---

## 0. What you are installing

Eight processes that start together: a physics simulator, a navigation stack, three robot nodes, a
web application, a prediction service, and a visualiser.

| Layer | Software | Where it comes from |
|---|---|---|
| Simulator | Gazebo Harmonic (`gz-sim` 8) | apt |
| Navigation | ROS 2 Jazzy + Nav2 | apt |
| Robot logic, web tier, AI service | RoboFetch itself | this repository |
| Python libraries | FastAPI, scikit-learn, … | pip, into a virtual environment |

---

## 1. Platform

The reference platform, and the one everything below is verified against:

- **Windows 11 + WSL2, Ubuntu 24.04 LTS**
- Python 3.12
- ROS 2 **Jazzy**
- Gazebo **Harmonic**

Native Ubuntu 24.04 works too, and is simpler — skip section 2 entirely. Other Ubuntu releases are
not recommended: ROS 2 Jazzy targets 24.04, and mixing releases means building ROS from source.

**Hardware.** A physics simulation, a navigation stack and a web application run at the same time.
Four cores and 8 GB of RAM is a realistic floor; the project's own notes record navigation goals
aborting on a heavily loaded machine. A GPU is not required.

---

## 2. WSL2 (Windows only)

In PowerShell **as Administrator**:

```powershell
wsl --install -d Ubuntu-24.04
```

Reboot when asked, then open Ubuntu and create your Linux user.

### 2.1 The shared-memory fix — do this before the first graphical launch

WSLg has a known bug where, if `/mnt/shared_memory` does not exist, it silently falls back to a
mode that delivers no pixels. Gazebo and RViz then open as blank windows, or do not appear at all,
with `[WARN:COPY MODE]` in the window title. It is not a graphics-driver problem and reinstalling
drivers will not fix it.

Add the mount:

```bash
echo 'tmpfs /mnt/shared_memory tmpfs defaults 0 0' | sudo tee -a /etc/fstab
```

Then, from **PowerShell**:

```powershell
wsl --shutdown
```

This must be a full shutdown. The display mode is negotiated once when WSLg starts, so mounting it
inside a running session has no effect.

Verify after restarting Ubuntu:

```bash
mount | grep shared_memory
```

One line of output means it worked.

---

## 3. ROS 2 Jazzy

The official instructions are authoritative and occasionally change how the apt key is added:
<https://docs.ros.org/en/jazzy/Installation/Ubuntu-Install-Debs.html>

At the time of writing:

```bash
sudo apt update && sudo apt install -y software-properties-common curl
sudo add-apt-repository universe -y
```

```bash
sudo curl -sSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.key -o /usr/share/keyrings/ros-archive-keyring.gpg
```

```bash
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] http://packages.ros.org/ros2/ubuntu $(. /etc/os-release && echo $UBUNTU_CODENAME) main" | sudo tee /etc/apt/sources.list.d/ros2.list > /dev/null
```

```bash
sudo apt update && sudo apt install -y ros-jazzy-desktop
```

Check it:

```bash
source /opt/ros/jazzy/setup.bash && ros2 --help
```

> **Do not** add `source /opt/ros/jazzy/setup.bash` to `.bashrc` yet. This project needs two things
> sourced in a specific order, and `scripts/run.sh` does both for you. Auto-sourcing only one of
> them is a common cause of confusing errors.

---

## 4. Gazebo Harmonic

```bash
sudo curl -sSL https://packages.osrfoundation.org/gazebo.gpg -o /usr/share/keyrings/pkgs-osrf-archive-keyring.gpg
```

```bash
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/pkgs-osrf-archive-keyring.gpg] http://packages.osrfoundation.org/gazebo/ubuntu-stable $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/gazebo-stable.list > /dev/null
```

```bash
sudo apt update && sudo apt install -y gz-harmonic
```

This is the modern `gz sim`, **not** Gazebo Classic. Classic is end-of-life and unavailable on
Jazzy; the project uses Harmonic's `DetachableJoint` plugin for the gripper, which has no Classic
equivalent here.

### 4.1 When the Gazebo window will not open — the most common problem on WSL

Gazebo's graphical interface is the single least reliable part of this stack on Windows. It is
worth knowing that **none of these are Gazebo bugs and none are fixed by reinstalling graphics
drivers** — they are all about how a Linux window reaches a Windows screen.

There are three separate failures and they look similar, so identify yours before changing
anything:

| What you see | Which problem | Fix |
|---|---|---|
| Neither Gazebo nor RViz appears, or both are blank; `[WARN:COPY MODE]` in a window title | WSLg is delivering no pixels at all | Section 2.1 |
| **RViz opens fine, but the Gazebo window never appears at all** | Qt platform mismatch | Below, 4.1a |
| A window opens but its contents are black or empty | GPU rendering path failing | Below, 4.1b |

The middle one is the common one, and the giveaway is that **RViz works while Gazebo does not**.

#### 4.1a Gazebo's window never appears, but the process is running

**Symptom.** You launch, the terminal shows Gazebo starting normally, no error is printed, the
process appears in `ps` and keeps running — and no window ever opens. RViz, launched by the same
command, opens perfectly.

**Cause.** Gazebo's interface is built with Qt Quick (QML), and under WSLg its Wayland window is
created but never registers with the compositor, so nothing is ever shown. RViz is built with Qt
Widgets, which takes a different path, and is unaffected — which is exactly why one appears and the
other does not. This asymmetry is the diagnostic: if both were missing you would be looking at
section 2.1 instead.

**Fix.** Force Qt to use X11 through XWayland rather than Wayland directly:

```bash
export QT_QPA_PLATFORM=xcb
```

**This project already does it for you.** The variable is set inside the simulation launch file
itself, so it applies however you start the system rather than depending on your shell:

```python
# src/robofetch_gazebo/launch/sim.launch.py
SetEnvironmentVariable("QT_QPA_PLATFORM", "xcb"),
```

It is harmless on native Linux, so it is set unconditionally. You only need to export it by hand if
you run `gz sim` directly rather than through the project's launch files.

#### 4.1b A window opens but renders blank or black

**Symptom.** The window frame appears and can be moved and resized, but the scene inside is black
or empty.

**Cause.** WSL renders through a Direct3D 12 Mesa driver that maps OpenGL onto your Windows GPU.
That path works on most machines and fails on some, depending on the GPU and driver version.

**Fix.** Fall back to software rendering, which is slower but works everywhere:

```bash
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=llvmpipe
```

Set those in the terminal before launching. Expect a noticeably lower frame rate — llvmpipe renders
on the CPU — but the scene will display.

#### 4.1c The option that avoids the problem entirely

None of the above is needed to run the system, only to watch it. The simulator runs perfectly well
with no graphical output at all:

```bash
cd ~/robofetch_ws && ./scripts/run.sh --headless
```

This is the more reliable way to work on Windows, and it is worth knowing that it is also the
*faster* one. Running Gazebo's interface and RViz together competes for CPU with the navigation
stack, and on a loaded machine that can be enough to stop the robot localising at all. If
deliveries succeed headless but fail with the interface open, you are looking at CPU starvation
rather than a broken installation.

You can still see everything the system is doing while headless: the web interface at
**http://localhost:8000** reports the robot's condition and every order's progress.

---

## 5. The remaining ROS packages

```bash
sudo apt install -y \
  ros-jazzy-navigation2 ros-jazzy-nav2-bringup \
  ros-jazzy-ros-gz ros-jazzy-ros-gz-sim ros-jazzy-ros-gz-bridge \
  ros-jazzy-rviz2 ros-jazzy-xacro \
  ros-jazzy-robot-state-publisher ros-jazzy-joint-state-publisher \
  python3-colcon-common-extensions python3-venv git
```

`ros-jazzy-slam-toolbox` is optional — the project navigates from a map generated out of the world
geometry and does not run SLAM, but the package is useful if you want to experiment.

---

## 6. Get the source

```bash
git clone git@github.com:ArianShafagh/robofetch.git ~/robofetch_ws
```

Use the HTTPS URL instead if you have no SSH key on this machine:

```bash
git clone https://github.com/ArianShafagh/robofetch.git ~/robofetch_ws
```

The path matters less than being consistent, but `~/robofetch_ws` is what every document assumes.

If the scripts are not executable — which happens when the code arrives as a zip rather than a
clone:

```bash
chmod +x ~/robofetch_ws/scripts/*.sh ~/robofetch_ws/scripts/*.py
```

### What the repository already contains

You do **not** need to generate these; they are committed:

- the trained classifier, `src/robofetch_ai/robofetch_ai/models/model.joblib`
- the navigation map, `src/robofetch_nav/maps/warehouse.pgm` and `.yaml`
- the world, the robot description and all configuration

### What gets created on your machine

Not in the repository, produced locally:

- `robofetch_venv/` — you create it in the next step
- `build/`, `install/`, `log/` — produced by the build
- `robofetch.db` — created on first launch and reset on every launch
- `logs/run_*.csv` — one telemetry file per launch

---

## 7. Python environment

The virtual environment **must** be created with `--system-site-packages`. ROS 2's Python bindings
(`rclpy`) are not installed by pip — they live on `PYTHONPATH` and only appear after sourcing ROS.
A sealed virtual environment cannot see them, and the web tier imports `rclpy` and FastAPI in the
same process.

```bash
cd ~/robofetch_ws && python3 -m venv --system-site-packages robofetch_venv
```

```bash
./robofetch_venv/bin/pip install --upgrade pip
```

```bash
./robofetch_venv/bin/pip install fastapi uvicorn jinja2 python-multipart scikit-learn joblib
```

That is everything needed to **run** the system. `numpy` and `scipy` arrive automatically as
scikit-learn dependencies, and `pydantic` and `starlette` as FastAPI's. The rest is optional:

```bash
# to run the automated tests
./robofetch_venv/bin/pip install pytest httpx

# to retrain the classifier (pandas) or rebuild the report charts (matplotlib)
./robofetch_venv/bin/pip install pandas matplotlib
```

There is deliberately no `requirements.txt`; if you want reproducible pinning, these are the
versions the system is known to work with:

| Package | Known-good |
|---|---|
| fastapi | 0.141.1 |
| uvicorn | 0.52.1 |
| starlette | 1.3.1 |
| jinja2 | 3.1.2 |
| python-multipart | 0.0.32 |
| scikit-learn | 1.9.0 |
| joblib | 1.5.3 |
| numpy | 1.26.4 |
| pandas | 3.0.5 |
| matplotlib | 3.6.3 |
| pytest | 7.4.4 |
| httpx | 0.28.1 |

Passwords are hashed with the standard library, so nothing needs installing for authentication.

---

## 8. Choose passwords before the first launch

Two accounts are seeded the first time the database is created. If you set nothing, they are
`admin`/`admin` and `controller`/`controller`, and the launch console prints a warning every time
until they change.

To set your own, export these **before the first launch**:

```bash
export ROBOFETCH_ADMIN_PASSWORD='choose-something'
export ROBOFETCH_CONTROLLER_PASSWORD='choose-something-else'
```

Accounts survive later launches, so changing these afterwards has no effect — change the password
from the admin page instead.

---

## 9. Build and launch

```bash
cd ~/robofetch_ws && ./scripts/run.sh
```

This does everything: stops leftovers, checks the ports are free, sources ROS, builds with
`colcon build --symlink-install`, sources the workspace, and launches all eight processes. It works
from a completely unsourced shell — no `source` needed first.

The first build takes several minutes. Later launches are much faster, and `--no-build` skips the
build entirely when nothing has changed.

Useful variants:

```bash
./scripts/run.sh --headless    # no Gazebo window, no RViz - faster and more reliable
./scripts/run.sh --no-build    # skip the build
```

**Wait for this line before ordering anything:**

```
[task_manager]: Localized and ready - orders submitted now will execute.
```

Orders submitted before it appears are accepted by the web application and never execute.

---

## 10. Verify the installation

**The system is up.** In a second terminal:

```bash
curl -s localhost:8000/health
```

Expect `"robot_connected": true` and `"orders_new_subscribers": 1`.

**The prediction service loaded its model:**

```bash
curl -s localhost:8001/health
```

Expect `"model_loaded": true`. If this says `false`, the system still works but every admission
decision silently falls back to the policy-only path without the classifier.

**The web application answers.** Open **http://localhost:8000**.

On WSL, open it in the **Windows** browser, not one inside Linux. That bypasses WSLg completely, so
the interface works even when Gazebo's own window does not.

**The tests pass** (no simulator needed):

```bash
cd ~/robofetch_ws && source install/setup.bash && ./robofetch_venv/bin/python -m pytest src/robofetch_core/test/ src/robofetch_bridge/test/ -q
```

**A delivery physically happens.** This is the only check that proves the whole stack works,
because it compares the parcel's real position in the simulator against the delivery bay rather
than trusting a status field:

```bash
cd ~/robofetch_ws && ./scripts/order.sh SKU-3001 delivery_1
```

It ends in **VERIFIED** or **NOT DELIVERED**.

---

## 11. Shutting down

Press `Ctrl+C` in the launch terminal, then **always**:

```bash
cd ~/robofetch_ws && ./scripts/stop.sh
```

This matters more than it looks. A killed launch leaves processes alive that break the *next* run
in ways that appear unrelated — see the troubleshooting table below.

To check for survivors while the system is running:

```bash
./scripts/stop.sh --check
```

Anything it lists is a process that a future stop would miss.

---

## 12. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Both Gazebo and RViz blank or absent; `[WARN:COPY MODE]` | WSLg fell back to copy mode | Section 2.1, then `wsl --shutdown` |
| RViz opens but Gazebo never appears | Qt Quick window not registering under Wayland | Section 4.1a — `QT_QPA_PLATFORM=xcb` |
| Window opens but the scene is black | WSL's GPU rendering path failing | Section 4.1b — software rendering |
| `address already in use` on 8000 or 8001 | A web service survived the last run | `./scripts/stop.sh`, then relaunch |
| Orders accepted but nothing moves | A stale API on port 8000 answering from an old database, or the robot never localized | `curl localhost:8000/health` — if `robot_connected` is false, stop everything and relaunch |
| `Failed to bring up all requested nodes` | An orphan navigation process from an earlier run holds a node name | `./scripts/stop.sh`, then relaunch |
| RViz shows no frames; planner reports no `map` | Same orphan problem | As above |
| `Detected jump back in time` | Two simulators publishing the clock | `./scripts/stop.sh` |
| Localization never succeeds with the GUI open | Gazebo and RViz together starve the sensor pipeline on a loaded machine | Use `--headless` |
| `rclpy` not found | Virtual environment created without `--system-site-packages`, or ROS not sourced | Recreate the venv per section 7 |
| Goals abort intermittently | CPU starvation | Close other work, or run headless |

**Never use `pkill` or `killall`** to clean up. Kill by PID, or use `./scripts/stop.sh`, which knows
the full set of processes to look for.

---

## 13. Optional extras

**Report diagrams.** Rendering the UML sources needs Java and PlantUML:

```bash
sudo apt install -y default-jre
```

`plantuml.jar` is not in the repository; download it into `tools/report/` from
<https://plantuml.com/download> if you need to re-render the diagrams.

**The written report** targets Overleaf and needs no local LaTeX. See `report/README.md`.

---

## 14. Starting over

A clean rebuild without touching anything you installed system-wide:

```bash
cd ~/robofetch_ws && ./scripts/stop.sh && rm -rf build install log && ./scripts/run.sh
```

To also discard runtime state — orders, history, telemetry and **user accounts**:

```bash
rm -f ~/robofetch_ws/robofetch.db
```

The database is recreated with the seeded catalogue on the next launch. Note that this resets any
changed passwords back to the defaults from section 8.

To remove everything the project put on the machine, delete `~/robofetch_ws`. ROS 2 and Gazebo were
installed through apt and are removed with `apt remove` if you want them gone as well.
