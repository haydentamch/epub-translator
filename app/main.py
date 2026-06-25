from pathlib import Path
from uuid import uuid4

from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.db import (
    claim_job_for_resume,
    claim_job_for_retry,
    claim_job_for_translation,
    delete_book,
    get_book_overview,
    get_book_cleanup_paths,
    get_books_for_title_refresh,
    get_history_items,
    get_job,
    get_job_settings,
    get_job_progress,
    get_latest_export_filename,
    get_preview_paragraphs,
    init_db,
    request_job_pause,
    save_export_record,
    save_job_settings,
    save_chapter_selection,
    save_parsed_book,
    update_chapter_titles,
)
from app.schemas import OpenRouterKeyRequest
from app.services.epub_parser import parse_epub
from app.services.exporter import EXPORT_DIR, ExportError, export_epub
from app.services.job_runner import retry_failed_chapters, run_translation_job
from app.services.openrouter_client import (
    OpenRouterError,
    fetch_models,
    test_api_key,
)
from app.services.translator import TranslationResponseError, translate_preview


BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "data" / "uploads"
ALLOWED_EXTENSION = ".epub"
TEXT_DIRECTIONS = {
    "horizontal-ltr",
    "horizontal-rtl",
    "vertical-rtl",
    "auto-detect",
}
LAYOUTS = {"side-by-side", "original-first", "translation-first"}
PREVIEW_SCOPES = {"first-3000-words", "first-10-pages", "first-chapter"}

app = FastAPI(title="EPUB Translator")
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")

templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")


@app.on_event("startup")
def ensure_upload_dir() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    _refresh_existing_chapter_titles()


@app.get("/", response_class=HTMLResponse)
def upload_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "upload.html",
        {"request": request, "uploaded_file": None, "error": None},
    )


@app.post("/upload")
async def upload_epub(
    request: Request,
    file: UploadFile = File(...),
) -> Response:
    original_name = Path(file.filename or "").name

    if not original_name:
        raise HTTPException(status_code=400, detail="No file was selected.")

    if Path(original_name).suffix.lower() != ALLOWED_EXTENSION:
        return templates.TemplateResponse(
            "upload.html",
            {
                "request": request,
                "uploaded_file": None,
                "error": "Please upload a valid .epub file.",
            },
            status_code=400,
        )

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    saved_name = f"{uuid4().hex}{ALLOWED_EXTENSION}"
    destination = UPLOAD_DIR / saved_name

    with destination.open("wb") as output:
        while chunk := await file.read(1024 * 1024):
            output.write(chunk)

    try:
        parsed_book = parse_epub(destination)
        book_id = save_parsed_book(parsed_book, original_name, destination)
    except ValueError as exc:
        destination.unlink(missing_ok=True)
        return templates.TemplateResponse(
            "upload.html",
            {
                "request": request,
                "uploaded_file": None,
                "error": str(exc),
            },
            status_code=400,
        )

    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@app.get("/books/{book_id}", response_class=HTMLResponse)
def book_overview(request: Request, book_id: int) -> HTMLResponse:
    overview = get_book_overview(book_id)

    if overview is None:
        raise HTTPException(status_code=404, detail="Book not found.")

    return templates.TemplateResponse(
        "book.html",
        {
            "request": request,
            **overview,
            "job": get_job_settings(book_id),
        },
    )


@app.get("/books/{book_id}/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    book_id: int,
    saved: bool = False,
) -> HTMLResponse:
    overview = get_book_overview(book_id)

    if overview is None:
        raise HTTPException(status_code=404, detail="Book not found.")

    settings = get_job_settings(book_id) or {
        "source_language": "Auto-detect",
        "target_language": "",
        "model": "",
        "text_direction": "auto-detect",
        "layout": "original-first",
    }

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "book": overview["book"],
            "chapters": overview["chapters"],
            "settings": settings,
            "saved": saved,
            "error": None,
        },
    )


@app.post("/books/{book_id}/settings", response_class=HTMLResponse)
def save_settings(
    request: Request,
    book_id: int,
    source_language: str = Form(...),
    target_language: str = Form(...),
    model: str = Form(...),
    text_direction: str = Form(...),
    layout: str = Form(...),
    selected_chapter_ids: list[int] = Form(default=[]),
) -> Response:
    overview = get_book_overview(book_id)

    if overview is None:
        raise HTTPException(status_code=404, detail="Book not found.")

    values = {
        "source_language": source_language.strip(),
        "target_language": target_language.strip(),
        "model": model.strip(),
        "text_direction": text_direction,
        "layout": layout,
    }

    error = _validate_settings(values)
    if error:
        return templates.TemplateResponse(
            "settings.html",
            {
                "request": request,
                "book": overview["book"],
                "chapters": overview["chapters"],
                "settings": values,
                "saved": False,
                "error": error,
            },
            status_code=400,
        )

    if not selected_chapter_ids:
        return templates.TemplateResponse(
            "settings.html",
            {
                "request": request,
                "book": overview["book"],
                "chapters": overview["chapters"],
                "settings": values,
                "saved": False,
                "error": "Select at least one chapter to translate.",
            },
            status_code=400,
        )

    save_chapter_selection(book_id, selected_chapter_ids)
    save_job_settings(book_id=book_id, **values)
    return RedirectResponse(
        url=f"/books/{book_id}/settings?saved=true",
        status_code=303,
    )


