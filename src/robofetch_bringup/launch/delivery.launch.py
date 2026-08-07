"""RoboFetch bring-up - starts the whole system.

    Gazebo + robot + ros_gz bridge
      -> Nav2 + AMCL + map
      -> gripper node + robot condition monitor
      -> task manager
      -> web API (port 8000) + AI feasibility service (port 8001)

Orders arrive over HTTP; the task manager publishes the robot's initial pose itself, so no
RViz clicking is needed.

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

    # Robot condition model: battery, temperature, wear, and the per-run CSV log (C1, FR12).
    robot_state = Node(
        package="robofetch_core",
        executable="robot_state_node",
        name="robot_state_node",
        output="screen",
        parameters=[{"use_sim_time": use_sim_time}],
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
        parameters=[{"use_sim_time": use_sim_time}],
    )

    # The web tier runs on the VENV interpreter: FastAPI/uvicorn live there, while rclpy
    # comes from the sourced ROS environment (the venv was created with
    # --system-site-packages precisely so both are importable in one process).
    #
    # ROBOFETCH_WEB is what activates the UI: app.py loads its Jinja2 templates and CSS from
    # that directory. Without it the pages cannot render at all.
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

    # Feasibility model, deliberately a SEPARATE process on its own port: the main API has to
    # keep working when this is down, and a network boundary makes that failure real rather
    # than theoretical (NFR2).
    ai_service = ExecuteProcess(
        cmd=[os.path.expanduser("~/robofetch_ws/robofetch_venv/bin/python"),
             "-m", "uvicorn", "robofetch_ai.service:app",
             "--host", "0.0.0.0", "--port", "8001"],
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
        navigation,
        # Give Gazebo and Nav2 time to come up before the gripper starts watching poses
        # and the task manager begins issuing goals.
        TimerAction(period=8.0, actions=[ai_service]),
        TimerAction(period=12.0, actions=[gripper, robot_state]),
        TimerAction(period=18.0, actions=[task_manager]),
        TimerAction(period=8.0, actions=[api]),
    ])
