cmake_minimum_required(VERSION 3.8)
project(robofetch_description)

find_package(ament_cmake REQUIRED)

# Install the robot description and launch files into the package share directory,
# so other packages (gazebo, bringup) can locate them via get_package_share_directory().
install(
  DIRECTORY urdf launch rviz
  DESTINATION share/${PROJECT_NAME}
)

ament_package()
