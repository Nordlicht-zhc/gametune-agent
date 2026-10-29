"""Independent configuration records; comparisons never move or merge test data."""
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
import json
from pathlib import Path
import sqlite3
from uuid import uuid4, uuid5, NAMESPACE_URL

from tools.resolution import normalize_resolution
from tools.presentmon import summarize_captures
from tools.hardware_telemetry import attach_telemetry
from tools.victoria3_report import METRICS


def now():
    return datetime.now(timezone.utc).isoformat()


def fingerprint(settings, machine):
    graphics = deepcopy(settings.get('Graphics', {}))
    try:
        graphics['resolution'] = normalize_resolution(graphics['resolution'])
    except (KeyError, ValueError):
        pass
    value = {'graphics': graphics, 'game': settings.get('game', {}), 'gui': settings.get('GUI', {}),
             'hardware': {key: machine.get(key, '') for key in ('cpu', 'gpu', 'version')}}
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def create_record(machine, settings, name='', origin='native'):
    return {'schema': 'configuration', 'id': uuid4().hex, 'name': name or '配置 · ' + settings.get('Graphics', {}).get('resolution', '自定义'),
            'created_at': now(), 'updated_at': now(), 'machine': {key: deepcopy(value) for key, value in machine.items() if key not in ('settings', 'saves')},
            'settings': deepcopy(settings), 'fingerprint': fingerprint(settings, machine), 'origin': origin,
            'observations': {}, 'sensor_status': {}, 'captures': [], 'sources': {}, 'revision': 0}


