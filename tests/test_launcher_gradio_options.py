from app.ui.candidate_review import CONFIG_TOOLTIP_CSS
from app.ui.launcher import launch_gradio_app


class FakeGradioApp:
    def __init__(self):
        self.kwargs = None

    def launch(self, **kwargs):
        self.kwargs = kwargs


def test_launcher_passes_tooltip_css_to_gradio():
    app = FakeGradioApp()

    launch_gradio_app(app)

    assert app.kwargs["prevent_thread_lock"] is True
    assert app.kwargs["css"] == CONFIG_TOOLTIP_CSS
    assert ".config-tooltip-icon:hover .config-tooltip-text" in app.kwargs["css"]