@app.post("/api/openrouter/test-key")
async def openrouter_test_key(payload: OpenRouterKeyRequest) -> dict:
    try:
        key_info = await test_api_key(payload.api_key)
    except OpenRouterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"valid": True, "key_info": key_info}


@app.post("/api/openrouter/models")
async def openrouter_models(payload: OpenRouterKeyRequest) -> dict:
    try:
        models = await fetch_models(payload.api_key)
    except OpenRouterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"models": models}


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "history.html",
        {
            "request": request,
            "history_items": _history_items_with_downloads(),
        },
    )


@app.post("/jobs/{job_id}/preview", response_class=HTMLResponse)
async def generate_preview(
    request: Request,
    job_id: int,
    api_key: str = Form(...),
    preview_scope: str = Form("first-3000-words"),
) -> HTMLResponse:
    job = get_job(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Translation job not found.")
    if preview_scope not in PREVIEW_SCOPES:
        raise HTTPException(status_code=400, detail="Invalid preview option.")
    if not api_key.strip():
        return _settings_error_response(
            request,
            job,
            "Enter an OpenRouter API key to generate a preview.",
        )

    paragraphs = get_preview_paragraphs(job_id, preview_scope)
    if not paragraphs:
        return _settings_error_response(
            request,
            job,
            "No paragraphs are available for this preview.",
        )

    try:
        preview = await translate_preview(job, paragraphs, api_key.strip())
    except (OpenRouterError, TranslationResponseError) as exc:
        return _settings_error_response(request, job, str(exc))

    return templates.TemplateResponse(
        "preview.html",
        {
            "request": request,
            "job": job,
            "preview": preview,
            "preview_scope": preview_scope,
            "preview_word_count": sum(
                len(paragraph["original_text"].split())
                for paragraph in preview
            ),
        },
    )


@app.post("/jobs/{job_id}/start")
async def start_translation(
    job_id: int,
    background_tasks: BackgroundTasks,
    api_key: str = Form(...),
) -> RedirectResponse:
    job = get_job(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Translation job not found.")
    if not api_key.strip():
        raise HTTPException(
            status_code=400,
            detail="Enter an OpenRouter API key to start translation.",
        )

    if claim_job_for_translation(job_id):
        background_tasks.add_task(
            run_translation_job,
            job_id,
            api_key.strip(),
        )

    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def translation_progress_page(request: Request, job_id: int) -> HTMLResponse:
    progress = get_job_progress(job_id)

    if progress is None:
        raise HTTPException(status_code=404, detail="Translation job not found.")

    return templates.TemplateResponse(
        "progress.html",
        {
            "request": request,
            "progress": progress,
        },
    )


@app.get("/jobs/{job_id}/status")
def translation_status(job_id: int) -> dict:
    progress = get_job_progress(job_id)

    if progress is None:
        raise HTTPException(status_code=404, detail="Translation job not found.")

    return progress


@app.post("/jobs/{job_id}/pause")
def pause_translation(job_id: int) -> RedirectResponse:
    job = get_job(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Translation job not found.")

    request_job_pause(job_id)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/resume")
async def resume_translation(
    job_id: int,
    background_tasks: BackgroundTasks,
    api_key: str = Form(...),
) -> RedirectResponse:
    job = get_job(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Translation job not found.")
    if not api_key.strip():
        raise HTTPException(
            status_code=400,
            detail="Enter an OpenRouter API key to resume translation.",
        )

    run_mode = claim_job_for_resume(job_id)
    if run_mode == "retry":
        background_tasks.add_task(
            retry_failed_chapters,
            job_id,
            api_key.strip(),
        )
    elif run_mode == "full":
        background_tasks.add_task(
            run_translation_job,
            job_id,
            api_key.strip(),
        )

    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/retry-failed")
async def retry_failed_translation(
    job_id: int,
    background_tasks: BackgroundTasks,
    api_key: str = Form(...),
) -> RedirectResponse:
    job = get_job(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Translation job not found.")
    if not api_key.strip():
        raise HTTPException(
            status_code=400,
            detail="Enter an OpenRouter API key to retry failed chapters.",
        )

    if claim_job_for_retry(job_id):
        background_tasks.add_task(
            retry_failed_chapters,
            job_id,
            api_key.strip(),
        )

    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/export/bilingual")
def export_bilingual(job_id: int) -> RedirectResponse:
    return _create_export(job_id, "bilingual")


@app.post("/jobs/{job_id}/export/translated-only")
def export_translated_only(job_id: int) -> RedirectResponse:
    return _create_export(job_id, "translated-only")


@app.post("/history/jobs/{job_id}/download/{mode}")
def history_download(job_id: int, mode: str) -> RedirectResponse:
    if mode not in {"bilingual", "translated-only"}:
        raise HTTPException(status_code=404, detail="Download not found.")

    filename = get_latest_export_filename(job_id, mode)
    if filename and _export_file_exists(filename):
        return RedirectResponse(url=f"/downloads/{filename}", status_code=303)

    return _create_export(job_id, mode)


@app.post("/history/books/{book_id}/delete")
def history_delete_book(book_id: int) -> RedirectResponse:
    cleanup = get_book_cleanup_paths(book_id)
    if cleanup is None:
        raise HTTPException(status_code=404, detail="Book not found.")

    delete_book(book_id)
    _remove_book_files(cleanup)
    return RedirectResponse(url="/history", status_code=303)


@app.get("/downloads/{filename}")
def download_export(filename: str) -> FileResponse:
    safe_name = Path(filename).name
    if safe_name != filename or not safe_name.lower().endswith(".epub"):
        raise HTTPException(status_code=404, detail="Download not found.")

    export_root = EXPORT_DIR.resolve()
    file_path = (EXPORT_DIR / safe_name).resolve()

    if file_path.parent != export_root or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Download not found.")

    return FileResponse(
        path=file_path,
        media_type="application/epub+zip",
        filename=safe_name,
    )


def _validate_settings(values: dict[str, str]) -> str | None:
    if not values["source_language"]:
        return "Source language is required."
    if not values["target_language"]:
        return "Target language is required."
    if not values["model"]:
        return "Select an OpenRouter model."
    if values["text_direction"] not in TEXT_DIRECTIONS:
        return "Select a valid text direction."
    if values["layout"] not in LAYOUTS:
        return "Select a valid bilingual layout."
    return None


def _refresh_existing_chapter_titles() -> None:
    for book in get_books_for_title_refresh():
        uploaded_path = Path(book["uploaded_path"])
        if not uploaded_path.is_file():
            continue

        try:
            parsed_book = parse_epub(uploaded_path)
            titles = {
                chapter.href: chapter.title
                for chapter in parsed_book.chapters
            }
        except Exception:
            continue

        update_chapter_titles(book["id"], titles)


def _create_export(job_id: int, mode: str) -> RedirectResponse:
    try:
        output_path = export_epub(job_id, mode)
    except ExportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    save_export_record(job_id, mode, output_path.name)
    return RedirectResponse(
        url=f"/downloads/{output_path.name}",
        status_code=303,
    )


def _history_items_with_downloads() -> list[dict]:
    items = get_history_items()
    for item in items:
        for key in ("bilingual_filename", "translated_filename"):
            filename = item.get(key)
            if filename and not _export_file_exists(str(filename)):
                item[key] = None
    return items


def _export_file_exists(filename: str) -> bool:
    safe_name = Path(filename).name
    if safe_name != filename:
        return False

    export_root = EXPORT_DIR.resolve()
    file_path = (EXPORT_DIR / safe_name).resolve()
    return file_path.parent == export_root and file_path.is_file()


def _remove_book_files(cleanup: dict) -> None:
    upload_root = UPLOAD_DIR.resolve()
    export_root = EXPORT_DIR.resolve()

    uploaded_path = Path(cleanup["uploaded_path"]).resolve()
    if uploaded_path.parent == upload_root:
        uploaded_path.unlink(missing_ok=True)

    for filename in cleanup["export_filenames"]:
        safe_name = Path(str(filename)).name
        if safe_name != filename:
            continue

        export_path = (EXPORT_DIR / safe_name).resolve()
        if export_path.parent == export_root:
            export_path.unlink(missing_ok=True)


def _settings_error_response(
    request: Request,
    job: dict,
    error: str,
) -> HTMLResponse:
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "book": {
                "id": job["book_id"],
                "title": job["book_title"],
            },
            "chapters": get_book_overview(job["book_id"])["chapters"],
            "settings": job,
            "saved": False,
            "error": error,
        },
        status_code=400,
    )
