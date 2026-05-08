<script lang="ts">
  import { onDestroy, onMount } from 'svelte';

  // Resolve sentinel host/port + optional API key from URL or env defaults.
  const params = new URLSearchParams(window.location.search);
  const SENTINEL = (
    params.get('sentinel') ||
    (window as any).SENTINEL_HOST ||
    'http://127.0.0.1:18765'
  ).replace(/\/$/, '');
  const API_KEY = params.get('api_key') || (window as any).SENTINEL_API_KEY || '';

  type Suggestion = { id: string; mode: string; text: string; ts: number };
  type TranscriptLine = { text: string; channel: string; ts: number; is_final: boolean };

  let connected = false;
  let suggestions: Suggestion[] = [];
  let transcript: TranscriptLine[] = [];
  let query = '';
  let mode = 'freeform';
  let sending = false;
  let ws: WebSocket | null = null;

  function authHeaders() {
    return API_KEY ? { Authorization: `Bearer ${API_KEY}` } : {};
  }

  function connect() {
    try {
      const wsUrl = SENTINEL.replace(/^http/, 'ws') + '/ws' + (API_KEY ? `?api_key=${encodeURIComponent(API_KEY)}` : '');
      ws = new WebSocket(wsUrl);
      ws.onopen = () => { connected = true; ws?.send(JSON.stringify({ kind: 'request_status' })); };
      ws.onclose = () => { connected = false; setTimeout(connect, 3000); };
      ws.onerror = () => { /* close handler retries */ };
      ws.onmessage = (ev) => {
        let msg: any;
        try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg.kind === 'suggestion') {
          suggestions = [{ id: msg.id, mode: msg.mode, text: msg.text, ts: msg.ts }, ...suggestions].slice(0, 50);
        } else if (msg.kind === 'transcript') {
          transcript = [...transcript, msg].slice(-200);
        }
      };
    } catch (_) { setTimeout(connect, 3000); }
  }

  async function suggest() {
    if (!query.trim() || sending) return;
    sending = true;
    try {
      const r = await fetch(`${SENTINEL}/api/sentinel/suggest`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ query, mode }),
      });
      const j = await r.json();
      if (j && j.text) {
        suggestions = [{ id: j.id || crypto.randomUUID(), mode, text: j.text, ts: Date.now() / 1000 }, ...suggestions].slice(0, 50);
      }
      query = '';
    } catch (e) {
      console.error('suggest failed', e);
    } finally {
      sending = false;
    }
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); suggest(); }
  }

  onMount(connect);
  onDestroy(() => { ws?.close(); });
</script>

<main class="overlay" data-connected={connected}>
  <header>
    <span class="dot" class:live={connected}></span>
    <span class="label">Sentinel</span>
    <span class="host">{SENTINEL.replace(/^https?:\/\//, '')}</span>
  </header>

  <section class="suggestions">
    {#each suggestions as s (s.id)}
      <article class="card">
        <div class="mode">{s.mode}</div>
        <pre>{s.text}</pre>
      </article>
    {:else}
      <div class="empty">No suggestions yet — ask below.</div>
    {/each}
  </section>

  <section class="transcript">
    {#each transcript.slice(-6) as t (t.ts + t.text)}
      <div class="line" class:final={t.is_final}>[{t.channel}] {t.text}</div>
    {/each}
  </section>

  <footer>
    <select bind:value={mode}>
      <option value="freeform">freeform</option>
      <option value="talking_points">talking points</option>
      <option value="reply">reply</option>
      <option value="recap">recap</option>
      <option value="objection">objection</option>
    </select>
    <input bind:value={query} on:keydown={onKey} placeholder="Ask the copilot…" />
    <button on:click={suggest} disabled={sending || !query.trim()}>Ask</button>
  </footer>
</main>

<style>
  :global(body) { background: transparent; margin: 0; font-family: ui-sans-serif, system-ui, sans-serif; color: #f1f5f9; }
  .overlay { display: flex; flex-direction: column; height: 100vh; padding: 8px; box-sizing: border-box;
    background: rgba(10, 14, 22, 0.85); border-radius: 12px; backdrop-filter: blur(10px); border: 1px solid rgba(148, 163, 184, 0.2); }
  header { display: flex; align-items: center; gap: 8px; font-size: 12px; padding: 4px 6px; }
  header .dot { width: 8px; height: 8px; border-radius: 50%; background: #ef4444; }
  header .dot.live { background: #22c55e; }
  header .label { font-weight: 600; }
  header .host { margin-left: auto; opacity: 0.6; }
  .suggestions { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; padding: 6px 4px; }
  .empty { opacity: 0.5; font-size: 12px; padding: 12px 6px; }
  .card { background: rgba(34, 197, 94, 0.08); border-left: 3px solid #22c55e; padding: 6px 8px; border-radius: 6px; }
  .card .mode { font-size: 10px; opacity: 0.7; text-transform: uppercase; letter-spacing: 0.5px; }
  .card pre { margin: 4px 0 0; white-space: pre-wrap; font-size: 12px; line-height: 1.4; }
  .transcript { font-size: 11px; opacity: 0.6; max-height: 80px; overflow-y: auto; padding: 0 4px; }
  .transcript .line.final { color: #fde68a; opacity: 1; }
  footer { display: flex; gap: 4px; padding-top: 6px; }
  footer select, footer input, footer button {
    background: rgba(15, 23, 42, 0.8); color: inherit; border: 1px solid rgba(148, 163, 184, 0.2);
    padding: 4px 6px; border-radius: 6px; font: inherit; font-size: 12px;
  }
  footer input { flex: 1; }
  footer button { background: #2563eb; border-color: #2563eb; cursor: pointer; }
  footer button:disabled { opacity: 0.5; cursor: not-allowed; }
</style>
