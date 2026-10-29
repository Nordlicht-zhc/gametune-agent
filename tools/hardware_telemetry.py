"""Read-only, best-effort whole-machine sensors synchronized to frame capture."""
from __future__ import annotations

import csv
import ctypes
import io
import json
import math
import os
import shutil
from statistics import mean
import subprocess
from threading import Event, Thread
import time

KEYS = ('gpu_usage', 'cpu_usage', 'gpu_temperature', 'cpu_temperature', 'gpu_power')


def reading(value=None, status='unsupported', source='', reason=''):
    return dict(value=value, status=status, source=source, reason=reason)


def numeric(value, low, high):
    try:
        number = float(value)
        return number if math.isfinite(number) and low <= number <= high else None
    except (ValueError, TypeError):
        return None


def run(command, timeout=5):
    result = subprocess.run(command, capture_output=True, timeout=timeout,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError('传感器接口返回错误')
    return result.stdout.decode('utf-8-sig', errors='replace')


def gpu_readings(output, expected_name=''):
    rows = list(csv.reader(io.StringIO(output)))
    rows = [row for row in rows if len(row) == 5]
    matching = [row for row in rows if row[0].strip().lower() == expected_name.strip().lower()]
    chosen = matching if matching else rows if len(rows) == 1 else []
    keys = ('gpu_usage', 'gpu_temperature', 'gpu_power')
    if len(chosen) != 1:
        return {key: reading(reason='未检测到显卡或多显卡无法唯一匹配游戏设备') for key in keys}
    row = chosen[0]
    result = {}
    for key, raw, high in zip(keys, row[1:4], (100, 150, 2000)):
        value = numeric(raw.strip(), 0, high)
        result[key] = reading(value, 'ok' if value is not None else 'unsupported',
                              'nvidia-smi / ' + row[0].strip() + ' / ' + row[4].strip(),
                              '' if value is not None else '驱动未提供此传感器值')
    return result


class CpuUsage:
    def __init__(self):
        self.previous = None

    @staticmethod
    def percentage(before, after):
        idle, kernel, user = [a - b for a, b in zip(after, before)]
        total = kernel + user
        if min(idle, kernel, user) < 0 or total <= 0:
            return None
        return min(100., max(0., (total - idle) / total * 100))

    def read(self):
        if os.name != 'nt':
            return reading(reason='整机 CPU 计数器目前仅支持 Windows')
        values = [ctypes.c_ulonglong() for _ in range(3)]
        if not ctypes.windll.kernel32.GetSystemTimes(*(ctypes.byref(value) for value in values)):
            return reading(status='error', reason='GetSystemTimes 调用失败')
        current = tuple(value.value for value in values)
        value = self.percentage(self.previous, current) if self.previous else None
        self.previous = current
        return reading(value, 'ok' if value is not None else 'warming_up', 'Windows GetSystemTimes（整机）', '等待第二次采样' if value is None else '')


def cpu_temperature_from_rows(rows, source):
    rows = rows or []
    if isinstance(rows, dict):
        rows = [rows]
    candidates = [row for row in rows if row.get('SensorType') == 'Temperature'
                  and ('cpu' in str(row.get('Identifier', '')).lower())]
    package = [row for row in candidates if any(word in str(row.get('Name', '')).lower() for word in ('package', 'tctl', 'tdie'))]
    candidates = package or candidates
    values = [numeric(row.get('Value'), -20, 150) for row in candidates]
    values = [value for value in values if value is not None]
    if not values:
        return reading(reason='监测服务没有提供可识别的 CPU 温度传感器')
    return reading(max(values), 'ok', source + (' / CPU Package 最大值' if package else ' / CPU 核心最高温度'))


class Sensors:
    def __init__(self, gpu_name=''):
        self.cpu = CpuUsage()
        self.gpu_name = gpu_name
        self.smi = shutil.which('nvidia-smi')
        self.temperature_namespace = None
        self.temperature_probed = False

    def cpu_temperature(self):
        namespaces = [self.temperature_namespace] if self.temperature_probed else ['root/LibreHardwareMonitor', 'root/OpenHardwareMonitor']
        self.temperature_probed = True
        for namespace in namespaces:
            if namespace is None:
                continue
            command = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                       f"Get-CimInstance -Namespace '{namespace}' -ClassName Sensor -ErrorAction Stop | "
                       "Where-Object SensorType -eq 'Temperature' | Select-Object Name,SensorType,Value,Identifier | ConvertTo-Json -Compress")
            try:
                output = run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command], timeout=4)
                result = cpu_temperature_from_rows(json.loads(output or '[]'), namespace)
                if result['status'] == 'ok':
                    self.temperature_namespace = namespace
                    return result
            except (OSError, ValueError, subprocess.TimeoutExpired):
                if self.temperature_namespace:
                    return reading(status='error', source=namespace, reason='CPU 温度服务读取失败或超时')
        return reading(reason='未检测到运行中的 LibreHardwareMonitor/OpenHardwareMonitor CPU 温度接口；不将主板或 ACPI 温度冒充 CPU 温度')

    def sample(self):
        result = {'cpu_usage': self.cpu.read(), 'cpu_temperature': self.cpu_temperature()}
        if not self.smi:
            result.update({key: reading(reason='未检测到 NVIDIA 驱动采集接口') for key in ('gpu_usage', 'gpu_temperature', 'gpu_power')})
        else:
            try:
                output = run([self.smi, '--query-gpu=name,utilization.gpu,temperature.gpu,power.draw,uuid', '--format=csv,noheader,nounits'])
                result.update(gpu_readings(output, self.gpu_name))
            except (OSError, ValueError, subprocess.TimeoutExpired):
                result.update({key: reading(status='error', source='nvidia-smi', reason='驱动接口读取失败或超时') for key in ('gpu_usage', 'gpu_temperature', 'gpu_power')})
        return result


