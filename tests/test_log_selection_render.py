import pytest

from robot_control.log_tui import StructuredLog
from robot_control.tui import RobotControlApp
from test_structured_log_tui import point, settled
from test_tui import FakeController


def rendered_characters(log, row):
    return [(char, segment.style) for segment in log.render_line(row) for char in segment.text]


@pytest.mark.asyncio
@pytest.mark.parametrize('reverse', [False, True])
async def test_partial_drag_changes_only_selected_background_and_keeps_glyphs_visible(reverse):
    app = RobotControlApp(FakeController())
    async with app.run_test(size=(90, 24)) as pilot:
        log = app.query_one(StructuredLog)
        log.write('prefix alpha beta gamma tail')
        log.write('next record is not selected')
        await settled(log, pilot)
        before = rendered_characters(log, 0)
        other = rendered_characters(log, 1)
        start, end = (14, 9) if reverse else (9, 14)
        await pilot.mouse_down(log, offset=point(log, start))
        await pilot.hover(log, offset=point(log, end))
        # Check the actual rendered strip during the drag, not just clipboard data.
        during = rendered_characters(log, 0)
        assert log.selected_text == 'alpha'
        assert ''.join(char for char, _ in during) == ''.join(char for char, _ in before)
        for i, ((_, original), (_, selected)) in enumerate(zip(before, during)):
            if 9 <= i < 14:  # Two gutter cells, then seven prefix characters.
                assert selected.bgcolor != original.bgcolor
                assert selected.color == original.color
                assert selected.color != selected.bgcolor
            else:
                assert selected == original
        assert rendered_characters(log, 1) == other
        await pilot.mouse_up(log, offset=point(log, end))
        await pilot.press('ctrl+shift+c')
        assert app.clipboard == 'alpha'
        await pilot.click(log, offset=point(log, 3, 4))
        assert rendered_characters(log, 0) == before


@pytest.mark.asyncio
async def test_multiline_selection_highlights_only_selected_record_ranges():
    app = RobotControlApp(FakeController())
    async with app.run_test(size=(90, 24)) as pilot:
        log = app.query_one(StructuredLog)
        for message in ('alpha beta', 'gamma delta', 'untouched'):
            log.write(message)
        await settled(log, pilot)
        baseline = [rendered_characters(log, y) for y in range(3)]
        await pilot.mouse_down(log, offset=point(log, 8, 0))
        await pilot.hover(log, offset=point(log, 7, 1))
        for y, selected_range in ((0, range(8, 12)), (1, range(2, 7)), (2, range(0))):
            during = rendered_characters(log, y)
            for x, ((_, before), (_, after)) in enumerate(zip(baseline[y], during)):
                assert (before.bgcolor != after.bgcolor) == (x in selected_range)
                assert after.color == before.color
        await pilot.mouse_up(log, offset=point(log, 7, 1))
        await pilot.press('ctrl+shift+c')
        assert app.clipboard == 'beta\ngamma'
