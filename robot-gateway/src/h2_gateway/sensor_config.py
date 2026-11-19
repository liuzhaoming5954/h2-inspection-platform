"""解析 PC2 温湿度代理的访问地址。"""

import os


DEFAULT_PC2_HOST = "192.168.124.162"


def pc2_agent_url(host: str) -> str:
    return f"http://{host}:8090/environment"


def pc2_vision_url(host: str) -> str:
    """PC2 视觉代理接口，转发头部相机识别结果。"""
    return f"http://{host}:8090/vision"


def pc2_vision_frame_url(host: str) -> str:
    """PC2 视觉代理接口，返回最新一帧带检测框的预览 JPEG。"""
    return f"http://{host}:8090/vision/frame"


def pc3_sensor_agent_url(host: str) -> str:
    return os.getenv("H2_SENSOR_AGENT_URL") or pc2_agent_url(host)
