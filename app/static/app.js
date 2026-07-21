// Common two-letter language codes, used only to *suggest* a value for the
// language filter from whatever URL is pasted in — not exhaustive (the
// backend's ISO 639-1 set is authoritative for actually filtering), just
// needs to avoid suggesting something wrong.
const COMMON_LANGUAGE_CODES = new Set([
  "en", "es", "fr", "de", "it", "pt", "nl", "ru", "ja", "zh", "ko", "ar", "hi", "tr", "pl",
  "sv", "da", "no", "fi", "cs", "el", "he", "th", "vi", "id", "ms", "ro", "hu", "uk", "bg",
  "sk", "sl", "hr", "sr", "lt", "lv", "et", "ca", "eu", "gl", "is", "ga", "cy", "sq", "mk",
  "az", "ka", "hy", "kk", "uz", "mn", "fa", "ur", "bn", "ta", "te", "ml", "kn", "mr", "gu",
  "pa", "si", "km", "lo", "my", "ne", "am",
]);

function suggestLanguageFromUrl(url) {
  try {
    const segments = new URL(url).pathname.split("/").filter(Boolean);
    if (!segments.length) return "";
    const code = segments[0].toLowerCase().split("-")[0].split("_")[0];
    return code.length === 2 && COMMON_LANGUAGE_CODES.has(code) ? code : "";
  } catch (e) {
    return "";
  }
}

function initHomeForm() {
  const form = document.getElementById("crawl-form");
  const errorEl = document.getElementById("form-error");
  const submitBtn = document.getElementById("submit-btn");
  const urlInput = document.getElementById("url");
  const domainScopeSelect = document.getElementById("domain_scope");
  const languageInput = document.getElementById("language");

  urlInput.addEventListener("blur", () => {
    if (!languageInput.value) {
      const suggestion = suggestLanguageFromUrl(urlInput.value);
      if (suggestion) languageInput.value = suggestion;
    }
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorEl.style.display = "none";
    submitBtn.disabled = true;
    submitBtn.textContent = "Starting…";

    const url = urlInput.value;
    const domainScope = domainScopeSelect.value;
    const language = languageInput.value.trim() || null;

    try {
      const res = await fetch("/crawl", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, domain_scope: domainScope, language }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || "Something went wrong");
      }
      window.location.href = "/crawl/" + data.run_id;
    } catch (err) {
      errorEl.textContent = err.message;
      errorEl.style.display = "block";
      submitBtn.disabled = false;
      submitBtn.textContent = "Crawl";
    }
  });
}

function badgeClass(status) {
  if (status === "completed") return "status-badge status-completed";
  if (status === "failed") return "status-badge status-failed";
  if (status === "cancelled") return "status-badge status-cancelled";
  return "status-badge status-crawling";
}

function statusLabel(status) {
  return status.charAt(0).toUpperCase() + status.slice(1);
}

const TERMINAL_STATUSES = ["completed", "failed", "cancelled"];

function formatDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const datePart = d.toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" });
  const timePart = d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  return `${datePart} · ${timePart}`;
}

function renderPageRow(tbody, page) {
  const tr = document.createElement("tr");
  if (page.blocked_by_host) {
    tr.className = "page-blocked-host";
    tr.title = page.error || "Blocked by the host's bot detection";
  } else if (!page.success) {
    tr.className = "page-failed";
  }
  const urlTd = document.createElement("td");
  urlTd.className = "url-cell";
  urlTd.title = page.url;
  urlTd.textContent = page.url;
  const titleTd = document.createElement("td");
  titleTd.textContent = page.title || "";
  const wordsTd = document.createElement("td");
  wordsTd.className = "words-cell";
  wordsTd.textContent = page.blocked_by_host ? "blocked" : page.success ? page.word_count.toLocaleString("en-US") : "failed";
  const openTd = document.createElement("td");
  openTd.className = "open-cell";
  const openLink = document.createElement("a");
  openLink.href = page.url;
  openLink.target = "_blank";
  openLink.rel = "noopener noreferrer";
  openLink.className = "open-link";
  openLink.textContent = "Open ↗";
  openTd.appendChild(openLink);
  tr.append(urlTd, titleTd, wordsTd, openTd);
  tbody.prepend(tr);
}

