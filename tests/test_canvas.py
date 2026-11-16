"""The 4PP spot bar chart with spots that have no Rs, or no SD."""
import warnings

import pytest

pytest.importorskip("PySide6")

NAN = float('nan')


@pytest.fixture
def canvas(app):
    from resistamet_gui.ui.canvas import HistogramCanvas
    return HistogramCanvas()


def _annotation(canvas):
    return [t.get_text() for t in canvas.axes.texts if 'Inter-spot' in t.get_text()]


def _mean_line(canvas):
    """The y of the dashed inter-spot mean line, or None when not drawn."""
    lines = [line for line in canvas.axes.lines if line.get_linestyle() == '--']
    return lines[0].get_ydata()[0] if lines else None


def _draw_strictly(canvas, names, means, stds):
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        canvas.update_bar_chart(names, means, stds)
        canvas.draw()


class TestBarChartWithMissingValues:
    def test_all_finite_is_unchanged(self, canvas):
        _draw_strictly(canvas, ['a', 'b'], [10.0, 12.0], [0.5, 0.8])
        assert _annotation(canvas) == ['Inter-spot RSD: 12.9%']
        assert _mean_line(canvas) == pytest.approx(11.0)

    def test_a_spot_with_no_rs_is_left_out_of_the_inter_spot_figures(self, canvas):
        """np.mean over a NaN mean gave 'Inter-spot RSD: nan%' and no line."""
        _draw_strictly(canvas, ['a', 'b', 'c'], [10.0, NAN, 12.0], [0.5, NAN, 0.8])
        assert _annotation(canvas) == ['Inter-spot RSD: 12.9%']
        assert _mean_line(canvas) == pytest.approx(11.0)
        # The spot keeps its place and its name on the axis.
        assert [t.get_text() for t in canvas.axes.get_xticklabels()] == ['a', 'b', 'c']

    def test_a_nan_sd_does_not_take_the_y_range_with_it(self, canvas):
        """min(m - s) with a NaN s made the limits NaN: set_ylim raised."""
        _draw_strictly(canvas, ['a', 'b'], [10.0, 12.0], [NAN, 0.8])
        lo, hi = canvas.axes.get_ylim()
        assert lo < 10.0 and hi > 12.8
        assert _annotation(canvas) == ['Inter-spot RSD: 12.9%']

    def test_none_is_taken_as_missing(self, canvas):
        _draw_strictly(canvas, ['a', 'b', 'c'], [10.0, None, 12.0], [None, None, 0.8])
        assert _annotation(canvas) == ['Inter-spot RSD: 12.9%']

    def test_one_spot_with_an_rs_has_no_inter_spot_figure(self, canvas):
        _draw_strictly(canvas, ['a', 'b'], [10.0, NAN], [0.5, NAN])
        assert _annotation(canvas) == []
        assert _mean_line(canvas) is None

    def test_no_spot_with_an_rs_still_draws(self, canvas):
        _draw_strictly(canvas, ['a', 'b'], [NAN, NAN], [NAN, NAN])
        assert _annotation(canvas) == []
