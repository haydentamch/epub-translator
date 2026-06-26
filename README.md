# EPUB Translator

A local web app for translating EPUB books with OpenRouter models. It lets you upload an EPUB, choose translation settings, preview the first part of the book, translate selected chapters, pause and resume translation, export bilingual or translated-only EPUB files, and manage completed books from a History page.

<img width="1575" height="847" alt="main_page" src="https://github.com/user-attachments/assets/621690a4-91a7-48e6-82e8-9ebf03edc4a4" />

## Features

- Upload and parse EPUB files locally
- Configure source language, target language, OpenRouter model, text direction, and bilingual layout
- Select which chapters should be translated
- Generate a preview before starting the full translation
- Start, pause, resume, and retry translation jobs
- Track translation progress and elapsed translation time
- Export bilingual EPUB and translated-only EPUB
- History page for completed translations with download and delete buttons
- Local SQLite database storage

## Requirements

- Docker Desktop
- An OpenRouter API key

You do not need to install Python or create a `.venv` if you run the app with Docker.

## Quick Start

Clone the repository:

```powershell
git clone https://github.com/haydentamch/epub-translator.git
cd epub-translator
```

Start the app:

```powershell
docker compose up --build
```

Open the app in your browser:

```text
http://localhost:8000
```

To stop the app, press `Ctrl+C` in the terminal. If it is running in the background, use:

```powershell
docker compose down
```

## How To Use

1. Open `http://localhost:8000`.
2. Upload an `.epub` file.
3. Go to the settings page.
4. Enter your OpenRouter API key.
5. Load models and choose a model.
6. Set the source language and target language.
7. Select the chapters you want to translate.
8. Save settings.
9. Generate a preview.
10. Start the full translation.
11. When translation is complete, download the bilingual or translated-only EPUB.

<img width="1506" height="902" alt="setting" src="https://github.com/user-attachments/assets/241f1595-b9a3-4b35-9764-1dad3718c301" />
<img width="1543" height="373" alt="gen_preview" src="https://github.com/user-attachments/assets/dee3c172-0111-464d-8c0d-ce5eb745c980" />
<img width="1620" height="807" alt="preview_para" src="https://github.com/user-attachments/assets/7feb5fdb-8ba1-4793-850a-70fb43924d07" />
<img width="1603" height="843" alt="translating" src="https://github.com/user-attachments/assets/9f7835b2-898d-4389-a14b-e0ec7aa10ac8" />
<img width="1625" height="842" alt="translation completed" src="https://github.com/user-attachments/assets/d9a1d8e6-90a9-4c2a-8d2c-34f92acd0605" />
<img width="1623" height="466" alt="history" src="https://github.com/user-attachments/assets/781a2543-def9-4723-916d-2298bc2debd8" />

## Data Storage

The app stores local data in the `data` folder:

```text
data/epub_translator.sqlite3
data/uploads/
data/exports/
```

These files are created automatically when the app runs.

The `data` folder may contain private books, translations, and translation history. These files are ignored by Git and should not be uploaded to GitHub.

## OpenRouter API Key

Your OpenRouter API key is used only for requests made while using the app. It is not saved in the database.

Each user should use their own OpenRouter API key.

## Exported EPUB Files

After translation finishes, you can export:

- Bilingual EPUB: original text and translated text
- Translated-only EPUB: translated text only

The History page also lets you download previous completed translations.

## Development

Run tests locally if you have Python dependencies installed:

```powershell
.venv\Scripts\python.exe -m pytest -q
```

Or run inside your own Python environment:

```powershell
python -m pytest -q
```

## Privacy Notes

Do not commit or upload these files:

```text
data/epub_translator.sqlite3
data/uploads/*.epub
data/exports/*.epub
.venv/
```

The repository should include only the app source code, tests, Docker files, and empty `.gitkeep` files for the data folders.

## Troubleshooting

If port `8000` is already in use, stop the other app or change the port in `docker-compose.yml`.

If Docker does not start, make sure Docker Desktop is running.

If a translation is slow, try a faster OpenRouter model or select fewer chapters.

If an export does not appear in Apple Books as a separate book, export both versions from the app again. The app writes separate EPUB metadata for bilingual and translated-only files.
