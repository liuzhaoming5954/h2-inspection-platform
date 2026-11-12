#!/usr/bin/env python3
"""Execute a deterministic synthetic ONNX model; not a business accuracy test."""
from __future__ import annotations

import json
import platform
from pathlib import Path
import time

import cv2
import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper
import onnxruntime as ort

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts' / 'validation'


def main() -> None:
    ort.disable_telemetry_events()
    OUT.mkdir(parents=True, exist_ok=True)
    # A tiny affine graph with known results verifies the installed CPU provider
    # and OpenCV -> NumPy -> ONNX Runtime preprocessing path.
    image = np.zeros((32, 48, 3), dtype=np.uint8)
    image[:, :, 1] = 128
    resized = cv2.resize(image, (16, 16), interpolation=cv2.INTER_AREA)
    features = np.array([[resized[:, :, 1].mean() / 255.0, 0.25]], dtype=np.float32)
    weights = np.array([[2.0, -1.0], [0.5, 3.0]], dtype=np.float32)
    bias = np.array([1.0, -0.5], dtype=np.float32)
    graph = helper.make_graph(
        [helper.make_node('MatMul', ['features', 'weights'], ['product']),
         helper.make_node('Add', ['product', 'bias'], ['output'])],
        'h2-synthetic-runtime-smoke',
        [helper.make_tensor_value_info('features', TensorProto.FLOAT, [None, 2])],
        [helper.make_tensor_value_info('output', TensorProto.FLOAT, [None, 2])],
        [numpy_helper.from_array(weights, 'weights'), numpy_helper.from_array(bias, 'bias')],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid('', 17)])
    model.ir_version = 9
    onnx.checker.check_model(model)
    model_path = OUT / 'synthetic-smoke.onnx'
    onnx.save(model, model_path)
    session = ort.InferenceSession(str(model_path), providers=['CPUExecutionProvider'])
    for _ in range(5):
        actual = session.run(None, {'features': features})[0]
    expected = features @ weights + bias
    np.testing.assert_allclose(actual, expected, rtol=1e-6, atol=1e-6)
    started = time.perf_counter()
    for _ in range(100):
        session.run(None, {'features': features})
    mean_ms = (time.perf_counter() - started) * 10
    result = {
        'passed': True,
        'scope': 'synthetic CPU runtime and preprocessing smoke test only',
        'business_accuracy_validated': False,
        'python': platform.python_version(),
        'architecture': platform.machine(),
        'onnxruntime': ort.__version__,
        'opencv': cv2.__version__,
        'providers': session.get_providers(),
        'telemetry_events_enabled': False,
        'model': model_path.name,
        'output': actual.tolist(),
        'expected': expected.tolist(),
        'tiny_graph_mean_ms': mean_ms,
        'note': 'Tiny synthetic graph timing is not a real robot model benchmark.',
    }
    (OUT / 'inference-result.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
