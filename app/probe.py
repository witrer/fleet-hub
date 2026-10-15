"""Minute resource aggregates and exact heartbeat RTT history for Fleet's SQLite DB."""
from __future__ import annotations

import ipaddress
import json
import math
import re
import time

RETENTION = 48 * 3600
PERCENT_FIELDS = ('cpu_percent', 'memory_percent', 'disk_percent', 'gpu_percent')
BYTE_FIELDS = ('memory_used_bytes', 'memory_total_bytes', 'disk_used_bytes', 'disk_total_bytes',
               'network_rx', 'network_tx')
LOCATION_FIELDS = ('country_code', 'country_name', 'region', 'city')
METRICS = {'cpu': 'cpu_percent', 'memory': 'memory_percent', 'disk': 'disk_percent', 'gpu': 'gpu_percent'}
RANGES = {'1h': 3600, '6h': 21600, '24h': 86400}
GPU_STATUSES = {'ok', 'no_gpu', 'driver_missing', 'permission_denied', 'error', 'unsupported'}


def number(value, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        finite = math.isfinite(value)
    except OverflowError:
        return None
    if not finite:
        return None
    if value < minimum or (maximum is not None and value > maximum):
        return None
    return float(value)


def integer(value):
    valid = number(value, maximum=2**63 - 1)
    return int(value) if valid is not None and int(value) == value else None


def location(payload, strict=False):
    result = {}
    for key in LOCATION_FIELDS:
        value = payload.get(key, '')
        if value is None:
            value = ''
        if not isinstance(value, str):
            if strict:
                raise ValueError(key + ' must be a string')
            value = ''
        value = value.strip()
        if key == 'country_code':
            value = value.upper()
            if value and not re.fullmatch('[A-Z]{2}', value):
                if strict:
                    raise ValueError('country_code must be a two-letter country code')
                value = ''
        elif len(value) > 100:
            if strict:
                raise ValueError(key + ' is too long')
            value = value[:100]
        result[key] = value
    return result


def latency_sample(value):
    if not isinstance(value, dict):
        return None
    stamp = number(value.get('timestamp'))
    success = value.get('success')
    if stamp is None or not isinstance(success, bool):
        return None
    latency = number(value.get('latency_ms'), maximum=300000) if success else None
    if success and latency is None:
        return None
    return {'timestamp': stamp, 'success': success, 'latency_ms': latency}


def clean_metrics(payload):
    """Drop malformed optional fields; preserve absent values as absent, never as zero."""
    if not isinstance(payload, dict):
        return {}
    result = {}
    for key in PERCENT_FIELDS:
        if key in payload:
            result[key] = number(payload[key], maximum=100)
    for key in BYTE_FIELDS:
        if key in payload:
            result[key] = integer(payload[key])
    for key in ('uptime', 'load'):
        if key in payload:
            result[key] = number(payload[key])
    for key, length in (('platform', 100), ('agent_version', 64), ('gpu_error', 240)):
        if isinstance(payload.get(key), str):
            result[key] = payload[key][:length]
    if 'gpu_status' in payload:
        value = payload['gpu_status']
        result['gpu_status'] = value if isinstance(value, str) and value in GPU_STATUSES else 'error'
    if isinstance(payload.get('public_ip'), str):
        try:
            result['public_ip'] = str(ipaddress.ip_address(payload['public_ip']))
        except ValueError:
            pass
    for key, value in location(payload).items():
        if key in payload:
            result[key] = value
    if 'gpus' in payload:
        gpus, used = [], set()
        for raw in payload['gpus'][:64] if isinstance(payload['gpus'], list) else []:
            if not isinstance(raw, dict):
                continue
            index = integer(raw.get('index'))
            if index is None or index > 255 or index in used:
                continue
            used.add(index)
            gpu = {'index': index, 'vendor': raw.get('vendor', '')[:32] if isinstance(raw.get('vendor', ''), str) else '',
                   'name': raw.get('name', '')[:160] if isinstance(raw.get('name', ''), str) else '',
                   'util_percent': number(raw.get('util_percent'), maximum=100),
                   'memory_used_bytes': integer(raw.get('memory_used_bytes')),
                   'memory_total_bytes': integer(raw.get('memory_total_bytes'))}
            if 'temperature_c' in raw:
                gpu['temperature_c'] = number(raw['temperature_c'], minimum=-273.15, maximum=250)
            gpus.append(gpu)
        result['gpus'] = gpus
        utilization = [gpu['util_percent'] for gpu in gpus if gpu['util_percent'] is not None]
        result['gpu_percent'] = sum(utilization) / len(utilization) if utilization else None
    samples = []
    if isinstance(payload.get('latency_samples'), list):
        for value in payload['latency_samples'][-180:]:
            sample = latency_sample(value)
            if sample is not None:
                samples.append(sample)
        result['latency_samples'] = samples
    if 'latency_ms' in payload:
        result['latency_ms'] = number(payload['latency_ms'], maximum=300000)
    if 'latency_sample_at' in payload:
        result['latency_sample_at'] = number(payload['latency_sample_at'])
    if isinstance(payload.get('latency_success'), bool):
        result['latency_success'] = payload['latency_success']
        if payload['latency_success'] is False:
            result['latency_ms'] = None
    return result


def percentile95(values):
    """Exact empirical nearest-rank P95 of successful RTT observations."""
    return sorted(values)[math.ceil(len(values) * .95) - 1] if values else None


class ProbeStore:
    clean_metrics = staticmethod(clean_metrics)

    def __init__(self, db):
        self.db = db
        self.observations = {}
        self.last_cleanup = 0
        db.executescript('''
CREATE TABLE IF NOT EXISTS probe_minutes (
 node_id TEXT NOT NULL, minute INTEGER NOT NULL, resources TEXT NOT NULL DEFAULT '{}',
 latencies TEXT NOT NULL DEFAULT '[]', offline_seconds REAL NOT NULL DEFAULT 0,
 offline_reason TEXT NOT NULL DEFAULT '', PRIMARY KEY(node_id,minute));
CREATE TABLE IF NOT EXISTS probe_metadata (
 node_id TEXT PRIMARY KEY, gpus TEXT NOT NULL DEFAULT '[]', manual_location TEXT);
''')
        db.commit()

    def _minute(self, node_id, minute):
        row = self.db.execute('SELECT resources,latencies,offline_seconds,offline_reason FROM probe_minutes '
                              'WHERE node_id=? AND minute=?', (node_id, minute)).fetchone()
        return {'resources': json.loads(row[0]), 'latencies': json.loads(row[1]),
                'offline_seconds': row[2], 'offline_reason': row[3]} if row else {
                    'resources': {}, 'latencies': [], 'offline_seconds': 0, 'offline_reason': ''}

    def _save(self, node_id, minute, row):
        self.db.execute('INSERT INTO probe_minutes(node_id,minute,resources,latencies,offline_seconds,offline_reason) '
                        'VALUES(?,?,?,?,?,?) ON CONFLICT(node_id,minute) DO UPDATE SET '
                        'resources=excluded.resources,latencies=excluded.latencies,'
                        'offline_seconds=excluded.offline_seconds,offline_reason=excluded.offline_reason',
                        (node_id, minute, json.dumps(row['resources']), json.dumps(row['latencies']),
                         row['offline_seconds'], row['offline_reason']))

    def _cleanup(self, now):
        if now - self.last_cleanup >= 3600:
            self.db.execute('DELETE FROM probe_minutes WHERE minute<?', (int(now - RETENTION) // 60 * 60,))
            self.last_cleanup = now

    def record(self, node_id, metrics, now=None):
        now = time.time() if now is None else float(now)
        metrics = clean_metrics(metrics)
        minute = int(now) // 60 * 60
        changed = {minute: self._minute(node_id, minute)}
        values = {key: metrics.get(field) for key, field in METRICS.items()}
        values.update({'gpu:' + str(gpu['index']): gpu['util_percent'] for gpu in metrics.get('gpus', [])})
        for key, value in values.items():
            if value is None:
                continue
            aggregate = changed[minute]['resources'].setdefault(key, {'count': 0, 'sum': 0, 'min': value, 'max': value})
            aggregate['count'] += 1
            aggregate['sum'] += value
            aggregate['min'] = min(aggregate['min'], value)
            aggregate['max'] = max(aggregate['max'], value)
        samples = list(metrics.get('latency_samples', []))
        previous = latency_sample({'timestamp': metrics.get('latency_sample_at'),
                                   'success': metrics.get('latency_success', metrics.get('latency_ms') is not None),
                                   'latency_ms': metrics.get('latency_ms')})
        if previous is not None:
            samples.append(previous)
        for sample in samples:
            stamp = sample['timestamp']
            if not now - RETENTION <= stamp <= now + 60:
                continue
            bucket = int(stamp) // 60 * 60
            if bucket not in changed:
                changed[bucket] = self._minute(node_id, bucket)
            stored = changed[bucket]['latencies']
            milliseconds = int(stamp * 1000)
            if len(stored) < 120 and all(item[0] != milliseconds for item in stored):
                stored.append([milliseconds, sample['success'], sample['latency_ms']])
        with self.db:
            for bucket, row in changed.items():
                self._save(node_id, bucket, row)
            if 'gpus' in metrics:
                self.db.execute('INSERT INTO probe_metadata(node_id,gpus) VALUES(?,?) '
                                'ON CONFLICT(node_id) DO UPDATE SET gpus=excluded.gpus',
                                (node_id, json.dumps(metrics['gpus'])))
            self._cleanup(now)

    def mark_offline(self, nodes, now=None):
        """Record only intervals witnessed by two consecutive monitor observations.

        A new process or a monitor gap over 30 seconds starts a new observation;
        last_seen is evidence for the current state, never a start time to backfill.
        """
        now = time.time() if now is None else float(now)
        active_ids = set()
        with self.db:
            for node in nodes:
                node_id = node['id']
                active_ids.add(node_id)
                seen = number(node.get('last_seen'))
                reason = 'frp_offline' if node.get('frp_status') == 'offline' else (
                    'heartbeat_timeout' if seen is not None and seen > 0 and now - seen >= 35 else '')
                previous = self.observations.get(node_id)
                if reason and previous and previous[1] and 0 < now - previous[0] <= 30:
                    start = previous[0]
                    while start < now:
                        minute = int(start) // 60 * 60
                        end = min(now, minute + 60)
                        row = self._minute(node_id, minute)
                        row['offline_seconds'] = min(60, row['offline_seconds'] + end - start)
                        row['offline_reason'] = reason
                        self._save(node_id, minute, row)
                        start = end
                self.observations[node_id] = (now, reason)
            self._cleanup(now)
        self.observations = {key: value for key, value in self.observations.items() if key in active_ids}

    def set_location(self, node_id, payload):
        if not isinstance(payload, dict):
            raise ValueError('Expected a location object')
        value = location(payload, strict=True)
        with self.db:
            self.db.execute('INSERT INTO probe_metadata(node_id,manual_location) VALUES(?,?) '
                            'ON CONFLICT(node_id) DO UPDATE SET manual_location=excluded.manual_location',
                            (node_id, json.dumps(value)))
        return dict(value, location_source='manual')

    def enrich(self, node_dict, now=None):
        now = time.time() if now is None else float(now)
        node = dict(node_dict)
        for field in PERCENT_FIELDS:
            node.setdefault(field, None)
        node.setdefault('gpus', [])
        node.setdefault('latency_ms', None)
        seen = number(node.get('last_seen'))
        age = max(0, now - seen) if seen is not None and seen > 0 else None
        freshness = 'unknown' if age is None else 'stale' if age >= 35 else 'fresh'
        node.update(metrics_age_seconds=age, metrics_stale=freshness != 'fresh', data_freshness=freshness,
                    freshness={'status': freshness, 'age_seconds': age, 'sampled_at': seen if age is not None else None})
        row = self.db.execute('SELECT manual_location FROM probe_metadata WHERE node_id=?', (node['id'],)).fetchone()
        if row and row[0] is not None:
            node.update(json.loads(row[0]), location_source='manual')
        else:
            value = location(node)
            node.update(value, location_source='node' if any(value.values()) else 'unknown')
        return node

    def history(self, node_id, metric, range='1h', device=None, now=None):
        if metric not in METRICS or range not in RANGES:
            raise ValueError('Unsupported metric or range')
        if device not in (None, ''):
            if metric != 'gpu' or not re.fullmatch(r'\d{1,3}', str(device)) or int(device) > 255:
                raise ValueError('GPU device must be an index from 0 to 255')
            device = int(device)
        else:
            device = None
        now = time.time() if now is None else float(now)
        end = int(now) // 60 * 60
        start = end - RANGES[range] + 60
        key = 'gpu:' + str(device) if device is not None else metric
        rows = self.db.execute('SELECT minute,resources FROM probe_minutes WHERE node_id=? AND minute>=? '
                               'AND minute<=? ORDER BY minute', (node_id, start, end)).fetchall()
        values, count, total, minimum, maximum = {}, 0, 0, None, None
        for minute, resources in rows:
            aggregate = json.loads(resources).get(key)
            if not aggregate or not aggregate['count']:
                continue
            values[minute] = aggregate['sum'] / aggregate['count']
            count += aggregate['count']
            total += aggregate['sum']
            minimum = aggregate['min'] if minimum is None else min(minimum, aggregate['min'])
            maximum = aggregate['max'] if maximum is None else max(maximum, aggregate['max'])
        row = self.db.execute('SELECT gpus FROM probe_metadata WHERE node_id=?', (node_id,)).fetchone()
        devices = json.loads(row[0]) if row else []
        return {'node_id': node_id, 'metric': metric, 'range': range, 'device': device,
                'points': [{'timestamp': minute, 'value': values.get(minute)} for minute in builtins_range(start, end + 60, 60)],
                'stats': {'min': minimum, 'max': maximum, 'avg': total / count if count else None},
                'gpus': devices, 'devices': devices}

    def latency_summary(self, ids, healthy_ms=100, critical_ms=250, now=None):
        if number(healthy_ms) is None or number(critical_ms) is None or critical_ms < healthy_ms:
            raise ValueError('Invalid latency thresholds')
        now = time.time() if now is None else float(now)
        ids = list(dict.fromkeys(ids))
        end = int(now) // 900 * 900
        start = end - 95 * 900
        output = {'generated_at': now, 'bucket_seconds': 900, 'range': '24h',
                  'thresholds': {'healthy_ms': healthy_ms, 'critical_ms': critical_ms}, 'nodes': {}}
        grouped = {node_id: {} for node_id in ids}
        if ids:
            rows = self.db.execute('SELECT node_id,minute,latencies,offline_seconds,offline_reason FROM probe_minutes '
                                   'WHERE node_id IN (' + ','.join('?' for _ in ids) + ') AND minute>=? AND minute<=?',
                                   (*ids, start, int(now) // 60 * 60)).fetchall()
            for node_id, minute, latencies, offline_seconds, offline_reason in rows:
                bucket = minute // 900 * 900
                value = grouped[node_id].setdefault(bucket, {'values': [], 'count': 0, 'offline_seconds': 0, 'reason': ''})
                for stamp, success, latency in json.loads(latencies):
                    if stamp / 1000 <= now:
                        value['count'] += 1
                        if success:
                            value['values'].append(latency)
                value['offline_seconds'] += offline_seconds
                if offline_reason:
                    value['reason'] = offline_reason
        for node_id in ids:
            buckets, all_values = [], []
            for stamp in builtins_range(start, end + 900, 900):
                value = grouped[node_id].get(stamp, {'values': [], 'count': 0, 'offline_seconds': 0, 'reason': ''})
                values = value['values']
                all_values.extend(values)
                p95 = percentile95(values)
                if value['count'] > len(values):
                    status, reason = 'critical', 'heartbeat_request_failed'
                elif value['offline_seconds'] and not values:
                    status, reason = 'offline', value['reason']
                elif value['offline_seconds']:
                    status, reason = 'critical', value['reason']
                elif not values:
                    status, reason = 'unknown', 'no_observations'
                elif p95 > critical_ms:
                    status, reason = 'critical', 'latency_above_critical_threshold'
                elif p95 > healthy_ms:
                    status, reason = 'degraded', 'latency_above_healthy_threshold'
                else:
                    status, reason = 'healthy', 'latency_within_threshold'
                buckets.append({'start': stamp, 'end': stamp + 900, 'sample_count': value['count'],
                                'success_count': len(values), 'avg_ms': sum(values) / len(values) if values else None,
                                'p95_ms': p95, 'status': status, 'reason': reason})
            output['nodes'][node_id] = {'buckets': buckets, 'avg_ms': sum(all_values) / len(all_values) if all_values else None,
                                        'p95_ms': percentile95(all_values)}
        return output


# Preserve the specified public history(..., range=...) signature without shadowing iteration.
builtins_range = range
