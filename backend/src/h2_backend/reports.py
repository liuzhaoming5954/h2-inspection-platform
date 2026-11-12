import base64
from io import BytesIO
import json
from pathlib import Path
from typing import Any

from jinja2 import Environment, select_autoescape
from openpyxl import Workbook
from weasyprint import HTML


TEMPLATE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<style>
@page { size:A4; margin:18mm; @bottom-center { content:counter(page); font-size:9pt } }
body { font:10pt 'Noto Sans CJK SC','WenQuanYi Micro Hei','DejaVu Sans',sans-serif; color:#183044 }
h1 { font-size:20pt; margin-bottom:6mm } h2 { font-size:13pt }
.demo { padding:9px; background:#fff1ce; border:1px solid #c48920 }
table { border-collapse:collapse; width:100%; margin:8mm 0 } th,td { border:1px solid #ccd6dd; padding:6px; text-align:left }
th { background:#eaf0f4 } .sample { break-inside:avoid; margin-top:8mm } img { width:125mm; max-height:75mm; object-fit:contain }
.note { white-space:pre-wrap } .meta { overflow-wrap:anywhere }
</style><title>巡检技术验证报告</title>
<h1>巡检技术验证报告</h1><p class="demo">模拟环境 · 所有传感器数值和图片均为合成数据，不代表实际设备检测结果。</p>
<p class="meta">任务：{{ task.name }}<br>任务编号：{{ task.id }}<br>状态：{{ task.status }}<br>创建时间：{{ task.created_at }}</p>
<h2>采集结果</h2><table><thead><tr><th>巡检点</th><th>温度 / ℃</th><th>湿度 / %RH</th><th>采集时间</th></tr></thead><tbody>
{% for s in task.samples %}<tr><td>{{ s.label }}</td><td>{{ '%.2f'|format(s.temperature_c) }}</td><td>{{ '%.2f'|format(s.humidity_percent) }}</td><td>{{ s.captured_at }}</td></tr>{% endfor %}
</tbody></table><h2>人工复核意见</h2><p class="note">{{ task.review_note or '尚未填写复核意见' }}</p>
{% for s in images %}<div class="sample"><h2>{{ s.label }} · 合成图像</h2><img src="data:image/png;base64,{{ s.image }}"></div>{% endfor %}
</html>"""


def text_cell(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def render_reports(task: dict[str, Any], images: list[dict[str, Any]]) -> dict[str, tuple[bytes, str]]:
    env = Environment(autoescape=select_autoescape(default=True))
    embedded = [{"label": s["label"], "image": base64.b64encode(s["bytes"]).decode()} for s in images]
    html = env.from_string(TEMPLATE).render(task=task, images=embedded)
    pdf = HTML(string=html).write_pdf()
    book = Workbook()
    sheet = book.active
    sheet.title = "巡检结果"
    sheet.append(["模拟数据 / SIMULATED", text_cell(task["name"]), task["id"]])
    sheet.append(["任务状态", task["status"], "复核意见", text_cell(task["review_note"])])
    sheet.append(["巡检点", "温度 ℃", "湿度 %RH", "采集时间", "模拟数据"])
    for sample in task["samples"]:
        sheet.append([text_cell(sample["label"]), sample["temperature_c"], sample["humidity_percent"], sample["captured_at"], True])
    for column, width in {"A": 28, "B": 24, "C": 42, "D": 36, "E": 16}.items():
        sheet.column_dimensions[column].width = width
    sheet.freeze_panes = "A4"
    binary = BytesIO()
    book.save(binary)
    export = {**task, "data_source": "simulated", "report_notice": "Technical validation only; all readings and images are synthetic."}
    export.pop("reports", None)
    return {
        "pdf": (pdf, "application/pdf"),
        "xlsx": (binary.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        "json": (json.dumps(export, ensure_ascii=False, indent=2).encode(), "application/json"),
    }
