// Подключается до загрузки SDK. Всё, что SDK выводит в динамики (AudioContext.destination
// или <audio> с WebRTC-потоком), заводим в общий микшер и режем на самостоятельные
// webm-куски по CHUNK_MS — каждый кусок отдельно уходит на /audio для whisper.
(() => {
  const CHUNK_MS = 20000;
  const mixCtx = new AudioContext();
  const mixDest = mixCtx.createMediaStreamDestination();
  const seen = new WeakSet();

  const addStream = (stream) => {
    if (!stream || seen.has(stream) || !stream.getAudioTracks().length) return;
    seen.add(stream);
    try { mixCtx.createMediaStreamSource(stream).connect(mixDest); } catch (e) {}
  };

  // 1) WASM-путь: SDK играет через свой AudioContext — дублируем всё, что идёт в destination
  const taps = new WeakMap();
  const origConnect = AudioNode.prototype.connect;
  AudioNode.prototype.connect = function (target, ...rest) {
    const res = origConnect.call(this, target, ...rest);
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
    } catch (e) {}
    return res;
  };

  // 2) WebRTC-путь: удалённые аудиотреки
  const OrigPC = window.RTCPeerConnection;
  window.RTCPeerConnection = function (...args) {
    const pc = new OrigPC(...args);
    pc.addEventListener('track', (ev) => {
      if (ev.track.kind === 'audio') addStream(ev.streams[0] || new MediaStream([ev.track]));
    });
    return pc;
  };
  window.RTCPeerConnection.prototype = OrigPC.prototype;

  // 3) <audio>.srcObject
  const desc = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'srcObject');
  Object.defineProperty(HTMLMediaElement.prototype, 'srcObject', {
    set(v) { if (v instanceof MediaStream) addStream(v); desc.set.call(this, v); },
    get() { return desc.get.call(this); },
  });

  const recordChunk = () => {
    if (mixCtx.state !== 'running') mixCtx.resume();
    const rec = new MediaRecorder(mixDest.stream, {mimeType: 'audio/webm;codecs=opus'});
    const parts = [];
    rec.ondataavailable = (e) => e.data.size && parts.push(e.data);
    rec.onstop = () => {
      const blob = new Blob(parts, {type: 'audio/webm'});
      if (blob.size > 2000) fetch('/audio?ts=' + Date.now(), {method: 'POST', body: blob});
    };
    rec.start();
    setTimeout(() => rec.stop(), CHUNK_MS);
  };
  setTimeout(() => { recordChunk(); setInterval(recordChunk, CHUNK_MS); }, 3000);
})();
