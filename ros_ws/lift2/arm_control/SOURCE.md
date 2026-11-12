# LIFT2 ROS 1 message provenance

The three `msg/*.msg` files are byte-for-byte copies from the user-provided image
`lerobot_lab/lift2-qwen3_5:c0f03bab6ffbfd69d953941e84956c2324e4885d12ed2b012df5063feaa61798`,
image ID `sha256:3b285e5d05184fba67b3533e326a05b3da997a88f4262350df7ad7fb34d7d76e`.

Source directory: `/opt/LIFT/R5/ROS/R5_ws/src/arx_msg_ros/arm_control/msg`.
R5 repository: https://github.com/ARXroboticsX/R5, commit
`e87d09c11edb65f6ae672abc7a41fe2277a2f12f`.
Parent LIFT repository commit: `f8e0fd8946b1adc8d9b718fd77d3c8dab7744136`.
Read and extracted on 2026-11-12. No inference/training implementation was copied.

| File | SHA256 |
| --- | --- |
| JointControl.msg | `4bd23be331b1a6413c6eb2b11c1b62a0159e47ed5315d5c782520ea60a9f212f` |
| JointInformation.msg | `4bd23be331b1a6413c6eb2b11c1b62a0159e47ed5315d5c782520ea60a9f212f` |
| PosCmd.msg | `2d3afe38c0f87432b2cb887a903d52d46135cd4f96ccc780056385099b6dee1c` |

Expected ROS 1 MD5: both joint messages `86ad2a9a176a812901cac6f69b936f2e`;
PosCmd `df5f2abbfa9e683f82c8a2751cdc09f0`.

Only CMakeLists.txt and package.xml are new build wrappers. The source message
files contain no license header; upstream package.xml says `TODO: License declaration`.
This file records provenance and grants no
additional license. The build does not compile or launch vendor controllers.
