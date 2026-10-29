"""Optional local OCR: date differences are distinct from rendered frame timing."""
from datetime import date
from pathlib import Path
import re
import subprocess
import asyncio
import sys
import importlib

OCR_DIRECTORY = Path(__file__).resolve().parent.parent / '.data/ocr-runtime'
OCR_PACKAGES = ['winrt-runtime', 'winrt-Windows.Foundation', 'winrt-Windows.Globalization',
                'winrt-Windows.Storage', 'winrt-Windows.Storage.Streams',
                'winrt-Windows.Graphics.Imaging', 'winrt-Windows.Media.Ocr']


def prepare_ocr(cancel):
    command = [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '--target', str(OCR_DIRECTORY),
               *[name + '==3.2.1' for name in OCR_PACKAGES]]
    if cancel.is_set():
        raise ValueError('已取消')
    result = subprocess.run(command, capture_output=True, timeout=180,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError('Windows OCR 组件准备失败，请检查网络后重试')
    importlib.invalidate_caches()


def parse_game_date(text):
    dates = set()
    patterns = [r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日',
                r'(?<!\d)(\d{4})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{1,2})(?!\d)']
    for pattern in patterns:
        for groups in re.findall(pattern, text):
            try:
                dates.add(date(*map(int, groups)))
            except ValueError:
                pass
    if len(dates) != 1:
        raise ValueError('日期区域未识别出唯一有效日期；请只框选游戏日期，使用中文或数字日期显示')
    return dates.pop()


def recognize_date(image):
    if str(OCR_DIRECTORY) not in sys.path:
        sys.path.insert(0, str(OCR_DIRECTORY))
    try:
        from winrt.windows.storage import StorageFile, FileAccessMode
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.media.ocr import OcrEngine
    except ImportError as error:
        raise ValueError('请先点击“准备日期识别组件”') from error
    async def recognize():
        stream = await (await StorageFile.get_file_from_path_async(str(Path(image).resolve()))).open_async(FileAccessMode.READ)
        bitmap = None
        try:
            decoder = await BitmapDecoder.create_async(stream)
            bitmap = await decoder.get_software_bitmap_async()
            engine = OcrEngine.try_create_from_user_profile_languages()
            if engine is None:
                raise ValueError('Windows 本地 OCR 语言组件不可用')
            return (await engine.recognize_async(bitmap)).text
        finally:
            if bitmap:
                bitmap.close()
            stream.close()
    try:
        text = asyncio.run(recognize())
    except (RuntimeError, OSError) as error:
        raise ValueError('Windows 原生 OCR 无法读取日期；本次不估算跑日速度') from error
    return parse_game_date(text).isoformat()


def summarize_dates(samples):
    rows = sorted(samples, key=lambda row: row['at_seconds'])
    if len(rows) < 2:
        return {'status': 'not_sampled', 'value': None, 'reason': '游戏日期识别至少需要前后两个有效样本'}
    days = [date.fromisoformat(row['date']).toordinal() for row in rows]
    elapsed = rows[-1]['at_seconds'] - rows[0]['at_seconds']
    if any(b < a for a, b in zip(days, days[1:])) or elapsed < 10:
        return {'status': 'error', 'value': None, 'reason': '游戏日期回退或识别时间跨度过短，本次跑日数据无效'}
    count = days[-1] - days[0]
    if count == 0:
        return {'status': 'not_sampled', 'value': None, 'reason': '识别到的日期没有推进：可能暂停或测试时长不足'}
    return {'status': 'ok', 'value': round(count / elapsed, 6), 'game_days': count,
            'elapsed_seconds': elapsed, 'samples': len(rows), 'peak': count / elapsed,
            'source': 'Windows 本地 OCR / 游戏日期选区',
            'scope': '游戏日期整数日差 / 实际秒数；含不足一天的边界误差，非受控重复实验'}
