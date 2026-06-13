import json

import gradio as gr
import httpx

API_BASE = "http://127.0.0.1:8000"


def load_next_for_review():
    """Fetch the next media item that needs review."""
    try:
        resp = httpx.get(f"{API_BASE}/api/status", timeout=5)
        status = resp.json()
    except Exception:
        return None, "", None, "", [], "", "Cannot connect to API", ""

    conn_info = f"Media: {status['media_count']} | Frames: {status['frame_count']} | Annotated: {status['annotated_media']}"

    return None, "", None, "", [], "", conn_info, ""


def rate(media_id, rating, tags, reason):
    """Save user feedback."""
    if not media_id:
        return "No media to rate"
    try:
        resp = httpx.post(
            f"{API_BASE}/api/feedback",
            params={"media_id": media_id, "rating": rating, "tags": tags, "reason": reason},
            timeout=5,
        )
        return f"Saved: {rating}"
    except Exception as e:
        return f"Error: {e}"


def build_ui():
    with gr.Blocks(title="GifAgent - Review") as demo:
        gr.Markdown("# GifAgent - Movie Scene Review")

        status_text = gr.Textbox(label="Status", interactive=False)

        with gr.Row():
            with gr.Column(scale=2):
                preview = gr.Image(label="Preview", interactive=False)
                similar_gallery = gr.Gallery(label="Similar Scenes")

            with gr.Column(scale=1):
                media_id_state = gr.State("")
                summary = gr.Textbox(label="Summary", interactive=False)
                emotional_core = gr.Textbox(label="Emotional Core", interactive=False)
                aesthetic = gr.Textbox(label="Aesthetic Notes", interactive=False)
                why = gr.Textbox(label="Why I Like It", interactive=False)
                tags = gr.Textbox(label="Tags (comma-separated)")
                reason = gr.Textbox(label="Your reason (optional)")

        with gr.Row():
            like_btn = gr.Button("Like (A)", variant="primary")
            neutral_btn = gr.Button("Neutral (S)")
            dislike_btn = gr.Button("Dislike (D)")
            refresh_btn = gr.Button("Next")

        result = gr.Textbox(label="Action Result")

        refresh_btn.click(
            load_next_for_review,
            outputs=[preview, media_id_state, summary, similar_gallery, status_text],
        )

        def like_action(mid, t, r):
            return rate(mid, "like", t, r)
        def neutral_action(mid, t, r):
            return rate(mid, "neutral", t, r)
        def dislike_action(mid, t, r):
            return rate(mid, "dislike", t, r)

        like_btn.click(like_action, inputs=[media_id_state, tags, reason], outputs=[result])
        neutral_btn.click(neutral_action, inputs=[media_id_state, tags, reason], outputs=[result])
        dislike_btn.click(dislike_action, inputs=[media_id_state, tags, reason], outputs=[result])

    return demo


if __name__ == "__main__":
    ui = build_ui()
    ui.launch(server_name="127.0.0.1", server_port=7860)
