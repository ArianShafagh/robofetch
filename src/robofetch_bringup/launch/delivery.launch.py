"""RoboFetch delivery demo (M4).

Brings up the whole robotic stack and runs one delivery order end to end:

    Gazebo + robot + bridge  ->  Nav2 + AMCL + map  ->  gripper node  ->  task manager

The task manager publishes the robot's initial pose itself, so no RViz clicking is
needed; it then drives to A, grabs the item, drives to B and releases it.

    ros2 launch robofetch_bringup delivery.launch.py
    ros2 launch robofetch_bringup delivery.launch.py rviz:=false     # headless-ish
"""
import os

from launch import LaunchDescription
from launch.actions import (DeclareLaunchArgument, ExecuteProcess, GroupAction,
                            IncludeLaunchDescription, TimerAction)
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare
from launch.conditions import IfCondition


def generate_launch_description():
    use_sim_time = LaunchConfiguration("use_sim_time")
    orders = LaunchConfiguration("orders")
    use_rviz = LaunchConfiguration("rviz")
    gz_extra = LaunchConfiguration("gz_extra")

    pkg_nav = FindPackageShare("robofetch_nav")

    # Navigation stack (which itself starts the simulation).
    # Scoped so its internal launch arguments do not leak into this file's scope.
    navigation = GroupAction(
        actions=[
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    PathJoinSubstitution([pkg_nav, "launch", "navigation.launch.py"])
                ),
                launch_arguments={
                    "use_sim_time": use_sim_time,
                    "rviz": use_rviz,
                    "gz_extra": gz_extra,
                }.items(),
            )
        ],
        scoped=True,
        forwarding=True,
    )

    gripper = Node(
        package="robofetch_core",
        executable="gripper_node",
        name="gripper_node",
        output="screen",
        parameters=[{"use_sim_time": use_sim_time}],
    )

    task_manager = Node(
        package="robofetch_core",
        executable="task_manager",
        name="task_manager",
        output="screen",
        parameters=[{
            "use_sim_time": use_sim_time,
            "orders": orders,
            # Orders now arrive over HTTP, so the manager must stay alive when idle.
            "run_forever": True,
        }],
    )

    # The web tier runs on the VENV interpreter: FastAPI/uvicorn live there, while rclpy
    # comes from the sourced ROS environment (the venv was created with
    # --system-site-packages precisely so both are importable in one process).
    #
    # ROBOFETCH_WEB is what activates the dashboard: app.py mounts that directory at /app
    # and serves its index.html at /, but only if the variable is set and points somewhere
    # real. Without this the mount is simply dead code and localhost:8000 has no UI.
    api = ExecuteProcess(
        cmd=[os.path.expanduser("~/robofetch_ws/robofetch_venv/bin/python"),
             "-m", "uvicorn", "robofetch_bridge.app:app",
             "--host", "0.0.0.0", "--port", "8000"],
        additional_env={
            "ROBOFETCH_WEB": PathJoinSubstitution([FindPackageShare("robofetch_web"), "web"]),
        },
        output="screen",
        condition=IfCondition(LaunchConfiguration("api")),
    )

    return LaunchDescription([
        DeclareLaunchArgument("use_sim_time", default_value="true"),
        DeclareLaunchArgument("rviz", default_value="true"),
        DeclareLaunchArgument("api", default_value="true",
                              description="Run the FastAPI web service on port 8000."),
        DeclareLaunchArgument("gz_extra", default_value="",
                              description="Extra gz args, e.g. '-s --headless-rendering'."),
        # Override to stage acceptance tests, e.g. pointing a pickup away from its parcel
        # to force grab failures and exercise the retry state machine:
        #   orders:="['item_1,0.0,0.0,-3.1,-2.2']"
        DeclareLaunchArgument(
            "orders", default_value="",
            description=("Semicolon-separated orders, e.g. "
                         "'item_1,-2.5,0.95,-3.1,-2.2; item_3,2.75,-1.0,-2.6,-1.5'. "
                         "Empty by default - orders normally arrive via the web API.")),
        navigation,
        # Give Gazebo and Nav2 time to come up before the gripper starts watching poses
        # and the task manager begins issuing goals.
        TimerAction(period=12.0, actions=[gripper]),
        TimerAction(period=18.0, actions=[task_manager]),
        TimerAction(period=8.0, actions=[api]),
    ])
