"""Robot State Publisher launch.

Expands the xacro to URDF and publishes the robot's link transforms (TF) plus the
/robot_description topic. Other launches (sim, bringup) include this so there is one
single source of truth for the robot model.
"""
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import Command, LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    use_sim_time = LaunchConfiguration("use_sim_time")

    xacro_path = PathJoinSubstitution(
        [FindPackageShare("robofetch_description"), "urdf", "robofetch.urdf.xacro"]
    )
    # `xacro <file>` prints the expanded URDF to stdout; Command captures it.
    # Wrap in ParameterValue(str) so launch treats the URDF as a plain string
    # instead of trying to parse it as YAML (which fails on the XML).
    robot_description = ParameterValue(Command(["xacro ", xacro_path]), value_type=str)

    return LaunchDescription([
        DeclareLaunchArgument("use_sim_time", default_value="true",
                              description="Use Gazebo /clock as the time source."),
        Node(
            package="robot_state_publisher",
            executable="robot_state_publisher",
            output="screen",
            parameters=[{
                "use_sim_time": use_sim_time,
                "robot_description": robot_description,
            }],
        ),
    ])
