#!/usr/bin/env python3
"""H2 建图数据桥 v2（容器内运行）：桥内累计地图，与机器人是否移动无关。

设计：订阅每帧机体坐标扫描 /body_cloud 与实时位姿 /slam_odom（同一回调
缓存位置与完整四元数），把点变换到世界系后累计进持久体素哈希（每格保留
首个命中点），每秒打包发布累计结果。机器人原地不动时，单帧环扫同样
持续成图；重复扫描同一表面命中相同体素，规模随环境收敛而非随时间膨胀。

容器为 noetic（python3.8），类型注解需延后求值以兼容 3.9 前语法。

HTTP 接口（容器为 host 网络，即 PC3:8097），全部带 CORS 头：
  GET /map   二进制体 = N 个 int16[3]（x/y/z，毫米，小端），响应头
             X-Map-Points / X-Map-Age-Ms / X-Map-Voxel
  GET /pose  JSON：{x, y, z, yaw, age_ms}
  GET /path  二进制体 = M 个 int16[3] 行走轨迹折线点（毫米）
"""
from __future__ import annotations

import json
import sys
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import rospy
from nav_msgs.msg import Odometry
from sensor_msgs.msg import Imu
from sensor_msgs import point_cloud2 as pc2
from sensor_msgs.msg import PointCloud2

PORT = 8097
VOXEL = 0.05              # 累计体素边长（米）
MAX_POINTS = 400_000      # 发布上限：超出时按步长抽稀（内存中仍保留全部体素）
PUBLISH_INTERVAL = 1.0    # /map 发布间隔（秒）
PATH_STEP = 0.2           # 轨迹折线最小位移（米），原地抖动不刷点

# 两把锁分工（2026-11-20 实测）：on_body_cloud 以 10Hz 在锁内跑逐点累计循环，
# 若与位姿共用一把锁，/pose 会被点云累计阻塞上百毫秒，演示执行器的指令流
# 因此断流（帧过期→零速→步态重置）。位姿/轨迹用独立小锁，读取永不等待地图。
_pose_lock = threading.Lock()
_map_lock = threading.Lock()
# 体素键（int64 打包）→ 该格首个命中点（世界系 xyz，米）
_voxel_map: dict[int, tuple[float, float, float]] = {}
_map_payload = b""
_map_mono = 0.0
_pose = {"x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0}
_pose_quat = {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}
_pose_mono = 0.0
_path_points: list[tuple[float, float, float]] = []
# Mid360 内置 IMU 陀螺 z 积分：快速原地旋转时扫描匹配会滞后/滑移，
# 旋转闭环用它做反馈（原生日志号：实测右转为正，与 ROS 左正相反）。
# _imu_mono 用本机单调接收时刻计龄：HTTP 轮询、重复样本或停更的源
# 时间戳都不能刷新有效性；尚无样本时显式置无效，防止初始 0 被当成
# 实际转角（2026-11-20 任务书第五节）。
_imu_yaw = 0.0
_imu_last_t = None
_imu_mono = None
_imu_bias = 0.0   # 陀螺零偏（rad/s），POST /imu/calibrate 时采样设置
_BRIDGE_BOOT = time.strftime("%Y%m%dT%H%M%S")   # 桥进程启动标识，区分重启
_path_payload = b""
_last_path_xyz: tuple[float, float, float] | None = None


def _quat_matrix(x: float, y: float, z: float, w: float) -> np.ndarray:
    """四元数 → 3x3 旋转矩阵（roll/pitch/yaw 完整保留，机器人会俯仰）。"""
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ], dtype=np.float64)


def _voxel_keys(world: np.ndarray) -> np.ndarray:
    """世界坐标（N,3）→ 打包体素键（N,），20 位/轴精确无碰撞。"""
    grid = np.floor(world / VOXEL).astype(np.int64)
    return ((grid[:, 0] & 0xFFFFF) << 40) | ((grid[:, 1] & 0xFFFFF) << 20) | (grid[:, 2] & 0xFFFFF)


