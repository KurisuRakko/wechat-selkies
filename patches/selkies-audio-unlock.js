(function installSelkiesAudioGate() {
  "use strict";

  if (window.wechatAudioGate) return;

  const pendingFactories = [];
  const pendingContexts = new Set();
  const resumePromises = new WeakMap();
  let gestureSeen = false;

  function hasUserActivation() {
    return gestureSeen || Boolean(
      window.navigator &&
      window.navigator.userActivation &&
      window.navigator.userActivation.hasBeenActive
    );
  }

  function pageIsHidden() {
    return document.visibilityState === "hidden";
  }

  function resumeContext(context) {
    if (!context || context.state === "running" || context.state === "closed") {
      return Promise.resolve();
    }

    if (!hasUserActivation() || pageIsHidden()) {
      pendingContexts.add(context);
      return Promise.resolve();
    }

    const existing = resumePromises.get(context);
    if (existing) return existing;

    let attempt;
    try {
      attempt = Promise.resolve(context.resume());
    } catch (_error) {
      pendingContexts.add(context);
      return Promise.resolve();
    }

    const tracked = attempt
      .catch(() => {
        // A transient browser-policy rejection should wait for another trusted
        // gesture, not generate one console error for every audio packet.
        pendingContexts.add(context);
      })
      .finally(() => {
        resumePromises.delete(context);
      });
    resumePromises.set(context, tracked);
    return tracked;
  }

  function createContext(factory) {
    if (typeof factory !== "function") {
      return Promise.reject(new TypeError("AudioContext factory must be a function"));
    }

    if (hasUserActivation() && !pageIsHidden()) {
      try {
        const context = factory();
        void resumeContext(context);
        return Promise.resolve(context);
      } catch (error) {
        return Promise.reject(error);
      }
    }

    return new Promise((resolve, reject) => {
      pendingFactories.push({ factory, resolve, reject });
    });
  }

  function flush() {
    if (!hasUserActivation() || pageIsHidden()) return;

    while (pendingFactories.length) {
      const pending = pendingFactories.shift();
      try {
        const context = pending.factory();
        pending.resolve(context);
        void resumeContext(context);
      } catch (error) {
        pending.reject(error);
      }
    }

    const contexts = Array.from(pendingContexts);
    pendingContexts.clear();
    for (const context of contexts) void resumeContext(context);
  }

  function onUserGesture(event) {
    if (event && event.isTrusted === false) return;
    gestureSeen = true;
    flush();
  }

  for (const type of ["pointerdown", "keydown", "touchend"]) {
    window.addEventListener(type, onUserGesture, { capture: true, passive: true });
  }
  document.addEventListener("visibilitychange", flush);

  window.wechatAudioGate = Object.freeze({
    create: createContext,
    resume: resumeContext,
    isUnlocked: hasUserActivation,
  });
})();
