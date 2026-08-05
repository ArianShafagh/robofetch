cmake_minimum_required(VERSION 3.8)
project(robofetch_web)

find_package(ament_cmake REQUIRED)

# Plain static files - no compilation. FastAPI mounts this directory at /app when the
# environment variable ROBOFETCH_WEB points at it, which delivery.launch.py sets to
# share/robofetch_web/web.
install(
  DIRECTORY web
  DESTINATION share/${PROJECT_NAME}
)

ament_package()
