from setuptools import find_packages, setup

package_name = "robofetch_bridge"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="robojazzy",
    maintainer_email="shafagh.arian2003@gmail.com",
    description="FastAPI + SQLite web tier for RoboFetch.",
    license="MIT",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "bridge = robofetch_bridge.app:main",
        ],
    },
)
