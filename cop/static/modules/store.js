/* ==========================================================
   cop/static/modules/store.js — Tiny pub/sub state container

   Classic script exposing window.NIZAM.store. Once the monolithic app.js
   is split into panel modules, each panel will register a subscriber
   here to redraw when its slice changes, instead of reaching into the
   sibling module's internals.

   Why pub/sub instead of a reactive framework: the map and panel render
   loop is driven explicitly by WebSocket messages, not by state
   mutation. We don't need reactivity — we need "tell me when this
   value changed" and "keep me out of the other panels' business".

   Usage:
     const { store } = window.NIZAM;
     store.set("wsConnected", true);
     const val = store.get("wsConnected");
     const unsub = store.subscribe("wsConnected", (ok) => updateDot(ok));
     // later: unsub();

   A misbehaving subscriber that throws does not abort the broadcast:
   the error is logged and the next subscriber still runs. This keeps
   one panel from breaking the rest of the UI.
   ========================================================== */

(function () {
  "use strict";

  window.NIZAM = window.NIZAM || {};

  class Store {
    constructor() {
      this._state = new Map();
      this._subs  = new Map();   // key → Set<listener>
    }

    get(key) { return this._state.get(key); }

    set(key, value) {
      this._state.set(key, value);
      this._notify(key, value);
    }

    /**
     * Reducer-style update for Maps/Sets/Arrays: pass a fn that takes
     * the current value and returns the next one. Guarantees exactly
     * one notification per logical change.
     */
    update(key, fn) {
      const cur = this._state.get(key);
      const next = fn(cur);
      this._state.set(key, next);
      this._notify(key, next);
    }

    /** Subscribe to a key. Returns an unsubscribe function. */
    subscribe(key, listener) {
      let set = this._subs.get(key);
      if (!set) {
        set = new Set();
        this._subs.set(key, set);
      }
      set.add(listener);
      return () => set.delete(listener);
    }

    _notify(key, value) {
      const set = this._subs.get(key);
      if (!set) return;
      for (const fn of set) {
        try {
          fn(value, key);
        } catch (err) {
          // eslint-disable-next-line no-console
          console.error("[store] subscriber threw:", err);
        }
      }
    }

    /** Wipe all state. Subscribers are kept so reused panels stay wired. */
    clear() {
      this._state.clear();
    }

    /** Debug helper: flat snapshot of all keys/values. */
    snapshot() {
      return Object.fromEntries(this._state.entries());
    }
  }

  window.NIZAM.store = new Store();

  // DevTools introspection alias — production code must use NIZAM.store.
  window.__NIZAM_STORE__ = window.NIZAM.store;
})();
