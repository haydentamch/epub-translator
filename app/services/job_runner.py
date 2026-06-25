from app.db import (
    get_job,
    get_job_chapters,
    get_pending_chapter_paragraphs,
    is_job_pause_requested,
    mark_job_paused,
    set_chapter_status,
    set_job_status,
)
from app.services.translator import TranslationPaused, translate_paragraphs


async def run_translation_job(job_id: int, api_key: str) -> None:
    await _run_translation_job(job_id, api_key, retry_only=False)


async def retry_failed_chapters(job_id: int, api_key: str) -> None:
    await _run_translation_job(job_id, api_key, retry_only=True)


async def _run_translation_job(
    job_id: int,
    api_key: str,
    retry_only: bool,
) -> None:
    job = get_job(job_id)
    if job is None:
        return

    for chapter in get_job_chapters(job_id):
        if not chapter["selected"]:
            set_chapter_status(chapter["id"], "Skipped")
            continue

        if retry_only and chapter["status"] not in {"Partial", "Failed"}:
            continue

        if is_job_pause_requested(job_id):
            mark_job_paused(job_id)
            return

        paragraphs = get_pending_chapter_paragraphs(chapter["id"])

        if not paragraphs:
            set_chapter_status(chapter["id"], "Completed")
            continue

        set_chapter_status(chapter["id"], "Translating")

        try:
            await translate_paragraphs(
                job,
                paragraphs,
                api_key,
                should_pause=lambda: is_job_pause_requested(job_id),
            )
            if is_job_pause_requested(job_id):
                raise TranslationPaused
            set_chapter_status(chapter["id"], "Completed")
        except TranslationPaused:
            set_chapter_status(chapter["id"], "Pending")
            mark_job_paused(job_id)
            return
        except Exception as exc:
            refreshed = _get_chapter(job_id, int(chapter["id"]))
            completed = refreshed["completed_paragraphs"] or 0
            status = "Partial" if completed else "Failed"
            set_chapter_status(
                chapter["id"],
                status,
                _safe_error_message(exc),
            )

    chapters = get_job_chapters(job_id)
    has_errors = any(
        chapter["selected"] and chapter["status"] in {"Partial", "Failed"}
        for chapter in chapters
    )
    set_job_status(job_id, "CompletedWithErrors" if has_errors else "Completed")


def _get_chapter(job_id: int, chapter_id: int) -> dict:
    return next(
        chapter
        for chapter in get_job_chapters(job_id)
        if int(chapter["id"]) == chapter_id
    )


def _safe_error_message(exc: Exception) -> str:
    message = str(exc).strip()
    return message[:500] if message else "Translation failed."
