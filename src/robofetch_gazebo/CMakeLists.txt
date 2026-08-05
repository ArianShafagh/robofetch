cmake_minimum_required(VERSION 3.8)
project(robofetch_gazebo)

find_package(ament_cmake REQUIRED)

install(
  DIRECTORY worlds launch config models
  DESTINATION share/${PROJECT_NAME}
)

ament_package()
