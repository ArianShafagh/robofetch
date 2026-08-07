from setuptools import find_packages, setup

package_name = "robofetch_ai"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    # The trained model ships inside the package so the service is self-contained.
    package_data={package_name: ["models/*.joblib"]},
    include_package_data=True,
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="robojazzy",
    maintainer_email="shafagh.arian2003@gmail.com",
    description="Order feasibility prediction service for RoboFetch.",
    license="MIT",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "ai_service = robofetch_ai.service:main",
        ],
    },
)