function folderForUrl(url) {
  try {
    const segments = new URL(url).pathname.split("/").filter(Boolean);
    return segments.length ? "/" + segments[0] : "(root)";
  } catch (e) {
    return "(root)";
  }
}

function renderFolderGroups(pages) {
  const groups = {};
  for (const p of pages) {
    const folder = folderForUrl(p.url);
    if (!groups[folder]) groups[folder] = { words: 0, count: 0 };
    groups[folder].words += p.word_count || 0;
    groups[folder].count += 1;
  }
  const rows = Object.entries(groups).sort((a, b) => b[1].words - a[1].words);
  const maxWords = rows.length ? rows[0][1].words : 1;
  const tbody = document.getElementById("folder-tbody");
  tbody.innerHTML = "";
  for (const [folder, stats] of rows) {
    const tr = document.createElement("tr");

    const folderTd = document.createElement("td");
    const bar = document.createElement("div");
    bar.className = "folder-bar";
    bar.style.width = Math.max(1, Math.round((stats.words / maxWords) * 100)) + "%";
    const folderSpan = document.createElement("span");
    folderSpan.textContent = folder;
    folderTd.append(bar, folderSpan);

    const countTd = document.createElement("td");
    countTd.className = "folder-pages";
    countTd.innerHTML = `<span>${stats.count.toLocaleString("en-US")}</span>`;

    const wordsTd = document.createElement("td");
    wordsTd.className = "folder-words";
    wordsTd.innerHTML = `<span>${stats.words.toLocaleString("en-US")}</span>`;

    tr.append(folderTd, countTd, wordsTd);
    tbody.appendChild(tr);
  }
}

function renderTopPages(pages) {
  const list = document.getElementById("top-pages-list");
  list.innerHTML = "";
  const top = pages
    .filter((p) => p.success)
    .sort((a, b) => b.word_count - a.word_count)
    .slice(0, 5);
  top.forEach((page, i) => {
    const li = document.createElement("li");
    const rank = document.createElement("span");
    rank.className = "rank";
    rank.textContent = i + 1;
    const path = document.createElement("span");
    path.className = "path";
    path.title = page.url;
    path.textContent = page.url;
    const words = document.createElement("span");
    words.className = "words";
    words.textContent = page.word_count.toLocaleString("en-US");
    li.append(rank, path, words);
    list.appendChild(li);
  });
}

function renderSummary(pages) {
  renderTopPages(pages);
  renderFolderGroups(pages);
  document.getElementById("summary").style.display = "";
}

function setStatCount(el, count) {
  el.textContent = count;
  el.classList.toggle("zero", count === 0);
}

