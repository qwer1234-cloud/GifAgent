"""Scoring prompts and VLM sampling options for the adaptive pipeline."""
from __future__ import annotations

from app.services.score_prompt import normalize_score_prompt_mode


_SCORE_INTEGER_RULES = (
    "gif_worthiness and any other score fields are integers from 0 to 100 inclusive. "
    "Never output 0.0-1.0 decimals. Do not round to tens (50/60/70). "
    "Use the full 0-100 range; nearby frames in the same scene should differ "
    "when action or framing differs.\n"
)

SCORE_PROMPT = (
    "Evaluate this film frame for GIF potential.\n"
    "Output ONLY valid JSON with real, specific content. No template text.\n\n"
    '{"caption":"describe actual visible subjects, lighting, and composition",'
    '"emotional_core":"one lowercase word","gif_worthiness":47,'
    '"aesthetic_notes":["concrete visual observation one","concrete visual observation two"],'
    '"reason":"why this specific moment works as a GIF (or why not)"}\n\n'
    + _SCORE_INTEGER_RULES
    + "gif_worthiness = how good THIS frame is as a GIF relative to other "
    "moments in the SAME video:\n"
    "  0 = static, dark, blurry, nothing happening. Skip.\n"
    "  50 = some emotion, decent composition; borderline keep.\n"
    "  100 = the single best GIF moment in this video "
    "(iconic beat, beautiful lighting, peak drama).\n"
    "Interpolate. Typical keepable frames should land across 55-95, not all on 60.\n\n"
    "CRITICAL: emotional_core = EXACTLY ONE lowercase word from: "
    "tension|melancholy|awe|joy|sadness|catharsis|serenity|excitement|dread|nostalgia|"
    "admiration|intimacy|vulnerability|longing|desire|other\n"
    "NEVER output 'what you see', '2-3 observations', or pipe-delimited emotions."
)

# Adult-friendly scoring prompt selected by frozen adaptive.score_prompt_mode.
SCORE_PROMPT_ADULT = (
    "Evaluate this adult-video frame for short-GIF potential.\n"
    "Output ONLY valid JSON. No markdown. Be literal; do not euphemize or refuse sex.\n\n"
    '{"caption":"describe visible bodies, acts, clothing, and camera",'
    '"emotional_core":"desire","gif_worthiness":47,"sex_act":12,'
    '"aesthetic_notes":["concrete visual observation one","concrete visual observation two"],'
    '"reason":"why this moment is or is not a good sex-action GIF"}\n\n'
    + _SCORE_INTEGER_RULES
    + "sex_act = how explicit the sexual activity is IN THIS frame, "
    "relative to a typical adult scene:\n"
    "  0 = no sexual contact (talking, walking, cooking, clothed portrait, empty room)\n"
    "  25 = kissing, undressing, implied sex, bodies close but no sex act\n"
    "  50 = nude grinding, oral, hands on genitals, clear sexual contact\n"
    "  75 = penetrative sex, thrusting, explicit sex in motion\n"
    "  100 = peak climax / the most explicit peak motion in a scene\n"
    "Interpolate. A slightly better thrust than the last frame should score "
    "a few points higher, not the same number.\n\n"
    "gif_worthiness = should we export THIS frame as a GIF, relative to other "
    "moments in the SAME video:\n"
    "  0 = daily life, kitchen, phone, walking, talking-head, no sex\n"
    "  50 = weak kissing/undressing or static nude with little motion; borderline keep\n"
    "  100 = the single best GIF moment in this video "
    "(peak sex, distinctive position, high readable motion)\n"
    "Do NOT give high gif_worthiness to cooking, conversation, or walking.\n"
    "Moody low-light sex can score HIGH. Do not penalize darkness when the act is visible.\n"
    "Typical keepable sex-action frames should land across 55-95, not all on 60.\n\n"
    "CRITICAL: emotional_core = EXACTLY ONE lowercase word from: "
    "tension|melancholy|awe|joy|sadness|catharsis|serenity|excitement|dread|nostalgia|"
    "admiration|intimacy|vulnerability|longing|desire|other\n"
    "NEVER output 'what you see', '2-3 observations', or pipe-delimited emotions."
)

