import json

import gradio as gr
import httpx

API_BASE = "http://127.0.0.1:8000"


def load_next_for_review():
    """Fetch the next un-reviewed media item from the API."""
    try:
        resp = httpx.get(f"{API_BASE}/api/review/next", timeout=10)
        if resp.status_code != 200:
            return None, "", None, None, [], "", "No items to review", ""
        data = resp.json()
        media = data.get("media", {})
        annotation = data.get("annotation", {})
        similar = data.get("similar", [])

        media_id = media.get("media_id", "")
        preview_path = media.get("file_path", "")
        summary = annotation.get("summary", "")
        emotional = annotation.get("emotional_core", "")
        aesthetic = ", ".join(json.loads(annotation.get("aesthetic_notes_json", "[]")) or [])
        why = annotation.get("why_i_like_it", "")
        tags_str = ", ".join(json.loads(annotation.get("tags_json", "[]")) or [])

        # Build status line
        conn_info = f"ID: {media_id[:16]}... | Emotion: {emotional}"

        similar_previews = []
        for s in similar[:3]:
            # Try to load thumbnail; fall back to placeholder
            similar_previews.append((None, f"{s.get('emotional_core','?')} | {s.get('film','?')}"))

        return (preview_path, media_id, summary, emotional,
                similar_previews, aesthetic, why, tags_str, conn_info)
    except Exception as e:
        return None, "", None, None, [], "", f"Error: {e}", "", ""


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
