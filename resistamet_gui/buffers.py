from collections import deque
from typing import Dict, List, Optional, Tuple
import numpy as np


class EnhancedDataBuffer:
    """Stores timestamps, resistance, voltage, current, events, and compliance per point.

    Provides per-channel statistics and plotting data selection.
    """

    def __init__(self, size: Optional[int] = None):
        self._max_len = size if size is not None and size > 0 else None
        self.reset()

    def reset(self):
        self.timestamps = deque(maxlen=self._max_len)
        self.resistance = deque(maxlen=self._max_len)
        self.voltage = deque(maxlen=self._max_len)
        self.current = deque(maxlen=self._max_len)
        self.events = deque(maxlen=self._max_len)
        self.compliance_status = deque(maxlen=self._max_len)  # 'OK', 'V_COMP', 'I_COMP'

        # Stats per channel
        self.stats = {
            'resistance': {'min': float('inf'), 'max': float('-inf'), 'avg': 0.0, 'count': 0},
            'voltage': {'min': float('inf'), 'max': float('-inf'), 'avg': 0.0, 'count': 0},
            'current': {'min': float('inf'), 'max': float('-inf'), 'avg': 0.0, 'count': 0},
        }
        self.last_compliance_hit = None

    @property
    def size(self):
        return self._max_len

    def _update_stat(self, key: str, value: float):
        if np.isfinite(value):
            st = self.stats[key]
            st['count'] += 1
            if value < st['min']:
                st['min'] = value
            if value > st['max']:
                st['max'] = value
            if st['count'] == 1:
                st['avg'] = value
            else:
                st['avg'] += (value - st['avg']) / st['count']

    def add_resistance(self, timestamp: float, resistance: float, compliance: str = 'OK', event: str = "") -> None:
        self.timestamps.append(timestamp)
        self.resistance.append(resistance if np.isfinite(resistance) and resistance >= 0 else float('nan'))
        self.voltage.append(None)
        self.current.append(None)
        self.events.append(event)
        self.compliance_status.append(compliance)
        if compliance != 'OK':
            self.last_compliance_hit = compliance
        if np.isfinite(resistance) and resistance >= 0:
            self._update_stat('resistance', resistance)

    def add_voltage_current(self, timestamp: float, voltage: float, current: float, compliance: str = 'OK', event: str = "") -> None:
        self.timestamps.append(timestamp)
        v = voltage if np.isfinite(voltage) else float('nan')
        i = current if np.isfinite(current) else float('nan')
        self.voltage.append(v)
        self.current.append(i)
        r = (v / i) if (np.isfinite(v) and np.isfinite(i) and i != 0) else float('nan')
        self.resistance.append(r)
        self.events.append(event)
        self.compliance_status.append(compliance)
        if compliance != 'OK':
            self.last_compliance_hit = compliance
        if np.isfinite(v):
            self._update_stat('voltage', v)
        if np.isfinite(i):
            self._update_stat('current', i)
        if np.isfinite(r) and r >= 0:
            self._update_stat('resistance', r)

    def get_data_for_plot(self, data_type: str = 'resistance') -> Tuple[List[float], List[float], List[str]]:
        ts = list(self.timestamps)
        if not ts:
            return [], [], []
        elapsed = [t - ts[0] for t in ts]
        if data_type == 'resistance':
            values = [x if x is not None else float('nan') for x in list(self.resistance)]
        elif data_type == 'voltage':
            values = [x if x is not None else float('nan') for x in list(self.voltage)]
        elif data_type == 'current':
            values = [x if x is not None else float('nan') for x in list(self.current)]
        else:
            values = []
        return elapsed, values, list(self.compliance_status)

    def get_statistics(self, data_type: str = 'resistance') -> Dict[str, float]:
        st = self.stats.get(data_type, None)
        if not st:
            return {'min': float('inf'), 'max': float('-inf'), 'avg': 0.0}
        return {'min': st['min'], 'max': st['max'], 'avg': st['avg']}

    def clear(self) -> None:
        self.reset()

