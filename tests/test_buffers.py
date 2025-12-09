"""
Unit tests for the EnhancedDataBuffer class.

Tests cover:
- Buffer initialization
- Adding data points
- Statistics calculation (min, max, avg, rsd)
- Data retrieval for plotting
- Buffer size limits
"""

import math
import pytest

from resistamet_gui.buffers import EnhancedDataBuffer


class TestBufferInitialization:
    """Tests for buffer initialization."""

    def test_default_initialization(self):
        """Test buffer initializes with no size limit by default."""
        buffer = EnhancedDataBuffer()
        assert buffer._max_len is None

    def test_initialization_with_size(self):
        """Test buffer initializes with specified size limit."""
        buffer = EnhancedDataBuffer(size=100)
        assert buffer._max_len == 100

    def test_empty_buffer_statistics(self):
        """Test statistics on empty buffer.

        Note: Empty buffer returns inf/-inf for min/max (standard behavior).
        """
        buffer = EnhancedDataBuffer()
        stats = buffer.get_statistics('resistance')
        # Empty stats have inf/-inf for min/max, 0.0 for avg
        assert stats['min'] == float('inf')
        assert stats['max'] == float('-inf')
        assert stats['avg'] == 0.0


class TestAddData:
    """Tests for adding data to buffer."""

    def test_add_resistance(self):
        """Test adding resistance data point."""
        buffer = EnhancedDataBuffer()
        buffer.add_resistance(timestamp=1.0, resistance=100.0, compliance='OK')
        assert len(buffer.timestamps) == 1
        assert len(buffer.resistance) == 1
        assert buffer.resistance[0] == 100.0

    def test_add_voltage_current(self):
        """Test adding voltage/current data point."""
        buffer = EnhancedDataBuffer()
        buffer.add_voltage_current(
            timestamp=1.0, voltage=5.0, current=0.001, compliance='OK'
        )
        assert len(buffer.timestamps) == 1
        assert len(buffer.voltage) == 1
        assert len(buffer.current) == 1
        assert buffer.voltage[0] == 5.0
        assert buffer.current[0] == 0.001

    def test_buffer_respects_size_limit(self):
        """Test that buffer respects size limit."""
        buffer = EnhancedDataBuffer(size=5)
        for i in range(10):
            buffer.add_resistance(timestamp=float(i), resistance=float(i * 100), compliance='OK')

        # Should only have 5 items (oldest removed)
        assert len(buffer.timestamps) == 5
        assert len(buffer.resistance) == 5

        # First item should be timestamp 5 (oldest 0-4 removed)
        assert buffer.timestamps[0] == 5.0

    def test_add_with_event(self):
        """Test adding data with event marker."""
        buffer = EnhancedDataBuffer()
        buffer.add_resistance(timestamp=1.0, resistance=100.0, compliance='OK', event='MARK')
        assert buffer.events[0] == 'MARK'


class TestStatistics:
    """Tests for statistics calculation."""

    def test_min_max_avg(self):
        """Test basic statistics calculation."""
        buffer = EnhancedDataBuffer()
        buffer.add_resistance(1.0, 100.0, 'OK')
        buffer.add_resistance(2.0, 200.0, 'OK')
        buffer.add_resistance(3.0, 300.0, 'OK')

        stats = buffer.get_statistics('resistance')
        assert stats['min'] == pytest.approx(100.0)
        assert stats['max'] == pytest.approx(300.0)
        assert stats['avg'] == pytest.approx(200.0)

    def test_internal_count_tracking(self):
        """Test that internal stats track count correctly."""
        buffer = EnhancedDataBuffer()
        # Add values with known statistics
        values = [100.0, 100.0, 100.0, 100.0]  # No variation
        for i, v in enumerate(values):
            buffer.add_resistance(float(i), v, 'OK')

        # Internal stats track count (not exposed via get_statistics)
        assert buffer.stats['resistance']['count'] == 4

    def test_statistics_ignores_nan(self):
        """Test that statistics ignore NaN values."""
        buffer = EnhancedDataBuffer()
        buffer.add_resistance(1.0, 100.0, 'OK')
        buffer.add_resistance(2.0, float('nan'), 'OK')
        buffer.add_resistance(3.0, 300.0, 'OK')

        stats = buffer.get_statistics('resistance')
        # Should only consider 100 and 300
        assert stats['min'] == pytest.approx(100.0)
        assert stats['max'] == pytest.approx(300.0)
        assert stats['avg'] == pytest.approx(200.0)

    def test_statistics_for_voltage(self):
        """Test statistics calculation for voltage data."""
        buffer = EnhancedDataBuffer()
        buffer.add_voltage_current(1.0, 1.0, 0.001, 'OK')
        buffer.add_voltage_current(2.0, 2.0, 0.001, 'OK')
        buffer.add_voltage_current(3.0, 3.0, 0.001, 'OK')

        stats = buffer.get_statistics('voltage')
        assert stats['min'] == pytest.approx(1.0)
        assert stats['max'] == pytest.approx(3.0)
        assert stats['avg'] == pytest.approx(2.0)

    def test_statistics_for_current(self):
        """Test statistics calculation for current data."""
        buffer = EnhancedDataBuffer()
        buffer.add_voltage_current(1.0, 1.0, 0.001, 'OK')
        buffer.add_voltage_current(2.0, 1.0, 0.002, 'OK')
        buffer.add_voltage_current(3.0, 1.0, 0.003, 'OK')

        stats = buffer.get_statistics('current')
        assert stats['min'] == pytest.approx(0.001)
        assert stats['max'] == pytest.approx(0.003)


class TestDataRetrieval:
    """Tests for data retrieval methods."""

    def test_get_data_for_plot_resistance(self):
        """Test getting resistance data for plotting."""
        buffer = EnhancedDataBuffer()
        buffer.add_resistance(1000.0, 100.0, 'OK')
        buffer.add_resistance(1001.0, 200.0, 'OK')
        buffer.add_resistance(1002.0, 300.0, 'OK')

        elapsed, values, events = buffer.get_data_for_plot('resistance')

        # Elapsed time should be relative to first timestamp
        assert elapsed[0] == pytest.approx(0.0)
        assert elapsed[1] == pytest.approx(1.0)
        assert elapsed[2] == pytest.approx(2.0)

        assert values == [100.0, 200.0, 300.0]

    def test_get_data_for_plot_empty(self):
        """Test getting data from empty buffer."""
        buffer = EnhancedDataBuffer()
        elapsed, values, events = buffer.get_data_for_plot('resistance')
        assert elapsed == []
        assert values == []
        assert events == []

    def test_get_data_for_plot_handles_none(self):
        """Test that None values are converted to NaN for plotting."""
        buffer = EnhancedDataBuffer()
        # When adding resistance, voltage/current are set to None
        buffer.add_resistance(1.0, 100.0, 'OK')

        # Getting voltage should convert None to NaN
        elapsed, values, compliance = buffer.get_data_for_plot('voltage')
        assert math.isnan(values[0])


class TestClear:
    """Tests for buffer clearing."""

    def test_clear_all_data(self):
        """Test clearing all buffer data."""
        buffer = EnhancedDataBuffer()
        buffer.add_resistance(1.0, 100.0, 'OK')
        buffer.add_resistance(2.0, 200.0, 'OK')

        buffer.clear()

        assert len(buffer.timestamps) == 0
        assert len(buffer.resistance) == 0
        assert len(buffer.voltage) == 0
        assert len(buffer.current) == 0
        assert len(buffer.events) == 0
        assert len(buffer.compliance_status) == 0
