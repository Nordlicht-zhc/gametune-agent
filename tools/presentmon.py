"""Pinned PresentMon console integration; only targets Victoria 3."""
import csv
import hashlib
import io
import math
import os
from pathlib import Path
import subprocess
import time
import urllib.request
from uuid import uuid4

from tools.import_benchmark import import_benchmark_csv
from tools.calculate_metrics import median_summary

VERSION = '2.6.0'
SHA256 = 'b2a706bc6ad475749e3b7e3409263aa1e6906d45bdcf993f6dbc0f660188f1af'
URL = f'https://github.com/GameTechDev/PresentMon/releases/download/v{VERSION}/PresentMon-{VERSION}-x64.exe'


class PresentMon:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.executable = self.directory / f'PresentMon-{VERSION}-x64.exe'

    def ready(self):
        return self.executable.is_file() and hashlib.sha256(self.executable.read_bytes()).hexdigest() == SHA256

    def prepare(self, cancel):
        if self.ready():
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.directory / (uuid4().hex + '.download')
        try:
            digest = hashlib.sha256()
            with urllib.request.urlopen(URL, timeout=30) as response, temporary.open('wb') as stream:
                for block in iter(lambda: response.read(65536), b''):
                    if cancel.is_set():
                        raise ValueError('已取消下载')
                    digest.update(block)
                    stream.write(block)
            if digest.hexdigest() != SHA256:
                raise ValueError('PresentMon 校验失败，未安装或执行')
            temporary.replace(self.executable)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def game_pid():
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        result = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq victoria3.exe', '/FO', 'CSV', '/NH'],
                                capture_output=True, timeout=10, creationflags=flags)
        rows = list(csv.reader(io.StringIO(result.stdout.decode(errors='replace'))))
        pids = [int(row[1]) for row in rows if len(row) > 1 and row[0].lower() == 'victoria3.exe']
        if result.returncode or len(pids) != 1:
            raise ValueError('请先启动 Victoria 3 并载入存档；需要且只能有一个游戏进程')
        return pids[0]

    def command(self, pid: int, output: Path, seconds: int):
        if not 10 <= seconds <= 300 or pid <= 0:
            raise ValueError('采集时长或游戏进程不合法')
        return [str(self.executable), '--process_id', str(pid), '--output_file', str(output),
                '--v1_metrics', '--delay', '8', '--timed', str(seconds), '--terminate_after_timed',
                '--session_name', 'Victoria3Optimizer-' + uuid4().hex, '--no_console_stats']

    def capture(self, folder: Path, seconds: int, cancel, on_started=None):
        if not self.ready():
            raise ValueError('请先准备并校验采集工具')
        pid = self.game_pid()
        folder.mkdir(parents=True, exist_ok=True)
        output = folder / (uuid4().hex + '.csv')
        log_path = output.with_suffix('.log')
        with log_path.open('wb') as log:
            process = subprocess.Popen(self.command(pid, output, seconds), stdout=log, stderr=log,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                if on_started:
                    on_started()
                deadline = time.monotonic() + seconds + 45
                while process.poll() is None:
                    if cancel.wait(.2):
                        raise ValueError('采集已取消，本次数据未写入报告')
                    if time.monotonic() > deadline:
                        raise ValueError('采集超时，本次数据未写入报告')
                if process.returncode:
                    detail = log_path.read_text(encoding='utf-8', errors='replace')[-6000:]
                    if 'failed to start trace session: access denied' in detail.lower():
                        raise ValueError('Windows 拒绝启动 ETW 采集会话（权限不足）。\n请点击“以管理员权限打开采集页”，在 Windows 弹窗中确认，然后在带“管理员”标记的新窗口重新采集。\n本次未写入报告。')
                    raise ValueError(f'PresentMon 退出码 {process.returncode}，本次未写入报告。\n采集日志：{detail.strip() or "工具未输出错误详情"}')
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
        if cancel.is_set():
            raise ValueError('采集已取消')
        metrics = load_capture(output, pid)
        if metrics.get('duration_seconds', 0) < seconds * .8:
            raise ValueError('有效帧时间覆盖不足，可能游戏已退出或未成功采集，请重试')
        return {'metrics': metrics, 'csv': str(output), 'pid': pid, 'seconds': seconds, 'version': VERSION}


def load_capture(output: Path, pid: int):
    """Keep the dominant swap chain; don't concatenate independent frame streams."""
    with output.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        fields = [name.strip().lower() for name in (reader.fieldnames or [])]
        if not fields or 'msbetweenpresents' not in fields:
            raise ValueError('PresentMon 未输出兼容帧时间列')
        groups = {}
        for row in reader:
            row = {key.strip().lower(): value for key, value in row.items() if key is not None}
            if row.get('processid') != str(pid):
                continue
            chain = row.get('swapchainaddress', 'default')
            frames = groups.setdefault(chain, [])
            try:
                value = float(row['msbetweenpresents'])
            except (ValueError, TypeError):
                raise ValueError('采集包含无效帧时间')
            if value == 0 and not frames:
                continue  # First presentation has no predecessor.
            if not math.isfinite(value) or value <= 0:
                raise ValueError('采集包含无效帧时间')
            frames.append(row)
    if not groups:
        raise ValueError('未采集到目标游戏进程的帧')
    chain, rows = max(groups.items(), key=lambda item: len(item[1]))
    filtered = output.with_suffix('.frames.csv')
    with filtered.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    metrics = import_benchmark_csv(filtered)
    metrics['selected_swap_chain'] = chain
    metrics['swap_chain_policy'] = '使用帧数最多的交换链，原始 CSV 已保留'
    return metrics


def summarize_captures(captures: list[dict]):
    # One CSV is one observation, never thousands of independent repeated runs.
    return median_summary([{'avg_fps': item['metrics']['avg_fps'], 'low_fps': item['metrics']['low_fps']}
                           for item in captures])
