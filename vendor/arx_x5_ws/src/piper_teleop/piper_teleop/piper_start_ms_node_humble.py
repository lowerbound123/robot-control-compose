#!/usr/bin/env python3
# -*-coding:utf8-*-
# 本文件为同时打开主从臂的节点，通过mode参数控制是读取还是控制
# 默认认为从臂有夹爪
# mode为0时为发送主从臂消息，
# mode为1时为控制从臂，不发送主臂消息，此时如果要控制从臂，需要给主臂的topic发送消息

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import JointState
from std_msgs.msg import Bool
from geometry_msgs.msg import PoseStamped
from piper_msgs.msg import PiperStatusMsg, PosCmd
from std_srvs.srv import Trigger
#from tf_transformations import quaternion_from_euler  # 用于欧拉角到四元数的转换
import time
import math
import numpy as np
from scipy.spatial.transform import Rotation
from piper_sdk import C_PiperInterface
import pybullet as p
import pybullet_data
import threading


class PiperRosNode(Node):
    """机械臂 ROS 2 节点"""

    def __init__(self):
        super().__init__('piper_start_all_node')

        # 声明参数
        self.declare_parameter('can_port', 'can_left')
        self.declare_parameter('mode', 0)
        self.declare_parameter('auto_enable', False)

        # 获取参数
        self.can_port = self.get_parameter('can_port').get_parameter_value().string_value
        self.mode = self.get_parameter('mode').get_parameter_value().integer_value
        self.auto_enable = self.get_parameter('auto_enable').get_parameter_value().bool_value

        self.get_logger().info(f"can_port: {self.can_port}")
        self.get_logger().info(f"mode: {self.mode}")
        self.get_logger().info(f"auto_enable: {self.auto_enable}")

        # 发布者
        self.joint_std_pub_puppet = self.create_publisher(JointState, '/puppet/joint_states', 10)
        self.arm_status_pub = self.create_publisher(PiperStatusMsg, '/puppet/arm_status', 10)
        self.end_pose_pub = self.create_publisher(PoseStamped, '/puppet/end_pose', 10)
        self.end_pose_euler_pub = self.create_publisher(PosCmd, '/puppet/end_pose_euler', 10)

        if self.mode == 0:
            self.joint_std_pub_master = self.create_publisher(JointState, '/master/joint_states', 10)
            self.end_pose_pub_master = self.create_publisher(PoseStamped, '/master/end_pose', 10)
            self.end_pose_euler_pub_master = self.create_publisher(PosCmd, '/master/end_pose_euler', 10)

        # 服务
        self.master_go_zero_service = self.create_service(Trigger, f'/{self.can_port}/go_zero_master', self.handle_master_go_zero_service)
        self.restore_ms_mode_service = self.create_service(Trigger, f'/{self.can_port}/restore_ms_mode', self.handle_restore_ms_mode_service)

        # 初始化机械臂接口
        self.piper = C_PiperInterface(can_name=self.can_port)
        self.piper.ConnectPort()

        # 初始化 PyBullet
        self.physics_client = p.connect(p.DIRECT)
        p.setAdditionalSearchPath(pybullet_data.getDataPath())
        self.robotId = p.loadURDF("/home/agilex/cobot_magic/Piper_ros_private-ros-noetic/src/piper_description/piper100.urdf", [0, 0, 0])

        # 初始化从臂和主臂状态
        self.joint_state_slave = JointState()
        self.joint_state_slave.name = ['joint0', 'joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6']
        self.joint_state_slave.position = [0.0] * 7
        self.joint_state_slave.velocity = [0.0] * 7
        self.joint_state_slave.effort = [0.0] * 7

        self.joint_state_master = JointState()
        self.joint_state_master.name = ['joint0', 'joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6']
        self.joint_state_master.position = [0.0] * 7
        self.joint_state_master.velocity = [0.0] * 7
        self.joint_state_master.effort = [0.0] * 7

        # 启动订阅线程
        if self.mode == 1:
            self.create_subscription(PosCmd, '/pos_cmd', self.pos_callback, 10)
            self.create_subscription(JointState, '/master/joint_states', self.joint_callback, 10)
            self.create_subscription(Bool, '/enable_flag', self.enable_callback, 10)

        # 启动发布线程
        # self.timer = self.create_timer(5, self.publish_data)  # 200 Hz
        # self.publisher_thread = threading.Thread(target=self.publish_data)
        # self.publisher_thread.start()
        self.lock = threading.Lock()
        self.running = True

    def publish_data(self):
        """发布机械臂状态数据"""
        if self.auto_enable and self.mode == 1:
            self.piper.EnableArm(7)

        # self.publish_slave_arm_state()
        # self.publish_slave_arm_end_pose()

        if self.mode == 0:
            # pass
            self.Pubilsh()


    def publish_slave_arm_state(self):
        """发布从臂状态"""
        arm_status = PiperStatusMsg()
        arm_status.ctrl_mode = self.piper.GetArmStatus().arm_status.ctrl_mode
        self.arm_status_pub.publish(arm_status)

    def publish_slave_arm_end_pose(self):
        """发布从臂末端位姿"""
        endpos = PoseStamped()
        endpos.pose.position.x = self.piper.GetArmEndPoseMsgs().end_pose.X_axis / 1000000
        endpos.pose.position.y = self.piper.GetArmEndPoseMsgs().end_pose.Y_axis / 1000000
        endpos.pose.position.z = self.piper.GetArmEndPoseMsgs().end_pose.Z_axis / 1000000
        roll = math.radians(self.piper.GetArmEndPoseMsgs().end_pose.RX_axis / 1000)
        pitch = math.radians(self.piper.GetArmEndPoseMsgs().end_pose.RY_axis / 1000)
        yaw = math.radians(self.piper.GetArmEndPoseMsgs().end_pose.RZ_axis / 1000)
        quaternion = Rotation.from_euler('xyz', [roll, pitch, yaw]).as_quat()
        endpos.pose.orientation.x = quaternion[0]
        endpos.pose.orientation.y = quaternion[1]
        endpos.pose.orientation.z = quaternion[2]
        endpos.pose.orientation.w = quaternion[3]
        endpos.header.stamp = self.get_clock().now().to_msg()
        self.end_pose_pub.publish(endpos)

    def handle_master_go_zero_service(self, request, response):
        self.get_logger().info("Resetting master arm to zero position")
        self.piper.ReqMasterArmMoveToHome(2)
        response.success = True
        response.message = "Master arm reset to zero position"
        return response

    def handle_restore_ms_mode_service(self, request, response):
        self.get_logger().info("Restoring master-slave mode")
        self.piper.ReqMasterArmMoveToHome(0)
        response.success = True
        response.message = "Master-slave mode restored"
        return response

    def pos_callback(self, msg):
        """处理末端位姿控制"""
        self.get_logger().info(f"Received PosCmd: x={msg.x}, y={msg.y}, z={msg.z}")

    def joint_callback(self, msg):
        """处理关节控制"""
        self.get_logger().info(f"Received JointState: {msg.position}")

    def enable_callback(self, msg):
        """处理使能控制"""
        self.get_logger().info(f"Enable flag: {msg.data}")
        if msg.data:
            self.piper.EnableArm(7)
        else:
            self.piper.DisableArm(7)
    
    def PublishSlaveArmState(self):
        arm_status = PiperStatusMsg()
        arm_status.ctrl_mode = self.piper.GetArmStatus().arm_status.ctrl_mode
        arm_status.arm_status = self.piper.GetArmStatus().arm_status.arm_status
        arm_status.mode_feedback = self.piper.GetArmStatus().arm_status.mode_feed
        arm_status.teach_status = self.piper.GetArmStatus().arm_status.teach_status
        arm_status.motion_status = self.piper.GetArmStatus().arm_status.motion_status
        arm_status.trajectory_num = self.piper.GetArmStatus().arm_status.trajectory_num
        arm_status.err_code = self.piper.GetArmStatus().arm_status.err_code
        arm_status.joint_1_angle_limit = self.piper.GetArmStatus().arm_status.err_status.joint_1_angle_limit
        arm_status.joint_2_angle_limit = self.piper.GetArmStatus().arm_status.err_status.joint_2_angle_limit
        arm_status.joint_3_angle_limit = self.piper.GetArmStatus().arm_status.err_status.joint_3_angle_limit
        arm_status.joint_4_angle_limit = self.piper.GetArmStatus().arm_status.err_status.joint_4_angle_limit
        arm_status.joint_5_angle_limit = self.piper.GetArmStatus().arm_status.err_status.joint_5_angle_limit
        arm_status.joint_6_angle_limit = self.piper.GetArmStatus().arm_status.err_status.joint_6_angle_limit
        arm_status.communication_status_joint_1 = self.piper.GetArmStatus().arm_status.err_status.communication_status_joint_1
        arm_status.communication_status_joint_2 = self.piper.GetArmStatus().arm_status.err_status.communication_status_joint_2
        arm_status.communication_status_joint_3 = self.piper.GetArmStatus().arm_status.err_status.communication_status_joint_3
        arm_status.communication_status_joint_4 = self.piper.GetArmStatus().arm_status.err_status.communication_status_joint_4
        arm_status.communication_status_joint_5 = self.piper.GetArmStatus().arm_status.err_status.communication_status_joint_5
        arm_status.communication_status_joint_6 = self.piper.GetArmStatus().arm_status.err_status.communication_status_joint_6
        self.arm_status_pub.publish(arm_status)
    
    def PublishSlaveArmEndPose(self):
        # 末端位姿
        endpos = PoseStamped()
        endpos.pose.position.x = self.piper.GetArmEndPoseMsgs().end_pose.X_axis/1000000
        endpos.pose.position.y = self.piper.GetArmEndPoseMsgs().end_pose.Y_axis/1000000
        endpos.pose.position.z = self.piper.GetArmEndPoseMsgs().end_pose.Z_axis/1000000
        roll = self.piper.GetArmEndPoseMsgs().end_pose.RX_axis/1000
        pitch = self.piper.GetArmEndPoseMsgs().end_pose.RY_axis/1000
        yaw = self.piper.GetArmEndPoseMsgs().end_pose.RZ_axis/1000
        roll = math.radians(roll)
        pitch = math.radians(pitch)
        yaw = math.radians(yaw)
        quaternion = Rotation.from_euler('xyz', [roll, pitch, yaw]).as_quat()
        endpos.pose.orientation.x = quaternion[0]
        endpos.pose.orientation.y = quaternion[1]
        endpos.pose.orientation.z = quaternion[2]
        endpos.pose.orientation.w = quaternion[3]
        # 为末端位姿增加时间戳
        endpos.header.stamp = 1
        self.end_pose_pub.publish(endpos)
        
        end_pose_euler = PosCmd()
        end_pose_euler.x = self.piper.GetArmEndPoseMsgs().end_pose.X_axis/1000000
        end_pose_euler.y = self.piper.GetArmEndPoseMsgs().end_pose.Y_axis/1000000
        end_pose_euler.z = self.piper.GetArmEndPoseMsgs().end_pose.Z_axis/1000000
        end_pose_euler.roll = roll
        end_pose_euler.pitch = pitch
        end_pose_euler.yaw = yaw
        end_pose_euler.gripper = self.piper.GetArmGripperMsgs().gripper_state.grippers_angle/1000000
        end_pose_euler.mode1 = 0
        end_pose_euler.mode2 = 0
        self.end_pose_euler_pub.publish(end_pose_euler)
    def PublishMasterArmEndPose(self):
        # 末端位姿
        end_pose_joints = self.piper.GetFK(mode='control')
        # # print(end_pose_joints)

        joint_0:float = (self.piper.GetArmJointCtrl().joint_ctrl.joint_1/1000) * 0.017444
        joint_1:float = (self.piper.GetArmJointCtrl().joint_ctrl.joint_2/1000) * 0.017444
        joint_2:float = (self.piper.GetArmJointCtrl().joint_ctrl.joint_3/1000) * 0.017444
        joint_3:float = (self.piper.GetArmJointCtrl().joint_ctrl.joint_4/1000) * 0.017444
        joint_4:float = (self.piper.GetArmJointCtrl().joint_ctrl.joint_5/1000) * 0.017444
        joint_5:float = (self.piper.GetArmJointCtrl().joint_ctrl.joint_6/1000) * 0.017444
        joint_angles = [joint_0, joint_1, joint_2, joint_3, joint_4, joint_5]
        for i in range(len(joint_angles)):
            p.resetJointState(self.robotId, i, joint_angles[i])

        link_state = p.getLinkState(self.robotId, 5, computeForwardKinematics=True)
        position = link_state[0]  # 位置
        orientation = link_state[1]  # 姿态四元数
        euler = Rotation.from_quat(orientation).as_euler('xyz', degrees=False)  # 转换为欧拉角
        print(euler)

        R_homo = np.eye(4)
        R_homo[:3, :3] = Rotation.from_quat(orientation).as_matrix()  # 旋转部分
        R_homo[:3, 3] = position  # 平移部分
        T = np.array([
            [0, 0, 1, 0],
            [1, 0, 0, 0],
            [0, 1, 0, 0],
            [0, 0, 0, 1]
        ])
        # T = np.linalg.inv(T)

        # 5. 左乘 T
        # result = T @ R_homo
        result = R_homo @ np.linalg.inv(T)

        # print(R_homo)
        # print(result)

        # 6. 提取结果中的旋转部分 → 欧拉角 (ZYX顺序，单位：弧度)
        final_rotation = Rotation.from_matrix(result[:3, :3])
        roll, pitch, yaw = final_rotation.as_euler('xyz', degrees=False)  # 弧度

        # 7. 提取平移部分
        final_position = result[:3, 3]

        # 8. 输出 ARX_POSE (位置 + 欧拉角)
        arx_pose = {
            'position': final_position,
            'euler_angles': {'roll': roll, 'pitch': pitch, 'yaw': yaw}
        }
        # print("new %.2f-%.2f-%.2f" % (final_position[0], final_position[1], final_position[2]))

        end_pose_euler = PosCmd()
        end_pose_euler.x = final_position[0]
        end_pose_euler.y = final_position[1]
        end_pose_euler.z = (final_position[2] - 0.2)
        end_pose_euler.roll = roll
        end_pose_euler.pitch = pitch
        end_pose_euler.yaw = yaw
        
        # print(position)
        end_pose_euler.gripper = round(self.piper.GetArmGripperCtrl().gripper_ctrl.grippers_angle/1000000, 6)

        end_pose_euler.mode1 = 0
        end_pose_euler.mode2 = 0

        # end_pose_euler.delta_x = final_position[0]
        print("end_pose_euler", final_position[0], final_position[1], final_position[2] - 0.2, roll, pitch, yaw, end_pose_euler.gripper )
        self.end_pose_euler_pub_master.publish(end_pose_euler)  

    def Pubilsh(self):
        """机械臂消息发布
        """
        # rate = rospy.Rate(200)  # 200 Hz
        enable_flag = False
        # 设置超时时间（秒）
        timeout = 5
        # 记录进入循环前的时间
        start_time = time.time()
        elapsed_time_flag = False
        rate = self.create_rate(200)
        while self.running and rclpy.ok():
            if(self.auto_enable and self.mode == 1):
                while not (enable_flag):
                    elapsed_time = time.time() - start_time
                    print("--------------------")
                    enable_flag = self.piper.GetArmLowSpdInfoMsgs().motor_1.foc_status.driver_enable_status and \
                        self.piper.GetArmLowSpdInfoMsgs().motor_2.foc_status.driver_enable_status and \
                        self.piper.GetArmLowSpdInfoMsgs().motor_3.foc_status.driver_enable_status and \
                        self.piper.GetArmLowSpdInfoMsgs().motor_4.foc_status.driver_enable_status and \
                        self.piper.GetArmLowSpdInfoMsgs().motor_5.foc_status.driver_enable_status and \
                        self.piper.GetArmLowSpdInfoMsgs().motor_6.foc_status.driver_enable_status
                    print("使能状态:",enable_flag)
                    if(enable_flag):
                        self.__enable_flag = True
                    self.piper.EnableArm(7)
                    self.piper.GripperCtrl(0,1000,0x02, 0)
                    self.piper.GripperCtrl(0,1000,0x01, 0)
                    print("--------------------")
                    # 检查是否超过超时时间
                    if elapsed_time > timeout:
                        print("超时....")
                        elapsed_time_flag = True
                        enable_flag = True
                        break
                    time.sleep(1)
                    pass
            if(elapsed_time_flag):
                print("程序自动使能超时,退出程序")
                exit(0)
            # 发布消息
            # self.PublishSlaveArmJointAndGripper()
            # self.PublishSlaveArmState()
            # self.PublishSlaveArmEndPose()
            # 模式为0的时候，发布主臂消息
            if(self.mode == 0):
                # self.PublishMasterArmJointAndGripper()
                self.PublishMasterArmEndPose()

                rate.sleep()
                # time.sleep(0.005) # 200hz

    
def main(args=None):
    rclpy.init(args=args)
    node = PiperRosNode()

    spin_thread = threading.Thread(target=rclpy.spin, args=(node,), daemon=True)
    spin_thread.start() 
    try:
        node.Pubilsh()

    except KeyboardInterrupt:
        node.get_logger().info("Received keyboard interrupt, shutting down")
    finally:
        with node.lock:
            node.running = False
        node.destroy_node()
        rclpy.shutdown()
        spin_thread.joint()


if __name__ == '__main__':
    main()