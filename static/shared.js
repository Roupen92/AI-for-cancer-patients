/* Shared front-end pieces for both views (chat and one-shot consult):
   agent visuals, escaping, markdown + citation rendering, and the citation
   hover tooltip. Loaded before app.js / consult.js.

   Kept in one file so the two views can't drift on agent names, colors, or the
   way a [N] citation behaves. */
(() => {
  "use strict";

  // --- Agent visuals (must match backend SPECIALIST_CONFIGS) ----------------
  const AGENT_VISUALS = {
    researcher: { initials: "R", label: "Medical Research",     color: "#3F6C8F", verb: "reading the research and guidelines" },
    genomics:   { initials: "G", label: "Genomics & Biomarkers", color: "#4F5D96", verb: "looking up your gene and marker results" },
    physio:     { initials: "P", label: "Physiotherapist",      color: "#4A7C6F", verb: "looking at movement and rehab" },
    exercise:   { initials: "A", label: "Exercise & Activity",  color: "#5C9E52", verb: "looking at safe activity" },
    dietician:  { initials: "D", label: "Dietitian",            color: "#8E9F4A", verb: "looking at food and nutrition" },
    slp:        { initials: "S", label: "Speech & Swallowing",  color: "#5A8FA8", verb: "checking speech and swallowing" },
    mental:     { initials: "W", label: "Emotional Wellbeing",  color: "#7A6BAA", verb: "thinking about emotional support" },
    trials:     { initials: "C", label: "Clinical Trials",      color: "#2C7A6B", verb: "searching the trial registry" },
    navigator:  { initials: "N", label: "Patient Navigator",    color: "#C97B3F", verb: "looking up practical help near you" },
    stories:    { initials: "V", label: "Stories from Others",  color: "#B05E6E", verb: "finding stories from people like you" },
    translator: { initials: "L", label: "Translator",           color: "#6B5F52", verb: "waiting to translate" },
  };

  const ACTIVITY_VERBS = {
    started:             "starting...",
    thinking:            "thinking...",
    tool_call:           "looking things up...",
    tool_result:         "reading sources...",
    self_checking:       "double-checking the answer...",
    drafting:            "writing it up...",
    retrieve_or_abstain: "looking for more evidence...",
    tool_loop_capped:    "wrapping up...",
  };

  // --- Escaping -------------------------------------------------------------
  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
  const escapeAttr = escapeHtml;

  // Reference URLs come from third-party search results (Perplexity, Brave,
  // PubMed, ClinicalTrials.gov) and are rendered as clickable links. The
  // markdown body is sanitized by DOMPurify, but the reference list and the
  // citation tooltip build their anchors directly — and escaping stops an
  // attribute breakout without stopping a `javascript:` or `data:` scheme from
  // being clickable. Allow http(s) only; anything else renders as inert text.
  function safeUrl(raw) {
    const url = String(raw == null ? "" : raw).trim();
    if (!url) return "";
    try {
      // Parsed with no base, so a relative path or a non-URL string throws
      // rather than silently becoming a same-origin link to nowhere. Every real
      // source URL is absolute.
      const parsed = new URL(url);
      return parsed.protocol === "http:" || parsed.protocol === "https:" ? url : "";
    } catch (e) {
      return "";
    }
  }

  // --- Markdown + citations -------------------------------------------------
  function expandCitationGroup(group) {
    const nums = (group.match(/\d+/g) || []).map(Number);
    if (nums.length === 2 && /[-–]/.test(group)) {
      const [lo, hi] = nums;
      if (hi >= lo && hi - lo < 50) {
        return Array.from({ length: hi - lo + 1 }, (_, i) => lo + i);
      }
    }
    return nums;
  }

  function transformCitations(html, idPrefix) {
    // Replace [N] / [N, M] / [N-M] with citation chips linking to the source list.
    // The prefix is stamped onto the chip as well as into its href: the chat gives
    // each room its own prefix (`ref-physio-3`), so a [3] rendered in one room must
    // resolve to that room's reference card and not to whatever `ref-3` happens to
    // exist first in the document.
    const prefix = escapeAttr(idPrefix);
    return html.replace(/\[(\d{1,3}(?:\s*[-–,;]\s*\d{1,3})*)\]/g, (match, group) =>
      expandCitationGroup(group)
        .map(
          (n) =>
            `<a class="cite" href="#${prefix}${n}" data-cite-label="${n}" data-cite-prefix="${prefix}" tabindex="0">[${n}]</a>`
        )
        .join(" ")
    );
  }

  function renderMarkdown(target, md, opts) {
    const idPrefix = (opts && opts.idPrefix) || "ref-";
    if (!window.marked || !window.DOMPurify) {
      target.textContent = md;
      return;
    }
    marked.setOptions({ breaks: true, gfm: true });
    let html = marked.parse(md || "");
    html = transformCitations(html, idPrefix);
    target.innerHTML = DOMPurify.sanitize(html, { ADD_ATTR: ["target", "rel", "dir"] });
    // Answers are translated into whatever language the patient asked for, which
    // includes right-to-left scripts (Arabic, Hebrew, Persian, Urdu). `dir="auto"`
    // lets the browser pick the direction per block from its first strong
    // character, so an Arabic answer reads right-to-left while the English
    // institution names and [N] labels inside it still sit correctly.
    target.setAttribute("dir", "auto");
    // TOP-LEVEL BLOCKS ONLY. The `dir=auto` algorithm resolves direction from
    // the first strong character in an element's text but SKIPS any descendant
    // that carries its own dir attribute. So stamping nested elements blinds
    // their parent: dir on the <li>s makes their <ul> find no strong character
    // and fall back to ltr, and dir on a blockquote's inner <p> does the same to
    // the blockquote — leaving bullets and quote bars on the wrong side of an
    // Arabic answer. Setting only the direct children lets each block resolve
    // from its own text, and everything inside inherits the result.
    Array.from(target.children).forEach((el) => el.setAttribute("dir", "auto"));
    // Make every outbound link safe to click from a shared device.
    target.querySelectorAll("a[href^='http']").forEach((a) => {
      a.setAttribute("target", "_blank");
      a.setAttribute("rel", "noopener noreferrer");
    });
  }

  function kindBadgeFor(kind) {
    if (kind === "patient_story") return `<span class="ref-badge ref-badge-story">Story</span>`;
    if (kind === "resource_directory") return `<span class="ref-badge ref-badge-resource">Resource</span>`;
    if (kind === "patient_source") return `<span class="ref-badge ref-badge-source">Patient info</span>`;
    if (kind === "clinical_trial") return `<span class="ref-badge ref-badge-trial">Trial</span>`;
    if (kind === "pubmed") return `<span class="ref-badge ref-badge-study">Study</span>`;
    return "";
  }

  function referenceListItem(ref, idPrefix) {
    const li = document.createElement("li");
    li.id = `${idPrefix}${escapeAttr(String(ref.label || ""))}`;
    const kind = (ref.source_kind || "other").replace(/[^a-z_]/gi, "");
    li.classList.add(`ref-kind-${kind}`);
    const meta = [];
    if (ref.journal) meta.push(escapeHtml(ref.journal));
    if (ref.year) meta.push(escapeHtml(String(ref.year)));
    if (ref.article_type) meta.push(escapeHtml(ref.article_type));
    const href = safeUrl(ref.url);
    const urlHtml = href
      ? `<a href="${escapeAttr(href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(href)}</a>`
      : escapeHtml(ref.url || "");
    const lay = (ref.lay_summary || "").trim();
    const layHtml = lay
      ? `<div class="ref-lay"><span class="ref-lay-label">In plain English:</span> ${escapeHtml(lay)}</div>`
      : "";
    li.innerHTML = `
      <div>
        ${kindBadgeFor(kind)}
        <span class="ref-label">[${escapeHtml(ref.label || "")}]</span>
        <span class="ref-title">${escapeHtml(ref.title || "(no title)")}</span>
      </div>
      <div class="ref-meta">${meta.join(" · ")}${meta.length && urlHtml ? " · " : ""}${urlHtml}</div>
      ${layHtml}
    `;
    return li;
  }

  // --- Citation hover/focus tooltip ----------------------------------------
  // A single tooltip element reused across all hovers. The page registers a
  // resolver so the tooltip works for both the chat (conversation-scoped refs)
  // and the consult (session-scoped refs).
  let tooltipEl = null;
  let resolveRef = () => null;           // (label) -> ref object | null
  let fetchLay = async () => "";         // (label) -> plain-English string
  let idPrefix = "ref-";

  // `resolveRef(label, anchorEl)` and `fetchLay(label, ref)` — the second argument
  // is what lets a page with more than one reference namespace (the chat, where
  // every room has its own ledger) work out WHICH `[3]` was clicked. A page with a
  // single namespace ignores it, so the consult's one-argument callbacks are
  // unaffected.
  function configureCitations(opts) {
    if (opts.resolveRef) resolveRef = opts.resolveRef;
    if (opts.fetchLay) fetchLay = opts.fetchLay;
    if (opts.idPrefix) idPrefix = opts.idPrefix;
  }

  // Moving the pointer from the citation to the tooltip crosses an 8px gap where
  // neither is hovered. Hiding on that first mouseout made the tooltip
  // unreachable — you could never click "Open source", and a lay summary that
  // was still loading vanished before it arrived. A short grace period, plus
  // keeping it open while the pointer is on the tooltip itself, fixes both.
  let hideTimer = null;
  let pinned = false;

  function cancelHide() {
    if (hideTimer) {
      clearTimeout(hideTimer);
      hideTimer = null;
    }
  }

  function scheduleHide(delay = 260) {
    cancelHide();
    hideTimer = setTimeout(() => {
      if (!pinned) hideTooltip();
    }, delay);
  }

  function ensureTooltip() {
    if (tooltipEl) return tooltipEl;
    const el = document.createElement("div");
    el.className = "cite-tooltip";
    el.setAttribute("role", "tooltip");
    el.hidden = true;
    // The tooltip is interactive: it holds a link to the source and text worth
    // selecting, so hovering it must keep it alive.
    el.addEventListener("mouseenter", cancelHide);
    el.addEventListener("mouseleave", () => scheduleHide());
    document.body.appendChild(el);
    tooltipEl = el;
    return el;
  }

  function positionTooltip(anchor, tt) {
    tt.style.left = "0px";
    tt.style.top = "-9999px";
    const aRect = anchor.getBoundingClientRect();
    const ttRect = tt.getBoundingClientRect();
    const margin = 8;
    let top = aRect.bottom + window.scrollY + margin;
    let left = aRect.left + window.scrollX;
    if (left + ttRect.width > window.scrollX + window.innerWidth - 12) {
      left = window.scrollX + window.innerWidth - ttRect.width - 12;
    }
    if (left < window.scrollX + 12) left = window.scrollX + 12;
    if (
      aRect.bottom + ttRect.height + margin > window.innerHeight &&
      aRect.top - ttRect.height - margin > 0
    ) {
      top = aRect.top + window.scrollY - ttRect.height - margin;
    }
    tt.style.top = `${top}px`;
    tt.style.left = `${left}px`;
  }

  // In-flight lay-summary fetches, keyed by scope + label. The chat now runs one
  // conversation PER ROOM, so `[3]` in the dietitian's room and `[3]` in the
  // trials room are different sources with different lay summaries — keying on
  // the label alone would hand one room's summary to the other. A ref may carry
  // `__scope` (set by the page when it files the ref away) to disambiguate;
  // pages with a single ref namespace, like the consult, simply never set it.
  const _layInflight = new Map();
  function layKey(label, ref) {
    return `${(ref && ref.__scope) || ""}|${label}`;
  }
  function fetchLayOnce(label, ref) {
    const key = layKey(label, ref);
    if (_layInflight.has(key)) return _layInflight.get(key);
    const p = Promise.resolve(fetchLay(label, ref))
      .catch(() => "")
      .finally(() => _layInflight.delete(key));
    _layInflight.set(key, p);
    return p;
  }

  function showTooltip(anchor, ref) {
    const tt = ensureTooltip();
    const meta = [ref.journal, ref.year, ref.article_type]
      .filter(Boolean)
      .map((v) => escapeHtml(String(v)))
      .join(" · ");
    const ttHref = safeUrl(ref.url);
    const urlBit = ttHref
      ? `<a href="${escapeAttr(ttHref)}" target="_blank" rel="noopener noreferrer">Open source ↗</a>`
      : "";
    const lay = (ref.lay_summary || "").trim();
    const layBlock = lay
      ? `<div class="cite-tooltip-lay">${escapeHtml(lay)}</div>`
      : `<div class="cite-tooltip-lay cite-tooltip-lay-loading">Writing a plain-English summary…</div>`;
    tt.innerHTML = `
      <div class="cite-tooltip-head">
        ${kindBadgeFor(ref.source_kind || "")}
        <span class="cite-tooltip-label">[${escapeHtml(String(ref.label || ""))}]</span>
        <span class="cite-tooltip-title">${escapeHtml(ref.title || "(no title)")}</span>
      </div>
      <div class="cite-tooltip-meta">${meta}${meta && urlBit ? " · " : ""}${urlBit}</div>
      <div class="cite-tooltip-laylabel">In plain English:</div>
      ${layBlock}
    `;
    tt.hidden = false;
    positionTooltip(anchor, tt);

    if (!lay && ref.label != null) {
      fetchLayOnce(String(ref.label), ref).then((text) => {
        if (!text) return;
        ref.lay_summary = text;
        if (!tt.hidden) {
          const layEl = tt.querySelector(".cite-tooltip-lay");
          if (layEl) {
            layEl.classList.remove("cite-tooltip-lay-loading");
            layEl.textContent = text;
          }
        }
        const cardPrefix = (anchor && anchor.dataset && anchor.dataset.citePrefix) || idPrefix;
        const card = document.getElementById(`${cardPrefix}${ref.label}`);
        if (card && !card.querySelector(".ref-lay")) {
          const div = document.createElement("div");
          div.className = "ref-lay";
          div.innerHTML = `<span class="ref-lay-label">In plain English:</span> ${escapeHtml(text)}`;
          card.appendChild(div);
        }
      });
    }
  }

  function hideTooltip() {
    cancelHide();
    pinned = false;
    if (tooltipEl) {
      tooltipEl.hidden = true;
      tooltipEl.classList.remove("is-pinned");
    }
  }

  document.addEventListener("mouseover", (e) => {
    const cite = e.target.closest && e.target.closest(".cite");
    if (!cite) return;
    cancelHide();
    const ref = resolveRef(cite.dataset.citeLabel, cite);
    if (ref) showTooltip(cite, ref);
  });
  document.addEventListener("mouseout", (e) => {
    const cite = e.target.closest && e.target.closest(".cite");
    if (!cite) return;
    // Don't hide on the way to the tooltip — scheduleHide gives the pointer time
    // to cross the gap, and the tooltip's own mouseenter cancels it.
    scheduleHide();
  });

  // Clicking a citation pins it open, which is the only way this works on touch
  // (no hover at all) and the reliable way to reach the source link on desktop.
  // The anchor keeps its href so it still jumps to the reference list without JS.
  document.addEventListener("click", (e) => {
    const cite = e.target.closest && e.target.closest(".cite");
    if (cite) {
      const ref = resolveRef(cite.dataset.citeLabel, cite);
      if (ref) {
        e.preventDefault();
        cancelHide();
        pinned = true;
        showTooltip(cite, ref);
        if (tooltipEl) tooltipEl.classList.add("is-pinned");
      }
      return;
    }
    // A click anywhere else dismisses it — but not a click inside the tooltip,
    // which would make the source link unusable.
    if (tooltipEl && !tooltipEl.hidden && !tooltipEl.contains(e.target)) {
      hideTooltip();
    }
  });

  document.addEventListener("focusin", (e) => {
    if (!e.target.classList || !e.target.classList.contains("cite")) return;
    cancelHide();
    const ref = resolveRef(e.target.dataset.citeLabel, e.target);
    if (ref) showTooltip(e.target, ref);
  });
  document.addEventListener("focusout", (e) => {
    if (!e.target.classList || !e.target.classList.contains("cite")) return;
    scheduleHide();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hideTooltip();
  });

  window.PG = {
    AGENT_VISUALS,
    ACTIVITY_VERBS,
    escapeHtml,
    escapeAttr,
    safeUrl,
    renderMarkdown,
    transformCitations,
    kindBadgeFor,
    referenceListItem,
    configureCitations,
    hideTooltip,
    visualsFor(id, fallbackName) {
      return (
        AGENT_VISUALS[id] || {
          initials: (id || "?")[0].toUpperCase(),
          label: fallbackName || id,
          color: "#4A7C6F",
          verb: "working...",
        }
      );
    },
  };
})();