function initCrawlPage(opts) {
  const statusBadge = document.getElementById("status-badge");
  const statusDot = document.getElementById("status-dot");
  const statusText = document.getElementById("status-text");
  const totalWordsEl = document.getElementById("total-words");
  const pageCountEl = document.getElementById("page-count");
  const pagesHeadingCountEl = document.getElementById("pages-heading-count");
  const loginBlockedEl = document.getElementById("login-blocked-count");
  const limitNote = document.getElementById("limit-note");
  const cancelNote = document.getElementById("cancel-note");
  const errorBox = document.getElementById("error-box");
  const tbody = document.getElementById("page-tbody");
  const cancelBtn = document.getElementById("cancel-btn");
  const recrawlForm = document.getElementById("recrawl-form");
  const recrawlBtn = document.getElementById("recrawl-btn");
  const recrawlDomainScopeSelect = document.getElementById("recrawl-domain-scope");
  const recrawlLanguageInput = document.getElementById("recrawl-language");
  const printBtn = document.getElementById("print-btn");
  const runDateEl = document.getElementById("run-date");
  const detectedLanguageNote = document.getElementById("detected-language-note");
  const blockedHostEl = document.getElementById("blocked-host-count");

  const updateBlockedHostCount = (pages) => {
    setStatCount(blockedHostEl, pages.filter((p) => p.blocked_by_host).length);
  };

  const showDetectedLanguage = (code) => {
    if (!code) return;
    detectedLanguageNote.textContent = "Auto-detected language: " + code;
    detectedLanguageNote.style.display = "block";
  };

  const updatePageCount = (count) => {
    pageCountEl.textContent = count;
    pagesHeadingCountEl.textContent = count ? `— ${count.toLocaleString("en-US")} crawled` : "";
  };

  const suggestedLanguage = suggestLanguageFromUrl(opts.sourceUrl);
  if (suggestedLanguage) recrawlLanguageInput.value = suggestedLanguage;

  printBtn.addEventListener("click", () => window.print());

  const setStatus = (status) => {
    statusBadge.className = badgeClass(status);
    statusText.textContent = statusLabel(status);
    statusDot.innerHTML = status === "crawling" ? '<span class="pulse-dot"></span>' : "";
  };

  const showLimitNote = () => {
    limitNote.textContent = "The configured page limit was reached — the crawl may be incomplete.";
    limitNote.style.display = "block";
  };

  const showError = (message) => {
    errorBox.textContent = message || "The crawl failed.";
    errorBox.style.display = "block";
  };

  cancelBtn.addEventListener("click", async () => {
    cancelBtn.disabled = true;
    cancelBtn.textContent = "Cancelling…";
    await fetch("/crawl/" + opts.runId + "/cancel", { method: "POST" });
    // The actual status change arrives via the SSE "status" event below.
  });

  recrawlForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    recrawlBtn.disabled = true;
    recrawlBtn.textContent = "Starting…";
    const res = await fetch("/crawl", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: opts.sourceUrl,
        domain_scope: recrawlDomainScopeSelect.value,
        language: recrawlLanguageInput.value.trim() || null,
        force_recrawl: true,
      }),
    });
    const data = await res.json();
    window.location.href = "/crawl/" + data.run_id;
  });

  if (opts.mode === "past") {
    runDateEl.textContent = "Run: " + formatDate(opts.createdAt);
    setStatus(opts.initialStatus);
    totalWordsEl.textContent = opts.initialTotalWords.toLocaleString("en-US");
    updatePageCount(opts.initialPageCount);
    setStatCount(loginBlockedEl, opts.initialLoginBlockedCount || 0);
    if (opts.initialLimitReached) showLimitNote();
    if (opts.initialStatus === "failed") showError();
    if (opts.initialStatus === "cancelled") cancelNote.style.display = "block";
    const initialPages = opts.initialPages || [];
    for (const page of initialPages) {
      renderPageRow(tbody, page);
    }
    updateBlockedHostCount(initialPages);
    renderSummary(initialPages);
    return;
  }

  // live mode
  runDateEl.textContent = "Started: " + formatDate(opts.startedAt);
  setStatus("starting");
  setStatCount(blockedHostEl, 0);
  cancelBtn.style.display = "";
  const pages = [];
  const source = new EventSource("/events/" + opts.runId);

  source.addEventListener("page", (evt) => {
    const data = JSON.parse(evt.data);
    pages.push(data.page);
    renderPageRow(tbody, data.page);
    totalWordsEl.textContent = data.total_words.toLocaleString("en-US");
    updatePageCount(pages.length);
    updateBlockedHostCount(pages);
  });

  source.addEventListener("login_blocked", (evt) => {
    const data = JSON.parse(evt.data);
    // Intentionally not rendered in the page list — just a running count of
    // pages that turned out to be login walls rather than real content.
    setStatCount(loginBlockedEl, data.login_blocked_count);
  });

  source.addEventListener("status", (evt) => {
    const data = JSON.parse(evt.data);
    setStatus(data.status);
    totalWordsEl.textContent = data.total_words.toLocaleString("en-US");
    updatePageCount(data.page_count);
    setStatCount(loginBlockedEl, data.login_blocked_count);
    showDetectedLanguage(data.detected_language);
    if (data.limit_reached) showLimitNote();
    if (data.status === "failed") showError(data.error);
    if (data.status === "cancelled") cancelNote.style.display = "block";
    if (TERMINAL_STATUSES.includes(data.status)) {
      cancelBtn.style.display = "none";
      renderSummary(pages);
      source.close();
    }
  });

  source.onerror = () => {
    // EventSource auto-retries; if the job is already gone server-side this
    // will just keep failing quietly, which is fine for a local dev tool.
  };
}
