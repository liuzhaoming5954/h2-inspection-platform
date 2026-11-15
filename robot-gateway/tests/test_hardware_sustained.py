"""持续运动回归测试，覆盖 FSM 轮询得不到执行和 SDK 回执迟到。"""

import time

from h2_gateway.hardware import H2HardwareGateway

from test_hardware import RecordingLoco, wait_for


class SlowAckLoco(RecordingLoco):
    """模拟 DDS 繁忙时的往返延迟，velocity() 耗时超过指令帧有效期。"""

    def __init__(self, delay: float = 0.30):
        super().__init__()
        self.delay = delay

    def velocity(self, vx, vy, omega, duration):
        time.sleep(self.delay)
        return super().velocity(vx, vy, omega, duration)


def test_sustained_frames_keep_the_lease_alive():
    # 曾经每 200 ms 发送一帧、单帧有效期 250 ms 时，FSM 轮询无法执行，
    # 导致状态过期、_ready() 检查失败，并在运动中撤销租约。
    # 持续接收运动帧时，监测循环仍需轮询 FSM。
    gateway = H2HardwareGateway(RecordingLoco())
    try:
        wait_for(gateway, lambda state: state["teleop_ready"])
        token = gateway.teleop_acquire()["token"]
        deadline = time.monotonic() + 2.5
        seq = 0
        while time.monotonic() < deadline:
            seq += 1
            reply = gateway.teleop_command(token, seq, "forward",
                                           time.monotonic_ns() // 1_000_000 + 350)
            assert reply["accepted"], reply
            time.sleep(0.2)
        assert gateway.get_state()["teleop_active"] is True
        assert gateway.get_state()["status"] == "teleop"
    finally:
        gateway.close()


def test_late_sdk_acknowledgement_halts_motion_but_keeps_the_lease():
    gateway = H2HardwareGateway(SlowAckLoco(delay=0.30))
    loco = gateway._loco
    try:
        wait_for(gateway, lambda state: state["teleop_ready"])
        token = gateway.teleop_acquire()["token"]
        # 剩余时间约 350 ms，实际帧时长为 250 ms。
        # RPC 等待 300 ms，回执到达时该帧已过期。
        reply = gateway.teleop_command(token, 1, "forward",
                                       time.monotonic_ns() // 1_000_000 + 350)
        assert reply["accepted"] and reply["code"] == "stopped", reply
        # 迟到的指令帧执行后，按顺序补发零速度指令。
        assert loco.commands.get(timeout=1)[0] == (0.5, 0, 0)
        assert loco.commands.get(timeout=1)[0] == (0, 0, 0)
        # 租约仍然有效，下一条指令可直接执行。
        reply = gateway.teleop_command(token, 2, "heartbeat",
                                       time.monotonic_ns() // 1_000_000 + 350)
        assert reply["accepted"] and reply["code"] == "renewed", reply
        assert gateway.get_state()["teleop_active"] is True
    finally:
        gateway.close()
