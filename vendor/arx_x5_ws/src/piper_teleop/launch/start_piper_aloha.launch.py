from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    # 启动左侧主机械臂节点
    piper_master_left_node = Node(
        package='piper_teleop',
        executable='piper_arx_master_node',
        name='piper_master_left',
        output='screen',
        remappings=[
            ('/ARX_PIPER', '/ARX_VR_L'),
            ('/master/end_pose_euler', '/master/end_pose_euler_left'),
            ('/arm_status', '/arm_l_status'),
            ('/follow_pos_back', '/follow1_pos_back'),
            ('/joint_information', '/joint_information')
        ]
    )

    # 启动右侧主机械臂节点
    piper_master_right_node = Node(
        package='piper_teleop',
        executable='piper_arx_master_node',
        name='piper_master_right',
        output='screen',
        remappings=[
            ('/ARX_PIPER', '/ARX_VR_R'),
            ('/master/end_pose_euler', '/master/end_pose_euler_right'),
            ('/arm_status', '/arm_r_status'),
            ('/follow_pos_back', '/follow2_pos_back'),
            ('/joint_information', '/joint2_information')
        ]
    )

    # 返回 LaunchDescription
    return LaunchDescription([
        piper_master_left_node,
        # piper_master_right_node
    ])
