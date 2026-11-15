#!/usr/bin/env python3
"""用实际运行的 HTTP 服务、PostgreSQL 和 gRPC 模拟器验证报告生成链路。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import time
import urllib.request
from uuid import uuid4

from openpyxl import load_workbook
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts' / 'validation'


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    args = parser.parse_args()
    base = args.base_url.rstrip('/')
    OUT.mkdir(parents=True, exist_ok=True)

    def request(path: str, body=None):
        encoded = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=encoded,
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.loads(response.read())

    health = request('/api/health')
    assert health['status'] == 'ok' and health['database'] == 'postgresql', health
    initial = request('/api/robot')
    points = [{'id': 'A01', 'label': 'A01机柜', 'x': -3, 'y': -1.8},
              {'id': 'A02', 'label': 'A02机柜', 'x': 0, 'y': -1.8},
              {'id': 'B01', 'label': 'B01机柜', 'x': 3, 'y': 1.8}]
    payload = {'name': '技术验证：三点巡检与报告', 'request_id': str(uuid4()), 'points': points}
    task = request('/api/tasks', payload)
    duplicate = request('/api/tasks', payload)
    assert task['id'] == duplicate['id']
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        task = request(f"/api/tasks/{task['id']}")
        if task['status'] == 'completed':
            break
        time.sleep(0.2)
    assert task['status'] == 'completed', task
    assert len(task['samples']) == 3 and task['progress'] == 100, task
    assert all(sample['simulated'] for sample in task['samples'])
    after = request('/api/robot')
    assert after['start_count'] == initial['start_count'] + 1, (initial, after)
    note = '人工复核：本轮为合成数据，温湿度、图片与任务点关联正确。'
    task = request(f"/api/tasks/{task['id']}/review", {'note': note})
    reports = request(f"/api/tasks/{task['id']}/reports", {})
    outputs = {}
    for report in reports['reports']:
        with urllib.request.urlopen(base + report['url'], timeout=30) as response:
            binary = response.read()
        path = OUT / f"sample-report.{report['format']}"
        path.write_bytes(binary)
        outputs[report['format']] = {'path': str(path), 'bytes': len(binary)}
        if report['format'] == 'pdf':
            assert binary.startswith(b'%PDF-')
        elif report['format'] == 'xlsx':
            sheet = load_workbook(BytesIO(binary)).active
            assert sheet.max_row == 6
            assert sheet['B4'].value == task['samples'][0]['temperature_c']
        elif report['format'] == 'json':
            exported = json.loads(binary)
            assert exported['review_note'] == note
            assert exported['data_source'] == 'simulated'
            assert len(exported['samples']) == 3
    assert set(outputs) == {'pdf', 'xlsx', 'json'}
    with urllib.request.urlopen(base + task['samples'][0]['image_url']) as response:
        image_data = response.read()
    Image.open(BytesIO(image_data)).verify()
    (OUT / 'sample-evidence.png').write_bytes(image_data)

    # 使用耗时较长的任务，便于在任务自然完成前验证停止操作的回执。
    stop_payload = {'name': '技术验证：直接停止', 'request_id': str(uuid4()),
                    'points': [{**points[i % 3], 'id': f'stop-{i}'} for i in range(10)]}
    stopping = request('/api/tasks', stop_payload)
    started = time.monotonic()
    stopped = request(f"/api/tasks/{stopping['id']}/stop", {})
    stop_elapsed_ms = (time.monotonic() - started) * 1000
    assert stopped['status'] == 'stopped', stopped
    result = {
        'passed': True, 'run_at': datetime.now(timezone.utc).isoformat(),
        'mode': 'simulated robot and synthetic samples; real HTTP/PostgreSQL/gRPC/files',
        'health': health, 'task_id': task['id'],
        'checks': {'request_id_deduplicated': True, 'one_robot_start_for_duplicate_requests': True,
                   'three_points_completed': True, 'samples_persisted': True,
                   'png_decoded': True, 'human_review_persisted': True,
                   'pdf_excel_json_exported': True, 'direct_stop_confirmed': True},
        'stop_round_trip_ms_observed': round(stop_elapsed_ms, 2),
        'timing_notice': 'Local simulator measurement, not a hardware emergency-stop guarantee.',
        'reports': outputs,
    }
    (OUT / 'live-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
