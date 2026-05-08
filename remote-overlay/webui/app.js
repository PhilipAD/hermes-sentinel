/**
 * Hermes Sentinel — full dashboard webui client.
 *
 * Talks to the Sentinel overlay server (FastAPI) over REST + WebSocket.
 * Designed to be served from the same backend at ``/webui`` or any static
 * host (GitHub Pages, etc.) and pointed at a remote backend with
 * ``?sentinel=http://host:port&api_key=…``.
 *
 * Capabilities (matches phase 2 spec):
 *   - Pre-meeting curation: goal, persona, docs, run sentinel_curate
 *   - Live session monitor: status, transcript, start/stop
 *   - Session switcher + context toggles (docs/persona/goal/history)
 *   - Suggestions feed with dismiss/apply
 *   - History search across stored transcripts
 *   - Settings panel (backend, audio source, VAD, mode)
 *   - Microphone capture → base64 PCM streamed over WebSocket
 */
const App = (() => {
  'use strict';

  const params = new URLSearchParams(location.search);
  let HOST = (params.get('sentinel') || localStorage.getItem('sentinel_host') ||
    (location.protocol === 'file:' ? 'http://127.0.0.1:18765'
      : location.origin)).replace(/\/$/, '');
  let KEY = params.get('api_key') || localStorage.getItem('sentinel_key') || '';
  const SETTINGS_KEY = 'sentinel_dashboard_settings_v1';
  let settings = Object.assign({
    backend: 'openai', audioSource: 'auto', vad: 50, mode: 'freeform',
    contexts: { docs: true, persona: true, goal: true, history: true },
  }, JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}'));
  settings.contexts = Object.assign({ docs: true, persona: true, goal: true, history: true }, settings.contexts || {});

  let ws = null;
  let suggestions = [];
  let transcript = [];
  let activeSessionId = '';
  let audioSources = [];
  let muteState = { mic: false, system: false };

  function $(id) { return document.getElementById(id); }
  function authHeaders() { return KEY ? { Authorization: 'Bearer ' + KEY } : {}; }

  async function call(path, body, method) {
    const opts = {
      method: method || (body ? 'POST' : 'GET'),
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
    };
    if (body) opts.body = JSON.stringify(body);
    const r = await fetch(HOST + path, opts);
    if (!r.ok) throw new Error('HTTP ' + r.status + ' ' + r.statusText);
    return await r.json();
  }

  function setStatus(connected) {
    const p = $('status-pill');
    p.classList.toggle('live', !!connected);
    p.classList.toggle('dead', !connected);
    p.textContent = connected ? 'connected' : 'disconnected';
  }

  function send(obj) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    try { ws.send(JSON.stringify(obj)); return true; } catch (_) { return false; }
  }

  // ---------- Audio mute / source filter helpers ----------

  function sendAudioMute(channel, muted) {
    return send({ type: 'audio_mute', kind: 'audio_mute', channel, muted });
  }
  function sendAudioSourceFilter(sources) {
    return send({ type: 'audio_source_filter', kind: 'audio_source_filter', sources });
  }
  function sendAudioLevelSnap() {
    return send({ type: 'audio_sources_request', kind: 'audio_sources_request' });
  }

  function applyMuteUI(channel) {
    const muted = !!muteState[channel];
    const btn = channel === 'mic' ? $('mic-mute-btn') : $('speaker-mute-btn');
    if (!btn) return;
    const label = channel === 'mic' ? 'Mic' : 'Speaker';
    btn.classList.toggle('muted', muted);
    btn.classList.toggle('active', !muted);
    btn.textContent = (muted ? '❌ ' : '✅ ') + label;
  }
  function iconFor(key) {
    const m = {
      chrome: '🌐', firefox: '🦊', safari: '🧭', edge: '🌐',
      music: '🎵', spotify: '🎧', slack: '💬', discord: '🎮',
      zoom: '📞', meet: '🎥', teams: '🟣', mic: '🎙', speaker: '🔊',
    };
    return m[(key || '').toLowerCase()] || '🔊';
  }
  function renderAudioSources(sources) {
    audioSources = sources.slice();
    const list = $('audio-sources-list');
    if (!list) return;
    if (!sources.length) {
      list.innerHTML = '<div class="empty">No sources detected.</div>';
      return;
    }
    list.innerHTML = '';
    for (const s of sources) {
      const enabled = s.enabled !== false;
      const row = document.createElement('label');
      row.className = 'audio-source-item' + (enabled ? '' : ' disabled');
      row.dataset.id = s.id;
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = enabled;
      cb.addEventListener('change', () => {
        const map = {};
        for (const item of list.querySelectorAll('.audio-source-item')) {
          const id = item.dataset.id;
          const c  = item.querySelector('input[type="checkbox"]');
          map[id] = !!(c && c.checked);
          item.classList.toggle('disabled', !(c && c.checked));
        }
        sendAudioSourceFilter(map);
      });
      const ic = document.createElement('span');
      ic.className = 'src-icon';
      ic.textContent = s.icon ? iconFor(s.icon) : '🔊';
      const nm = document.createElement('span');
      nm.className = 'src-name';
      nm.textContent = s.name || s.id || 'unknown';
      row.append(cb, ic, nm);
      list.appendChild(row);
    }
  }
  function applyStatusToAudio(state) {
    if (!state) return;
    if (typeof state.mic_muted === 'boolean')    { muteState.mic = state.mic_muted;       applyMuteUI('mic'); }
    if (typeof state.system_muted === 'boolean') { muteState.system = state.system_muted; applyMuteUI('system'); }
    if (Array.isArray(state.audio_sources)) renderAudioSources(state.audio_sources);
    if (typeof state.audio_level === 'number') {
      const fill = $('volume-fill');
      if (fill) fill.style.width = Math.max(0, Math.min(100, Math.round(state.audio_level * 100))) + '%';
    }
  }

  // ---------- Suggestions / transcript renderers ----------

  function renderSuggestions() {
    const feed = $('suggestions-feed');
    if (!suggestions.length) {
      feed.innerHTML = '<div class="empty">Awaiting…</div>';
      return;
    }
    feed.innerHTML = '';
    for (const s of suggestions) {
      const card = document.createElement('div');
      card.className = 'item';
      card.dataset.id = s.id || ('sugg-' + Date.now());
      const ts = (s.ts ? s.ts * 1000 : Date.now());
      card.innerHTML = `
        <div class="mode">${s.mode || 'suggestion'} · ${new Date(ts).toLocaleTimeString()}</div>
        <pre></pre>
        <div class="actions">
          <button class="dismiss">dismiss</button>
          <button class="apply">apply</button>
        </div>`;
      card.querySelector('pre').textContent = s.text || '';
      card.querySelector('.dismiss').onclick = () => {
        suggestions = suggestions.filter(x => x !== s);
        renderSuggestions();
        send({ kind: 'suggestion_action', action: 'dismiss', id: card.dataset.id });
      };
      card.querySelector('.apply').onclick = () => {
        send({ kind: 'suggestion_action', action: 'apply', id: card.dataset.id });
        card.style.opacity = 0.55;
      };
      feed.append(card);
    }
  }

  function renderTranscript() {
    const feed = $('transcript');
    feed.innerHTML = '';
    if (!transcript.length) {
      feed.textContent = 'No transcript yet.';
      return;
    }
    for (const t of transcript.slice(-60)) {
      const line = document.createElement('div');
      line.className = 'line ' + (t.channel || 'mixed') + (t.is_final ? ' final' : ' partial');
      line.textContent = '[' + (t.channel || '?') + (t.speaker ? ':' + t.speaker : '') + '] ' + (t.text || '');
      feed.append(line);
    }
    feed.scrollTop = feed.scrollHeight;
  }

  function renderSessions(sessions) {
    const sel = $('session-select');
    const cur = sel.value;
    sel.innerHTML = '<option value="">— pick session —</option>';
    for (const s of sessions || []) {
      const o = document.createElement('option');
      o.value = s.session_id;
      o.textContent = (s.meeting_id || s.session_id).slice(0, 32) + (s.is_active ? ' •' : '');
      sel.append(o);
    }
    if (cur) sel.value = cur;
  }

  // ---------- Status / WebSocket ----------

  async function refreshStatus() {
    try {
      const j = await call('/api/sentinel/status');
      setStatus(true);
      $('state-out').textContent = j.state ? JSON.stringify(j.state, null, 2) : 'no session';
      if (j.state && j.state.session_id) {
        const sel = $('session-select');
        if (!sel.querySelector(`option[value="${CSS.escape(j.state.session_id)}"]`)) {
          const o = document.createElement('option');
          o.value = j.state.session_id;
          o.textContent = (j.state.meeting_id || j.state.session_id).slice(0, 32);
          sel.append(o);
          if (!sel.value) sel.value = j.state.session_id;
        }
      }
    } catch (e) { setStatus(false); }
  }

  function wsUrl() {
    return HOST.replace(/^http/, 'ws') + '/ws' + (KEY ? '?api_key=' + encodeURIComponent(KEY) : '');
  }

  function connect() {
    HOST = ($('host-input').value || HOST).replace(/\/$/, '');
    KEY = $('key-input').value || KEY;
    localStorage.setItem('sentinel_host', HOST);
    if (KEY) localStorage.setItem('sentinel_key', KEY);

    try { if (ws) ws.close(); } catch (_) {}
    try {
      ws = new WebSocket(wsUrl());
      ws.onopen = () => {
        setStatus(true);
        refreshStatus();
        send({ kind: 'request_status' });
        send({ kind: 'list_sessions' });
        send({ kind: 'context_toggle', contexts: settings.contexts });
        send({ kind: 'overlay_hello', client: 'webui' });
      };
      ws.onclose = () => { setStatus(false); setTimeout(connect, 4000); };
      ws.onerror = () => {};
      ws.onmessage = (ev) => {
        let m; try { m = JSON.parse(ev.data); } catch { return; }
        switch (m.kind || m.type) {
          case 'suggestion':
            suggestions.unshift({ id: m.id, mode: m.mode, text: m.text, ts: m.ts });
            suggestions = suggestions.slice(0, 50);
            renderSuggestions();
            break;
          case 'transcript':
          case 'transcript_delta':
            transcript.push({
              text: m.text, channel: m.channel || 'mixed',
              speaker: m.speaker, ts: m.ts, is_final: m.is_final !== false,
            });
            transcript = transcript.slice(-300);
            renderTranscript();
            break;
          case 'status':
            if (m.state) {
              $('state-out').textContent = JSON.stringify(m.state, null, 2);
              applyStatusToAudio(m.state);
            }
            break;
          case 'sessions':
            renderSessions(m.sessions || []);
            break;
          case 'audio_sources_list':
            renderAudioSources(m.sources || []);
            break;
          case 'audio_level':
            if (typeof m.level === 'number') {
              const fill = $('volume-fill');
              if (fill) fill.style.width = Math.max(0, Math.min(100, Math.round(m.level * 100))) + '%';
            }
            break;
          case 'pong': /* ignore */ break;
          case 'error':
            console.warn('sentinel error:', m.message || m);
            break;
        }
      };
    } catch (e) { setTimeout(connect, 4000); }
  }

  // ---------- Tool dispatch helpers ----------

  async function curate() {
    const meeting_id = $('curate-meeting-id').value.trim();
    if (!meeting_id) return alert('meeting id required');
    const goal = $('curate-goal').value;
    const persona = $('curate-persona').value;
    const raw = $('curate-docs').value;
    const docs = raw
      ? raw.split('---').map((t, i) => ({ title: 'Doc ' + (i + 1), inline: t.trim() })).filter(d => d.inline)
      : [];
    const args = { meeting_id, goal, persona, docs };
    // Try the WebSocket tool channel first; fall back to surfacing the
    // copy-into-Hermes payload (since the overlay can't unilaterally curate
    // without the agent loop).
    const sent = send({ kind: 'tool_call', tool: 'sentinel_curate', args });
    $('curate-out').textContent = JSON.stringify({
      ok: true,
      sent_via_ws: sent,
      run_in_hermes: { tool: 'sentinel_curate', args },
    }, null, 2);
  }
  function clearCurate() {
    ['curate-meeting-id','curate-goal','curate-persona','curate-docs'].forEach(id => $(id).value = '');
    $('curate-out').textContent = '—';
  }

  async function start() {
    const backend = $('start-backend').value;
    settings.backend = backend; saveSettings(true);
    const sent = send({ kind: 'tool_call', tool: 'sentinel_start', args: { backend } });
    if (!sent) {
      try { await call('/api/sentinel/overlay', { action: 'show' }); } catch (_) {}
    }
    $('state-out').textContent = 'starting (' + backend + ')…';
    setTimeout(refreshStatus, 600);
  }

  async function stop() {
    const sent = send({ kind: 'tool_call', tool: 'sentinel_stop', args: { auto_extract: true } });
    if (!sent) {
      try { await call('/api/sentinel/overlay', { action: 'hide' }); } catch (_) {}
    }
    setTimeout(refreshStatus, 600);
  }

  async function suggest() {
    const q = $('suggest-query').value.trim();
    if (!q) return;
    const mode = $('suggest-mode').value;
    try {
      const j = await call('/api/sentinel/suggest', { query: q, mode });
      if (j && j.text) {
        suggestions.unshift({ id: j.id || crypto.randomUUID(), mode, text: j.text, ts: Date.now() / 1000 });
        renderSuggestions();
      }
      $('suggest-query').value = '';
    } catch (e) { alert('suggest failed: ' + e.message); }
  }

  async function history() {
    const q = $('history-query').value.trim();
    if (!q) return;
    // Try the canonical sentinel_history tool over WS first.
    const sent = send({ kind: 'tool_call', tool: 'sentinel_history', args: { query: q, limit: 10 } });
    if (sent) {
      $('history-out').textContent = '… searching (results arrive over WS)';
      return;
    }
    // Fallback: search the in-memory transcript buffer.
    try {
      const j = await call('/api/sentinel/transcript?last=400');
      const matches = (j.transcript || []).filter(t => (t.text || '').toLowerCase().includes(q.toLowerCase()));
      $('history-out').textContent = matches.length
        ? matches.map(m => '• ' + (m.text || '')).join('\n')
        : '(no matches)';
    } catch (e) { $('history-out').textContent = 'error: ' + e.message; }
  }

  // ---------- Settings ----------

  function saveSettings(silent) {
    settings.backend = $('cfg-backend').value;
    settings.audioSource = $('cfg-audio-source').value;
    settings.vad = parseInt($('cfg-vad').value, 10);
    settings.mode = $('cfg-mode').value;
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
    send({ kind: 'settings_update', settings });
    if (!silent) $('settings-out').textContent = JSON.stringify(settings, null, 2);
  }
  function openOverlay() {
    const url = HOST + '/?sentinel=' + encodeURIComponent(HOST) + (KEY ? '&api_key=' + encodeURIComponent(KEY) : '');
    window.open(url, '_blank', 'noopener');
  }

  // ---------- Context toggles ----------

  function bindToggles() {
    document.querySelectorAll('#ctx-toggles .toggle').forEach(t => {
      t.classList.toggle('on', !!settings.contexts[t.dataset.ctx]);
      t.addEventListener('click', () => {
        const k = t.dataset.ctx;
        settings.contexts[k] = !settings.contexts[k];
        t.classList.toggle('on', settings.contexts[k]);
        localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
        send({ kind: 'context_toggle', contexts: settings.contexts });
      });
    });
  }

  // ---------- Mic capture (matches simple overlay) ----------

  let micStream = null, audioCtx = null, micNode = null, processor = null;
  const TARGET_SR = 16000;

  function pcm16FromFloat32(input) {
    const out = new Int16Array(input.length);
    for (let i = 0; i < input.length; i++) {
      const s = Math.max(-1, Math.min(1, input[i]));
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return out;
  }
  function downsampleTo16k(buf, srcRate) {
    if (srcRate === TARGET_SR) return buf;
    const ratio = srcRate / TARGET_SR;
    const newLen = Math.round(buf.length / ratio);
    const out = new Float32Array(newLen);
    let off = 0, idx = 0;
    while (off < newLen) {
      const next = Math.round((off + 1) * ratio);
      let acc = 0, n = 0;
      for (let i = idx; i < next && i < buf.length; i++) { acc += buf[i]; n++; }
      out[off] = n ? acc / n : 0;
      off++; idx = next;
    }
    return out;
  }
  function ab2b64(buf) {
    const bytes = new Uint8Array(buf);
    let bin = '';
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    return btoa(bin);
  }

  async function toggleMic() {
    if (micStream) {
      try { processor && processor.disconnect(); } catch (_) {}
      try { micNode && micNode.disconnect(); } catch (_) {}
      try { audioCtx && audioCtx.close(); } catch (_) {}
      try { micStream && micStream.getTracks().forEach(t => t.stop()); } catch (_) {}
      processor = null; micNode = null; audioCtx = null; micStream = null;
      $('mic-btn').classList.remove('active');
      send({ kind: 'remote_audio_stop' });
      return;
    }
    try {
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, sampleRate: TARGET_SR, echoCancellation: true, noiseSuppression: true },
        video: false,
      });
    } catch (e) {
      alert('Microphone permission denied: ' + e.message);
      return;
    }
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const srcRate = audioCtx.sampleRate;
    micNode  = audioCtx.createMediaStreamSource(micStream);
    processor = audioCtx.createScriptProcessor(2048, 1, 1);
    processor.onaudioprocess = (e) => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      const f32 = e.inputBuffer.getChannelData(0);
      const ds  = downsampleTo16k(f32, srcRate);
      const pcm = pcm16FromFloat32(ds);
      send({
        kind: 'audio_chunk', type: 'audio_chunk', channel: 'mic',
        sample_rate: TARGET_SR, data: ab2b64(pcm.buffer), ts: Date.now(),
      });
    };
    micNode.connect(processor);
    processor.connect(audioCtx.destination);
    $('mic-btn').classList.add('active');
    send({ kind: 'remote_audio_start', sample_rate: TARGET_SR, channels: 1 });
  }

  // ---------- Bootstrap ----------

  document.addEventListener('DOMContentLoaded', () => {
    $('host-input').value = HOST;
    $('key-input').value  = KEY;
    $('cfg-backend').value      = settings.backend;
    $('cfg-audio-source').value = settings.audioSource;
    $('cfg-vad').value          = settings.vad;
    $('cfg-mode').value         = settings.mode;
    $('suggest-mode').value     = settings.mode;
    $('start-backend').value    = settings.backend;
    bindToggles();

    $('suggest-query').addEventListener('keydown', e => { if (e.key === 'Enter') suggest(); });
    $('history-query').addEventListener('keydown', e => { if (e.key === 'Enter') history(); });
    $('refresh-sessions').addEventListener('click', () => send({ kind: 'list_sessions' }));
    $('session-select').addEventListener('change', () => {
      activeSessionId = $('session-select').value;
      send({ kind: 'session_switch', session_id: activeSessionId });
    });
    $('mic-btn').addEventListener('click', toggleMic);

    // Audio bar wiring (mute mic / speaker + refresh sources).
    const micMute = $('mic-mute-btn');
    if (micMute) micMute.addEventListener('click', () => {
      muteState.mic = !muteState.mic;
      applyMuteUI('mic');
      sendAudioMute('mic', muteState.mic);
    });
    const spkMute = $('speaker-mute-btn');
    if (spkMute) spkMute.addEventListener('click', () => {
      muteState.system = !muteState.system;
      applyMuteUI('system');
      sendAudioMute('system', muteState.system);
    });
    const refreshSrc = $('refresh-sources-btn');
    if (refreshSrc) refreshSrc.addEventListener('click', (e) => {
      e.preventDefault(); e.stopPropagation();
      sendAudioLevelSnap();
    });
    applyMuteUI('mic'); applyMuteUI('system');

    setInterval(refreshStatus, 5000);
    connect();
  });

  return {
    connect, curate, clearCurate, start, stop, suggest, history, saveSettings, openOverlay,
    sendAudioMute, sendAudioSourceFilter, sendAudioLevelSnap,
  };
})();
