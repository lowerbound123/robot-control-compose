from setuptools import find_packages, setup

package_name = "robot_control_test_sim"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="robot-control maintainers",
    maintainer_email="maintainer@example.com",
    description="Test-only closed-loop ROS simulator for robot-control.",
    license="Apache-2.0",
    entry_points={"console_scripts": ["simulator = robot_control_test_sim.simulator:main"]},
)
