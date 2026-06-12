/* ───────────────────────────────────────────────────────────────────────────
 * overrides.js — shared dismiss / bookmark layer for all dashboards.
 *
 * Used by index.html, l2h2.html and asking.html. Provides:
 *   • per-card ✕ dismiss + ★ bookmark buttons
 *   • dismissed cards hidden by default (toggle to reveal + undo)
 *   • "bookmarked only" filter
 *   • a Sync button that commits choices to output/user_overrides.json via the
 *     GitHub Contents API, using a fine-grained PAT kept only in localStorage.
 *
 * State model: localStorage is authoritative for THIS browser. Sync is
 * last-write-wins on the whole file (push local state, using the remote sha).
 * Un-dismiss therefore works; the only caveat is editing from two devices
 * between syncs. For a single-user tool that's the right trade-off.
 *
 * The scraper (run.py / registry.py / asking_feed.py) reads the committed
 * user_overrides.json and feeds dismissed URLs into permanent_rejects, so a
 * dismissed lot stops being re-scraped and drops out of the feeds entirely.
 * ────────────────────────────────────────────────────────────────────────── */
(function () {
  "use strict";

  const REPO   = "bananacocodrilo/troostwijk-scraper";
  const BRANCH = "main";
  const PATH   = "output/user_overrides.json";
  const RAW_URL = `https://raw.githubusercontent.com/${REPO}/${BRANCH}/${PATH}`;
  const API_URL = `https://api.github.com/repos/${REPO}/contents/${PATH}`;

  const LS_STATE = "tw_overrides_state";   // {version, dismissed, bookmarked}
  const LS_BASE  = "tw_overrides_base";    // last-synced snapshot (for pending diff)
  const LS_PREFS = "tw_overrides_prefs";   // {showDismissed, bookmarkedOnly}
  const LS_PAT   = "tw_gh_pat";

  let state = { version: 1, dismissed: {}, bookmarked: {} };
  let base  = { dismissed: {}, bookmarked: {} };
  let prefs = { showDismissed: false, bookmarkedOnly: false };
  let onChange = function () {};

  // ── persistence helpers ──────────────────────────────────────────────────
  function loadLS(key, fallback) {
    try { const v = JSON.parse(localStorage.getItem(key)); return v || fallback; }
    catch (e) { return fallback; }
  }
  function saveState() { localStorage.setItem(LS_STATE, JSON.stringify(state)); }
  function saveBase()  { localStorage.setItem(LS_BASE, JSON.stringify(base)); }
  function savePrefs() { localStorage.setItem(LS_PREFS, JSON.stringify(prefs)); }

  function normalize(o) {
    return {
      version: 1,
      dismissed: (o && o.dismissed) || {},
      bookmarked: (o && o.bookmarked) || {},
    };
  }

  // ── query ────────────────────────────────────────────────────────────────
  function isDismissed(url)  { return !!(url && state.dismissed[url]); }
  function isBookmarked(url) { return !!(url && state.bookmarked[url]); }

  function pendingCount() {
    let n = 0;
    const keys = new Set([
      ...Object.keys(state.dismissed), ...Object.keys(base.dismissed),
      ...Object.keys(state.bookmarked), ...Object.keys(base.bookmarked),
    ]);
    for (const k of keys) {
      if (!!state.dismissed[k] !== !!base.dismissed[k]) n++;
      if (!!state.bookmarked[k] !== !!base.bookmarked[k]) n++;
    }
    return n;
  }

  // ── mutation ─────────────────────────────────────────────────────────────
  function toggleDismiss(url) {
    if (!url) return;
    if (state.dismissed[url]) {
      delete state.dismissed[url];
    } else {
      state.dismissed[url] = { at: new Date().toISOString() };
      delete state.bookmarked[url]; // dismissing clears a bookmark
    }
    saveState(); refreshToolbar(); onChange();
  }
  function toggleBookmark(url) {
    if (!url) return;
    if (state.bookmarked[url]) {
      delete state.bookmarked[url];
    } else {
      state.bookmarked[url] = { at: new Date().toISOString() };
      delete state.dismissed[url]; // bookmarking un-dismisses
    }
    saveState(); refreshToolbar(); onChange();
  }

  // ── filtering (called from each page's render()) ──────────────────────────
  function applyFilter(list) {
    return list.filter(function (v) {
      const url = v && v.url;
      if (prefs.bookmarkedOnly && !isBookmarked(url)) return false;
      if (!prefs.showDismissed && isDismissed(url)) return false;
      return true;
    });
  }

  // ── per-card buttons ─────────────────────────────────────────────────────
  function cardButtonsHtml(url) {
    if (!url) return "";
    const d = isDismissed(url), b = isBookmarked(url);
    const u = String(url).replace(/"/g, "&quot;");
    return (
      '<div class="ov-actions" data-ov-url="' + u + '">' +
      '<button class="ov-btn ov-bookmark' + (b ? " is-on" : "") + '" data-ov-act="bookmark" ' +
        'title="' + (b ? "Remove bookmark" : "Bookmark this lot") + '">' + (b ? "★" : "☆") + "</button>" +
      '<button class="ov-btn ov-dismiss' + (d ? " is-on" : "") + '" data-ov-act="dismiss" ' +
        'title="' + (d ? "Undo dismiss" : "Dismiss / hide this lot") + '">' + (d ? "↩" : "✕") + "</button>" +
      "</div>"
    );
  }

  // single delegated handler — survives re-renders, no per-card rebinding
  function onDocClick(e) {
    const btn = e.target.closest("[data-ov-act]");
    if (!btn) return;
    const holder = btn.closest("[data-ov-url]");
    if (!holder) return;
    e.preventDefault();
    e.stopPropagation();
    const url = holder.getAttribute("data-ov-url");
    if (btn.getAttribute("data-ov-act") === "dismiss") toggleDismiss(url);
    else toggleBookmark(url);
  }

  // ── GitHub sync ──────────────────────────────────────────────────────────
  function b64encode(str) {
    return btoa(unescape(encodeURIComponent(str)));
  }
  function getPAT(forcePrompt) {
    let pat = localStorage.getItem(LS_PAT);
    if (pat && !forcePrompt) return pat;
    const msg =
      "Paste a GitHub fine-grained personal access token with " +
      "Contents: Read and write on " + REPO + " only.\n\n" +
      "It is stored only in this browser (localStorage). Leave blank to clear.";
    const v = window.prompt(msg, "");
    if (v === null) return pat || null;       // cancelled
    if (v.trim() === "") { localStorage.removeItem(LS_PAT); return null; }
    pat = v.trim();
    localStorage.setItem(LS_PAT, pat);
    return pat;
  }

  async function sync() {
    const pat = getPAT(false);
    if (!pat) { setStatus("no token", true); return; }
    setStatus("syncing…");
    try {
      // 1. current sha (if the file exists)
      let sha = null;
      const getRes = await fetch(API_URL + "?ref=" + BRANCH, {
        headers: { Authorization: "Bearer " + pat, Accept: "application/vnd.github+json" },
        cache: "no-store",
      });
      if (getRes.status === 200) {
        sha = (await getRes.json()).sha;
      } else if (getRes.status === 401 || getRes.status === 403) {
        setStatus("auth failed", true); return;
      } else if (getRes.status !== 404) {
        setStatus("error " + getRes.status, true); return;
      }
      // 2. push local state (last-write-wins)
      const payload = normalize(state);
      const body = {
        message: "chore: update user overrides (" + Object.keys(payload.dismissed).length +
                 " dismissed, " + Object.keys(payload.bookmarked).length + " bookmarked)",
        content: b64encode(JSON.stringify(payload, null, 2) + "\n"),
        branch: BRANCH,
      };
      if (sha) body.sha = sha;
      const putRes = await fetch(API_URL, {
        method: "PUT",
        headers: { Authorization: "Bearer " + pat, Accept: "application/vnd.github+json" },
        body: JSON.stringify(body),
      });
      if (!putRes.ok) {
        setStatus("push failed " + putRes.status, true);
        return;
      }
      base = { dismissed: { ...payload.dismissed }, bookmarked: { ...payload.bookmarked } };
      saveBase();
      setStatus("synced ✓");
      refreshToolbar();
    } catch (err) {
      setStatus("network error", true);
    }
  }

  // ── toolbar ──────────────────────────────────────────────────────────────
  let elPending = null, elStatus = null;
  function setStatus(text, isErr) {
    if (!elStatus) return;
    elStatus.textContent = text || "";
    elStatus.style.color = isErr ? "var(--red)" : "var(--muted)";
    if (text && !isErr && text.indexOf("…") === -1) {
      setTimeout(function () { if (elStatus) elStatus.textContent = ""; }, 2500);
    }
  }
  function refreshToolbar() {
    if (elPending) {
      const n = pendingCount();
      elPending.textContent = n ? "⇅ Sync (" + n + ")" : "⇅ Synced";
      elPending.disabled = n === 0;
    }
  }
  function mountToolbar() {
    const nav = document.querySelector(".nav-bar");
    if (!nav || nav.querySelector(".ov-toolbar")) return;
    const wrap = document.createElement("span");
    wrap.className = "ov-toolbar";
    wrap.innerHTML =
      '<label class="ov-toggle"><input type="checkbox" id="ovShowDismissed"> show dismissed</label>' +
      '<label class="ov-toggle"><input type="checkbox" id="ovBookmarkedOnly"> ★ only</label>' +
      '<button class="ov-sync" id="ovSync">⇅ Synced</button>' +
      '<button class="ov-key" id="ovKey" title="Set / replace GitHub token">🔑</button>' +
      '<span class="ov-status" id="ovStatus"></span>';
    nav.appendChild(wrap);

    const cbShow = wrap.querySelector("#ovShowDismissed");
    const cbBook = wrap.querySelector("#ovBookmarkedOnly");
    cbShow.checked = prefs.showDismissed;
    cbBook.checked = prefs.bookmarkedOnly;
    cbShow.addEventListener("change", function () {
      prefs.showDismissed = cbShow.checked; savePrefs(); onChange();
    });
    cbBook.addEventListener("change", function () {
      prefs.bookmarkedOnly = cbBook.checked; savePrefs(); onChange();
    });
    elPending = wrap.querySelector("#ovSync");
    elStatus  = wrap.querySelector("#ovStatus");
    elPending.addEventListener("click", sync);
    wrap.querySelector("#ovKey").addEventListener("click", function () { getPAT(true); });
    refreshToolbar();
  }

  // ── injected styles ──────────────────────────────────────────────────────
  function injectCSS() {
    const css =
      ".card { position: relative; }" +
      ".ov-actions { position: absolute; top: 8px; right: 8px; display: flex; gap: 6px; z-index: 5; }" +
      ".ov-btn { width: 30px; height: 30px; border-radius: 8px; cursor: pointer;" +
        " border: 1px solid var(--border); background: rgba(15,17,23,.78); color: var(--text);" +
        " font-size: 15px; line-height: 1; display: flex; align-items: center; justify-content: center;" +
        " backdrop-filter: blur(2px); transition: transform .12s, background .12s, color .12s; }" +
      ".ov-btn:hover { transform: scale(1.1); }" +
      ".ov-bookmark.is-on { color: var(--yellow); border-color: var(--yellow); background: rgba(45,38,0,.9); }" +
      ".ov-dismiss:hover { color: var(--red); border-color: var(--red); }" +
      ".ov-dismiss.is-on { color: var(--green); border-color: var(--green); }" +
      /* mute a dismissed card when 'show dismissed' reveals it */
      ".card:has(.ov-dismiss.is-on) { opacity: .5; filter: grayscale(.4); }" +
      ".card:has(.ov-dismiss.is-on):hover { opacity: 1; filter: none; }" +
      /* toolbar */
      ".ov-toolbar { margin-left: auto; display: flex; align-items: center; gap: 10px; font-size: 12px; }" +
      ".ov-toggle { color: var(--muted); display: flex; align-items: center; gap: 4px; cursor: pointer; }" +
      ".ov-toggle input { cursor: pointer; }" +
      ".ov-sync, .ov-key { font-size: 12px; font-weight: 600; padding: 4px 10px; border-radius: 6px;" +
        " border: 1px solid var(--border); background: var(--surface); color: var(--accent); cursor: pointer; }" +
      ".ov-sync:disabled { color: var(--muted); cursor: default; }" +
      ".ov-sync:not(:disabled):hover, .ov-key:hover { border-color: var(--accent); }" +
      ".ov-status { color: var(--muted); min-width: 70px; }";
    const tag = document.createElement("style");
    tag.textContent = css;
    document.head.appendChild(tag);
  }

  // ── init ─────────────────────────────────────────────────────────────────
  async function init(opts) {
    opts = opts || {};
    onChange = opts.onChange || function () {};
    prefs = Object.assign(prefs, loadLS(LS_PREFS, {}));

    const localState = loadLS(LS_STATE, null);
    injectCSS();
    mountToolbar();

    // Fetch the committed overrides (authoritative for a fresh browser).
    let remote = null;
    try {
      const r = await fetch(RAW_URL + "?t=" + Date.now(), { cache: "no-store" });
      if (r.ok) remote = normalize(await r.json());
    } catch (e) { /* offline / 404 → treat as empty */ }

    if (localState) {
      // Existing local edits win; baseline for "pending" is the remote file.
      state = normalize(localState);
      base = remote
        ? { dismissed: { ...remote.dismissed }, bookmarked: { ...remote.bookmarked } }
        : loadLS(LS_BASE, { dismissed: {}, bookmarked: {} });
    } else if (remote) {
      // Fresh browser → seed from the committed file; nothing pending.
      state = remote;
      base = { dismissed: { ...remote.dismissed }, bookmarked: { ...remote.bookmarked } };
    }
    saveState(); saveBase();

    document.addEventListener("click", onDocClick, true);
    refreshToolbar();
    onChange();
  }

  window.Overrides = {
    init, applyFilter, cardButtonsHtml, isDismissed, isBookmarked,
    toggleDismiss, toggleBookmark, sync,
  };
})();
