"""Evidence-based starting points for Victoria 3 performance analysis."""
def analyze(record):
    values = record.get('observations', {})
    gpu = values.get('gpu_usage')
    if not values.get('run_count'):
        return '先完成一次性能测试', ['打开测试存档，选择固定视角和游戏速度。', '点击性能测试，完成 60 秒采集后返回分析。']
    if gpu is not None and gpu >= 95:
        return '优先检查显卡渲染负载', [f'GPU 平均占用 {gpu:.1f}%，接近满负载。', '先降低分辨率或阴影质量，再测试一次并比较 FPS。']
    return '下一步定位 CPU 模拟与渲染限制', ['降低一档分辨率，在相同存档、视角和速度下再测一次。', 'FPS 基本不变且 GPU 未满载时，重点检查 CPU 主线程、模拟负载和后台进程。', '比较游戏天/秒：推进慢时优先测试人口密集存档、模组与游戏速度；整机 CPU 占用不能代替单核负载。']
