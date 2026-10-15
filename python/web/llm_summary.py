"""
End-of-day performance summary using App Lab's local, on-device
LargeLanguageModel Brick - deliberately NOT the Cloud LLM Brick, per the
project's "fully local, no cloud dependency" requirement.

Confirmed Brick API:
    LargeLanguageModel(system_prompt=..., temperature=..., max_tokens=...,
                       timeout=..., model=...)
    .chat(message) -> str   # blocking, returns the complete response

Model: Qwen 3.5 0.8B (507MB) is the pick over Gemma 3 1B (722MB) - this
board's 4GB RAM is already shared with the camera pipeline, the Object
Detection Brick and the web server, and summarizing six numbers into three
sentences doesn't need the extra capacity. Download it from the Brick's
"AI models" tab in App Lab before first use.

UX note: .chat() blocks until the full response is ready. Even at 0.8B this
can take a while on-device, so this is only ever triggered by an explicit
button click, never automatically - see assets/app.js.
"""
import logging

import config

logger = logging.getLogger("workspace_ai.llm_summary")

SYSTEM_PROMPT = (
    "You are a brief, encouraging productivity coach embedded in a desk device. "
    "You give short, warm, practical feedback based on desk-work data. "
    "Never invent numbers that weren't given to you. Keep responses to 3-4 sentences."
)

_llm = None
_llm_load_attempted = False


def _get_llm():
    """Lazily initializes the LLM Brick on first use rather than at app
    startup - loading a model shouldn't slow the app's boot if this feature
    is never used in a given session."""
    global _llm, _llm_load_attempted
    if _llm_load_attempted:
        return _llm
    _llm_load_attempted = True

    try:
        from arduino.app_bricks.large_language_model import LargeLanguageModel
    except ImportError:
        # Module path not confirmed from the docs (the class name is, the
        # module name isn't) - try the other plausible name before giving up.
        try:
            from arduino.app_bricks.llm import LargeLanguageModel  # type: ignore
        except Exception as exc:  # noqa: BLE001
            logger.warning("LargeLanguageModel Brick not importable (%s) - daily summary disabled.", exc)
            return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("LargeLanguageModel Brick unavailable (%s) - daily summary disabled.", exc)
        return None

    try:
        kwargs = {
            "system_prompt": SYSTEM_PROMPT,
            "temperature": 0.4,   # low-ish: we want consistent, grounded feedback, not creative writing
            "max_tokens": 300,    # 3-4 sentences with headroom
            "timeout": config.LLM_SUMMARY_MAX_WAIT_SECONDS,
        }
        if config.LLM_MODEL:
            kwargs["model"] = config.LLM_MODEL
        _llm = LargeLanguageModel(**kwargs)
        logger.info("Local LargeLanguageModel Brick loaded (model=%s).", config.LLM_MODEL or "brick default")
    except Exception as exc:  # noqa: BLE001 - never crash the app over an optional feature
        logger.warning("Failed to initialize LargeLanguageModel (%s) - daily summary disabled.", exc)
        _llm = None
    return _llm


def build_prompt(today_rows: list):
    """Turns today's hourly rollup rows into a compact stats block.
    Hardware-independent - this is just arithmetic and string formatting."""
    if not today_rows:
        return None

    engaged = sum(r.get("engaged_seconds") or 0 for r in today_rows)
    distracted = sum(r.get("distracted_seconds") or 0 for r in today_rows)
    away = sum(r.get("away_seconds") or 0 for r in today_rows)
    phone = sum(r.get("phone_seconds") or 0 for r in today_rows)
    posture_poor = sum(r.get("posture_poor_seconds") or 0 for r in today_rows)
    posture_good = sum(r.get("posture_good_seconds") or 0 for r in today_rows)

    scored_engagement = [r["avg_engagement_score"] for r in today_rows if r.get("avg_engagement_score") is not None]
    scored_posture = [r["avg_posture_score"] for r in today_rows if r.get("avg_posture_score") is not None]
    avg_engagement = round(sum(scored_engagement) / len(scored_engagement), 1) if scored_engagement else None
    avg_posture = round(sum(scored_posture) / len(scored_posture), 1) if scored_posture else None

    def mins(seconds):
        return round(seconds / 60)

    stats_lines = [
        f"Focused/engaged time: {mins(engaged)} min",
        f"Distracted time: {mins(distracted)} min",
        f"Time away from desk: {mins(away)} min",
        f"Phone-in-view time: {mins(phone)} min",
        f"Time with good posture: {mins(posture_good)} min",
        f"Time with poor posture: {mins(posture_poor)} min",
    ]
    if avg_engagement is not None:
        stats_lines.append(f"Average engagement score: {avg_engagement} out of 100")
    if avg_posture is not None:
        stats_lines.append(f"Average posture score: {avg_posture} out of 100 (relative to their own baseline)")

    return (
        "Here is today's desk-work data. Write a short summary with: one honest "
        "observation, one specific actionable suggestion, and one encouraging note.\n\n"
        + "\n".join(f"- {line}" for line in stats_lines)
    )


def generate_summary(today_rows: list) -> dict:
    """Returns {"status": "ok", "summary": str} or {"status": ..., "message": str}."""
    prompt = build_prompt(today_rows)
    if prompt is None:
        return {"status": "no_data", "message": "No data recorded yet today - nothing to summarize."}

    llm = _get_llm()
    if llm is None:
        return {
            "status": "unavailable",
            "message": (
                "Local LLM isn't available. Check that the Large Language Model Brick is added "
                "to this app and a model has been downloaded from its 'AI models' tab."
            ),
        }

    try:
        text = llm.chat(prompt)
        return {"status": "ok", "summary": (text or "").strip()}
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM summary generation failed (%s)", exc)
        return {"status": "error", "message": "Summary generation failed - check the app logs for details."}
