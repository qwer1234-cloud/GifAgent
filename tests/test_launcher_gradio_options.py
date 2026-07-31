from app.ui.candidate_review import CONFIG_TOOLTIP_CSS, CONFIG_TOOLTIP_JS, REVIEW_LAYOUT_CSS, REVIEW_SHORTCUTS_JS
from app.ui.launcher import launch_gradio_app
from app.ui.workbench import load_layout_css


class FakeGradioApp:
    def __init__(self):
        self.kwargs = None

    def launch(self, **kwargs):
        self.kwargs = kwargs


def test_launcher_passes_tooltip_css_to_gradio():
    app = FakeGradioApp()

    launch_gradio_app(app)

    assert app.kwargs["prevent_thread_lock"] is True
    assert app.kwargs["css"] == CONFIG_TOOLTIP_CSS + REVIEW_LAYOUT_CSS + load_layout_css()
    assert app.kwargs["js"] == CONFIG_TOOLTIP_JS + REVIEW_SHORTCUTS_JS
    assert "preference-memory-enabled" in app.kwargs["js"]
    assert app.kwargs["js"].lstrip().startswith("(() => {")
    assert ".ga-review-layout" in app.kwargs["css"]
    assert ".config-tooltip-icon" in app.kwargs["css"]