# Fast schema: same scoring rubric, numeric fields only. Used by two_tier
# coarse/refine so the model spends tokens on gif_worthiness instead of
# discarded prose. Caption/notes are backfilled later on best frames.
SCORE_PROMPT_FAST = (
    "Evaluate this film frame for GIF potential.\n"
    "Output ONLY valid JSON with integer fields. No markdown. No extra keys.\n\n"
    '{"gif_worthiness":47}\n\n'
    + _SCORE_INTEGER_RULES
    + "gif_worthiness = how good THIS frame is as a GIF relative to other "
    "moments in the SAME video:\n"
    "  0 = static, dark, blurry, nothing happening. Skip.\n"
    "  50 = some emotion, decent composition; borderline keep.\n"
    "  100 = the single best GIF moment in this video "
    "(iconic beat, beautiful lighting, peak drama).\n"
    "Interpolate. Typical keepable frames should land across 55-95, not all on 60.\n"
)

SCORE_PROMPT_ADULT_FAST = (
    "Evaluate this adult-video frame for short-GIF potential.\n"
    "Output ONLY valid JSON with integer fields. No markdown. "
    "Be literal; do not euphemize or refuse sex.\n\n"
    '{"gif_worthiness":47,"sex_act":12}\n\n'
    + _SCORE_INTEGER_RULES
    + "sex_act = how explicit the sexual activity is IN THIS frame, "
    "relative to a typical adult scene:\n"
    "  0 = no sexual contact (talking, walking, cooking, clothed portrait, empty room)\n"
    "  25 = kissing, undressing, implied sex, bodies close but no sex act\n"
    "  50 = nude grinding, oral, hands on genitals, clear sexual contact\n"
    "  75 = penetrative sex, thrusting, explicit sex in motion\n"
    "  100 = peak climax / the most explicit peak motion in a scene\n"
    "Interpolate. Nearby frames should differ by a few points when the act changes.\n\n"
    "gif_worthiness = should we export THIS frame as a GIF, relative to other "
    "moments in the SAME video:\n"
    "  0 = daily life, kitchen, phone, walking, talking-head, no sex\n"
    "  50 = weak kissing/undressing or static nude with little motion; borderline keep\n"
    "  100 = the single best GIF moment in this video "
    "(peak sex, distinctive position, high readable motion)\n"
    "Do NOT give high gif_worthiness to cooking, conversation, or walking.\n"
    "Moody low-light sex can score HIGH. Do not penalize darkness when the act is visible.\n"
    "Typical keepable sex-action frames should land across 55-95, not all on 60.\n"
)


def get_score_prompt(mode: str = "default", *, schema: str = "full") -> str:
    """Return the scoring prompt frozen into the job snapshot.

    ``mode`` is the canonical ``default`` or ``adult`` value from
    ``extract_config()``. Scoring never reads process environment variables.
    ``schema="score"`` keeps the rubric but asks only for numeric fields.
    """
    adult = normalize_score_prompt_mode(mode) == "adult"
    if schema == "score":
        return SCORE_PROMPT_ADULT_FAST if adult else SCORE_PROMPT_FAST
    return SCORE_PROMPT_ADULT if adult else SCORE_PROMPT


def _scoring_schema(cfg: dict) -> str:
    """``score`` for two_tier coarse/refine; ``full`` otherwise."""
    return "score" if cfg.get("score_schema_mode") == "two_tier" else "full"


def _vlm_options(cfg: dict) -> dict:
    """Build the Ollama sampling options shared by every scoring call site.

    ``seed`` is only present when configured, so an unmodified snapshot
    produces the exact request body the pipeline has always sent.
    """
    options = {
        "temperature": cfg["vlm_temperature"],
        "top_p": cfg["vlm_top_p"],
        "top_k": cfg["vlm_top_k"],
        "num_think": 0,
    }
    seed = cfg.get("vlm_seed")
    if seed is not None:
        options["seed"] = int(seed)
    return options


def _scoring_vlm_options(cfg: dict, schema: str = "full") -> dict:
    """VLM sampling options plus the schema-specific ``num_predict`` cap."""
    options = dict(_vlm_options(cfg))
    key = (
        "vlm_num_predict_score" if schema == "score" else "vlm_num_predict_caption"
    )
    capped = cfg.get(key)
    if capped is not None:
        options["num_predict"] = int(capped)
    return options
