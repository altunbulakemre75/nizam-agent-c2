/* ==========================================================
   cop/static/modules/ws-client.js — WebSocket connection with
   exponential back-off and clean reconnect semantics.

   Classic script loaded before app.js. Exposes window.NIZAM.wsClient.

   Usage (from app.js):
     const { wsClient } = window.NIZAM;
     wsClient.connect(
       url,
       ev  => CopEngine.onEvent(ev),          // called per WS message
       txt => setStatus(txt),                  // human-readable status
       ok  => { UI.wsConnected = ok; },        // true on open, false on close
     );

   Back-off: 500 ms → 1 s → 2 s → … → 30 s (capped), resets to 500 ms
   on a successful handshake.

   Disconnecting via wsClient.disconnect() kills the socket and prevents
   any further reconnect attempts.
   ========================================================== */

(function () {
  "use strict";

  window.NIZAM = window.NIZAM || {};

  const _DELAY_BASE = 500;
  const _DELAY_MAX  = 30_000;

  class WsClient {
    constructor() {
      this._delay        = _DELAY_BASE;
      this._ws           = null;
      this._url          = null;
      this._onMsg        = null;
      this._onStatus     = null;
      this._onConnChange = null;
      this._stopped      = false;
      /** true while the socket is open and healthy */
      this.connected     = false;
    }

    /** current reconnect backoff delay in ms (read-only; exposed for tests) */
    get delay() { return this._delay; }

    /** reference to the live WebSocket, or null — diagnostic only */
    get socket() { return this._ws; }

    /**
     * Open (or re-open) the WebSocket.
     *
     * @param {string}   url          - full ws:// / wss:// URL
     * @param {Function} onMessage    - called with the parsed JSON object per message
     * @param {Function} onStatus     - called with a human-readable status string
     * @param {Function} [onConnChange] - called with (true|false) on open/close
     */
    connect(url, onMessage, onStatus, onConnChange) {
      this._url          = url;
      this._onMsg        = onMessage      || null;
      this._onStatus     = onStatus       || null;
      this._onConnChange = onConnChange   || null;
      this._stopped      = false;
      this._delay        = _DELAY_BASE;
      this._open();
    }

    /** Permanently close — no further reconnect attempts. */
    disconnect() {
      this._stopped  = true;
      this.connected = false;
      if (this._ws) {
        this._ws.onclose = null;   // prevent the reconnect loop
        this._ws.close();
        this._ws = null;
      }
    }

    /* ── private ──────────────────────────────────────────── */

    _open() {
      if (this._stopped) return;
      this._onStatus?.("WS: connecting\u2026");
      let ws;
      try {
        ws = new WebSocket(this._url);
      } catch (e) {
        this._scheduleReconnect("WS: failed");
        return;
      }
      this._ws = ws;

      ws.onopen = () => {
        this._delay    = _DELAY_BASE;
        this.connected = true;
        this._onConnChange?.(true);
        this._onStatus?.("WS: connected");
      };

      ws.onclose = () => {
        this.connected = false;
        this._onConnChange?.(false);
        this._scheduleReconnect("WS: closed");
      };

      ws.onerror = () => {};

      ws.onmessage = (msg) => {
        let ev;
        try { ev = JSON.parse(msg.data); } catch { return; }
        this._onMsg?.(ev);
      };
    }

    _scheduleReconnect(reason) {
      if (this._stopped) return;
      const delaySec = (this._delay / 1000).toFixed(1);
      this._onStatus?.(`${reason} \u2014 retry in ${delaySec}s`);
      setTimeout(() => this._open(), this._delay);
      this._delay = Math.min(this._delay * 2, _DELAY_MAX);
    }
  }

  window.NIZAM.wsClient = new WsClient();
})();
