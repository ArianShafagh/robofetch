# RoboFetch — UML models

Diagrams for the project report. All are Mermaid, so they render on GitHub and stay editable as
text rather than being screenshots that drift out of date.

---

## 1. Use case diagram

Two actors. The **Client** submits and tracks deliveries; the **Administrator** maintains the
waypoint registry and reads statistics. Nav2 and Gazebo appear as a supporting system actor because
the proposal treats them as third-party services rather than parts we implement.

```mermaid
flowchart LR
    client(("Client"))
    admin(("Administrator"))
    sim(("Nav2 + Gazebo<br/>(third-party)"))

    UC1["UC1 · Submit a delivery order"]
    UC2["UC2 · Track order status"]
    UC3["UC3 · Schedule pending orders"]
    UC4["UC4 · Execute pick-and-place"]
    UC5["UC5 · Maintain waypoints"]
    UC6["UC6 · View analytics"]
    UC7["UC7 · Cancel a pending order"]

    client --- UC1
    client --- UC2
    client --- UC7
    admin --- UC5
    admin --- UC6

    UC1 -.->|includes| UC3
    UC3 -.->|includes| UC4
    UC4 --- sim
```

---

## 2. Class diagram

The logic tier is deliberately free of ROS imports where possible: `Order`, `scheduler` and `retry`
are plain Python, which is what makes them unit-testable without a simulator.

```mermaid
classDiagram
    class Order {
        +int order_id
        +str item
        +tuple pickup
        +tuple dropoff
        +OrderState state
        +int attempts
        +str detail
        +list history
        +set_state(state, detail)
        +finished() bool
    }

    class OrderState {
        <<enumeration>>
        PENDING
        NAVIGATING
        GRABBING
        RETRYING
        DELIVERING
        RELEASING
        COMPLETED
        FAILED
    }

    class TaskManager {
        -ActionClient nav_client
        -Client grab_client
        -tuple robot_position
        +execute_order(order) bool
        -_grab_with_retries(order) bool
        -_run_queue()
        -_publish_status(order)
    }

    class Scheduler {
        <<module>>
        +distance(a, b) float
        +pending_orders(orders) list
        +select_next_order(position, orders) Order
    }

    class RetryPolicy {
        <<module>>
        +decide_after_grab(ok, attempts, max) GrabDecision
        +backoff_seconds(attempt) float
        +next_state(decision) OrderState
    }

    class GripperNode {
        -dict poses
        -dict attach_state
        -str held_item
        +on_grab(request, response)
        +on_release(request, response)
        -_is_held(item) bool
        -_gap_to_gripper(item) float
    }

    class Database {
        +create_order(item, a, b) dict
        +update_order(id, ...) dict
        +cancel_order(id) tuple
        +list_locations() list
        +analytics() dict
    }

    class RosLink {
        +Publisher order_pub
        +dict robot_pose
        +dict item_poses
        +submit_order(id, item, pickup, dropoff)
    }

    class FastAPIApp {
        +create_order(request)
        +get_order(id)
        +cancel_order(id)
        +analytics()
        +telemetry_socket(ws)
    }

    Order --> OrderState
    TaskManager --> Order
    TaskManager ..> Scheduler : uses
    TaskManager ..> RetryPolicy : uses
    TaskManager ..> GripperNode : grab/release services
    FastAPIApp --> Database
    FastAPIApp --> RosLink
    RosLink ..> TaskManager : /orders/new, /orders/status
```

---

## 3. Sequence diagram — a delivery end to end

The path an order takes from HTTP request to a parcel physically on the delivery station.

```mermaid
sequenceDiagram
    actor C as Client
    participant API as FastAPI
    participant DB as SQLite
    participant TM as Task Manager
    participant SCH as Scheduler
    participant NAV as Nav2
    participant GR as Gripper
    participant GZ as Gazebo

    C->>API: POST /orders {item, point_a, point_b}
    API->>DB: create_order() → PENDING
    DB-->>API: order row
    API->>TM: publish /orders/new (names resolved to coordinates)
    API-->>C: 201 {id, status: pending}

    TM->>SCH: select_next_order(robot_position, pending)
    SCH-->>TM: nearest order

    TM->>API: /orders/status NAVIGATING
    API->>DB: update
    API-->>C: WebSocket push
    TM->>NAV: NavigateToPose(pickup)
    NAV->>GZ: /cmd_vel
    NAV-->>TM: SUCCEEDED

    TM->>API: /orders/status GRABBING
    TM->>GR: grab(item)
    GR->>GZ: publish attach
    GZ-->>GR: joint state "attached"
    GR-->>TM: success (authoritative, not proximity)

    TM->>API: /orders/status DELIVERING
    TM->>NAV: NavigateToPose(dropoff)
    NAV-->>TM: SUCCEEDED

    TM->>API: /orders/status RELEASING
    TM->>GR: release(item)
    GR->>GZ: publish detach

    TM->>GZ: read parcel world pose
    GZ-->>TM: (x, y)
    Note over TM: Confirm the parcel is really at the<br/>drop-off before claiming success
    TM->>API: /orders/status COMPLETED
    API->>DB: update + delivery_history
    API-->>C: WebSocket push
```

