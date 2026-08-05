"""M2 step 2 — AUTONOMOUS NAVIGATION.

Starts the simulation + Nav2 (map_server, AMCL, planner, controller, recoveries) + RViz
with the previously saved maze map.

In RViz:
  1. "2D Pose Estimate" -> click/drag where the robot actually is (it spawns near marker A).
  2. "2D Goal Pose"     -> click/drag a destination; the robot plans and drives there.
"""
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, GroupAction, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    use_sim_time = LaunchConfiguration("use_sim_time")
    map_yaml = LaunchConfiguration("map")
    params_file = LaunchConfiguration("params_file")
    use_rviz = LaunchConfiguration("rviz")

    pkg_nav = FindPackageShare("robofetch_nav")
    pkg_gazebo = FindPackageShare("robofetch_gazebo")
    pkg_nav2_bringup = FindPackageShare("nav2_bringup")

    default_map = PathJoinSubstitution([pkg_nav, "maps", "warehouse.yaml"])
    default_params = PathJoinSubstitution([pkg_nav, "config", "nav2_params.yaml"])
    rviz_config = PathJoinSubstitution([pkg_nav, "rviz", "nav.rviz"])

    # 1) Simulation (Gazebo + robot + ros_gz bridge). We start RViz ourselves below with
    #    the navigation config, so the sim's own RViz is disabled.
    #    NOTE: wrapped in a scoped GroupAction. Without scoping, the launch arguments
    #    passed here leak into this file's scope, so `rviz:=false` would also switch off
    #    OUR RViz node below.
    sim = GroupAction(
        actions=[
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    PathJoinSubstitution([pkg_gazebo, "launch", "sim.launch.py"])
                ),
                launch_arguments={"use_sim_time": use_sim_time, "rviz": "false"}.items(),
            )
        ],
        scoped=True,
        forwarding=True,
    )

    # 2) Nav2 stack (localization + navigation), using the stock bringup with our params.
    nav2 = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            PathJoinSubstitution([pkg_nav2_bringup, "launch", "bringup_launch.py"])
        ),
        launch_arguments={
            "map": map_yaml,
            "use_sim_time": use_sim_time,
            "params_file": params_file,
            "autostart": "true",
            "use_composition": "False",
        }.items(),
    )

    rviz = Node(
        package="rviz2",
        executable="rviz2",
        output="screen",
        arguments=["-d", rviz_config],
        parameters=[{"use_sim_time": use_sim_time}],
        condition=IfCondition(use_rviz),
    )

    return LaunchDescription([
        DeclareLaunchArgument("use_sim_time", default_value="true"),
        DeclareLaunchArgument("map", default_value=default_map),
        DeclareLaunchArgument("params_file", default_value=default_params),
        DeclareLaunchArgument("rviz", default_value="true",
                              description="Set false to run navigation headless."),
        sim,
        nav2,
        rviz,
    ])
