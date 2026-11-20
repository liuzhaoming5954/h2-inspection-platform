#!/usr/bin/env python3
"""独立转角裁判：积分 Mid360 内置 IMU 的陀螺仪 z 轴（容器内运行）。

用途：FastLIO 的位姿来自扫描匹配，快速原地旋转时可能跟不上；本节点
只做原始陀螺仪积分（不含尺度/匹配），用于交叉验证机器人实际转角。
漂移来源只有陀螺零偏，数十秒窗口内误差远小于待验证的量级差异。

输出：/tmp/imu_yaw.log，每 2 秒一行：墙钟时间 + 累计转角（度，左转为正）。
"""
from __future__ import annotations

import math
import time

import rospy
from sensor_msgs.msg import Imu

_state = {"last_t": None, "integral": 0.0}


def on_imu(msg: Imu) -> None:
    t = msg.header.stamp.to_sec()
    if t <= 0.0:
        t = time.time()
    last = _state["last_t"]
    if last is not None and 0.0 < t - last < 0.1:   # 剔除乱序与断流
        _state["integral"] += msg.angular_velocity.z * (t - last)
    _state["last_t"] = t


def main() -> None:
    rospy.init_node("imu_yaw_probe", anonymous=True)
    rospy.Subscriber("/livox/imu", Imu, on_imu, queue_size=400)
    with open("/tmp/imu_yaw.log", "a") as log:
        log.write(f"# probe start {time.strftime('%H:%M:%S')}\n")
    while not rospy.is_shutdown():
        time.sleep(2.0)
        with open("/tmp/imu_yaw.log", "a") as log:
            log.write(f"{time.strftime('%H:%M:%S')} {math.degrees(_state['integral']):.2f}\n")


if __name__ == "__main__":
    main()
