/* Patient Guide — chat client.
   One conversation, many turns. Each turn: POST /api/chat, then stream its SSE
   until turn_complete. Progress (which helpers were picked, what they're doing)
   renders inline in the transcript so the wait is legible instead of a spinner. */
(() => {
  "use strict";

  const PG = window.PG;
  const $ = (sel) => document.querySelector(sel);

  const PROFILE_KEY = "pg-profile-v1";
  const CONV_KEY = "pg-conversation-v1";

  const state = {
    conversationId: null,
    turnId: null,
    source: null,
    busy: false,
    refsByLabel: new Map(),   // label -> ref (conversation-wide)
    currentTurnEl: null,      // the .msg-assistant being built
    agentCards: new Map(),    // agent id -> card element (current turn)
    elapsedTimer: null,       // 1s ticker on the current turn
    targetLanguage: "English",
  };

  // ------------------------------------------------------------------ profile
  function loadProfile() {
    try {
      return JSON.parse(localStorage.getItem(PROFILE_KEY) || "{}") || {};
    } catch (e) {
      return {};
    }
  }

  function readProfileForm() {
    return {
      condition: $("#p-condition").value.trim(),
      location: $("#p-location").value.trim(),
      language: $("#p-language").value.trim() || "English",
      preferences: $("#p-preferences").value.trim(),
      age: $("#p-age").value.trim(),
    };
  }

  function writeProfileForm(p) {
    $("#p-condition").value = p.condition || "";
    $("#p-location").value = p.location || "";
    $("#p-language").value = p.language || "English";
    $("#p-preferences").value = p.preferences || "";
    $("#p-age").value = p.age || "";
  }

  function saveProfile(p) {
    try {
      localStorage.setItem(PROFILE_KEY, JSON.stringify(p));
    } catch (e) { /* private browsing — the profile just won't persist */ }
    renderProfileSummary(p);
  }

  function renderProfileSummary(p) {
    const bits = [];
    if (p.condition) bits.push(p.condition);
    if (p.location) bits.push(p.location);
    if (p.language && p.language.toLowerCase() !== "english") bits.push("in " + p.language);
    $("#profile-summary").textContent = bits.length
      ? bits.join(" · ")
      : "Optional — helps us give answers you can actually use";
  }

  const profile = loadProfile();
  writeProfileForm(profile);
  renderProfileSummary(profile);

  $("#profile-toggle").addEventListener("click", () => {
    const body = $("#profile-body");
    const open = body.hidden;
    body.hidden = !open;
    $("#profile-toggle").setAttribute("aria-expanded", String(open));
    $("#profile-toggle").classList.toggle("is-open", open);
  });

  $("#profile-save").addEventListener("click", () => {
    saveProfile(readProfileForm());
    $("#profile-body").hidden = true;
    $("#profile-toggle").setAttribute("aria-expanded", "false");
    $("#profile-toggle").classList.remove("is-open");
    $("#message").focus();
  });

  $("#profile-clear").addEventListener("click", () => {
    writeProfileForm({ language: "English" });
    saveProfile(readProfileForm());
    try { localStorage.removeItem(CONV_KEY); } catch (e) {}
    state.conversationId = null;
  });

  // --------------------------------------------------------------- team strip
  (async function loadTeam() {
    try {
      const r = await fetch("/api/team");
      if (!r.ok) return;
      const data = await r.json();
      const strip = $("#team-strip");
      (data.specialists || []).forEach((s) => {
        const v = PG.visualsFor(s.id, s.display_name);
        const chip = document.createElement("span");
        chip.className = "team-chip";
        chip.innerHTML = `
          <span class="team-dot" style="background:${PG.escapeAttr(s.color || v.color)}">${PG.escapeHtml(v.initials)}</span>
          <span>${PG.escapeHtml(s.display_name)}</span>`;
        chip.title = v.verb;
        strip.appendChild(chip);
      });
    } catch (e) { /* the strip is decorative */ }
  })();

  // --------------------------------------------------------------- transcript
  const transcript = $("#transcript");

  function dismissIntro() {
    const intro = $("#intro");
    if (intro) intro.remove();
  }

  function scrollToBottom(smooth) {
    window.scrollTo({
      top: document.body.scrollHeight,
      behavior: smooth ? "smooth" : "auto",
    });
  }

  function addUserMessage(text) {
    const el = document.createElement("article");
    el.className = "msg msg-user";
    el.innerHTML = `<div class="msg-bubble"></div>`;
    el.querySelector(".msg-bubble").textContent = text;
    transcript.appendChild(el);
    return el;
  }

  function addAssistantShell() {
    const el = document.createElement("article");
    el.className = "msg msg-assistant is-working";
    el.innerHTML = `
      <div class="msg-meta">
        <span class="msg-status" aria-live="polite">Working out who should answer this…</span>
        <span class="msg-elapsed" hidden>0:00</span>
      </div>
      <div class="msg-agents" hidden></div>
      <div class="msg-progress" hidden>
        <ol class="work-steps"></ol>
        <div class="work-bar" aria-hidden="true"></div>
        <p class="work-note">We read the real sources before we answer, so this usually takes one to three minutes. Nothing is stuck — you can leave this tab open and come back.</p>
      </div>
      <div class="msg-body"></div>
      <div class="msg-sources" hidden>
        <button type="button" class="sources-toggle" aria-expanded="false">Sources used <span class="sources-count"></span></button>
        <ul class="references-list" hidden></ul>
      </div>
      <div class="msg-actions" hidden>
        <button type="button" class="btn btn-ghost btn-small act-copy">Copy</button>
        <button type="button" class="btn btn-ghost btn-small act-print">Print / PDF</button>
      </div>
    `;
    transcript.appendChild(el);
    return el;
  }

  function setTurnStatus(text) {
    if (!state.currentTurnEl) return;
    const el = state.currentTurnEl.querySelector(".msg-status");
    if (el) el.textContent = text;
  }

  // ------------------------------------------------------- proof of life
  // The helpers finish researching well before the answer exists: synthesis,
  // the institution-naming pass and the plain-language rewrite are another
  // 30-100 seconds of work with no chips left to animate. A status line that
  // changes three times in two minutes is indistinguishable from a hung page,
  // so the wait gets a clock that ticks every second and a checklist that shows
  // what is left to do.

  function startElapsed() {
    stopElapsed();
    const el = state.currentTurnEl && state.currentTurnEl.querySelector(".msg-elapsed");
    if (!el) return;
    const startedAt = Date.now();
    const tick = () => {
      const s = Math.round((Date.now() - startedAt) / 1000);
      el.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    };
    el.hidden = false;
    tick();
    state.elapsedTimer = setInterval(tick, 1000);
  }

  function stopElapsed() {
    if (state.elapsedTimer) {
      clearInterval(state.elapsedTimer);
      state.elapsedTimer = null;
    }
  }

  // Keyed by the server's `phase` names so an arriving phase event can mark its
  // own step. `researching` has no phase event of its own — the agent chips are
  // that step — so it is marked done by whichever write-up phase lands first.
  const STEP_LABELS = {
    researching: "Reading the sources",
    synthesizing: "Putting it together into one answer",
    naming_sources: "Checking every source is named clearly",
    simplifying: "Rewriting it in plain language",
    translating: "Translating",
  };

  function planSteps(mode) {
    if (mode === "clarify") return [];
    const steps = ["researching"];
    if (mode === "team") steps.push("synthesizing", "naming_sources");
    steps.push("simplifying");
    if ((state.targetLanguage || "English").toLowerCase() !== "english") steps.push("translating");
    return steps;
  }

  function renderSteps(keys) {
    if (!state.currentTurnEl || !keys.length) return;
    const wrap = state.currentTurnEl.querySelector(".msg-progress");
    const list = wrap.querySelector(".work-steps");
    list.innerHTML = "";
    keys.forEach((key) => {
      const li = document.createElement("li");
      li.className = "work-step";
      li.dataset.step = key;
      li.innerHTML = `<span class="work-step-icon" aria-hidden="true">○</span><span></span>`;
      li.lastElementChild.textContent = STEP_LABELS[key];
      list.appendChild(li);
    });
    wrap.hidden = false;
    markStep(keys[0]);
  }

  function markStep(key) {
    if (!state.currentTurnEl) return;
    const steps = [...state.currentTurnEl.querySelectorAll(".work-step")];
    const at = steps.findIndex((li) => li.dataset.step === key);
    if (at < 0) return;
    steps.forEach((li, i) => {
      li.classList.toggle("is-done", i < at);
      li.classList.toggle("is-active", i === at);
      const icon = li.querySelector(".work-step-icon");
      if (icon) icon.textContent = i < at ? "✓" : i === at ? "◍" : "○";
    });
  }

  // ------------------------------------------------------------- agent cards
  function renderAgentCards(specialists) {
    const wrap = state.currentTurnEl.querySelector(".msg-agents");
    wrap.hidden = false;
    wrap.innerHTML = "";
    state.agentCards.clear();
    specialists.forEach((s) => {
      const v = PG.visualsFor(s.id, s.display_name);
      const card = document.createElement("div");
      card.className = "agent-chip is-waiting";
      card.dataset.agent = s.id;
      card.innerHTML = `
        <span class="agent-dot" style="background:${PG.escapeAttr(s.color || v.color)}">${PG.escapeHtml(v.initials)}</span>
        <span class="agent-chip-body">
          <span class="agent-chip-name">${PG.escapeHtml(s.display_name || v.label)}</span>
          <span class="agent-chip-status">${PG.escapeHtml(v.verb)}</span>
        </span>
        <span class="agent-chip-icon" aria-hidden="true">○</span>
      `;
      if (s.focus) card.title = s.focus;
      wrap.appendChild(card);
      state.agentCards.set(s.id, card);
    });
  }

  function setAgentStatus(id, text) {
    const card = state.agentCards.get(id);
    if (!card) return;
    const el = card.querySelector(".agent-chip-status");
    if (el) el.textContent = text;
  }

  function setAgentState(id, klass, icon) {
    const card = state.agentCards.get(id);
    if (!card) return;
    card.classList.remove("is-waiting", "is-done", "is-skipped", "is-error");
    if (klass) card.classList.add(klass);
    const ic = card.querySelector(".agent-chip-icon");
    if (ic) ic.textContent = icon;
  }

  // ------------------------------------------------------------------ sending
  async function send(text) {
    if (state.busy) return;
    const message = (text || "").trim();
    if (message.length < 2) return;

    dismissIntro();
    state.busy = true;
    $("#send-btn").disabled = true;
    $("#stop-btn").hidden = false;

    const currentProfile = readProfileForm();
    saveProfile(currentProfile);

    addUserMessage(message);
    state.currentTurnEl = addAssistantShell();
    state.targetLanguage = currentProfile.language || "English";
    startElapsed();
    scrollToBottom(true);

    let payload;
    try {
      const r = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message,
          conversation_id: state.conversationId,
          profile: currentProfile,
        }),
      });
      if (r.status === 404 && state.conversationId) {
        // The conversation expired server-side. Retry once as a fresh one so the
        // patient doesn't lose the question they just typed.
        state.conversationId = null;
        try { sessionStorage.removeItem(CONV_KEY); } catch (e) {}
        const retry = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message, conversation_id: null, profile: currentProfile }),
        });
        if (!retry.ok) throw new Error((await retry.json().catch(() => ({}))).detail || `Server returned ${retry.status}`);
        payload = await retry.json();
      } else if (r.status === 429) {
        // Rate limited. The server's message is already written for a patient,
        // so show it as a calm reply rather than an error the user must decode.
        const body = await r.json().catch(() => ({}));
        softStop(body.detail || "Please try again in a few minutes.");
        return;
      } else if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        throw new Error(body.detail || `Server returned ${r.status}`);
      } else {
        payload = await r.json();
      }
    } catch (err) {
      failTurn(err.message || String(err));
      return;
    }

    state.conversationId = payload.conversation_id;
    state.turnId = payload.turn_id;
    try { sessionStorage.setItem(CONV_KEY, state.conversationId); } catch (e) {}

    startStream();
  }

  function softStop(msg) {
    // A limit or a pause is not a crash — no red error styling, no alarm. The
    // reader is often unwell; being told to wait should look like being told to
    // wait, not like something broke.
    if (state.currentTurnEl) {
      state.currentTurnEl.classList.remove("is-working");
      setTurnStatus("");
      const body = state.currentTurnEl.querySelector(".msg-body");
      body.innerHTML = "";
      const p = document.createElement("p");
      p.textContent = msg;
      body.appendChild(p);
    }
    finishTurn();
    scrollToBottom(true);
  }

  function failTurn(msg) {
    if (state.currentTurnEl) {
      state.currentTurnEl.classList.remove("is-working");
      state.currentTurnEl.classList.add("is-error");
      setTurnStatus("Something went wrong");
      const body = state.currentTurnEl.querySelector(".msg-body");
      body.innerHTML = "";
      const p = document.createElement("p");
      p.textContent =
        "Sorry — that didn't work: " + msg + ". Your question is still in the box below if you'd like to try again.";
      body.appendChild(p);
    }
    finishTurn();
  }

  function finishTurn() {
    stopElapsed();
    // Every exit from a turn comes through here — completed, failed, rate-limited
    // or stopped — so the progress chrome is retired in one place.
    if (state.currentTurnEl) {
      const progress = state.currentTurnEl.querySelector(".msg-progress");
      if (progress) progress.hidden = true;
      const elapsed = state.currentTurnEl.querySelector(".msg-elapsed");
      if (elapsed) elapsed.hidden = true;
    }
    state.busy = false;
    $("#send-btn").disabled = false;
    $("#stop-btn").hidden = true;
    if (state.source) {
      state.source.close();
      state.source = null;
    }
    state.currentTurnEl = null;
    state.agentCards.clear();
  }

  function startStream() {
    const url = `/api/chat/${encodeURIComponent(state.conversationId)}/turns/${encodeURIComponent(state.turnId)}/stream`;
    const es = new EventSource(url);
    state.source = es;
    es.addEventListener("message", (ev) => {
      let data;
      try {
        data = JSON.parse(ev.data);
      } catch (e) {
        return;
      }
      handleEvent(data);
    });
    es.addEventListener("error", () => {
      // EventSource reconnects on its own, and the turn's event log is
      // replayable from index 0, so a blip recovers without losing anything.
      if (state.busy) setTurnStatus("Reconnecting…");
    });
  }

  $("#stop-btn").addEventListener("click", async () => {
    if (state.conversationId && state.turnId) {
      try {
        await fetch(`/api/chat/${state.conversationId}/turns/${state.turnId}`, { method: "DELETE" });
      } catch (e) {}
    }
    setTurnStatus("Stopped");
    if (state.currentTurnEl) state.currentTurnEl.classList.remove("is-working");
    finishTurn();
  });

  // ------------------------------------------------------------------- events
  const PHASE_TEXT = {
    triaging: "Working out who should answer this…",
    synthesizing: "Putting it together into one answer…",
    naming_sources: "Checking every source is named clearly…",
    simplifying: "Rewriting it in plain language…",
    translating: "Translating…",
    lay_summarizing: "Summarizing the sources…",
  };

  function handleEvent(ev) {
    const p = ev.payload || {};
    switch (ev.type) {
      case "turn_started":
        // The server normalizes the language, so prefer its answer over the raw
        // profile field when deciding whether a translation step is coming.
        if (p.target_language) state.targetLanguage = p.target_language;
        break;

      case "red_flag":
        showRedFlagPreview(p);
        break;

      case "routed":
        onRouted(p);
        break;

      case "phase":
        if (PHASE_TEXT[p.phase]) setTurnStatus(PHASE_TEXT[p.phase]);
        if (p.phase === "translating" && p.target_language) {
          setTurnStatus(`Translating to ${p.target_language}…`);
        }
        markStep(p.phase);
        break;

      case "fallback":
        setTurnStatus("That helper had nothing solid — asking our researcher instead…");
        setAgentState(p.from, "is-skipped", "–");
        setAgentStatus(p.from, "sat this one out");
        break;

      case "specialist_event":
        onSpecialistEvent(p);
        break;

      case "specialist_round_complete":
        onSpecialistComplete(p);
        break;

      case "turn_complete":
        onTurnComplete(p);
        break;

      case "error":
        failTurn(p.message || "unknown error");
        break;

      default:
        break;
    }
  }

  function showRedFlagPreview(p) {
    if (!state.currentTurnEl) return;
    if (state.currentTurnEl.querySelector(".redflag-preview")) return;
    const div = document.createElement("div");
    div.className = "redflag-preview";
    div.setAttribute("role", "alert");
    div.innerHTML =
      p.kind === "crisis"
        ? `<strong>⚠️ Please reach out for help right now.</strong> If you're thinking about hurting yourself:
           call or text <strong>988</strong> in the US, <strong>116 123</strong> (Samaritans) in the UK,
           <strong>13 11 14</strong> (Lifeline) in Australia, or find a local line at
           <a href="https://findahelpline.com" target="_blank" rel="noopener noreferrer">findahelpline.com</a>.`
        : `<strong>⚠️ What you described may need urgent care.</strong> Please contact emergency services
           or your care team's urgent line now — don't wait for this answer.`;
    state.currentTurnEl.querySelector(".msg-meta").after(div);
    scrollToBottom(true);
  }

  function onRouted(p) {
    const specialists = p.specialists || [];
    if (p.mode === "clarify") {
      setTurnStatus("One quick question first…");
      return;
    }
    renderSteps(planSteps(p.mode));
    if (specialists.length) {
      renderAgentCards(specialists);
      const names = specialists.map((s) => s.display_name).join(", ");
      setTurnStatus(
        specialists.length === 1
          ? `Asking our ${names}…`
          : `Bringing in ${specialists.length} helpers: ${names}…`
      );
    }
    if (p.degraded) {
      setTurnStatus("Answering with our medical researcher…");
    }
    scrollToBottom(true);
  }

  function onSpecialistEvent(p) {
    const id = p.specialist;
    const card = state.agentCards.get(id);
    if (!card) return;
    card.classList.remove("is-waiting");
    if (p.type === "tool_result") {
      const n = (Number(card.dataset.sources || 0) || 0) + 1;
      card.dataset.sources = String(n);
      setAgentStatus(id, `read ${n} source${n === 1 ? "" : "s"}`);
    } else if (PG.ACTIVITY_VERBS[p.type]) {
      setAgentStatus(id, PG.ACTIVITY_VERBS[p.type]);
    }
  }

  function onSpecialistComplete(p) {
    const id = p.specialist;
    const n = (p.evidence_labels || []).length;
    if (p.status === "done") {
      setAgentState(id, "is-done", "✓");
      setAgentStatus(id, n ? `done · ${n} source${n === 1 ? "" : "s"}` : "done");
    } else if (p.status === "skipped") {
      setAgentState(id, "is-skipped", "–");
      setAgentStatus(id, "not relevant here");
    } else if (p.status === "no_evidence") {
      setAgentState(id, "is-error", "!");
      setAgentStatus(id, "couldn't find solid sources");
    } else {
      setAgentState(id, "is-error", "!");
      setAgentStatus(id, "hit a problem");
    }
  }

  function onTurnComplete(p) {
    const el = state.currentTurnEl;
    if (!el) {
      finishTurn();
      return;
    }

    // Merge this turn's references into the conversation-wide map so citation
    // hovers keep working in older messages too.
    (p.all_references || p.references || []).forEach((ref) => {
      state.refsByLabel.set(String(ref.label), ref);
    });

    el.classList.remove("is-working");
    const md = p.markdown || p.english_markdown || "*No answer was produced.*";
    PG.renderMarkdown(el.querySelector(".msg-body"), md, { idPrefix: "ref-" });

    // Status line: what actually happened, in the patient's terms.
    const used = (p.route && p.route.specialists) || [];
    const secs = (p.timing && p.timing.total_s) || 0;
    if (p.mode === "clarify") {
      setTurnStatus("Just need one detail");
    } else if (used.length) {
      const names = used.map((s) => s.display_name).join(" · ");
      setTurnStatus(`${names} · ${(p.references || []).length} source${(p.references || []).length === 1 ? "" : "s"} · ${secs}s`);
    } else {
      setTurnStatus(`${secs}s`);
    }

    // Per-message source list.
    const refs = p.references || [];
    if (refs.length) {
      const wrap = el.querySelector(".msg-sources");
      const list = wrap.querySelector(".references-list");
      wrap.hidden = false;
      wrap.querySelector(".sources-count").textContent = `(${refs.length})`;
      list.innerHTML = "";
      refs.forEach((ref) => list.appendChild(PG.referenceListItem(ref, "ref-")));
      const toggle = wrap.querySelector(".sources-toggle");
      toggle.addEventListener("click", () => {
        const open = list.hidden;
        list.hidden = !open;
        toggle.setAttribute("aria-expanded", String(open));
      });
    }

    if (p.mode !== "clarify") {
      const actions = el.querySelector(".msg-actions");
      actions.hidden = false;
      actions.querySelector(".act-copy").addEventListener("click", async () => {
        const refText = refs
          .map((r) => `[${r.label}] ${r.title || ""} — ${r.journal || ""} ${r.url ? "(" + r.url + ")" : ""}`)
          .join("\n");
        const text =
          md +
          (refText ? "\n\n---\nSources:\n" + refText : "") +
          "\n\nThis is general information from public sources. It is not medical advice — please talk to your care team.";
        try {
          await navigator.clipboard.writeText(text);
          const b = actions.querySelector(".act-copy");
          const orig = b.textContent;
          b.textContent = "Copied!";
          setTimeout(() => { b.textContent = orig; }, 1600);
        } catch (e) {
          alert("Could not copy. You can select the text and copy it manually.");
        }
      });
      actions.querySelector(".act-print").addEventListener("click", () => {
        // Print the answer AND the question that produced it. The whole point of
        // this button is handing the page to a clinician, and an answer with no
        // question on it is a page they cannot place.
        const question = el.previousElementSibling;
        const asked = question && question.classList.contains("msg-user") ? question : null;
        el.classList.add("print-target");
        if (asked) asked.classList.add("print-target");
        document.body.classList.add("printing-one");
        window.print();
        setTimeout(() => {
          el.classList.remove("print-target");
          if (asked) asked.classList.remove("print-target");
          document.body.classList.remove("printing-one");
        }, 500);
      });
    }

    addFollowUpRow(el, p.mode);
    finishTurn();
    scrollToBottom(true);
  }

  // --------------------------------------------------------------- follow-ups
  // The router already carries conversation context, so follow-ups work — but
  // nothing on screen said so, and after a long answer the composer reads like
  // the end of the exchange rather than an invitation to keep going.
  const FOLLOW_UPS = [
    "Explain that more simply",
    "What should I ask my care team?",
    "What should I watch out for?",
  ];

  function addFollowUpRow(el, mode) {
    if (mode === "clarify" || el.querySelector(".followups")) return;

    const wrap = document.createElement("div");
    wrap.className = "followups";
    wrap.innerHTML = `<span class="followups-label">Ask a follow-up:</span>`;
    FOLLOW_UPS.forEach((text) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "followup";
      b.textContent = text;
      b.addEventListener("click", () => {
        if (state.busy) return;
        send(text);
      });
      wrap.appendChild(b);
    });

    const actions = el.querySelector(".msg-actions");
    if (actions && !actions.hidden) actions.before(wrap);
    else el.appendChild(wrap);

    // Once a conversation exists, the composer should invite continuation.
    input.placeholder = "Ask a follow-up, or start a new question…";

    // The profile is what makes answers specific rather than generic, and the
    // moment someone has just read an answer is when that trade is legible.
    maybeNudgeProfile(el);
  }

  function maybeNudgeProfile(el) {
    const p = readProfileForm();
    const empty = !p.condition && !p.location && !p.preferences;
    if (!empty || document.querySelector(".profile-nudge")) return;

    const nudge = document.createElement("div");
    nudge.className = "profile-nudge";
    nudge.innerHTML =
      `<button type="button" class="profile-nudge-btn">Tell us about you</button>
       <span>— your condition and where you live make answers specific instead of general.</span>`;
    nudge.querySelector(".profile-nudge-btn").addEventListener("click", () => {
      const body = $("#profile-body");
      body.hidden = false;
      $("#profile-toggle").setAttribute("aria-expanded", "true");
      $("#profile-toggle").classList.add("is-open");
      $("#p-condition").focus();
      $(".profile-panel").scrollIntoView({ behavior: "smooth", block: "center" });
    });
    el.appendChild(nudge);
  }

  // ---------------------------------------------------------------- citations
  PG.configureCitations({
    idPrefix: "ref-",
    resolveRef: (label) => state.refsByLabel.get(String(label)) || null,
    fetchLay: async (label) => {
      if (!state.conversationId) return "";
      try {
        const r = await fetch(
          `/api/chat/${encodeURIComponent(state.conversationId)}/lay_summary/${encodeURIComponent(label)}`
        );
        if (!r.ok) return "";
        const data = await r.json();
        return (data && data.lay_summary) || "";
      } catch (e) {
        return "";
      }
    },
  });

  // ----------------------------------------------------------------- composer
  const input = $("#message");

  function autoGrow() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 220) + "px";
  }
  input.addEventListener("input", autoGrow);

  input.addEventListener("keydown", (e) => {
    // Enter sends; Shift+Enter makes a new line. On touch keyboards Enter is
    // usually a newline, so don't hijack it there.
    const isTouch = window.matchMedia("(pointer: coarse)").matches;
    if (e.key === "Enter" && !e.shiftKey && !isTouch) {
      e.preventDefault();
      $("#composer").requestSubmit();
    }
  });

  $("#composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value;
    if (state.busy || text.trim().length < 2) return;
    input.value = "";
    autoGrow();
    send(text);
  });

  document.querySelectorAll(".starter").forEach((btn) => {
    btn.addEventListener("click", () => {
      input.value = btn.dataset.fill || btn.textContent.trim();
      autoGrow();
      input.focus();
    });
  });

  // Restore the conversation id (not the transcript) so a refresh mid-chat keeps
  // citation labels stable on the server side.
  try {
    const saved = sessionStorage.getItem(CONV_KEY);
    if (saved) state.conversationId = saved;
  } catch (e) {}

  input.focus();
})();
