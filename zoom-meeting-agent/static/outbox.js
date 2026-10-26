// Delivery is claimed once and acknowledged only after the SDK callback.
// A lost/ambiguous callback is never retried automatically in the meeting.
window.__zbCreateOutbox = function (sdk, log, timeoutMs = 15000) {
  let busy = false;
  const pendingAcks = new Map();
  async function post(path, value) {
    return fetch(path, {method: 'POST', body: JSON.stringify(value)});
  }
  async function acknowledge(id, state) {
    const response = await post('/outbox/ack', {id, state});
    if (!response.ok) throw Error('Delivery acknowledgement failed');
    pendingAcks.delete(id);
  }
  function send(item) {
    return new Promise(resolve => {
      let completed = false;
      const finish = state => {
        if (completed) return;
        completed = true;
        clearTimeout(timer);
        resolve(state);
      };
      const timer = setTimeout(() => finish('uncertain'), timeoutMs);
      try {
        sdk.sendChat({message: item.message,
          success: () => {
            Promise.resolve(log('chat_sent', {m: item.message})).catch(() => {});
            finish('sent');
          },
          error: () => finish('failed')});
      } catch (_) { finish('uncertain'); }
    });
  }
  return async function poll() {
    if (busy) return;
    busy = true;
    try {
      for (const [id, state] of pendingAcks) await acknowledge(id, state);
      const response = await fetch('/outbox');
      if (!response.ok) throw Error('Outbox unavailable');
      for (const item of await response.json()) {
        if (item.state !== 'queued') continue;
        const claim = await post('/outbox/claim', {id: item.id});
        if (claim.status === 409) continue;
        if (!claim.ok) throw Error('Delivery claim failed');
        const state = await send(item);
        pendingAcks.set(item.id, state);
        await acknowledge(item.id, state);
      }
    } catch (_) {
      // Retain acknowledgements for retry, but never repeat the SDK send.
    } finally { busy = false; }
  };
};
