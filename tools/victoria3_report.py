"""Evidence-backed Victoria 3 reports, independent of model explanations."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
import sqlite3
from contextlib import closing
from uuid import uuid4

from tools.victoria3 import compare_runs


SETTING_LABELS = {"resolution": "分辨率", "quality": "画质模式", "multi_sampling": "多重采样",
                  "anisotropic_filtering": "各向异性过滤", "refraction_quality": "折射质量",
                  "shadowmap_resolution": "阴影分辨率"}
PHASE_LABELS = {"assessment": "本机诊断 · 尚未应用", "applied": "设置已应用 · 待性能验证",
                "restored": "设置已恢复", "external_change": "当前设置已发生额外变化"}
METRICS = (("avg_fps", "平均 FPS", "FPS"), ("low_fps", "1% Low", "FPS"),
           ("days_per_second", "模拟推进速度", "游戏天/秒"), ("gpu_usage", "GPU 占用率", "%"),
           ("cpu_usage", "CPU 占用率", "%"), ("gpu_temperature", "GPU 温度", "℃"),
           ("cpu_temperature", "CPU 温度", "℃"), ("gpu_power", "GPU 功耗", "W"))


def new_report(machine: dict, before: dict, after: dict | None = None, *, phase="assessment",
               backup: str = "", mode: str = "") -> dict:
    before, after = deepcopy(before), deepcopy(after if after is not None else before)
    changes = [{"id": key, "label": SETTING_LABELS.get(key, key),
                "before": before.get("Graphics", {}).get(key),
                "after": after.get("Graphics", {}).get(key)}
               for key in sorted(set(before.get("Graphics", {})) | set(after.get("Graphics", {})))
               if before.get("Graphics", {}).get(key) != after.get("Graphics", {}).get(key)]
    return {"id": uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat(),
            "phase": phase, "machine": {key: value for key, value in machine.items() if key not in ("settings", "saves")},
            "before": before, "after": after, "changes": changes, "backup": backup, "mode": mode,
            "observations": {"baseline": {}, "candidate": {}}, "sources": {},
            "comparison_confirmed": False, "events": [{"phase": phase, "at": datetime.now(timezone.utc).isoformat()}]}


def attach_benchmark(report: dict, side: str, metrics: dict, source: str) -> dict:
    if side not in ("baseline", "candidate"):
        raise ValueError("未知性能记录位置")
    if side == "candidate" and report["phase"] == "assessment":
        raise ValueError("尚未应用方案，不能记录优化后结果")
    updated = deepcopy(report)
    updated["observations"][side] = {key: metrics[key] for key in
        ("avg_fps", "low_fps", "gpu_usage", "run_count", "avg_spread_pct", "low_spread_pct") if key in metrics}
    # Keep simulation observations from a separately controlled experiment.
    for key in ("days_per_second", "simulation_spread_pct", "simulation_repeats", "cpu_usage", "gpu_temperature", "cpu_temperature", "gpu_power"):
        if key in report["observations"][side]:
            updated["observations"][side][key] = report["observations"][side][key]
    updated["sources"][side + "_fps"] = source
    updated["comparison_confirmed"] = False
    return updated


def attach_simulation(report: dict, experiment: dict) -> dict:
    try:
        before = json.loads(experiment["baseline"]["fields"]["configuration"])
        after = json.loads(experiment["candidate"]["fields"]["configuration"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("高级测试需记录 JSON 图形配置快照，才能关联此自动优化报告") from error
    if before != report["before"]["Graphics"] or after != report["after"]["Graphics"]:
        raise ValueError("高级测试配置与本报告不一致，未关联这些性能数据")
    result = compare_runs(experiment["baseline"], experiment["candidate"], experiment["configuration_confirmed"])
    updated = deepcopy(report)
    updated["simulation"] = {"result": result, "conditions": experiment["baseline"]["conditions"],
                             "configuration_confirmed": experiment["configuration_confirmed"]}
    for side, key in (("baseline", "baseline"), ("candidate", "candidate")):
        metrics = result[key]
        updated["observations"][side].update(days_per_second=metrics["days_per_second"],
            simulation_spread_pct=metrics["spread_pct"], simulation_repeats=metrics["repeats"])
        updated["sources"][side + "_simulation"] = "配置匹配的高级受控实验"
    return updated


def metric_rows(report: dict) -> list[list[str]]:
    before, after = (report["observations"][key] for key in ("baseline", "candidate"))
    rows = []
    for key, label, unit in METRICS:
        a, b = before.get(key), after.get(key)
        delta, status = "—", "未采集"
        if a is not None or b is not None:
            status = "仅有一侧数据"
        allowed = report.get("comparison_confirmed", False)
        if key == "days_per_second":
            allowed = report.get("simulation", {}).get("configuration_confirmed", False) or (
                report.get('comparison_confirmed', False) and all(report.get('sensor_status', {}).get(side, {}).get(key, {}).get('status') == 'ok' for side in ('baseline', 'candidate')))
        if a is not None and b is not None:
            if allowed:
                delta = f"{b - a:+.2f} 个百分点" if unit == "%" else (f"{b - a:+.2f} {unit}" if unit == '℃' or a == 0 else f"{(b / a - 1) * 100:+.2f}%")
                repeats = min(before.get("run_count", 0), after.get("run_count", 0))
                spread = max(before.get("avg_spread_pct", 0), before.get("low_spread_pct", 0),
                             after.get("avg_spread_pct", 0), after.get("low_spread_pct", 0))
                status = "已确认归属的实测对比" if repeats >= 3 and spread <= 5 else "样本不足或波动较大，仅供参考"
                if key == "days_per_second":
                    result = report.get("simulation", {}).get("result")
                    status = result["reason"] if result else '游戏日期 OCR 参考测量，含整数日边界误差；未完成受控重复实验'
            else:
                status = "待确认配置归属与场景一致性"
        if report["phase"] in ("restored", "external_change") and b is not None:
            status += "；优化后数据仅代表历史配置"
        def fmt(value, side):
            if value is not None:
                return f"{value:.2f} {unit}"
            sensor = report.get('sensor_status', {}).get(side, {}).get(key)
            if not sensor:
                return '未采集'
            name = {'unsupported': '设备或接口不支持', 'not_supported': '暂不支持自动采集',
                    'error': '采集失败', 'warming_up': '样本不足', 'not_sampled': '未采集'}.get(sensor['status'], '未采集')
            return name + ('：' + sensor['reason'] if sensor.get('reason') else '')
        if key in ('gpu_usage', 'cpu_usage', 'gpu_temperature', 'cpu_temperature', 'gpu_power'):
            sensors = [report.get('sensor_status', {}).get(side, {}).get(key, {}) for side in ('baseline', 'candidate')]
            sources = [row.get('scope', '') for row in sensors if row.get('status') == 'ok']
            if sources:
                if a is not None and b is not None and allowed:
                    status = '硬件传感器均值参考对比'
                    if any(row.get('runs', 0) < 3 or row.get('failed_runs', 0) for row in sensors):
                        status += '；有效重复测试不足或存在缺测'
                status += '；' + sources[-1]
        rows.append([label, fmt(a, 'baseline'), fmt(b, 'candidate'), delta, status])
    return rows


def advice(report: dict) -> list[str]:
    rows = report["observations"]
    notes = []
    if not rows["baseline"] or not rows["candidate"]:
        notes.append("先补齐优化前后同场景的三次测试；当前无法判断 FPS 水平、跑日收益或硬件瓶颈。")
    gpu = rows["candidate"].get("gpu_usage")
    if gpu is not None and gpu >= 95:
        notes.append("候选测试 GPU 占用接近满载：可逐项测试阴影、抗锯齿与分辨率，观察 FPS 和画质取舍。占用率本身不等于确定瓶颈。")
    elif gpu is not None and gpu < 75:
        notes.append("候选测试 GPU 占用偏低：先检查限帧、垂直同步及测试场景，再比较分辨率；不能仅据此认定 CPU 瓶颈。")
    notes.append("模拟推进速度应固定存档、游戏日期区间、速度档位和模组单独测试；画面 FPS 提升不能证明跑日更快。")
    if "Laptop" in report["machine"].get("gpu", ""):
        notes.append("笔记本建议：插电并核对厂商性能/散热模式，观察长时间温度与功耗；本工具未更改这些系统选项。")
    notes.append("若后期存档仍卡顿：以同一存档逐个排查模组影响，并记录自动存档触发时刻；先备份存档再做对照。")
    if len(report["changes"]) > 1:
        notes.append("本轮同时调整多项画质：即便性能改善，也不能把收益归因于其中单一设置。高级实验应一次只改一项。")
    if report.get("simulation"):
        notes.insert(0, "受控跑日实验结论：" + report["simulation"]["result"]["reason"])
    return notes


def report_html(report: dict) -> str:
    e = lambda value: escape(str(value if value is not None else "游戏默认（未显式保存）"))
    def table(headers, rows):
        return '<table width="100%" cellspacing="0" cellpadding="8" border="1"><tr>' + ''.join(
            '<th bgcolor="#e9f0df">' + e(cell) + '</th>' for cell in headers) + '</tr>' + ''.join(
            '<tr>' + ''.join('<td>' + e(cell) + '</td>' for cell in row) + '</tr>' for row in rows) + '</table>'
    machine = report["machine"]
    html = '<html><head><meta charset="utf-8"></head><body style="font-family:Microsoft YaHei UI;color:#202422">'
    html += '<h1>Victoria 3 优化报告</h1><p>' + e(PHASE_LABELS[report["phase"]]) + '</p>'
    html += '<p>生成时间：' + e(report["created_at"]) + ' · 报告编号：' + e(report["id"][:12]) + '</p>'
    html += '<h2>本机配置与来源</h2>' + table(["项目", "读取结果"], [
        ["游戏版本", machine.get("version", "未识别")], ["CPU", machine.get("cpu", "未识别")],
        ["GPU", machine.get("gpu", "未识别")], ["内存", machine.get("ram", "未识别")],
        ["操作系统", machine.get("os", "未识别")], ["硬件来源", machine.get("hardware_source", "最近一次启动日志")]])
    html += '<h2>优化前后性能水平</h2><p>指标来自导入或受控实验。未采集项不估算；GPU 占用变化不直接代表性能收益。</p>'
    html += table(["指标", "优化前实测", "优化后实测", "变化", "证据状态"], metric_rows(report))
    if report.get('sensor_status'):
        html += '<h2>传感器来源与采样情况</h2>' + table(['阶段', '指标', '来源/原因', '有效样本', '峰值'], [
            ['优化前' if side == 'baseline' else '优化后', next((label for ident, label, _ in METRICS if ident == key), key),
             row.get('source') or row.get('reason', ''), row.get('samples', 0), row.get('peak', '—')]
            for side, sensors in report['sensor_status'].items() for key, row in sensors.items()])
    html += '<h2>一键优化实际做了什么</h2>'
    html += table(["设置", "原始值", "写入值"], [[c["label"], c["before"], c["after"]] for c in report["changes"]]) if report["changes"] else '<p>尚未执行优化，没有已应用的改动。</p>'
    html += '<p>备份位置：' + e(report.get("backup") or "尚未创建") + '</p>'
    html += '<p>只处理图形设置；不修改存档、系统供电、风扇模式或模组。</p>'
    html += '<h2>完整设置快照</h2>' + table(["设置", "优化前", "优化后"], [
        [SETTING_LABELS.get(key, key), report["before"]["Graphics"].get(key), report["after"]["Graphics"].get(key)]
        for key in sorted(set(report["before"]["Graphics"]) | set(report["after"]["Graphics"]))])
    html += '<h2>高级优化建议</h2><ul>' + ''.join('<li>' + e(item) + '</li>' for item in advice(report)) + '</ul>'
    html += '<h2>数据来源与操作记录</h2>'
    html += '<p>' + e(json.dumps(report["sources"], ensure_ascii=False)) + '</p>'
    html += table(["状态", "时间"], [[PHASE_LABELS.get(event["phase"], event["phase"]), event["at"]] for event in report["events"]])
    return html + '</body></html>'


class ReportRepository:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS victoria3_reports (id TEXT PRIMARY KEY, created_at TEXT, payload TEXT)")

    def save(self, report: dict):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("INSERT OR REPLACE INTO victoria3_reports VALUES(?,?,?)", (
                report["id"], report["created_at"], json.dumps(report, ensure_ascii=False)))

    def list(self) -> list[dict]:
        with closing(sqlite3.connect(self.path)) as connection:
            return [json.loads(row[0]) for row in connection.execute("SELECT payload FROM victoria3_reports ORDER BY created_at DESC")]