def on_odom(odom: Odometry) -> None:
    """位姿回调：缓存位置/偏航/完整四元数，并按位移阈值落轨迹点。"""
    global _pose_mono, _last_path_xyz
    p = odom.pose.pose.position
    q = odom.pose.pose.orientation
    siny = 2.0 * (q.w * q.z + q.x * q.y)
    cosy = 1.0 - 2.0 * (q.y * q.y + q.z * q.z)
    xyz = (p.x, p.y, p.z)
    with _pose_lock:
        _pose.update(x=p.x, y=p.y, z=p.z, yaw=math.atan2(siny, cosy))
        _pose_quat.update(x=q.x, y=q.y, z=q.z, w=q.w)
        _pose_mono = time.monotonic()
        # 轨迹点按位移增量落盘，静止时不会重复堆积
        if _last_path_xyz is None or math.dist(_last_path_xyz, xyz) >= PATH_STEP:
            _last_path_xyz = xyz
            _path_points.append(xyz)


def on_imu(msg: Imu) -> None:
    """陀螺积分（约 200Hz，仅取小锁更新，不与点云累计互卡）。"""
    global _imu_yaw, _imu_last_t, _imu_mono
    stamp = msg.header.stamp.to_sec()
    t = stamp if stamp > 0.0 else time.time()
    with _pose_lock:
        last = _imu_last_t
        if last is not None and 0.0 < t - last < 0.1:
            _imu_yaw += (msg.angular_velocity.z - _imu_bias) * (t - last)
        _imu_last_t = t
        _imu_mono = time.monotonic()
        _imu_wz_samples.append(msg.angular_velocity.z)
        del _imu_wz_samples[:-200]   # 约 1 秒的 200Hz 样本


def calibrate_imu_bias() -> float:
    """采样 1 秒陀螺数据取平均作为零偏（转向前调用，消除温漂累计误差）。"""
    global _imu_bias
    import rospy as _rospy
    samples = []
    start = time.monotonic()
    # 直接从最近的消息流采样：借用回调里缓存的原始角速度
    while time.monotonic() - start < 1.0 and not _rospy.is_shutdown():
        time.sleep(0.01)
    # on_imu 已在更新 _imu_last_wz（下面加这个缓存），采样期结束后取均值
    if _imu_wz_samples:
        _imu_bias = sum(_imu_wz_samples) / len(_imu_wz_samples)
    _imu_wz_samples.clear()
    return _imu_bias

_imu_wz_samples: list = []   # 零偏采样窗口的原始角速度


def on_body_cloud(cloud: PointCloud2) -> None:
    """每帧扫描：用缓存位姿变换到世界系，累计进体素哈希。"""
    with _pose_lock:
        has_pose = _pose_mono > 0.0
        position = np.array([_pose["x"], _pose["y"], _pose["z"]], dtype=np.float64)
        quat = dict(_pose_quat)
    if not has_pose:
        return  # 尚未收到位姿（开机最初几帧），丢弃
    try:
        # 该 noetic 的 point_cloud2 助手函数不全，直接按 fields 偏移解析原始缓冲
        offsets = {field.name: field.offset for field in cloud.fields}
        step = cloud.point_step
        count = len(cloud.data) // step if step else 0
        if count == 0 or not {"x", "y", "z"} <= offsets.keys():
            return
        dtype = np.dtype({"names": ["x", "y", "z"], "formats": [np.float32] * 3,
                          "offsets": [offsets["x"], offsets["y"], offsets["z"]],
                          "itemsize": step})
        pts = np.frombuffer(cloud.data, dtype=dtype, count=count)
        body = np.column_stack([pts["x"], pts["y"], pts["z"]]).astype(np.float32)
        body = body[np.isfinite(body).all(axis=1)]
    except Exception as error:
        rospy.logwarn(f"body_cloud 解析失败：{error}")
        return
    if body.size == 0:
        return
    # 位姿与点云同为 10Hz，误差为一帧时延；避免回调内再订阅造成时序抖动
    world = body.astype(np.float64) @ _quat_matrix(quat["x"], quat["y"], quat["z"], quat["w"]).T + position
    keys = _voxel_keys(world)
    added = 0
    with _map_lock:
        voxel_map = _voxel_map
        for key, point in zip(keys.tolist(), world.tolist()):
            if key not in voxel_map:
                voxel_map[key] = point
                added += 1
    if added and rospy.get_param("~verbose", False):
        rospy.loginfo(f"累计 +{added} 体素，共 {len(_voxel_map)}")


