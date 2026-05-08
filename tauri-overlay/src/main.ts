// Tauri overlay — Svelte entry point.
//
// Mounts <App /> into #app and bootstraps the WS connection to the sentinel
// backend. The sentinel host/port is read from the URL query string so the
// same compiled binary works against any backend.

import App from './App.svelte';

const target = document.getElementById('app');
if (!target) {
  throw new Error('overlay: #app mount target missing');
}

new App({ target });
