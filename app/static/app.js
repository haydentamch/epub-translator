const apiKeyInput = document.querySelector("#api-key");
const testKeyButton = document.querySelector("#test-key");
const loadModelsButton = document.querySelector("#load-models");
const modelSelect = document.querySelector("#model");
const statusElement = document.querySelector("#openrouter-status");

async function openRouterRequest(path) {
  const apiKey = apiKeyInput.value.trim();

  if (!apiKey) {
    throw new Error("Enter an OpenRouter API key first.");
  }

  const response = await fetch(path, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({api_key: apiKey}),
  });
  const body = await response.json();

  if (!response.ok) {
    throw new Error(body.detail || "OpenRouter request failed.");
  }

  return body;
}

function setStatus(message, isError = false) {
  statusElement.textContent = message;
  statusElement.classList.toggle("request-error", isError);
}

if (testKeyButton) {
  testKeyButton.addEventListener("click", async () => {
    testKeyButton.disabled = true;
    setStatus("Testing API key...");

    try {
      await openRouterRequest("/api/openrouter/test-key");
      setStatus("API key is valid.");
    } catch (error) {
      setStatus(error.message, true);
    } finally {
      testKeyButton.disabled = false;
    }
  });
}

if (loadModelsButton) {
  loadModelsButton.addEventListener("click", async () => {
    loadModelsButton.disabled = true;
    setStatus("Loading available models...");

    try {
      const body = await openRouterRequest("/api/openrouter/models");
      const selectedModel = modelSelect.dataset.selectedModel;
      modelSelect.replaceChildren();

      for (const model of body.models) {
        const option = document.createElement("option");
        option.value = model.id;
        option.textContent = model.name === model.id
          ? model.id
          : `${model.name} (${model.id})`;
        option.selected = model.id === selectedModel;
        modelSelect.append(option);
      }

      if (!body.models.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "No models available";
        modelSelect.append(option);
      }

      setStatus(`Loaded ${body.models.length} models.`);
    } catch (error) {
      setStatus(error.message, true);
    } finally {
      loadModelsButton.disabled = false;
    }
  });
}

const chapterCheckboxes = document.querySelectorAll(".chapter-checkbox");
const selectAllChapters = document.querySelector("#select-all-chapters");
const clearAllChapters = document.querySelector("#clear-all-chapters");

if (selectAllChapters) {
  selectAllChapters.addEventListener("click", () => {
    for (const checkbox of chapterCheckboxes) {
      checkbox.checked = true;
    }
  });
}

if (clearAllChapters) {
  clearAllChapters.addEventListener("click", () => {
    for (const checkbox of chapterCheckboxes) {
      checkbox.checked = false;
    }
  });
}

const progressRoot = document.querySelector("#translation-progress");

if (progressRoot) {
  const jobId = progressRoot.dataset.jobId;
  const jobStatus = document.querySelector("#job-status");
  const progressBar = document.querySelector(".progress-track");
  const progressFill = document.querySelector("#progress-fill");
  const progressCopy = document.querySelector("#progress-copy");
  const chapterSummary = document.querySelector("#chapter-summary");
  const retryForm = document.querySelector("#retry-failed-form");
  const exportPanel = document.querySelector("#export-panel");
  const elapsedTime = document.querySelector("#elapsed-time");
  const pauseResumePanel = document.querySelector("#pause-resume-panel");
  const pauseForm = document.querySelector("#pause-form");
  const pausingMessage = document.querySelector("#pausing-message");
  const resumeForm = document.querySelector("#resume-form");
  let elapsedSeconds = Number(elapsedTime.dataset.elapsedSeconds || 0);
  let currentJobStatus = jobStatus.textContent.trim();

  function formatElapsedTime(totalSeconds) {
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;

    return [hours, minutes, seconds]
      .map((value) => String(value).padStart(2, "0"))
      .join(":");
  }

  function renderElapsedTime() {
    elapsedTime.textContent = formatElapsedTime(elapsedSeconds);
  }

  renderElapsedTime();
  window.setInterval(() => {
    if (["Translating", "Pausing"].includes(currentJobStatus)) {
      elapsedSeconds += 1;
      renderElapsedTime();
    }
  }, 1000);

  async function refreshProgress() {
    try {
      const response = await fetch(`/jobs/${jobId}/status`, {
        headers: {"Accept": "application/json"},
        cache: "no-store",
      });

      if (!response.ok) {
        throw new Error("Could not load translation status.");
      }

      const progress = await response.json();
      currentJobStatus = progress.status;
      elapsedSeconds = progress.elapsed_seconds;
      renderElapsedTime();
      jobStatus.textContent = progress.status;
      progressBar.setAttribute("aria-valuenow", progress.progress_percent);
      progressFill.style.width = `${progress.progress_percent}%`;
      progressCopy.textContent =
        `${progress.completed_paragraphs} of ${progress.total_paragraphs} ` +
        `paragraphs completed (${progress.progress_percent}%)`;
      chapterSummary.textContent =
        `${progress.completed_chapters} completed, ` +
        `${progress.partial_chapters} partial, ` +
        `${progress.failed_chapters} failed, ` +
        `${progress.skipped_chapters} skipped chapters`;
      retryForm.hidden = !["CompletedWithErrors", "Failed"].includes(
        progress.status,
      );
      exportPanel.hidden = !["Completed", "CompletedWithErrors"].includes(
        progress.status,
      );
      pauseResumePanel.hidden = !["Translating", "Pausing", "Paused"].includes(
        progress.status,
      );
      pauseForm.hidden = progress.status !== "Translating";
      pausingMessage.hidden = progress.status !== "Pausing";
      resumeForm.hidden = progress.status !== "Paused";

      for (const chapter of progress.chapters) {
        const row = document.querySelector(
          `[data-chapter-id="${chapter.id}"]`,
        );

        if (!row) {
          continue;
        }

        const chapterProgress = row.querySelector(".chapter-progress");
        const chapterStatus = row.querySelector(".status");
        chapterProgress.textContent =
          `${chapter.completed_paragraphs || 0} of ` +
          `${chapter.paragraph_count} paragraphs`;
        chapterStatus.textContent = chapter.status;
        chapterStatus.className =
          `status status-${chapter.status.toLowerCase()}`;
      }

      if (!["Paused", "Completed", "CompletedWithErrors", "Failed"].includes(
        progress.status,
      )) {
        window.setTimeout(refreshProgress, 1000);
      }
    } catch (error) {
      jobStatus.textContent = error.message;
      jobStatus.classList.add("request-error");
      window.setTimeout(refreshProgress, 3000);
    }
  }

  refreshProgress();
}