def summarize(samples):
    result = {}
    for key in KEYS:
        valid = [(sample['at_seconds'], sample['readings'][key]) for sample in samples if sample['readings'].get(key, {}).get('status') == 'ok']
        if valid:
            values = [row['value'] for _, row in valid]
            result[key] = dict(value=round(mean(values), 2), peak=round(max(values), 2), status='ok',
                               samples=len(values), source=valid[-1][1]['source'],
                               first_sample_seconds=valid[0][0], last_sample_seconds=valid[-1][0],
                               failed_samples=len(samples) - len(valid), scope='整机/整块显卡，包含其他进程负载')
        else:
            rows = [sample['readings'][key] for sample in samples if key in sample['readings']]
            row = next((row for row in reversed(rows) if row['status'] == 'error'), rows[-1] if rows else reading(status='not_sampled', reason='本次没有可用传感器样本'))
            result[key] = {**row, 'samples': 0}
    result['days_per_second'] = reading(status='not_supported', reason='当前无可靠自动游戏日期接口；请使用高级跑日测试，不以 FPS 代替跑日速度')
    return result


def attach_telemetry(report, side, captures):
    statuses = {}
    observations = report['observations'][side]
    for key in (*KEYS, 'days_per_second'):
        if key == 'days_per_second' and report.get('simulation'):
            continue  # Keep controlled experiment values and their decision together.
        rows = [item.get('telemetry', {}).get(key) for item in captures]
        rows = [row for row in rows if row]
        valid = [row for row in rows if row['status'] == 'ok']
        if valid:
            value = round(mean(row['value'] for row in valid), 2)
            observations[key] = value
            statuses[key] = dict(value=value, status='ok', runs=len(valid), samples=sum(row['samples'] for row in valid),
                                 peak=max(row['peak'] for row in valid), source=valid[-1]['source'],
                                 scope='整机/整块显卡；每次采集均值再取均值', failed_runs=len(captures) - len(valid),
                                 failed_samples=sum(row.get('failed_samples', 0) for row in valid))
        else:
            # No fabricated zeros; preserve separate controlled simulation measurements.
            if key != 'days_per_second':
                observations.pop(key, None)
            statuses[key] = rows[-1] if rows else reading(status='not_sampled', reason='本次未采集此传感器')
    report.setdefault('sensor_status', {})[side] = statuses
    report['sources'][side + '_sensors'] = {key: row.get('source') or row.get('reason') for key, row in statuses.items()}
    return report


class TelemetrySession:
    def __init__(self, gpu_name='', sensors=None):
        self.sensors = sensors or Sensors(gpu_name)
        self.samples = []
        self.stop_event = Event()
        self.thread = None

    def start(self):
        self.thread = Thread(target=self.collect, daemon=True)
        self.thread.start()

    def collect(self):
        if self.stop_event.wait(8):
            return
        origin = time.monotonic()
        while not self.stop_event.is_set():
            try:
                readings = self.sensors.sample()
            except Exception:
                readings = {key: reading(status='error', reason='本次传感器读取失败') for key in KEYS}
            at_seconds = time.monotonic() - origin
            if not self.stop_event.is_set():
                self.samples.append({'at_seconds': round(at_seconds, 3), 'readings': readings})
            if self.stop_event.wait(1):
                return

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=15)
        return summarize(self.samples)
