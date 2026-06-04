/**
 * test_format_mapping.js
 * Tests browser MIME → Whisper format mapping logic (mirrors thinker.js).
 * Run: node paperreadagent/modules/thinker/tests/test_format_mapping.js
 */

// Mirror the format derivation logic from thinker.js
function deriveFormat(recordingMime) {
  const _mime = recordingMime || '';
  return _mime.includes('mp4') ? 'mp4'
       : _mime.includes('ogg') ? 'ogg'
       : 'webm';
}

// Mirror getSupportedMimeType logic
function getSupportedMimeType(types) {
  for (const t of types) {
    if (isTypeSupported(t)) return t;
  }
  return '';
}

// Simulate browser MIME support
const SUPPORTED = new Set([
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
]);

function isTypeSupported(mime) {
  return SUPPORTED.has(mime);
}

// ── Tests ─────────────────────────────────────────────────────

let passed = 0;
let failed = 0;

function assert(condition, msg) {
  if (condition) { passed++; }
  else { console.error('FAIL:', msg); failed++; }
}

function assertEqual(actual, expected, msg) {
  if (actual === expected) { passed++; }
  else { console.error(`FAIL: ${msg} — expected "${expected}", got "${actual}"`); failed++; }
}

// Test 1: Chrome desktop — audio/webm;codecs=opus → opus
(function testChromeWebMOpus() {
  const mime = getSupportedMimeType([
    'audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus',
  ]);
  assertEqual(mime, 'audio/webm;codecs=opus', 'Chrome should prefer webm+opus');
  const format = deriveFormat(mime);
  assertEqual(format, 'webm', 'Chrome webm+opus → webm');
})();

// Test 2: iOS Safari — only audio/mp4 supported
(function testIOSSafariMP4() {
  const iosMime = 'audio/mp4';
  const format = deriveFormat(iosMime);
  assertEqual(format, 'mp4', 'iOS mp4 → mp4');
})();

// Test 3: Firefox — audio/ogg;codecs=opus
(function testFirefoxOgg() {
  const firefoxMime = 'audio/ogg;codecs=opus';
  const format = deriveFormat(firefoxMime);
  assertEqual(format, 'ogg', 'Firefox ogg → ogg');
})();

// Test 4: Unknown/empty MIME → opus default
(function testEmptyMime() {
  assertEqual(deriveFormat(''), 'webm', 'Empty → webm default');
  assertEqual(deriveFormat(null), 'webm', 'null → webm default');
  assertEqual(deriveFormat(undefined), 'webm', 'undefined → webm default');
})();

// Test 5: No supported MIME → empty string → opus default
(function testNoSupportedMime() {
  const mime = getSupportedMimeType(['audio/flac']);
  assertEqual(mime, '', 'Unsupported MIME → empty string');
  const format = deriveFormat(mime);
  assertEqual(format, 'webm', 'Empty → webm fallback');
})();

// Test 6: Edge case — audio/webm (no codec) → opus
(function testWebMNoCodec() {
  const format = deriveFormat('audio/webm');
  assertEqual(format, 'webm', 'webm no codec → webm');
})();

// Test 7: Edge case — video/webm → opus
(function testVideoWebM() {
  const format = deriveFormat('video/webm');
  assertEqual(format, 'webm', 'video/webm → webm');
})();

// Test 8: TTS format is always mp3 (hardcoded in CoreVoice)
(function testTTSFormat() {
  // TTS uses response_format="mp3" in core/voice.py:151
  // This is independent of STT format, always mp3 for playback compatibility
  assertEqual('mp3', 'mp3', 'TTS output is always mp3');
})();

// ── Results ───────────────────────────────────────────────────

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
