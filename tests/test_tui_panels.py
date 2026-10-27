import asyncio

import pytest
from robot_control.log_tui import StructuredLog

from test_controller import setup_controller as setup_controller, ready, until


@pytest.mark.parametrize("name", ["camera", "monitor"])
def test_panel_names_are_reserved_and_unavailable_to_headless_and_mcp(name, setup_controller):
    from rc_policy_sdk.commands import command_specs
    from robot_control.terminal_commands import TerminalCommandRouter
    controller, _, _ = setup_controller
    ready(controller)
    with pytest.raises(ValueError, match="reserved"):
        command_specs({"tui_commands": {name: {"handler": "f", "args": []}}})
    router = TerminalCommandRouter(controller)
    with pytest.raises(ValueError, match="TUI"):
        router.submit(name + " on")
    with pytest.raises(PermissionError, match="local-only"):
        controller.submit(name + " on", origin="mcp")


@pytest.mark.asyncio
async def test_panels_toggle_layout_monitor_and_live_camera(setup_controller):
    from robot_control.graphics import GraphicsSupport
    from robot_control.tui import CommandInput, RobotControlApp
    from robot_control.tui_panels import CameraImage, CameraPanel, MonitorPanel

    controller, _, _ = setup_controller
    ready(controller)
    writes = []
    app = RobotControlApp(controller, start_controller=False, graphics=GraphicsSupport("kitty"))
    app._write_graphics = writes.append
    async with app.run_test(size=(160, 42)) as pilot:
        camera, monitor = app.query_one(CameraPanel), app.query_one(MonitorPanel)
        assert not camera.display and not monitor.display
        command = app.query_one(CommandInput)
        command.value = "mon"
        await pilot.press("tab")
        assert command.value == "monitor "
        command.value = "monitor on"
        await pilot.press("enter")
        command.value = "camera on"
        await pilot.press("enter")
        assert camera.display and monitor.display
        await pilot.pause(.2)
        await asyncio.to_thread(until, lambda: app.preview.samples)
        await pilot.pause(.2)
        assert monitor.region.y < camera.region.y < command.region.y
        images = list(camera.query(CameraImage))
        assert [item.camera for item in images] == ["left_wrist", "head", "right_wrist"]
        assert len({item.region.y for item in images}) == 1
        assert all(item.frame is not None for item in images)
        assert any("f=100" in text for text in writes)
        assert "qpos" in monitor.query_one("#monitor-values").content.plain
        assert "epos" in monitor.query_one("#monitor-values").content.plain
        # Repeating on must preserve state; disabling the other panel is independent.
        app.router.submit("camera on")
        app.router.submit("monitor off")
        assert camera.display and not monitor.display
        app.router.submit("camera off")
        assert not camera.display and all(item.frame is None for item in images)
        assert any("a=d,d=I" in text for text in writes)
        command.value = "camera unexpected"
        await pilot.press("enter")
        assert app.is_running and not camera.display
        assert app.query_one(StructuredLog).records
        command.value = "exit"
        await pilot.press("enter")
        await asyncio.to_thread(until, controller.done.is_set)
        await pilot.pause(.1)
    assert app.preview.closed


@pytest.mark.asyncio
async def test_unsupported_camera_keeps_monitor_and_commands_usable(setup_controller):
    from robot_control.graphics import GraphicsSupport
    from robot_control.tui import CommandInput, RobotControlApp
    from robot_control.tui_panels import CameraPanel, MonitorPanel
    controller, _, _ = setup_controller
    ready(controller)
    app = RobotControlApp(controller, start_controller=False,
                          graphics=GraphicsSupport("sixel", error="unsupported terminal"))
    async with app.run_test(size=(60, 24)) as pilot:
        command = app.query_one(CommandInput)
        for line in ("camera on", "monitor on", "status"):
            command.value = line
            await pilot.press("enter")
        assert not app.query_one(CameraPanel).display
        assert app.query_one(MonitorPanel).display
        assert command.region.bottom <= app.size.height
        assert app.is_running
        command.value = "quit"
        await pilot.press("enter")
        await asyncio.to_thread(until, controller.done.is_set)
        await pilot.pause(.1)


