/* Jarvis UI: WebSocket client for /ws, orb states, approval card, free browser voice.
 *
 * Server events: route | token | tool_call | tool_result | approval_required | done | error
 * Voice, no keys and no cost:
 *   - speech-to-text: offline Whisper on the server (/transcribe) when installed, else the
 *     Web Speech API (Chrome/Edge; Chrome sends audio to its speech service)
 *   - text-to-speech: speechSynthesis (voices installed on this computer)
 */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const orb = new Orb($("orb"));
  const ACTION_TOOLS = new Set(["add_task", "complete_task"]);

  // ---------- small helpers ----------
  const store = {
    get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };
  const newThread = () => "ui-" + Math.random().toString(36).slice(2, 10);
  let threadId = store.get("jarvis.thread", null) || newThread();
  store.set("jarvis.thread", threadId);

  function setOrb(state) {
    orb.setState(state);
    $("orbLabel").textContent = orb.label;
    const [r, g, b] = orb.color;
    document.documentElement.style.setProperty("--accent", `rgb(${r},${g},${b})`);
  }
  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  const renderText = (s) => escapeHtml(s).replace(/\[(\d+)\]/g, '<span class="cite">[$1]</span>');

  function addMsg(kind, text) {
    const el = document.createElement("div");
    el.className = "msg " + kind;
    el.innerHTML = renderText(text);
    $("log").appendChild(el);
    $("log").scrollTop = $("log").scrollHeight;
    return el;
  }
  function chip(kind, text) {
    const c = document.createElement("span");
    c.className = "chip " + kind;
    c.textContent = text;
    $("activity").appendChild(c);
  }

  // ---------- voice output ----------
  const canSpeak = "speechSynthesis" in window;
  $("speakToggle").checked = canSpeak && store.get("jarvis.speak", "0") === "1";
  $("speakToggle").disabled = !canSpeak;
  $("speakToggle").onchange = (e) => store.set("jarvis.speak", e.target.checked ? "1" : "0");
  function speak(text, after) {
    if (!$("speakToggle").checked || !canSpeak || !text) return after();
    const u = new SpeechSynthesisUtterance(text.replace(/\[\d+\]/g, "").replace(/#(\d+)/g, "number $1"));
    u.rate = 1.05;
    u.onstart = () => setOrb("speaking");
    u.onboundary = () => orb.setLevel(0.4 + Math.random() * 0.5);
    u.onend = u.onerror = () => { orb.setLevel(0); after(); };
    speechSynthesis.cancel();
    speechSynthesis.speak(u);
  }

  // ---------- morph toggle ----------
  $("morphToggle").checked = store.get("jarvis.morph", "1") === "1";
  orb.setMorph($("morphToggle").checked);
  $("morphToggle").onchange = (e) => { orb.setMorph(e.target.checked); store.set("jarvis.morph", e.target.checked ? "1" : "0"); };

  // ---------- WebSocket ----------
  let ws, busy = false, current = null, actionDone = false, retry = 0;

  function setBusy(b) {
    busy = b;
    $("send").disabled = b;
    $("input").disabled = b;
    if (!b) $("input").focus();
  }

  function connect() {
    ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
    ws.onopen = () => { retry = 0; health(); };
    ws.onclose = () => {
      $("health").textContent = "disconnected"; $("health").className = "pill bad";
      setTimeout(connect, Math.min(8000, 500 * 2 ** retry++));
    };
    ws.onmessage = (ev) => handle(JSON.parse(ev.data));
  }

  function handle(e) {
    switch (e.type) {
      case "route":
        e.agents.forEach((a) => chip("agent", "▸ " + a));
        break;
      case "token":
        if (!current) current = addMsg("bot pending", "");  // replaced by the verified reply on "done"
        current.dataset.raw = (current.dataset.raw || "") + e.text;
        current.innerHTML = renderText(current.dataset.raw);
        $("log").scrollTop = $("log").scrollHeight;
        break;
      case "tool_call":
        chip("tool", "⚙ " + e.name);
        break;
      case "tool_result":
        if (ACTION_TOOLS.has(e.name) && !String(e.content).startsWith("error")) { actionDone = true; chip("okc", "✓ " + e.name); }
        current = null;  // text after a tool result starts a new (still pending) bubble
        break;
      case "approval_required":
        showApproval(e.approval);
        break;
      case "done":
        finishTurn(e.reply, e.agents);
        break;
      case "error":
        addMsg("err", "Error: " + e.message);
        setOrb("error"); setBusy(false);
        break;
    }
  }

  function finishTurn(reply, agents) {
    // The server's final text is verified (it can replace an unconfirmed claim), so it wins.
    document.querySelectorAll(".msg.bot.pending").forEach((m) => m.remove());
    if (current) current.remove();
    current = null;
    const el = addMsg("bot", reply || "(no reply)");
    if (agents?.length) el.insertAdjacentHTML("beforeend", `<span class="meta">${agents.join(" → ")}</span>`);
    setOrb(actionDone ? "mission" : "speaking");
    speak(reply, () => setTimeout(() => setOrb("idle"), actionDone ? 2500 : 600));
    setBusy(false);
  }

  // ---------- approval ----------
  function describe(a) {
    const args = a.args || {};
    if (a.tool === "add_task") {
      return `Add task: "${args.title}"` + (args.due ? `\nDue: ${args.due}` : "") +
             (args.priority && args.priority !== "normal" ? `\nPriority: ${args.priority}` : "");
    }
    if (a.tool === "complete_task") return `Mark as done: ${a.target || "#" + args.task_id}`;
    return `${a.tool}(${JSON.stringify(args)})`;
  }
  function showApproval(a) {
    if (current) { current.remove(); current = null; }
    $("approvalText").textContent = describe(a);
    $("approval").hidden = false;
    setOrb("asking");
    speak("I need your approval. " + describe(a).split("\n")[0], () => {});
    $("approveBtn").focus();
  }
  function answerApproval(approved) {
    if ($("approval").hidden) return;
    $("approval").hidden = true;
    chip(approved ? "okc" : "tool", approved ? "approved" : "rejected");
    setOrb("thinking");
    ws.send(JSON.stringify({ thread_id: threadId, approved }));
  }
  $("approveBtn").onclick = () => answerApproval(true);
  $("rejectBtn").onclick = () => answerApproval(false);

  // ---------- sending ----------
  function send(text) {
    text = text.trim();
    if (!text || busy || ws.readyState !== 1) return;
    $("activity").innerHTML = "";
    $("approval").hidden = true;
    actionDone = false;
    addMsg("user", text);
    setBusy(true);
    setOrb("thinking");
    ws.send(JSON.stringify({ thread_id: threadId, message: text }));
  }
  $("form").onsubmit = (e) => { e.preventDefault(); send($("input").value); $("input").value = ""; };
  $("newChat").onclick = () => {
    threadId = newThread(); store.set("jarvis.thread", threadId);
    $("log").innerHTML = ""; $("activity").innerHTML = ""; $("approval").hidden = true; setOrb("idle");
  };

  // ---------- voice input (push-to-talk) ----------
  // "whisper": record here, transcribe offline on the server (/transcribe) - nothing leaves the PC.
  // "browser": Web Speech API fallback (Chrome/Edge; Chrome sends audio to its speech service).
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const canRecord = !!(navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
  let sttMode = "browser", rec = null, recording = false;

  function updateMic() {
    const ok = sttMode === "whisper" ? canRecord : !!SR;
    $("mic").disabled = !ok;
    $("mic").title = !ok ? "Voice input is not supported in this browser"
      : `Talk: click, or hold Space (empty input) · ${sttMode === "whisper" ? "offline Whisper" : "browser speech"}`;
  }

  function startRec() {
    if (recording || busy) return;
    if (sttMode === "whisper" && canRecord) return startWhisper();
    if (SR) return startBrowserSR();
  }
  function beginRec() {
    recording = true; $("mic").classList.add("rec"); setOrb("listening");
    speechSynthesis?.cancel();
  }
  function endRec() { recording = false; $("mic").classList.remove("rec"); orb.setLevel(0); }

  async function startWhisper() {
    beginRec();
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      endRec(); setOrb("error"); addMsg("err", "Microphone access was blocked."); return;
    }
    const chunks = [];
    const mr = new MediaRecorder(stream);
    // Level meter so the orb reacts to your voice.
    const ctx = new AudioContext(), an = ctx.createAnalyser(), buf = new Uint8Array(256);
    ctx.createMediaStreamSource(stream).connect(an);
    const meter = setInterval(() => {
      an.getByteTimeDomainData(buf);
      let peak = 0; for (const v of buf) peak = Math.max(peak, Math.abs(v - 128));
      orb.setLevel(Math.min(1, peak / 50));
    }, 60);
    mr.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    mr.onstop = async () => {
      clearInterval(meter); ctx.close(); stream.getTracks().forEach((t) => t.stop());
      endRec();
      const blob = new Blob(chunks, { type: mr.mimeType || "audio/webm" });
      if (blob.size < 1000) { setOrb("idle"); return; }   // a tap, not speech
      setOrb("thinking"); $("input").placeholder = "Transcribing…";
      try {
        const r = await fetch("/transcribe", { method: "POST", headers: { "content-type": blob.type }, body: blob });
        if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
        const { text } = await r.json();
        if (text.trim()) send(text); else setOrb("idle");
      } catch (err) {
        addMsg("err", "Transcription failed: " + err.message); setOrb("error");
      } finally { $("input").placeholder = PLACEHOLDER; }
    };
    rec = { stop: () => mr.state !== "inactive" && mr.stop() };
    mr.start();
  }

  function startBrowserSR() {
    const r = new SR();
    r.lang = "en-US"; r.interimResults = true; r.maxAlternatives = 1;
    let finalText = "";
    r.onresult = (ev) => {
      let interim = "";
      for (const x of ev.results) (x.isFinal ? (finalText = x[0].transcript) : (interim += x[0].transcript));
      $("input").value = finalText || interim;
      orb.setLevel(0.5);
    };
    r.onend = () => {
      endRec();
      const text = $("input").value;
      $("input").value = "";
      if (text.trim()) send(text); else setOrb("idle");
    };
    r.onerror = () => {};
    rec = r;
    beginRec();
    r.start();
  }
  const stopRec = () => { if (recording && rec) rec.stop(); };
  $("mic").onclick = () => (recording ? stopRec() : startRec());
  const PLACEHOLDER = $("input").placeholder;
  updateMic();

  // ---------- keyboard ----------
  document.addEventListener("keydown", (e) => {
    if (!$("approval").hidden && (e.key === "y" || e.key === "n") && e.target !== $("input")) {
      e.preventDefault(); answerApproval(e.key === "y"); return;
    }
    if (e.code === "Space" && !e.repeat && !$("input").value && !e.target.closest("button, label")) {
      e.preventDefault(); startRec();
    }
    if (e.key === "Escape") speechSynthesis?.cancel();
  });
  document.addEventListener("keyup", (e) => { if (e.code === "Space" && recording) { e.preventDefault(); stopRec(); } });

  // ---------- health ----------
  async function health() {
    try {
      const h = await (await fetch("/health")).json();
      $("health").textContent = `${h.provider} · ${h.tools.length} tools · ${h.memory}`;
      sttMode = h.stt || "browser"; updateMic();
      $("health").className = "pill ok";
    } catch { $("health").textContent = "offline"; $("health").className = "pill bad"; }
  }

  setOrb("idle");
  connect();
})();
