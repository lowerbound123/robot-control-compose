# ARX 厂商工作区备份

`arx_x5_ws/` 来自用户在本次迁移前放入目标项目的厂商工作区备份，按本地提供内容纳入版本管理；未宣称或推测其上游仓库版本。保留其中源码、URDF/网格、头文件、随附二进制库及已有声明。厂商原文件未做格式或依赖修订，不重新授权这些资源。

默认 Docker 构建上下文排除此备份，不在此目录运行 colcon，不发现或启动其中的硬件控制器。仅 `ros_ws/src/arx5_arm_msg` 的单独消息包副本参与主镜像 Humble 编译；该副本补齐 std_msgs/geometry_msgs 的依赖发现/声明，`RobotCmd.msg`、`RobotStatus.msg` 与备份逐字节一致。

ROS 模拟测试使用项目自己的 `robot_control_test_msgs` 和 `robot_control_test_sim`。真实 ACOne 验收仍需单独启动并核对厂商驱动、消息字段、DDS 网络和急停行为。

构建、安装、日志、Python 缓存和本项目运行产物由根 `.gitignore` 排除。上游自带 `.so` 属于备份交付资源，保留入库；它们不会在默认镜像中执行。