@pytest.mark.asyncio
async def test_camera_disconnect_retains_image_and_render_error_disables_only_camera(setup_controller):
    from robot_control.graphics import GraphicsSupport
    from robot_control.tui import RobotControlApp
    from robot_control.tui_panels import CameraImage, CameraPanel, MonitorPanel
    controller, robot, _ = setup_controller
    ready(controller)
    samples = robot.camera_preview_snapshot()
    robot.camera_preview_snapshot = lambda: samples.copy()
    writes = []
    app = RobotControlApp(controller, start_controller=False, graphics=GraphicsSupport("sixel"))
    app._write_graphics = writes.append
    async with app.run_test(size=(120, 36)) as pilot:
        app.router.submit("camera on")
        app.router.submit("monitor on")
        await pilot.pause(.3)
        image = app.query_one("#image-head", CameraImage)
        assert image.frame is not None
        retained = image.frame
        samples["head"] = {**samples["head"], "received": 0.}
        await pilot.pause(.15)
        camera = app.query_one(CameraPanel)
        status = camera.query_one("#camera-status-head").content
        assert "disconnected" in status.plain and status.style == "red"
        assert image.frame is retained
        output = app.screen._compositor.render_full_update().render_segments(app.console)
        assert retained.payload in output or any(retained.payload in text for text in writes)
        await pilot.resize_terminal(80, 30)
        await pilot.pause(.3)
        assert image.frame is not None and image.frame.size == image.pixel_size
        assert app.query_one("#command").region.bottom <= app.size.height
        app._camera_failed("encoder failure")
        assert not camera.display and app.query_one(MonitorPanel).display
        controller.submit("exit")
        await asyncio.to_thread(until, controller.done.is_set)
        await pilot.pause(.1)


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [0, -1])
async def test_sixel_partial_repaint_restores_retained_frame(row, setup_controller):
    from textual.geometry import Region
    from robot_control.graphics import GraphicsSupport
    from robot_control.tui import RobotControlApp
    from robot_control.tui_panels import CameraImage
    controller, robot, _ = setup_controller
    ready(controller)
    samples = robot.camera_preview_snapshot()
    robot.camera_preview_snapshot = lambda: samples
    writes = []
    app = RobotControlApp(controller, start_controller=False, graphics=GraphicsSupport("sixel"))
    app._write_graphics = writes.append
    async with app.run_test(size=(120, 36)) as pilot:
        app.router.submit("camera on")
        image = app.query_one("#image-head", CameraImage)
        # Encoding is asynchronous; wait for its result rather than assuming
        # the first frame always fits a 300ms budget on a busy container host.
        for _ in range(40):
            if image.frame is not None:
                break
            await pilot.pause(.05)
        assert image.frame is not None
        payload = image.frame.payload
        writes.clear()
        image.refresh(Region(2, row % image.size.height, 5, 1))
        compositor = app.screen._compositor
        compositor.update_widgets({image})
        update = compositor.render_partial_update()
        assert update is not None
        output = update.render_segments(app.console)  # must survive cropping
        await pilot.pause(.1)
        assert payload in output or any(payload in text for text in writes)
        controller.submit("exit")
        await asyncio.to_thread(until, controller.done.is_set)
        await pilot.pause(.1)


@pytest.fixture
def kitty_preview_app():
    from textual.app import App
    from robot_control.graphics import GraphicsSupport
    from robot_control.tui_panels import CameraPanel

    class PreviewApp(App):
        CSS = "CameraPanel { display: block; height: 1fr; }"

        def __init__(self):
            super().__init__()
            self.output = []

        def compose(self):
            yield CameraPanel(GraphicsSupport("kitty", (2, 4)), self.write_graphics, self.fail)

        def write_graphics(self, text):
            self.output.append(("graphics", text))

        def fail(self, error):
            pytest.fail(error)

        def _display(self, screen, renderable):
            if renderable is not None:
                self.output.append(("screen", renderable.render_segments(self.console)))
            super()._display(screen, renderable)

    return PreviewApp()


def present_test_frame(image, value):
    import numpy as np
    from robot_control.graphics import prepare_frame
    image.present(prepare_frame(np.full((12, 16, 3), value, dtype=np.uint8), image.pixel_size, "kitty"))
    return image._frame_id


@pytest.mark.asyncio
async def test_kitty_retires_all_three_old_images_after_placeholder_output(kitty_preview_app):
    from robot_control.tui_panels import CameraImage
    app = kitty_preview_app
    async with app.run_test(size=(120, 30)) as pilot:
        images = list(app.query(CameraImage))
        old_ids = [present_test_frame(image, index) for index, image in enumerate(images)]
        await pilot.pause()
        app.output.clear()
        new_ids = [present_test_frame(image, index + 10) for index, image in enumerate(images)]
        await pilot.pause()
        for old_id, new_id in zip(old_ids, new_ids):
            upload = next(i for i, (_, text) in enumerate(app.output) if f"i={new_id},f=100" in text)
            deletes = [i for i, (_, text) in enumerate(app.output) if f"a=d,d=I,i={old_id}," in text]
            assert len(deletes) == 1
            assert any(kind == "screen" and "\U0010eeee" in text
                       for kind, text in app.output[upload + 1:deletes[0]])
            assert all(f"a=d,d=I,i={new_id}," not in text for _, text in app.output)


@pytest.mark.asyncio
@pytest.mark.parametrize("clear_before_next_frame", [False, True])
async def test_kitty_pending_cleanup_preserves_newest_frame(clear_before_next_frame, kitty_preview_app):
    from textual.geometry import Region
    from robot_control.tui_panels import CameraImage
    app = kitty_preview_app
    async with app.run_test(size=(120, 30)) as pilot:
        image = app.query_one("#image-head", CameraImage)
        first = present_test_frame(image, 1)
        await pilot.pause()
        app.output.clear()
        second = present_test_frame(image, 2)
        image.render_lines(Region(0, 0, image.size.width, image.size.height))
        assert all("a=d" not in text for _, text in app.output)
        if clear_before_next_frame:
            image.clear()
        newest = present_test_frame(image, 3)
        image.render_lines(Region(0, 0, image.size.width, image.size.height))
        await pilot.pause()
        for identity in (first, second):
            assert sum(text.count(f"a=d,d=I,i={identity},") for _, text in app.output) == 1
        assert all(f"a=d,d=I,i={newest}," not in text for _, text in app.output)
        assert image._displayed_id == newest
        await pilot.resize_terminal(90, 24)
        assert image.frame is None
        assert sum(text.count(f"a=d,d=I,i={newest},") for _, text in app.output) == 1
