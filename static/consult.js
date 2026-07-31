/* Patient Guide — one-shot full consult client.
   form → working → result, SSE-driven. This is the original flow: the whole
   standing team researches one written-out case at once and you get a
   multi-section summary to print and take to an appointment. The chat at / is
   the everyday entry point; this is for "here is my whole situation". */
(() => {
  "use strict";

  const PG = window.PG;
  const $ = (sel) => document.querySelector(sel);

  // The standing full-consult roster, in the order the summary presents it.
  // Mirrors config.FULL_CONSULT_IDS.
  const ROSTER_IDS = ["researcher", "physio", "dietician", "slp", "mental", "navigator", "stories"];

  const state = {
    sid: null,
    source: null,
    targetLanguage: "English",
    agentStatus: new Map(),
    agentDetail: new Map(),
    englishMarkdown: "",
    translatedMarkdown: "",
    references: [],
    refsByLabel: new Map(),
  };

  const views = {
    form: $("#view-form"),
    working: $("#view-working"),
    result: $("#view-result"),
  };

  function showView(name) {
    Object.entries(views).forEach(([k, el]) => {
      el.hidden = k !== name;
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // ------------------------------------------------------------------- form
  const form = $("#patient-form");
  const caseInput = $("#case");
  const caseError = $("#case-error");
  const FORM_KEY = "pg-consult-form";

  try {
    const saved = JSON.parse(sessionStorage.getItem(FORM_KEY) || "null");
    if (saved) {
      caseInput.value = saved.case || "";
      $("#location").value = saved.location || "";
      $("#preferences").value = saved.preferences || "";
      $("#target_language").value = saved.target_language || "English";
    }
  } catch (e) { /* ignore */ }

  function persistForm() {
    try {
      sessionStorage.setItem(
        FORM_KEY,
        JSON.stringify({
          case: caseInput.value,
          location: $("#location").value,
          preferences: $("#preferences").value,
          target_language: $("#target_language").value,
        })
      );
    } catch (e) { /* ignore */ }
  }

  ["#case", "#location", "#preferences", "#target_language"].forEach((sel) =>
    $(sel).addEventListener("input", persistForm)
  );
  caseInput.addEventListener("input", () => {
    if (caseInput.getAttribute("aria-invalid") === "true") {
      caseInput.setAttribute("aria-invalid", "false");
      caseError.textContent = "";
    }
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const caseText = caseInput.value.trim();
    if (caseText.length < 20) {
      caseInput.setAttribute("aria-invalid", "true");
      caseError.textContent = "Please tell us a bit more — at least a sentence or two.";
      caseInput.focus();
      return;
    }
    if (!$("#ack").checked) {
      alert("Please confirm you understand this is not medical advice.");
      return;
    }

    state.targetLanguage = $("#target_language").value.trim() || "English";
    const btn = $("#submit-btn");
    btn.disabled = true;
    btn.textContent = "Starting...";

    try {
      const r = await fetch("/api/board", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          case: caseText,
          location: $("#location").value.trim(),
          preferences: $("#preferences").value.trim(),
          target_language: state.targetLanguage,
        }),
      });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        throw new Error(body.detail || `Server returned ${r.status}`);
      }
      const data = await r.json();
      state.sid = data.session_id;
      sessionStorage.removeItem(FORM_KEY);
      renderPlaceholderCards();
      $("#phase-line").textContent = "Connecting to your helpers...";
      showView("working");
      startStream();
    } catch (err) {
      alert(`Couldn't start your consult: ${err.message || err}`);
    } finally {
      btn.disabled = false;
      btn.textContent = "Get my summary";
    }
  });

  $("#reset-btn").addEventListener("click", () => {
    caseInput.value = "";
    $("#location").value = "";
    $("#preferences").value = "";
    $("#target_language").value = "English";
    $("#ack").checked = false;
    caseInput.setAttribute("aria-invalid", "false");
    caseError.textContent = "";
    sessionStorage.removeItem(FORM_KEY);
    caseInput.focus();
  });

  $("#cancel-btn").addEventListener("click", async () => {
    if (state.sid) {
      try { await fetch(`/api/board/${state.sid}`, { method: "DELETE" }); } catch (e) {}
    }
    if (state.source) { state.source.close(); state.source = null; }
    resetState();
    showView("form");
  });

  $("#restart-btn").addEventListener("click", () => {
    resetState();
    showView("form");
  });

  $("#print-btn").addEventListener("click", () => window.print());

  $("#copy-btn").addEventListener("click", async () => {
    const md = state.translatedMarkdown || state.englishMarkdown || "";
    const refs = state.references
      .map((r) => `[${r.label}] ${r.title || ""} — ${r.journal || ""} ${r.url ? "(" + r.url + ")" : ""}`)
      .join("\n");
    const text =
      "This contains information about my health situation. Please share carefully.\n\n" +
      md +
      "\n\n---\nSources:\n" +
      refs +
      "\n\nThis is not medical advice. Talk to your care team.";
    try {
      await navigator.clipboard.writeText(text);
      const btn = $("#copy-btn");
      const orig = btn.textContent;
      btn.textContent = "Copied!";
      setTimeout(() => { btn.textContent = orig; }, 1800);
    } catch (e) {
      alert("Could not copy. You can select the text and copy it manually.");
    }
  });

  function resetState() {
    state.sid = null;
    state.source = null;
    state.agentStatus.clear();
    state.agentDetail.clear();
    state.englishMarkdown = "";
    state.translatedMarkdown = "";
    state.references = [];
    state.refsByLabel.clear();
    $("#agent-stack").innerHTML = "";
    $("#phase-line").textContent = "";
    $("#result-markdown").innerHTML = "";
    $("#references-list").innerHTML = "";
  }

  // -------------------------------------------------------------------- SSE
  function startStream() {
    const es = new EventSource(`/api/board/${state.sid}/stream`);
    state.source = es;
    es.addEventListener("message", (ev) => {
      let data;
      try { data = JSON.parse(ev.data); } catch (e) { return; }
      handleEvent(data);
    });
    es.addEventListener("error", () => {
      $("#phase-line").textContent = "Reconnecting...";
    });
  }

  const PHASE_TEXT = {
    synthesizing: "Putting it all together into one summary...",
    naming_sources: "Checking every source is named clearly...",
    simplifying: "Rewriting it in plain language...",
  };

  function handleEvent(ev) {
    const p = ev.payload || {};
    switch (ev.type) {
      case "board_started":             onBoardStarted(p); break;
      case "specialist_event":          onSpecialistEvent(p); break;
      case "specialist_round_complete": onSpecialistComplete(p); break;
      case "phase":                     onPhase(p); break;
      case "synthesis_complete":        state.englishMarkdown = p.english_markdown || ""; break;
      case "final":                     onFinal(p); break;
      case "error":                     onError(p); break;
      default: break;
    }
  }

  function makeAgentCard(s) {
    const v = PG.visualsFor(s.id, s.display_name);
    const card = document.createElement("div");
    card.className = "agent-card is-waiting";
    card.setAttribute("role", "listitem");
    card.setAttribute("data-agent", s.id);
    card.innerHTML = `
      <div class="agent-avatar" style="background:${PG.escapeAttr(s.color || v.color)}">${PG.escapeHtml(v.initials)}</div>
      <div class="agent-body">
        <div class="agent-name">${PG.escapeHtml(s.display_name || v.label)}</div>
        <div class="agent-status" aria-live="polite">${PG.escapeHtml(s.id === "translator" ? "Waiting for the others to finish" : "Starting...")}</div>
        <div class="agent-sources" hidden></div>
      </div>
      <div class="agent-icon" aria-hidden="true">○</div>
    `;
    return card;
  }

  function renderPlaceholderCards() {
    const stack = $("#agent-stack");
    stack.innerHTML = "";
    ROSTER_IDS.forEach((id) => {
      const v = PG.visualsFor(id);
      state.agentStatus.set(id, "working");
      state.agentDetail.set(id, { status: "working", sourceCount: 0 });
      stack.appendChild(makeAgentCard({ id, display_name: v.label, color: v.color }));
    });
  }

  function findCard(id) {
    return document.querySelector(`.agent-card[data-agent="${id}"]`);
  }

  function setStatus(id, text) {
    const card = findCard(id);
    if (card) card.querySelector(".agent-status").textContent = text;
  }

  function setSources(id, n) {
    const card = findCard(id);
    if (!card) return;
    const el = card.querySelector(".agent-sources");
    if (n > 0) {
      el.hidden = false;
      el.textContent = `${n} source${n === 1 ? "" : "s"} found`;
    } else {
      el.hidden = true;
    }
  }

  function setCardState(id, klass, icon) {
    const card = findCard(id);
    if (!card) return;
    card.classList.remove("is-waiting", "is-done", "is-skipped", "is-error");
    if (klass) card.classList.add(klass);
    card.querySelector(".agent-icon").textContent = icon;
  }

  function onBoardStarted(p) {
    const roster = p.specialists || [];
    const present = new Set(roster.map((s) => s.id));
    const stack = $("#agent-stack");

    ROSTER_IDS.forEach((id) => {
      const card = findCard(id);
      if (card && !present.has(id)) {
        card.remove();
        state.agentStatus.delete(id);
        state.agentDetail.delete(id);
      }
    });
    roster.forEach((s) => {
      if (!findCard(s.id)) {
        state.agentStatus.set(s.id, s.id === "translator" ? "waiting" : "working");
        state.agentDetail.set(s.id, { status: "working", sourceCount: 0 });
        stack.appendChild(makeAgentCard(s));
      }
    });

    const lang = p.target_language || "";
    $("#phase-line").textContent =
      lang && lang.toLowerCase() !== "english"
        ? `Helpers researching · will translate to ${lang} at the end.`
        : "Helpers researching...";
  }

  function onSpecialistEvent(p) {
    const id = p.specialist;
    const card = findCard(id);
    if (!card) return;
    card.classList.remove("is-waiting");
    const detail = state.agentDetail.get(id) || {};
    if (p.type === "tool_result") {
      detail.sourceCount = (detail.sourceCount || 0) + 1;
      state.agentDetail.set(id, detail);
      setSources(id, detail.sourceCount);
      setStatus(id, "reading sources...");
    } else if (PG.ACTIVITY_VERBS[p.type]) {
      setStatus(id, PG.ACTIVITY_VERBS[p.type]);
    }
  }

  function onSpecialistComplete(p) {
    const id = p.specialist;
    state.agentStatus.set(id, p.status);
    const n = (p.evidence_labels || []).length;
    if (p.status === "skipped") {
      setCardState(id, "is-skipped", "–");
      setStatus(id, "Not relevant to your situation — sat this out");
      setSources(id, 0);
    } else if (p.status === "error") {
      setCardState(id, "is-error", "!");
      setStatus(id, "Something went wrong");
    } else if (p.status === "no_evidence") {
      setCardState(id, "is-error", "!");
      setStatus(id, "Couldn't find trustworthy sources for this");
    } else {
      setCardState(id, "is-done", "✓");
      setStatus(id, n ? `Done — used ${n} source${n === 1 ? "" : "s"}` : "Done");
    }
  }

  function onPhase(p) {
    if (PHASE_TEXT[p.phase]) {
      $("#phase-line").textContent = PHASE_TEXT[p.phase];
      return;
    }
    if (p.phase === "translating") {
      const lang = p.target_language || state.targetLanguage;
      if (lang && lang.toLowerCase() !== "english") {
        $("#phase-line").textContent = `Translating to ${lang}...`;
        if (findCard("translator")) {
          findCard("translator").classList.remove("is-waiting");
          setStatus("translator", `Translating to ${lang}...`);
        }
      } else {
        $("#phase-line").textContent = "Almost done...";
      }
    }
  }

  function onFinal(p) {
    state.englishMarkdown = p.english_markdown || state.englishMarkdown;
    state.translatedMarkdown = p.translated_markdown || state.englishMarkdown;
    state.references = p.references || [];

    const lang = (p.target_language || "").toLowerCase();
    if (findCard("translator")) {
      if (lang && lang !== "english") {
        setCardState("translator", "is-done", "✓");
        setStatus("translator", `Translated to ${p.target_language}`);
      } else {
        setCardState("translator", "is-skipped", "–");
        setStatus("translator", "Not needed — your summary is in English");
      }
    }

    if (state.source) { state.source.close(); state.source = null; }
    renderResult(p);
    showView("result");
  }

  function onError(p) {
    $("#phase-line").textContent = "";
    alert(`We had a problem: ${p.message || "Something went wrong."}`);
    if (state.source) { state.source.close(); state.source = null; }
    showView("form");
  }

  function renderResult(p) {
    const targetLang = p.target_language || state.targetLanguage || "English";
    const refCount = (p.references || []).length;
    const activeCount = Array.from(state.agentStatus.values()).filter((s) => s === "done").length;

    $("#result-sub").textContent =
      `${activeCount} helper${activeCount === 1 ? "" : "s"} weighed in · ${refCount} source${refCount === 1 ? "" : "s"} · in ${targetLang}`;

    const md = state.translatedMarkdown || state.englishMarkdown || "*Your summary is empty.*";
    PG.renderMarkdown($("#result-markdown"), md, { idPrefix: "ref-" });

    state.refsByLabel = new Map((p.references || []).map((ref) => [String(ref.label), ref]));

    const refsList = $("#references-list");
    refsList.innerHTML = "";
    (p.references || []).forEach((ref) => refsList.appendChild(PG.referenceListItem(ref, "ref-")));
  }

  PG.configureCitations({
    idPrefix: "ref-",
    resolveRef: (label) => state.refsByLabel.get(String(label)) || null,
    fetchLay: async (label) => {
      if (!state.sid) return "";
      try {
        const r = await fetch(`/api/board/${encodeURIComponent(state.sid)}/lay_summary/${encodeURIComponent(label)}`);
        if (!r.ok) return "";
        const data = await r.json();
        return (data && data.lay_summary) || "";
      } catch (e) {
        return "";
      }
    },
  });
})();
