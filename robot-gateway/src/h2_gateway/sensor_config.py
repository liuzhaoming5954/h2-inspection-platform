"""解析 PC2 温湿度代理的访问地址。"""

import os


DEFAULT_PC2_HOST = "192.168.124.162"


def pc2_agent_url(host: str) -> str:
    return f"http://{host}:8090/environment"


def pc3_sensor_agent_url(host: str) -> str:
    return os.getenv("H2_SENSOR_AGENT_URL") or pc2_agent_url(host)