def _pack_points(points: np.ndarray) -> bytes:
    mm = np.clip(points * 1000.0, -32767, 32767).astype("<i2")
    return mm.tobytes()


def publish_loop() -> None:
    """定时打包累计地图与轨迹；抽稀保证单帧体积在局域网预算内。"""
    global _map_payload, _map_mono, _path_payload
    while not rospy.is_shutdown():
        time.sleep(PUBLISH_INTERVAL)
        with _map_lock:
            values = list(_voxel_map.values())
        with _pose_lock:
            path = np.asarray(_path_points, dtype=np.float32) if _path_points else None
        if not values:
            continue
        cloud = np.asarray(values, dtype=np.float32)
        if len(cloud) > MAX_POINTS:
            step = (len(cloud) + MAX_POINTS - 1) // MAX_POINTS
            cloud = cloud[::step]
        payload = _pack_points(cloud)
        with _map_lock:
            _map_payload = payload
            _map_mono = time.monotonic()
            if path is not None:
                _path_payload = _pack_points(path)


class Handler(BaseHTTPRequestHandler):
    def _common(self, body: bytes, content_type: str, extra: dict = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - 标准库方法名
        if self.path == "/map":
            with _map_lock:
                payload, mono = _map_payload, _map_mono
            if not payload:
                self.send_error(503, "map not ready")
                return
            age = int(max(0.0, (time.monotonic() - mono) * 1000.0))
            self._common(payload, "application/octet-stream",
                         {"X-Map-Points": str(len(payload) // 6),
                          "X-Map-Age-Ms": str(age),
                          "X-Map-Voxel": f"{VOXEL:.3f}"})
        elif self.path == "/pose":
            with _pose_lock:
                pose, mono = dict(_pose), _pose_mono
                imu_yaw, imu_mono = _imu_yaw, _imu_mono
            age_ms = int(max(0.0, (time.monotonic() - mono) * 1000.0)) if mono else None
            imu_age_ms = int(max(0.0, (time.monotonic() - imu_mono) * 1000.0)) if imu_mono else None
            self._common(json.dumps({**pose, "age_ms": age_ms,
                                     "imu_yaw": round(imu_yaw, 6),
                                     "imu_age_ms": imu_age_ms,
                                     "imu_valid": imu_mono is not None,
                                     "bridge_boot": _BRIDGE_BOOT}).encode(), "application/json")
        elif self.path == "/imu/calibrate":
            # 转向前校零：采样 1 秒陀螺取均值，从积分中扣除
            bias = calibrate_imu_bias()
            self._common(json.dumps({"ok": True, "bias": round(bias, 6)}).encode(),
                         "application/json")
        elif self.path == "/path":
            with _map_lock:
                payload = _path_payload
            if not payload:
                self.send_error(503, "path not ready")
                return
            self._common(payload, "application/octet-stream")
        else:
            self.send_error(404)

    def log_message(self, *args: object) -> None:
        pass  # 静默访问日志


def main() -> None:
    # GIL 快切换（2026-11-23）：点云累计线程是纯 Python 长循环，默认 5ms 的
    # 线程切换周期下 HTTP 应答线程在行走建图的插入爆发期抢不到执行权，
    # /pose 出现秒级阵发停顿（12 步实测因此触发执行器盲转保护）。把切换
    # 周期压到 1ms，让 /pose 的处理与网络写有机会插进累积循环的间隙。
    sys.setswitchinterval(0.001)
    rospy.init_node("h2_map_bridge", anonymous=True)
    rospy.Subscriber("/body_cloud", PointCloud2, on_body_cloud,
                     queue_size=1, buff_size=1 << 24)
    rospy.Subscriber("/slam_odom", Odometry, on_odom, queue_size=10)
    rospy.Subscriber("/livox/imu", Imu, on_imu, queue_size=400)
    threading.Thread(target=publish_loop, daemon=True, name="publish").start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    rospy.loginfo(f"h2_map_bridge v2 serving :{PORT}/map /pose /path")
    server.serve_forever()


if __name__ == "__main__":
    main()
