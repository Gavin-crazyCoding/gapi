/* gapi browser/environment fingerprint.
 *
 * Zero-dependency, same-origin (CSP script-src 'self' forbids CDN libs).
 * Collects stable, non-PII environment signals and hashes them with SHA-256.
 * The server only ever sees the hash, never the raw signals.
 */
(function () {
  'use strict';

  async function sha256(text) {
    var buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
    return Array.from(new Uint8Array(buf))
      .map(function (b) { return b.toString(16).padStart(2, '0'); })
      .join('');
  }

  async function collect() {
    var n = navigator;
    var s = window.screen;
    var tz = 'UTC';
    try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch (e) { /* ignore */ }

    // Canvas adds a small GPU/font rendering signal without storing pixels.
    var canvasSig = '';
    try {
      var c = document.createElement('canvas');
      c.width = 220; c.height = 40;
      var ctx = c.getContext('2d');
      ctx.textBaseline = 'top';
      ctx.font = "16px 'Arial', sans-serif";
      ctx.fillStyle = '#f60';
      ctx.fillRect(0, 0, 80, 20);
      ctx.fillStyle = '#069';
      ctx.fillText('gapi,fingerprint,1.0', 4, 4);
      canvasSig = c.toDataURL().slice(-64);
    } catch (e) { canvasSig = 'no-canvas'; }

    // Only stable signals: window size, availability, devicePixelRatio and
    // the DST-dependent offset all drift on the SAME browser (resize, zoom,
    // taskbar toggle, daylight saving) and used to lock their owner out.
    var signals = [
      n.userAgent,
      (n.languages || []).join(','),
      n.platform || '',
      String(n.hardwareConcurrency || ''),
      String(n.deviceMemory || ''),
      String(n.maxTouchPoints || ''),
      s.width, s.height, s.colorDepth,
      tz,
      canvasSig,
    ].join('|');

    return sha256(signals);
  }

  window.GapiFingerprint = { collect: collect };
})();
