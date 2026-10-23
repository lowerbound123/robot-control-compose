#!/usr/bin/env python3
# -*-coding:utf8-*-
# 本文件为打开主臂读取

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import JointState
from geometry_msgs.msg import PoseStamped
from piper_msgs.msg import PiperStatusMsg
from piper_msgs.msg import PosCmd as PiperPosCmd
from arm_control.msg import PosCmd, JointControl
from scipy.spatial.transform import Rotation
from copy import deepcopy
from pykalman import KalmanFilter
import numpy as np
import math
import time
import threading

class AlohaPiperRosNode(Node):
    """机械臂 ROS 2 节点"""

    def __init__(self):
        super().__init__('piper_arx_aloha_node')

        # 初始化变量
        self.piper_pos_cmd = PiperPosCmd()
        self.piper_pse_cmd_timestamp = 0
        self.arx_pos_cmd = PosCmd()
        self.arx_joint_information = JointControl()
        self.arx_end_pose = PosCmd()


        # 发布者
        self.arx_vr_pub_master = self.create_publisher(PosCmd, '/ARX_PIPER', 10) #/ARX_PIPER

        # 订阅者
        self.create_subscription(PiperPosCmd, '/master/end_pose_euler', self.pos_callback, 10)
        self.create_subscription(PoseStamped, '/master/end_pose', self.pos_quat_callback, 10)
        self.create_subscription(JointControl, '/joint_information', self.arx_joint_callback, 10)
        self.create_subscription(PosCmd, '/arm_l_status', self.arx_end_pose_callback, 10)


        # 初始状态矩阵
        self.initial_state_mean = np.zeros(6)
        # 初始状态协方差矩阵 (初始不确定性较大)
        self.initial_state_covariance = np.eye(6) * 10
        # 状态转移矩阵 (假设状态是线性的，且没有外部控制输入)
        self.transition_matrix = np.eye(6)
        # 观测矩阵 (假设我们直接观测到状态)
        self.observation_matrix = np.eye(6)
        # 过程噪声协方差矩阵 (假设过程噪声较小)
        self.process_noise_covariance = np.eye(6) * 0.01
        # 观测噪声协方差矩阵 (假设观测噪声较大)
        self.observation_noise_covariance = np.eye(6) * 0.5
        # 初始化卡尔曼滤波器
        self.kf = KalmanFilter(
            transition_matrices=self.transition_matrix,
            observation_matrices=self.observation_matrix,
            initial_state_mean=self.initial_state_mean,
            initial_state_covariance=self.initial_state_covariance,
            transition_covariance=self.process_noise_covariance,
            observation_covariance=self.observation_noise_covariance
        )

        self.use_kalman = True # False
        self.step_id = 0

        # 定时器
        # self.timer = self.create_timer(1, self.publish_data)  # 100 Hz
        # self.publisher_thread = threading.Thread(target=self.publish_data)
        # self.publisher_thread.start()
        self.lock = threading.Lock()

    def _6d_to_pose(self, pose6d, degrees=False):
        """将 6D 位姿转换为 4x4 齐次变换矩阵"""
        pose = np.eye(4)
        pose[:3, 3] = pose6d[:3]
        pose[:3, :3] = Rotation.from_euler("xyz", pose6d[3:6], degrees=degrees).as_matrix()
        return pose

    def pose_to_6d(self, pose, degrees=False):
        """将 4x4 齐次变换矩阵转换为 6D 位姿"""
        pose6d = np.zeros(6)
        pose6d[:3] = pose[:3, 3]
        pose6d[3:6] = Rotation.from_matrix(pose[:3, :3]).as_euler("xyz", degrees=degrees)
        return pose6d

    def publish_data(self):
        """机械臂消息发布"""
        target_pose6d = np.zeros(6)
        curr2piper_pose = np.eye(4)
        prev2piper_pose =  np.eye(4)
        filtered_state_means = np.zeros(6)
        filtered_state_covariances =  np.eye(6) * 10
        last_piper_pos_cmd_timestamp = 0
        rate = self.create_rate(200)

        while rclpy.ok():
            print(f"publish data thread start")
            # import pdb; pdb.set_trace()

            # if self.piper_pos_cmd.x == 0 and self.piper_pos_cmd.y == 0 and self.piper_pos_cmd.z == 0:
            #     return
            print(f"self.piper_pos_cmd:{self.piper_pse_cmd_timestamp} {self.piper_pos_cmd}")
            if last_piper_pos_cmd_timestamp != self.piper_pse_cmd_timestamp:
                curr2piper_pose = self._6d_to_pose([
                    self.piper_pos_cmd.x, self.piper_pos_cmd.y, self.piper_pos_cmd.z,
                    self.piper_pos_cmd.roll, self.piper_pos_cmd.pitch, self.piper_pos_cmd.yaw
                ])
                last_piper_pos_cmd_timestamp = self.piper_pse_cmd_timestamp
            else:
                continue

            print(f"self.arx_end_pose is {self.arx_end_pose}")
            if self.step_id == 0:
                self.last2robot_pose = deepcopy(self._6d_to_pose([
                    self.arx_end_pose.x, self.arx_end_pose.y, self.arx_end_pose.z,
                    self.arx_end_pose.roll, self.arx_end_pose.pitch, self.arx_end_pose.yaw
                ], degrees=False))
                prev2piper_pose = deepcopy(curr2piper_pose)

            curr2prev_pose = np.linalg.inv(prev2piper_pose) @ curr2piper_pose
            print(f"curr2prev_pose is {curr2prev_pose}")

            curr2prev_pose6d = self.pose_to_6d(curr2prev_pose)

            if self.use_kalman:
                if self.step_id == 0:
                    filtered_state_means, filtered_state_covariances = self.kf.filter_update(
                        filtered_state_mean=self.initial_state_mean,
                        filtered_state_covariance=self.initial_state_covariance,
                        observation=curr2prev_pose6d
                    )
                else:
                    filtered_state_means, filtered_state_covariances = self.kf.filter_update(
                        filtered_state_mean=filtered_state_means,
                        filtered_state_covariance=filtered_state_covariances,
                        observation=curr2prev_pose6d
                    )
                curr2prev_pose6d = filtered_state_means

            curr2prev_pose = self._6d_to_pose(curr2prev_pose6d)
            self.last2robot_pose = deepcopy(self.last2robot_pose @ curr2prev_pose)
            target_pose6d = self.pose_to_6d(self.last2robot_pose) #curr2prev_pose6d

            arx_pos_cmd = PosCmd()
            arx_pos_cmd.x = float(target_pose6d[0])
            arx_pos_cmd.y = float(target_pose6d[1])
            arx_pos_cmd.z = float(target_pose6d[2])
            arx_pos_cmd.roll = float(target_pose6d[3])
            arx_pos_cmd.pitch = float(target_pose6d[4])
            arx_pos_cmd.yaw = float(target_pose6d[5])
            # arx_pos_cmd.gripper = 3.5 if self.piper_pos_cmd.gripper <= 0.04 else 0.0
            arx_pos_cmd.gripper = float(-35 * self.piper_pos_cmd.gripper + 3.5)


            print(f" \n arx_vr_pub_master.publish {arx_pos_cmd} \n")
            self.arx_vr_pub_master.publish(arx_pos_cmd)
            self.step_id += 1
            prev2piper_pose = deepcopy(curr2piper_pose)
            rate.sleep()

    def arx_joint_callback(self, msg):
        """关节信息回调"""
        self.arx_joint_information = deepcopy(msg)

    def arx_end_pose_callback(self, msg):
        """末端位姿回调"""
        self.arx_end_pose = deepcopy(msg)

    def pos_callback(self, msg):
        """机械臂末端位姿订阅回调函数"""
        # print(f"\n /master/end_pose_euler PiperPosCmd is {msg} \n")
        with self.lock:
            self.piper_pos_cmd.x = msg.x
            self.piper_pos_cmd.y = msg.y
            self.piper_pos_cmd.z = msg.z # -0.2
            self.piper_pos_cmd.roll = msg.roll
            self.piper_pos_cmd.pitch = msg.pitch
            self.piper_pos_cmd.yaw = msg.yaw
            self.piper_pos_cmd.gripper = msg.gripper
            self.piper_pse_cmd_timestamp = self.get_clock().now().to_msg()
        

    def pos_quat_callback(self, msg):
        """机械臂末端位姿（四元数）订阅回调函数"""
        quat = np.array([
            msg.pose.orientation.x,
            msg.pose.orientation.y,
            msg.pose.orientation.z,
            msg.pose.orientation.w
        ])
        position = np.array([
            msg.pose.position.x,
            msg.pose.position.y,
            msg.pose.position.z
        ])
        R = Rotation.from_quat(quat).as_matrix()
        R_homo = np.eye(4)
        R_homo[:3, :3] = R
        R_homo[:3, 3] = position
        T = np.array([
            [0, 0, 1, 0],
            [1, 0, 0, 0],
            [0, 1, 0, 0],
            [0, 0, 0, 1]
        ])
        result = T @ R_homo
        final_rotation = Rotation.from_matrix(result[:3, :3])
        roll, pitch, yaw = final_rotation.as_euler('xyz', degrees=False)
        final_position = result[:3, 3]
        self.piper_pos_cmd.x = final_position[0]
        self.piper_pos_cmd.y = final_position[1]
        self.piper_pos_cmd.z = final_position[2]
        self.piper_pos_cmd.roll = roll
        self.piper_pos_cmd.pitch = pitch
        self.piper_pos_cmd.yaw = yaw

def main(args=None):
    rclpy.init(args=args)
    node = AlohaPiperRosNode()

    spin_thread = threading.Thread(target=rclpy.spin, args=(node,), daemon=True)
    spin_thread.start()
    try:
        node.publish_data()

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