---

## 4. State machine — order lifecycle and the retry FSM

The shaded cycle is the grab-retry state machine (proposal "complex functionality" #2, FR4).

```mermaid
stateDiagram-v2
    [*] --> PENDING : order created
    PENDING --> CANCELLED : client cancels
    PENDING --> NAVIGATING : scheduler selects it

    NAVIGATING --> GRABBING : reached pickup
    NAVIGATING --> FAILED : cannot reach pickup

    GRABBING --> DELIVERING : joint reports "attached"
    GRABBING --> RETRYING : grab failed, attempts < 3
    GRABBING --> FAILED : grab failed, attempts = 3

    RETRYING --> NAVIGATING : back off, re-approach pickup
    note right of RETRYING
        Backoff 2 s, 4 s, 8 s.
        Re-approaching is the point:
        the usual cause is parking
        too far from the parcel, and
        waiting alone would just fail
        again identically.
    end note

    DELIVERING --> RELEASING : reached drop-off
    DELIVERING --> FAILED : cannot reach drop-off

    RELEASING --> COMPLETED : parcel confirmed at drop-off in Gazebo
    RELEASING --> FAILED : parcel is not where it should be

    COMPLETED --> [*]
    FAILED --> [*]
    CANCELLED --> [*]
```

---

## 5. Deployment / component diagram

Everything runs on one WSL2 host. The browser is the only component outside it — which is
deliberate, because serving the dashboard over HTTP sidesteps WSLg's GUI problems entirely.

```mermaid
flowchart TB
    subgraph win ["Windows 11 host"]
        BR["Browser<br/>localhost:8000"]
    end

    subgraph wsl ["WSL2 · Ubuntu 24.04 · ROS 2 Jazzy"]
        subgraph p1 ["Process: uvicorn"]
            API["FastAPI app<br/>REST + WebSocket"]
            RL["rclpy node<br/>(background thread)"]
        end
        FS[("robofetch.db<br/>SQLite file")]
        subgraph p2 ["Process: task_manager"]
            TM["Task Manager<br/>+ scheduler + retry FSM"]
        end
        subgraph p3 ["Process: gripper_node"]
            GR["Gripper services"]
        end
        subgraph p4 ["Nav2 processes"]
            NAV["amcl · planner · controller<br/>bt_navigator · costmaps"]
        end
        subgraph p5 ["Process: gz sim"]
            GZ["Gazebo Harmonic<br/>physics · lidar · DetachableJoint"]
        end
        BRIDGE["ros_gz_bridge"]
    end

    BR -->|HTTP + WS| API
    API --- FS
    API --- RL
    RL <-->|/orders/new<br/>/orders/status| TM
    TM <-->|NavigateToPose| NAV
    TM <-->|Grab.srv| GR
    NAV <-->|/scan /odom /cmd_vel /tf| BRIDGE
    GR <-->|attach /detach /state| BRIDGE
    BRIDGE <--> GZ
```

---

## 6. Database schema

```mermaid
erDiagram
    LOCATIONS {
        TEXT name PK
        REAL x
        REAL y
    }
    ORDERS {
        INTEGER id PK
        TEXT item
        TEXT point_a FK
        TEXT point_b FK
        TEXT status
        INTEGER retries
        TEXT detail
        REAL created_at
        REAL completed_at
    }
    DELIVERY_HISTORY {
        INTEGER order_id PK
        REAL duration
        REAL distance
        TEXT outcome
    }
    ROBOT_STATE {
        INTEGER id PK
        REAL x
        REAL y
        REAL battery
        TEXT status
        REAL last_update
    }

    LOCATIONS ||--o{ ORDERS : "pickup / drop-off"
    ORDERS ||--o| DELIVERY_HISTORY : "recorded on completion"
```
