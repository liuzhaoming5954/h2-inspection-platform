"""使用当前环境的 grpcio-tools 生成纳入版本管理的 Protobuf 桩代码。"""

from pathlib import Path

from grpc_tools import protoc


root = Path(__file__).resolve().parent
contracts = root.parent / "contracts"
output = root / "src" / "h2_gateway" / "generated"
output.mkdir(parents=True, exist_ok=True)
result = protoc.main(
    [
        "grpc_tools.protoc",
        f"-I{contracts}",
        f"--python_out={output}",
        f"--grpc_python_out={output}",
        str(contracts / "robot_gateway.proto"),
    ]
)
if result:
    raise SystemExit(result)
grpc_file = output / "robot_gateway_pb2_grpc.py"
grpc_file.write_text(
    grpc_file.read_text().replace(
        "import robot_gateway_pb2 as robot__gateway__pb2",
        "from . import robot_gateway_pb2 as robot__gateway__pb2",
    )
)
print(f"Generated stubs in {output}")
