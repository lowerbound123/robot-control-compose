"""Test-only ROS 1 master and fake LIFT2; run with Docker --network none."""
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import threading
import time
import xmlrpc.client
import zlib


def png_rgb(width, height, color):
    def chunk(kind, data):
        return struct.pack('!I', len(data))+kind+data+struct.pack('!I', zlib.crc32(kind+data))
    rows = (b'\x00'+bytes(color)*width)*height
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR', struct.pack('!2I5B', width, height, 8, 2, 0, 0, 0))
            +chunk(b'IDAT', zlib.compress(rows))+chunk(b'IEND', b''))


def main():
    assert os.environ['ROS_MASTER_URI'] == 'http://127.0.0.1:11311'
    assert os.environ['ROS_IP'] == '127.0.0.1'
    master = subprocess.Popen(['roscore'], start_new_session=True)
    try:
        deadline = time.monotonic()+20
        while True:
            try:
                xmlrpc.client.ServerProxy(os.environ['ROS_MASTER_URI']).getPid('/lift2_test')
                break
            except OSError:
                if time.monotonic() >= deadline: raise
                time.sleep(.05)
        import rospy
        from arm_control.msg import JointInformation, JointControl, PosCmd
        from sensor_msgs.msg import Image, CompressedImage
        rospy.init_node('lift2_simulator', disable_rosout=True)
        lock = threading.Lock()
        positions = {'left': [.1]*6+[4.], 'right': [.2]*6+[3.]}
        evidence = Path('/evidence')
        events = (evidence/'simulator-events.jsonl').open('a', buffering=1)

        def receive(side, message):
            with lock:
                positions[side] = list(message.joint_pos)
                events.write(json.dumps({'arm': side, 'values': positions[side]})+'\n')

        joints, poses, cameras, compressed = {}, {}, {}, {}
        for side, suffix, index in (('left', '', '1'), ('right', '2', '2')):
            rospy.Subscriber('/joint_control'+suffix, JointControl,
                             lambda message, side=side: receive(side, message), queue_size=None)
            joints[side] = rospy.Publisher('/joint_information'+suffix, JointInformation, queue_size=10)
            poses[side] = rospy.Publisher('/follow'+index+'_pos_back', PosCmd, queue_size=10)
        for camera, suffix in (('head', 'h'), ('left_wrist', 'l'), ('right_wrist', 'r')):
            topic = '/camera_'+suffix+'/color/image_raw'
            cameras[camera] = rospy.Publisher(topic, Image, queue_size=2)
            compressed[camera] = rospy.Publisher(topic+'/compressed', CompressedImage, queue_size=2)
        (evidence/'simulator-ready').write_text('ready')
        sequence = 0
        rate = rospy.Rate(50)
        while not rospy.is_shutdown():
            sequence += 1
            with lock: current = {side: list(values) for side, values in positions.items()}
            for side in positions:
                if not rospy.get_param('/lift2_test/pause_joint', False):
                    message = JointInformation(joint_pos=current[side], joint_vel=[.01]*7, joint_cur=[.02]*7)
                    joints[side].publish(message)
                if not rospy.get_param('/lift2_test/pause_eef', False):
                    pose = PosCmd(x=.1, y=.2, z=.3, roll=.4, pitch=.5, yaw=.6, gripper=current[side][6]/5.)
                    poses[side].publish(pose)
            if not rospy.get_param('/lift2_test/pause_images', False):
                for index, camera in enumerate(cameras):
                    color = [10+index, 20+index, 30+index]
                    message = Image(height=2, width=3, encoding='bgr8', step=11, is_bigendian=0)
                    message.header.seq, message.header.stamp = sequence, rospy.Time.now()
                    message.data = (bytes(color[::-1])*3+b'\xee\xff')*2
                    cameras[camera].publish(message)
                    image = CompressedImage(format='rgb8; png compressed rgb8', data=png_rgb(3, 2, color))
                    image.header = message.header
                    compressed[camera].publish(image)
            rate.sleep()
    finally:
        if master.poll() is None:
            os.killpg(master.pid, signal.SIGTERM)
            try: master.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(master.pid, signal.SIGKILL)
                master.wait()


if __name__ == '__main__':
    main()
