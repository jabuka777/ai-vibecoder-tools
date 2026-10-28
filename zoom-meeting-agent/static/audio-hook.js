// Capture is local. Web mode uses exposed bindings; SDK mode uses its own origin.
(() => {
  if (window.__zbStopCapture) return;
  // This nonce lives only in this document. A fresh hook after navigation must
  // not be mistaken for a successful flush of the previous recorder.
  window.__zbCaptureGeneration = crypto.randomUUID();
  const CHUNK_MS = 20000;
  const MIN_FINAL_MS = 500;
  const mixCtx = new AudioContext();
  const mixDest = mixCtx.createMediaStreamDestination();
  // Keep the destination rendering silent frames when a recording starts or
  // all meeting inputs are disconnected. An input-free WebAudio graph can
  // otherwise leave MediaRecorder with a header-only, undecodable WebM.
  const silence = mixCtx.createConstantSource();
  silence.offset.value = 0;
  silence.connect(mixDest);
  silence.start();
  const streams = new Set();
  const inputs = new Set();
  const seen = new WeakSet();
  const pending = new Set();
  let stopped = false, captureFailed = false, recorder = null, timer = null;
  const local = location.hostname === '127.0.0.1';
  const event = (kind, data) => {
    if (window.__zbEvent) return window.__zbEvent(kind, data);
    if (local) return fetch('/event', {method: 'POST', body: JSON.stringify({kind, data})});
    return Promise.resolve();
  };
  const sourceActive = () => [...streams].some(s => s.getAudioTracks().some(t => t.readyState === 'live'));
  const addStream = stream => {
    if (stopped || !stream || seen.has(stream) || !stream.getAudioTracks().length) return;
    seen.add(stream);
    try {
      const input = mixCtx.createMediaStreamSource(stream);
      input.connect(mixDest);
      inputs.add(input);
      streams.add(stream);
      if (recorder) recorder.__hadSource = true;
      event('audio_source', {active: sourceActive()}).catch(() => {});
      for (const track of stream.getAudioTracks()) track.addEventListener('ended', () => {
        event('audio_source', {active: sourceActive()}).catch(() => {});
      });
    } catch (_) { captureFailed = true; event('capture_error', {stage: 'attach'}).catch(() => {}); }
  };
  const taps = new WeakMap();
  const origConnect = AudioNode.prototype.connect;
  AudioNode.prototype.connect = function (target, ...rest) {
    const result = origConnect.call(this, target, ...rest);
    try {
      if (target instanceof AudioDestinationNode && this.context !== mixCtx) {
        let tap = taps.get(this.context);
        if (!tap) {
          tap = this.context.createMediaStreamDestination();
          taps.set(this.context, tap);
          addStream(tap.stream);
        }
        origConnect.call(this, tap);
      }
    } catch (_) {}
    return result;
  };
  const OrigPC = window.RTCPeerConnection;
  window.RTCPeerConnection = function (...args) {
    const pc = new OrigPC(...args);
    pc.addEventListener('track', e => {
      if (e.track.kind === 'audio') addStream(e.streams[0] || new MediaStream([e.track]));
    });
    return pc;
  };
  window.RTCPeerConnection.prototype = OrigPC.prototype;
  const desc = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'srcObject');
  Object.defineProperty(HTMLMediaElement.prototype, 'srcObject', {
    set(value) { if (value instanceof MediaStream) addStream(value); desc.set.call(this, value); },
    get() { return desc.get.call(this); },
  });
  const upload = async (blob, hadSource) => {
    if (!blob.size || !hadSource) return;
    const stamp = Date.now();
    if (window.__zbAudio) {
      const bytes = new Uint8Array(await blob.arrayBuffer());
      let binary = '';
      for (let i = 0; i < bytes.length; i += 0x8000)
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
      await window.__zbAudio(stamp, btoa(binary));
    } else if (local) {
      const response = await fetch('/audio?ts=' + stamp, {method: 'POST', body: blob});
      if (!response.ok) throw Error('capture upload failed');
    } else {
      throw Error('capture transport missing');
    }
  };
  let stoppedChunk = Promise.resolve();
  const record = () => {
    if (stopped) return;
    if (mixCtx.state !== 'running') mixCtx.resume().catch(() => {});
    const current = recorder = new MediaRecorder(mixDest.stream, {mimeType: 'audio/webm;codecs=opus'});
    current.__hadSource = sourceActive();
    const parts = [];
    current.ondataavailable = e => { if (e.data.size) parts.push(e.data); };
    stoppedChunk = new Promise(resolve => {
      current.onstop = () => {
        const promise = upload(new Blob(parts, {type: 'audio/webm'}), current.__hadSource);
        pending.add(promise);
        promise.catch(() => { captureFailed = true; return event('capture_error', {stage: 'upload'}).catch(() => {}); }).finally(() => pending.delete(promise));
        resolve();
        if (!stopped) record();
      };
    });
    current.__startedAt = performance.now();
    current.start();
    timer = setTimeout(() => current.stop(), CHUNK_MS);
  };
  window.__zbStopCapture = async () => {
    stopped = true;
    clearTimeout(timer);
    // Stop taking meeting audio immediately. A newly created recorder still
    // needs a few silent frames to finish a decodable WebM on macOS Chromium.
    for (const input of inputs) {
      try { input.disconnect(mixDest); } catch (_) {}
    }
    inputs.clear();
    if (recorder && recorder.state !== 'inactive') {
      const remaining = Math.max(0, MIN_FINAL_MS - (performance.now() - recorder.__startedAt));
      if (remaining) await new Promise(resolve => setTimeout(resolve, remaining));
      if (recorder.state !== 'inactive') recorder.stop();
    }
    await stoppedChunk;
    await Promise.all([...pending]);
    try { silence.stop(); silence.disconnect(); } catch (_) {}
    if (captureFailed) throw Error('One or more audio chunks failed to upload');
  };
  timer = setTimeout(record, 1000);
})();
