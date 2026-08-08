/* Patient Guide — the care-team dashboard.

   A left sidebar lists the team; clicking one enters THAT specialist's room and
   every message typed there is answered by that one agent. There is also a front
   door ("Not sure who to ask?") where the backend picks the agent and tells us
   who answered.

   Three things follow from "one room = one agent" and are easy to get wrong:

   * Each room has its OWN server-side conversation, because the evidence ledger
     lives on the conversation. Sharing one conversation across rooms would make
     `[3]` mean two different sources depending on which room you read it in — and
     the server refuses it with a 409 anyway.
   * Each room therefore keeps its own `refs` map AND its own citation id prefix,
     so a [3] chip resolves inside its own room.
   * Each room keeps its rendered transcript in the DOM (just `hidden`), so
     switching rooms never loses what another room already said. */
(() => {
  "use strict";

  const PG = window.PG;
  const $ = (sel) => document.querySelector(sel);

  const PROFILE_KEY = "pg-profile-v1";
  // roomId -> conversation_id, one JSON object under one key. The front door uses
  // the reserved room id below, which can never collide with a specialist id.
  const ROOMS_KEY = "pg-rooms-v1";
  const FRONT_DOOR = "__front__";

  // Front-door starters. Everything a *specialist* room says about itself comes
  // from /api/team and is never hardcoded here; the front door is not a
  // specialist, so its copy lives with the rest of the front-door chrome.
  const FRONT_DOOR_EXAMPLES = [
    "I was just diagnosed with type 2 diabetes and I don't really understand what it means. Where do I start?",
    "My doctor says I have heart failure and I should cut back on salt. How much salt is actually allowed, and what does that look like in real meals?",
    "I'm struggling to afford my medication and getting to appointments is hard. What help exists where I live?",
  ];

  const state = {
    team: [],                 // /api/team payload, in server order
    byId: new Map(),          // specialist id -> catalogue entry
    rooms: new Map(),         // roomId -> room object (see makeRoom)
    activeRoom: null,         // roomId, or null on the overview
    turnRoom: null,           // roomId that owns the in-flight turn
    turnQuestion: "",         // the question that produced the in-flight turn
    turnId: null,
    source: null,
    busy: false,
    currentTurnEl: null,      // the .msg-assistant being built
    agentCards: new Map(),    // agent id -> card element (current turn)
    elapsedTimer: null,
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

  function openProfilePanel(open) {
    const body = $("#profile-body");
    body.hidden = !open;
    $("#profile-toggle").setAttribute("aria-expanded", String(open));
    $("#profile-toggle").classList.toggle("is-open", open);
  }

  $("#profile-toggle").addEventListener("click", () => {
    openProfilePanel($("#profile-body").hidden);
  });

  $("#profile-save").addEventListener("click", () => {
    saveProfile(readProfileForm());
    openProfilePanel(false);
    $("#message").focus();
  });

  $("#profile-clear").addEventListener("click", () => {
    writeProfileForm({ language: "English" });
    saveProfile(readProfileForm());
    // Clearing "about me" is the closest thing to a reset switch on this page, so
    // it drops every room's server-side conversation too.
    state.rooms.forEach((room) => { room.conversationId = null; });
    persistRooms();
  });

  // ------------------------------------------------- per-room conversation ids
  function readRoomConversations() {
    try {
      const raw = JSON.parse(sessionStorage.getItem(ROOMS_KEY) || "{}");
      return raw && typeof raw === "object" ? raw : {};
    } catch (e) {
      return {};
    }
  }

  function persistRooms() {
    const out = {};
    state.rooms.forEach((room, id) => {
      if (room.conversationId) out[id] = room.conversationId;
    });
    try {
      sessionStorage.setItem(ROOMS_KEY, JSON.stringify(out));
    } catch (e) { /* private browsing — labels just restart on reload */ }
  }

  // ------------------------------------------------------------------- colors
  function tint(hex, alpha) {
    const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || "").trim());
    if (!m) return `rgba(74, 124, 111, ${alpha})`;
    const n = parseInt(m[1], 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
  }

  // Front-door visuals are the app's own, not a specialist's.
  const FRONT_DOOR_META = {
    id: FRONT_DOOR,
    display_name: "Not sure who to ask?",
    color: "#6B5F52",
    initials: "?",
    tagline: "Ask here and we'll bring in the right one",
    blurb:
      "Describe what's going on in your own words. We work out which of the team " +
      "is the right one for it, and they answer — then you can carry on in their room.",
    covers: [],
    examples: FRONT_DOOR_EXAMPLES,
  };

  function roomMeta(roomId) {
    if (roomId === FRONT_DOOR) return FRONT_DOOR_META;
    const s = state.byId.get(roomId);
    if (!s) return null;
    const v = PG.visualsFor(s.id, s.display_name);
    return {
      id: s.id,
      display_name: s.display_name,
      color: s.color || v.color,
      initials: v.initials,
      tagline: s.tagline || "",
      blurb: s.blurb || "",
      covers: s.covers || [],
      examples: s.examples || [],
    };
  }

  // --------------------------------------------------------------- team + DOM
  const roomRows = $("#room-rows");
  const teamGrid = $("#team-grid");
  const roomPanes = $("#room-panes");
  const roomHeader = $("#room-header");
  const viewOverview = $("#view-overview");
  const viewRoom = $("#view-room");
  const composerWrap = $("#composer-wrap");

  function dot(meta, extraClass) {
    return `<span class="room-dot ${extraClass || ""}" style="background:${PG.escapeAttr(meta.color)}" aria-hidden="true">${PG.escapeHtml(meta.initials)}</span>`;
  }

  function chipsHtml(covers) {
    if (!covers || !covers.length) return "";
    return `<span class="chips">${covers
      .map((c) => `<span class="chip">${PG.escapeHtml(c)}</span>`)
      .join("")}</span>`;
  }

  function buildSidebarRow(meta) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "room-row";
    row.dataset.room = meta.id;
    row.style.setProperty("--room-color", meta.color);
    row.style.setProperty("--room-tint", tint(meta.color, 0.1));
    row.innerHTML = `
      ${dot(meta)}
      <span class="room-row-text">
        <span class="room-row-name"></span>
        <span class="room-row-tag"></span>
      </span>`;
    row.querySelector(".room-row-name").textContent = meta.display_name;
    row.querySelector(".room-row-tag").textContent = meta.tagline;
    return row;
  }

  function buildTeamCard(meta) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "team-card";
    card.dataset.room = meta.id;
    card.style.setProperty("--room-color", meta.color);
    card.style.setProperty("--room-tint", tint(meta.color, 0.09));
    card.innerHTML = `
      <span class="team-card-head">
        ${dot(meta, "room-dot-lg")}
        <span class="team-card-titles">
          <span class="team-card-name"></span>
          <span class="team-card-tag"></span>
        </span>
      </span>
      <span class="team-card-blurb"></span>
      ${chipsHtml(meta.covers)}
      <span class="team-card-go">Talk to them →</span>`;
    card.querySelector(".team-card-name").textContent = meta.display_name;
    card.querySelector(".team-card-tag").textContent = meta.tagline;
    card.querySelector(".team-card-blurb").textContent = meta.blurb;
    return card;
  }

  function makeRoom(meta) {
    const pane = document.createElement("div");
    pane.className = "room-pane";
    pane.dataset.room = meta.id;
    pane.hidden = true;

    const starters = document.createElement("div");
    starters.className = "room-starters";
    const label = document.createElement("p");
    label.className = "room-starters-label";
    label.textContent =
      meta.id === FRONT_DOOR
        ? "Not sure how to put it? Try one of these:"
        : "Things people ask in this room:";
    starters.appendChild(label);
    const grid = document.createElement("div");
    grid.className = "room-starter-grid";
    (meta.examples || []).forEach((text) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "starter";
      b.textContent = text;
      b.addEventListener("click", () => {
        input.value = text;
        autoGrow();
        input.focus();
      });
      grid.appendChild(b);
    });
    starters.appendChild(grid);
    if (!(meta.examples || []).length) starters.hidden = true;
    pane.appendChild(starters);

    const transcript = document.createElement("section");
    transcript.className = "transcript";
    transcript.setAttribute("aria-live", "polite");
    transcript.setAttribute("aria-label", `Conversation with ${meta.display_name}`);
    pane.appendChild(transcript);

    roomPanes.appendChild(pane);

    return {
      id: meta.id,
      meta,
      pane,
      starters,
      transcript,
      conversationId: null,
      refs: new Map(),                 // label -> ref, scoped to THIS room's ledger
      idPrefix: `ref-${meta.id}-`,     // so [3] here never resolves to [3] there
      // One entry per question asked in this room: {question, mode}. `mode` is
      // filled in when the turn completes, and is what lets the next turn know it
      // is answering a clarifying question rather than asking a fresh one.
      turns: [],
    };
  }

  function registerRoom(meta) {
    const room = makeRoom(meta);
    state.rooms.set(meta.id, room);
    return room;
  }

  async function loadTeam() {
    let data = { specialists: [] };
    try {
      const r = await fetch("/api/team");
      if (r.ok) data = await r.json();
    } catch (e) { /* handled below */ }

    state.team = data.specialists || [];
    state.team.forEach((s) => state.byId.set(s.id, s));

    // Front door first, both in the sidebar (its own row, already in the markup)
    // and as a room.
    registerRoom(FRONT_DOOR_META);
    const frontRow = document.querySelector(".room-row-front");
    frontRow.style.setProperty("--room-color", FRONT_DOOR_META.color);
    frontRow.style.setProperty("--room-tint", tint(FRONT_DOOR_META.color, 0.1));

    state.team.forEach((s) => {
      const meta = roomMeta(s.id);
      roomRows.appendChild(buildSidebarRow(meta));
      teamGrid.appendChild(buildTeamCard(meta));
      registerRoom(meta);
    });

    if (!state.team.length) {
      const p = document.createElement("p");
      p.className = "team-grid-empty";
      p.textContent =
        "We couldn't load the care team just now. Refresh the page, or use " +
        "“Not sure who to ask?” and we'll route your question.";
      teamGrid.appendChild(p);
    }

    // Restore each room's conversation id so a refresh keeps [N] labels stable.
    const saved = readRoomConversations();
    Object.keys(saved).forEach((id) => {
      const room = state.rooms.get(id);
      if (room && typeof saved[id] === "string") room.conversationId = saved[id];
    });
  }

  // ---------------------------------------------------------------- routing
  function hashForRoom(roomId) {
    if (!roomId) return "#/";
    return roomId === FRONT_DOOR ? "#/ask" : `#/room/${encodeURIComponent(roomId)}`;
  }

  function roomFromHash() {
    const h = (location.hash || "").replace(/^#/, "");
    if (h === "/ask") return FRONT_DOOR;
    const m = /^\/room\/([^/?#]+)$/.exec(h);
    if (m) {
      const id = decodeURIComponent(m[1]);
      return state.rooms.has(id) ? id : null;
    }
    return null;
  }

  function navigate(roomId) {
    const want = hashForRoom(roomId);
    if (location.hash === want) {
      applyHash();
      return;
    }
    location.hash = want;   // hashchange does the rest
  }

  function applyHash() {
    showRoom(roomFromHash());
  }

  function showRoom(roomId) {
    state.activeRoom = roomId;

    state.rooms.forEach((room, id) => { room.pane.hidden = id !== roomId; });
    document.querySelectorAll(".room-row").forEach((row) => {
      const on = row.dataset.room === roomId;
      row.classList.toggle("is-active", on);
      if (on) row.setAttribute("aria-current", "true");
      else row.removeAttribute("aria-current");
    });

    viewOverview.hidden = !!roomId;
    viewRoom.hidden = !roomId;
    composerWrap.hidden = !roomId;
    document.body.classList.toggle("in-room", !!roomId);

    if (roomId) {
      renderRoomHeader(roomId);
      const room = state.rooms.get(roomId);
      input.placeholder = room.turns.length
        ? "Ask a follow-up, or start a new question…"
        : `Ask your ${room.meta.display_name}…`;
      if (!room.turns.length) window.scrollTo({ top: 0, behavior: "auto" });
      else scrollToBottom(false);
      if (!isDrawer()) input.focus();
    } else {
      roomHeader.innerHTML = "";
      window.scrollTo({ top: 0, behavior: "auto" });
    }
    closeDrawer();
  }

  function renderRoomHeader(roomId) {
    const meta = state.rooms.get(roomId).meta;
    roomHeader.style.setProperty("--room-color", meta.color);
    roomHeader.style.setProperty("--room-tint", tint(meta.color, 0.09));
    roomHeader.innerHTML = `
      <button type="button" class="room-back">← Your care team</button>
      <div class="room-header-main">
        ${dot(meta, "room-dot-xl")}
        <div class="room-header-text">
          <h1 class="room-title"></h1>
          <p class="room-tagline"></p>
          ${chipsHtml(meta.covers)}
        </div>
      </div>`;
    roomHeader.querySelector(".room-title").textContent = meta.display_name;
    roomHeader.querySelector(".room-tagline").textContent = meta.tagline;
    roomHeader.querySelector(".room-back").addEventListener("click", () => navigate(null));
  }

  // ------------------------------------------------------------- mobile drawer
  const sidebar = $("#sidebar");
  const scrim = $("#sidebar-scrim");
  const navToggle = $("#nav-toggle");

  function isDrawer() {
    return window.matchMedia("(max-width: 899px)").matches;
  }

  function openDrawer() {
    sidebar.classList.add("is-open");
    scrim.hidden = false;
    navToggle.setAttribute("aria-expanded", "true");
    document.body.classList.add("drawer-open");
  }

  function closeDrawer() {
    sidebar.classList.remove("is-open");
    scrim.hidden = true;
    navToggle.setAttribute("aria-expanded", "false");
    document.body.classList.remove("drawer-open");
  }

  navToggle.addEventListener("click", () => {
    if (sidebar.classList.contains("is-open")) closeDrawer();
    else openDrawer();
  });
  scrim.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && sidebar.classList.contains("is-open")) closeDrawer();
  });

  // The disclaimer banner is sticky and its height depends on how the compliance
  // text wraps, so the sidebar's sticky offset is measured rather than guessed.
  function syncBannerHeight() {
    const banner = document.querySelector(".disclaimer-banner");
    const h = banner ? Math.ceil(banner.getBoundingClientRect().height) : 0;
    document.documentElement.style.setProperty("--banner-h", `${h}px`);
  }
  window.addEventListener("resize", syncBannerHeight);
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(syncBannerHeight).catch(() => {});
  }

  // Clicks on a sidebar row or an overview card both mean "enter that room".
  // Deliberately NOT `[data-room]`: the room panes carry that attribute too, so a
  // broad selector would turn every click inside a transcript into a navigation.
  document.addEventListener("click", (e) => {
    const target = e.target.closest && e.target.closest(".room-row, .team-card");
    if (!target || !target.dataset.room) return;
    e.preventDefault();
    navigate(target.dataset.room);
  });

  $("#sidebar-title").addEventListener("click", (e) => {
    e.preventDefault();
    navigate(null);
  });

  window.addEventListener("hashchange", applyHash);
  window.addEventListener("popstate", applyHash);

  // --------------------------------------------------------------- transcript
  function activeRoom() {
    return state.activeRoom ? state.rooms.get(state.activeRoom) : null;
  }

  function scrollToBottom(smooth) {
    window.scrollTo({
      top: document.body.scrollHeight,
      behavior: smooth ? "smooth" : "auto",
    });
  }

  function addUserMessage(room, text) {
    const el = document.createElement("article");
    el.className = "msg msg-user";
    el.innerHTML = `<div class="msg-bubble"></div>`;
    el.querySelector(".msg-bubble").textContent = text;
    room.transcript.appendChild(el);
    return el;
  }

  function addAssistantShell(room) {
    const el = document.createElement("article");
    el.className = "msg msg-assistant is-working";
    el.innerHTML = `
      <div class="msg-meta">
        <span class="msg-status" aria-live="polite">Getting started…</span>
        <span class="msg-elapsed" hidden>0:00</span>
      </div>
      <div class="msg-agents" hidden></div>
      <div class="msg-progress" hidden>
        <ol class="work-steps"></ol>
        <div class="work-bar" aria-hidden="true"></div>
        <p class="work-note">This usually takes one to four minutes. We're reading the actual guidelines, studies and registry entries before answering rather than writing from memory, and that takes as long as it takes. Nothing is stuck — you can leave this tab and come back, the answer will be here.</p>
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
    room.transcript.appendChild(el);
    return el;
  }

  function setTurnStatus(text) {
    if (!state.currentTurnEl) return;
    const el = state.currentTurnEl.querySelector(".msg-status");
    if (el) el.textContent = text;
  }

  // ------------------------------------------------------- proof of life
  // A single-agent turn measured live runs from ~60s to ~260s — it scales with
  // how many sources the agent pulls. Over a wait that long the clock and the
  // step checklist are load-bearing, not decoration: they stay up for the whole
  // turn and only come down in finishTurn(). Almost all of that time is the
  // `researching` step, which has no phase event of its own — the per-source
  // counter on the agent chip ("read 3 sources", from `specialist_event`) is the
  // only thing that moves during it, and is the main proof of life.

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

  // Mode-driven, and deliberately still handling `team`: /api/chat only produces
  // one agent per turn now, so the synthesis steps simply stop appearing — but
  // the branch costs nothing and the board path still emits those phases.
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

  // What a referral should carry into the next room. Usually that is simply the
  // question just asked — but not after a clarifying question. On that path the
  // last thing the patient typed is the ANSWER to the clarification ("Stage 3
  // lung cancer, and I live in Manchester"), which lands in a brand-new room with
  // its own empty conversation as a bare statement of facts and no question at
  // all. So when the previous turn in this room was a clarify, stitch its
  // question back onto the front.
  //
  // Exactly one level: `prev.question` is whatever triggered the clarify, and we
  // take it verbatim rather than walking further back, so a second clarify can
  // never compound into a run-on sentence.
  function carryTextFor(room, message) {
    const prev = room.turns[room.turns.length - 1];
    if (prev && prev.mode === "clarify" && prev.question) {
      return `${prev.question} — ${message}`;
    }
    return message;
  }

  // ------------------------------------------------------------------ sending
  async function send(text) {
    if (state.busy) return;
    const message = (text || "").trim();
    if (message.length < 2) return;

    const room = activeRoom();
    if (!room) return;               // no room selected — nothing to send to

    state.busy = true;
    state.turnRoom = room.id;
    state.turnQuestion = carryTextFor(room, message);
    $("#send-btn").disabled = true;
    $("#stop-btn").hidden = false;

    const currentProfile = readProfileForm();
    saveProfile(currentProfile);

    room.starters.hidden = true;
    room.turns.push({ question: message, mode: null });
    addUserMessage(room, message);
    state.currentTurnEl = addAssistantShell(room);
    state.targetLanguage = currentProfile.language || "English";
    startElapsed();
    scrollToBottom(true);

    // null for the front door — the backend picks the agent and names it back.
    const pinned = room.id === FRONT_DOOR ? null : room.id;

    const post = (conversationId) =>
      fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message,
          conversation_id: conversationId,
          profile: currentProfile,
          specialist: pinned,
        }),
      });

    let payload;
    try {
      const r = await post(room.conversationId);
      if ((r.status === 404 || r.status === 409) && room.conversationId) {
        // 404: the conversation expired server-side. 409: it belongs to a
        // different room (or is still busy). Either way this room's stored id is
        // no longer usable — drop it and retry once as a fresh conversation so
        // the patient doesn't lose the question they just typed.
        room.conversationId = null;
        persistRooms();
        const retry = await post(null);
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

    room.conversationId = payload.conversation_id;
    state.turnId = payload.turn_id;
    persistRooms();

    startStream(room.conversationId, state.turnId);
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
    state.turnRoom = null;
    state.agentCards.clear();
  }

  function startStream(cid, tid) {
    const url = `/api/chat/${encodeURIComponent(cid)}/turns/${encodeURIComponent(tid)}/stream`;
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
    const room = state.turnRoom ? state.rooms.get(state.turnRoom) : null;
    if (room && room.conversationId && state.turnId) {
      try {
        await fetch(`/api/chat/${room.conversationId}/turns/${state.turnId}`, { method: "DELETE" });
      } catch (e) {}
    }
    setTurnStatus("Stopped");
    if (state.currentTurnEl) state.currentTurnEl.classList.remove("is-working");
    finishTurn();
  });

  // ------------------------------------------------------------------- events
  const PHASE_TEXT = {
    triaging: "Getting your question straight…",
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

      case "referral":
        renderReferral(state.currentTurnEl, p);
        break;

      case "fallback":
        // In a room, `from === to` — the same agent is being asked again after an
        // over-strict self-check, so nothing has been handed over and the chip
        // must not be marked as sitting it out.
        if (p.from && p.to && p.from !== p.to) {
          setTurnStatus("That helper had nothing solid — asking our researcher instead…");
          setAgentState(p.from, "is-skipped", "–");
          setAgentStatus(p.from, "sat this one out");
        } else {
          setTurnStatus("Going back for a second look…");
        }
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
          ? `Your ${names} is looking this up…`
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
      setAgentStatus(id, "this isn't their area");
    } else if (p.status === "no_evidence") {
      setAgentState(id, "is-error", "!");
      setAgentStatus(id, "couldn't find solid sources");
    } else {
      setAgentState(id, "is-error", "!");
      setAgentStatus(id, "hit a problem");
    }
  }

  // ------------------------------------------------------------- referral card
  // Two kinds, and the difference matters:
  //   handoff    — the room could not answer. The referral IS the reply, so it is
  //                the loudest thing in the message.
  //   suggestion — the room answered well, but another room can go further. It
  //                sits under a real answer, so it has to read as a signpost, not
  //                as a correction of the answer above it.
  // Both arrive twice (mid-turn SSE, then on turn_complete), so this is idempotent.
  function renderReferral(el, payload) {
    if (!el || !payload || !payload.to) return;
    if (el.querySelector(".referral-card")) return;

    const target = state.rooms.get(payload.to);
    const meta = target ? target.meta : null;
    const name = payload.to_display_name || (meta && meta.display_name) || payload.to;
    const kind = payload.kind === "handoff" ? "handoff" : "suggestion";

    const card = document.createElement("div");
    card.className = `referral-card is-${kind}`;
    if (meta) {
      card.style.setProperty("--room-color", meta.color);
      card.style.setProperty("--room-tint", tint(meta.color, 0.1));
    }
    card.innerHTML = `
      <div class="referral-line">
        ${meta ? dot(meta) : ""}
        <p class="referral-reason"></p>
      </div>
      <button type="button" class="btn referral-btn"></button>`;
    card.querySelector(".referral-reason").textContent =
      payload.reason || `${name} can help with this.`;
    const btn = card.querySelector(".referral-btn");
    btn.textContent = `Ask the ${name} →`;
    btn.classList.add(kind === "handoff" ? "btn-primary" : "btn-ghost", "btn-small");

    // Carry the question across so a wrong door is never a dead end: switch rooms
    // AND put what they just asked back in the composer, ready to send.
    const question = state.turnQuestion;
    btn.addEventListener("click", () => {
      navigate(payload.to);
      if (question) {
        input.value = question;
        autoGrow();
      }
      input.focus();
    });

    const sources = el.querySelector(".msg-sources");
    if (sources) sources.before(card);
    else el.appendChild(card);
  }

  // -------------------------------------------------------- front-door credit
  // In the front door the patient has no idea who is answering. Name them, and
  // offer the room so the next question goes straight there.
  function renderAnsweredBy(el, specialistId) {
    if (!el || !specialistId) return;
    if (el.querySelector(".answered-by")) return;
    const target = state.rooms.get(specialistId);
    if (!target) return;
    const meta = target.meta;

    const wrap = document.createElement("div");
    wrap.className = "answered-by";
    wrap.style.setProperty("--room-color", meta.color);
    wrap.style.setProperty("--room-tint", tint(meta.color, 0.1));
    wrap.innerHTML = `
      ${dot(meta)}
      <span class="answered-by-text">Answered by your <strong></strong></span>
      <button type="button" class="btn btn-ghost btn-small answered-by-btn">Continue in that room →</button>`;
    wrap.querySelector("strong").textContent = meta.display_name;
    wrap.querySelector(".answered-by-btn").addEventListener("click", () => {
      navigate(specialistId);
      input.focus();
    });

    const meta_el = el.querySelector(".msg-meta");
    if (meta_el) meta_el.after(wrap);
    else el.prepend(wrap);
  }

  function onTurnComplete(p) {
    const el = state.currentTurnEl;
    const room = state.turnRoom ? state.rooms.get(state.turnRoom) : null;
    if (!el || !room) {
      finishTurn();
      return;
    }

    // Remember how this turn was answered. A `clarify` here is what tells the
    // NEXT turn in this room that the patient is about to answer a question
    // rather than ask one — see carryTextFor().
    const thisTurn = room.turns[room.turns.length - 1];
    if (thisTurn) thisTurn.mode = p.mode || "";

    // Merge this turn's references into THIS ROOM's map, tagged with the room so
    // the tooltip can tell one room's [3] from another's.
    (p.all_references || p.references || []).forEach((ref) => {
      ref.__scope = room.id;
      room.refs.set(String(ref.label), ref);
    });

    el.classList.remove("is-working");
    const md = p.markdown || p.english_markdown || "*No answer was produced.*";
    PG.renderMarkdown(el.querySelector(".msg-body"), md, { idPrefix: room.idPrefix });

    // Status line: what actually happened, in the patient's terms.
    const used = (p.route && p.route.specialists) || [];
    const secs = (p.timing && p.timing.total_s) || 0;
    const refs = p.references || [];
    if (p.mode === "clarify") {
      setTurnStatus("Just need one detail");
    } else if (used.length) {
      const names = used.map((s) => s.display_name).join(" · ");
      setTurnStatus(`${names} · ${refs.length} source${refs.length === 1 ? "" : "s"} · ${secs}s`);
    } else {
      setTurnStatus(`${secs}s`);
    }

    // The front door doesn't say who is answering until now.
    if (room.id === FRONT_DOOR && p.specialist) renderAnsweredBy(el, p.specialist);

    // Per-message source list.
    if (refs.length) {
      const wrap = el.querySelector(".msg-sources");
      const list = wrap.querySelector(".references-list");
      wrap.hidden = false;
      wrap.querySelector(".sources-count").textContent = `(${refs.length})`;
      list.innerHTML = "";
      refs.forEach((ref) => list.appendChild(PG.referenceListItem(ref, room.idPrefix)));
      const toggle = wrap.querySelector(".sources-toggle");
      toggle.addEventListener("click", () => {
        const open = list.hidden;
        list.hidden = !open;
        toggle.setAttribute("aria-expanded", String(open));
      });
    }

    // Either kind of referral; the mid-turn event may already have drawn it.
    if (p.referral) renderReferral(el, p.referral);

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
  // The room carries conversation context, so follow-ups work — but nothing on
  // screen said so, and after a long answer the composer reads like the end of
  // the exchange rather than an invitation to keep going.
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
      // The panel lives in the sidebar now, which is a drawer on a phone.
      if (isDrawer()) openDrawer();
      openProfilePanel(true);
      $("#p-condition").focus();
      $(".profile-panel").scrollIntoView({ behavior: "smooth", block: "center" });
    });
    el.appendChild(nudge);
  }

  // ---------------------------------------------------------------- citations
  // A [3] chip resolves against the room it was rendered in, not against whatever
  // room happens to be open — otherwise the tooltip on an older room's answer
  // would show a source from somewhere else entirely.
  function roomForCitation(anchor) {
    const pane = anchor && anchor.closest ? anchor.closest(".room-pane") : null;
    if (pane && state.rooms.has(pane.dataset.room)) return state.rooms.get(pane.dataset.room);
    return activeRoom();
  }

  PG.configureCitations({
    idPrefix: "ref-",
    resolveRef: (label, anchor) => {
      const room = roomForCitation(anchor);
      return (room && room.refs.get(String(label))) || null;
    },
    fetchLay: async (label, ref) => {
      const room =
        (ref && ref.__scope && state.rooms.get(ref.__scope)) || activeRoom();
      if (!room || !room.conversationId) return "";
      try {
        const r = await fetch(
          `/api/chat/${encodeURIComponent(room.conversationId)}/lay_summary/${encodeURIComponent(label)}`
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

  // ---------------------------------------------------------------- bootstrap
  (async function start() {
    syncBannerHeight();
    await loadTeam();
    applyHash();
    syncBannerHeight();
  })();
})();