class ConfigurationRepository:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('CREATE TABLE IF NOT EXISTS victoria3_configurations (id TEXT PRIMARY KEY, created_at TEXT, payload TEXT)')
            connection.execute('CREATE TABLE IF NOT EXISTS victoria3_comparisons (id TEXT PRIMARY KEY, payload TEXT)')
            connection.execute('CREATE TABLE IF NOT EXISTS victoria3_configuration_actions (id TEXT PRIMARY KEY, created_at TEXT, payload TEXT)')

    def list(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return [json.loads(row[0]) for row in connection.execute('SELECT payload FROM victoria3_configurations ORDER BY created_at DESC')]

    def get(self, identifier):
        with closing(sqlite3.connect(self.path)) as connection:
            row=connection.execute('SELECT payload FROM victoria3_configurations WHERE id=?',(identifier,)).fetchone()
            return json.loads(row[0]) if row else None

    def rename(self,identifier,name):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            row=connection.execute('SELECT payload FROM victoria3_configurations WHERE id=?',(identifier,)).fetchone()
            if not row:raise ValueError('配置不存在')
            record=json.loads(row[0]);record['name']=name;record['revision']+=1;record['updated_at']=now()
            record.pop('model_explanation',None)
            connection.execute('UPDATE victoria3_configurations SET payload=? WHERE id=?',(json.dumps(record,ensure_ascii=False),identifier))

    def save(self, record):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('INSERT OR REPLACE INTO victoria3_configurations VALUES(?,?,?)',
                               (record['id'], record['created_at'], json.dumps(record, ensure_ascii=False)))

    def current(self, machine, settings, preferred=None, force_new=False, name=''):
        key = fingerprint(settings, machine)
        records = self.list()
        if not force_new:
            record = next((row for row in records if row['id'] == preferred and row['fingerprint'] == key), None)
            record = record or next((row for row in records if row['fingerprint'] == key and row['origin'] == 'native'), None)
            record = record or next((row for row in records if row['fingerprint'] == key and row['captures']), None)
            if record:
                return record
        record = create_record(machine, settings, name)
        self.save(record)
        return record

    def migrate(self, reports):
        existing = {row['id'] for row in self.list()}
        action_ids={row['id'] for row in self.actions()}
        for report in reports:
            if report.get('backup'):
                action_id=sha256(report['backup'].encode()).hexdigest()
                if action_id not in action_ids:
                    self.save_action({'id':action_id,'created_at':report['created_at'],'backup':report['backup'],
                                      'phase':report['phase'],'origin':'legacy'})
                    action_ids.add(action_id)
            for side, settings_key in (('baseline', 'before'), ('candidate', 'after')):
                if not report['observations'][side]:
                    continue
                identifier = uuid5(NAMESPACE_URL, 'victoria3:' + report['id'] + ':' + side).hex
                if identifier in existing:
                    continue
                record = create_record(report['machine'], report[settings_key], origin='legacy')
                record.update(id=identifier, name='历史配置 · ' + report[settings_key].get('Graphics', {}).get('resolution', '自定义') + ' · ' + report['id'][:6],
                              observations=deepcopy(report['observations'][side]),
                              sensor_status=deepcopy(report.get('sensor_status', {}).get(side, {})),
                              captures=deepcopy(report.get('captures', {}).get(side, [])),
                              sources={'历史来源': report['id']}, legacy_origin={'report_id': report['id'], 'side': side})
                record['created_at']=report['created_at']
                record['updated_at']=report['created_at']
                self.save(record)
                existing.add(identifier)

    def append_capture(self, identifier, expected_key, result):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT payload FROM victoria3_configurations WHERE id=?', (identifier,)).fetchone()
            if not row:
                raise ValueError('配置记录不存在，采集文件已保留')
            record = json.loads(row[0])
            if record['fingerprint'] != expected_key:
                raise ValueError('配置快照发生变化，本次未关联')
            if record['captures'] and any((item.get('context') or '') != (result.get('context') or '') or item.get('seconds') != result.get('seconds') for item in record['captures']):
                raise ValueError('本次场景标记或时长不同，请另存为新配置测试记录，原始文件已保留')
            record['captures'].append(deepcopy(result))
            record['observations'] = summarize_captures(record['captures'])
            wrapper = {'observations': {'record': record['observations']}, 'sources': {}}
            attach_telemetry(wrapper, 'record', record['captures'])
            record['sensor_status'] = wrapper['sensor_status']['record']
            record['sources'] = wrapper['sources']
            record['revision'] += 1
            record['updated_at'] = now()
            record.pop('model_explanation', None)
            connection.execute('UPDATE victoria3_configurations SET payload=? WHERE id=?', (json.dumps(record, ensure_ascii=False), identifier))
            return record

    def save_explanation(self, snapshot, result):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT payload FROM victoria3_configurations WHERE id=?', (snapshot['id'],)).fetchone()
            if not row or json.loads(row[0]) != snapshot:
                return False
            record = json.loads(row[0])
            record['model_explanation'] = deepcopy(result)
            connection.execute('UPDATE victoria3_configurations SET payload=? WHERE id=?', (json.dumps(record, ensure_ascii=False), record['id']))
            return True

    def comparison_explanation(self, identifier):
        with closing(sqlite3.connect(self.path)) as connection:
            row=connection.execute('SELECT payload FROM victoria3_comparisons WHERE id=?',(identifier,)).fetchone()
            return json.loads(row[0]) if row else None

    def actions(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return [json.loads(row[0]) for row in connection.execute('SELECT payload FROM victoria3_configuration_actions ORDER BY created_at DESC')]

    def save_action(self,action):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('INSERT OR REPLACE INTO victoria3_configuration_actions VALUES(?,?,?)',
                               (action['id'],action['created_at'],json.dumps(action,ensure_ascii=False)))

    def save_comparison_explanation(self, snapshot, result):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('INSERT OR REPLACE INTO victoria3_comparisons VALUES(?,?)',
                               (snapshot['id'],json.dumps({'snapshot':snapshot,'explanation':result},ensure_ascii=False)))


def configuration_advice(record):
    notes = ['各项结果属于此配置的独立测试；需要比较时，在自由对比页选择任意 A、B 配置。']
    count = record['observations'].get('run_count', 0)
    if count < 3:
        notes.append('当前有效重复测试不足三次，建议同一场景复测。')
    if record['observations'].get('gpu_temperature', 0) >= 85:
        notes.append('实测 GPU 温度较高，可核对长时间运行时的散热与功耗变化；此数值本身不证明降频。')
    notes.append('整机 CPU/GPU 传感器包含后台负载；跑日 OCR 结果为参考测量，不等同于画面 FPS。')
    return notes


def value_text(record, key, unit):
    value = record['observations'].get(key)
    if value is not None:
        return f'{value:.2f} {unit}'
    state = record.get('sensor_status', {}).get(key, {})
    return {'unsupported': '设备或接口不支持', 'error': '采集失败', 'not_supported': '暂不支持', 'not_sampled': '未采集'}.get(state.get('status'), '未采集')


def comparison_rows(a, b):
    rows = []
    for key, label, unit in METRICS:
        x, y = a['observations'].get(key), b['observations'].get(key)
        change = '—'
        if x is not None and y is not None:
            change = f'{y-x:+.2f} 个百分点' if unit == '%' else (f'{y-x:+.2f} {unit}' if unit == '℃' or x == 0 else f'{(y/x-1)*100:+.2f}%')
        rows.append([label, value_text(a, key, unit), value_text(b, key, unit), change])
    return rows


def comparison_snapshot(a,b):
    value={'schema':'configuration_comparison','a':deepcopy(a),'b':deepcopy(b)}
    for record in (value['a'],value['b']):
        record.pop('model_explanation',None)
    value['id']=sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return value


def comparison_warnings(a, b):
    notes = []
    if a['id'] == b['id']:
        notes.append('当前选择的是同一条配置记录。')
    for key in ('cpu', 'gpu', 'version'):
        if a['machine'].get(key) != b['machine'].get(key):
            notes.append(f'{key} 不同，差异不能直接归因于图形设置。')
    if min(a['observations'].get('run_count', 0), b['observations'].get('run_count', 0)) < 3:
        notes.append('至少一组重复次数不足三次，差异仅供参考。')
    contexts = [{json.dumps(item.get('context'), sort_keys=True) for item in record['captures'] if item.get('context')} for record in (a,b)]
    if contexts[0] != contexts[1] or not all(contexts):
        notes.append('场景标记不同或缺失；需自行确认存档、视角、速度与采集方法可比较。')
    durations=[{item.get('seconds') for item in record['captures']} for record in (a,b)]
    if durations[0]!=durations[1]:notes.append('两组采集时长不同，需复核测试方法。')
    if any(max(record['observations'].get('avg_spread_pct', 0), record['observations'].get('low_spread_pct', 0)) > 5 for record in (a,b)):
        notes.append('组内波动超过 5%，需复测。')
    return notes


def table(headers, rows):
    e = lambda value: escape(str(value if value is not None else '未显式保存'))
    return '<table width="100%" border="1" cellspacing="0" cellpadding="7"><tr>' + ''.join('<th bgcolor="#e9f0df">'+e(x)+'</th>' for x in headers) + '</tr>' + ''.join('<tr>'+''.join('<td>'+e(x)+'</td>' for x in row)+'</tr>' for row in rows) + '</table>'


def configuration_html(record):
    e = lambda value: escape(str(value))
    text = '<html><meta charset="utf-8"><body style="color:#202422;font-family:Microsoft YaHei UI"><h1>'+e(record['name'])+'</h1><p>独立配置报告 · '+e(record['id'][:12])+'</p>'
    text += '<h2>本配置测试结果</h2>' + table(['指标','实测','来源或缺失原因'], [[label, value_text(record,key,unit), record.get('sensor_status',{}).get(key,{}).get('source') or record.get('sensor_status',{}).get(key,{}).get('reason','PresentMon' if key in ('avg_fps','low_fps') and key in record['observations'] else '未采集')] for key,label,unit in METRICS])
    text += '<p>独立测试次数：'+str(record['observations'].get('run_count',0))+'</p>'
    if record['captures']:
        text += '<h2>测试记录</h2>'+table(['次数','采集时长（秒）','场景标记'],[[index,item.get('seconds'),item.get('context') or '未填写'] for index,item in enumerate(record['captures'],1)])
    text += '<h2>配置快照</h2>'+table(['设置','值'], [[key,value] for key,value in record['settings'].get('Graphics',{}).items()])
    text += '<h2>设备与版本</h2>'+table(['项目','信息'], [[key,record['machine'].get(key,'未识别')] for key in ('cpu','gpu','ram','version')])
    text += '<h2>建议</h2><ul>'+''.join('<li>'+e(item)+'</li>' for item in configuration_advice(record))+'</ul>'
    if record.get('model_explanation'):
        note = record['model_explanation']
        text += '<h2>补充解读（未经性能验证）</h2><p>'+e(note['summary'])+'</p><ul>'+''.join('<li>'+e(item)+'</li>' for item in note['steps'])+'</ul>'
    return text+'</body></html>'


def comparison_html(a,b):
    text = '<html><meta charset="utf-8"><body style="color:#202422;font-family:Microsoft YaHei UI"><h1>配置自由对比</h1><p>差异为 B 相对 A；两条配置及原始采集数据保持独立。</p>'
    text += table(['指标', 'A · '+a['name'], 'B · '+b['name'], '数值差异'], comparison_rows(a,b))
    text += '<h2>测试条件说明</h2><ul>'+''.join('<li>'+escape(item)+'</li>' for item in comparison_warnings(a,b))+'</ul>'
    keys = set(a['settings'].get('Graphics',{})) | set(b['settings'].get('Graphics',{}))
    text += '<h2>图形设置对照</h2>'+table(['设置','A','B'], [[key,a['settings'].get('Graphics',{}).get(key),b['settings'].get('Graphics',{}).get(key)] for key in sorted(keys)])
    return text+'</body></html>'
