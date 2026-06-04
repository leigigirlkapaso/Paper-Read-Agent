/**
 * thinker.js — Thinker standalone page logic v2.0
 * Dependencies: Alpine.js, Marked, DOMPurify (loaded by think_page.html)
 */

// ── Global utilities ─────────────────────────────────────────────

function renderMarkdown(text) {
  if (!text) return '';
  const html = marked.parse(text);
  return DOMPurify.sanitize(html);
}

function formatTime(seconds) {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}

async function safeFetch(url, options) {
  const resp = await fetch(url, options);
  if (!resp.ok || resp.headers.get('content-type')?.includes('text/html')) {
    // Auth expired or server error — reload to show login page
    if (resp.status === 403 || resp.headers.get('content-type')?.includes('text/html')) {
      window.location.reload();
    }
    throw new Error(`Request failed: ${resp.status}`);
  }
  return resp;
}

function getSupportedMimeType() {
  const types = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/mp4',
    'audio/ogg;codecs=opus',
  ];
  for (const t of types) {
    if (MediaRecorder.isTypeSupported(t)) return t;
  }
  return '';  // browser default
}

// ── Main page controller ─────────────────────────────────────────

document.addEventListener('alpine:init', () => {
  Alpine.data('thinkerPage', () => ({
    activeTab: 'chat',
  }));
});

// ── Chat Tab ─────────────────────────────────────────────────────

document.addEventListener('alpine:init', () => {
  Alpine.data('thinkerChat', () => ({
    conversationId: null,
    conversations: [],
    messages: [],
    input: '',
    loading: false,
    mode: 'chat',
    recording: false,
    transcribing: false,
    mediaRecorder: null,
    audioChunks: [],
    _currentAudio: null,

    async initChat() {
      const resp = await fetch('/thinker/api/conversations');
      this.conversations = await resp.json();
      const active = this.conversations.filter(c => c.status === 'active');
      if (active.length > 0) {
        this.conversationId = active[0].id;
        this.mode = active[0].mode || 'chat';
        await this.loadMessages();
      } else {
        await this.newConversation();
      }
    },

    async newConversation() {
      const fd = new FormData();
      fd.append('mode', 'chat');
      const resp = await fetch('/thinker/api/conversations', { method: 'POST', body: fd });
      const c = await resp.json();
      this.conversationId = c.id;
      this.messages = [];
      this.mode = 'chat';
      const r2 = await fetch('/thinker/api/conversations');
      this.conversations = await r2.json();
    },

    async loadMessages() {
      if (!this.conversationId) return;
      const resp = await fetch(`/thinker/api/conversations/${this.conversationId}/messages`);
      this.messages = await resp.json();
      this.$nextTick(() => this.scrollToBottom());
    },

    async updateMode() {
      const fd = new FormData();
      fd.append('mode', this.mode);
      await fetch(`/thinker/api/conversations/${this.conversationId}/mode`, { method: 'POST', body: fd });
    },

    async send() {
      const text = this.input.trim();
      if (!text || this.loading || !this.conversationId) return;
      this.loading = true;
      this.messages.push({ role: 'user', content: text });
      this.input = '';
      this.$nextTick(() => this.scrollToBottom());

      const aiIdx = this.messages.length;
      this.messages.push({ role: 'assistant', content: '', streaming: true });

      const fd = new FormData();
      fd.append('conversation_id', this.conversationId);
      fd.append('message', text);

      try {
        const resp = await fetch('/thinker/api/chat', { method: 'POST', body: fd });
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
          let result;
          try {
            result = await Promise.race([
              reader.read(),
              new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), 60000)),
            ]);
          } catch (e) {
            break;  // timeout or error — stop reading, show what we have
          }
          const { done, value } = result;
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';
          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            const payload = line.slice(6);
            try {
              const data = JSON.parse(payload);
              if (data.chunk) {
                this.messages[aiIdx].content += data.chunk;
                this.$nextTick(() => this.scrollToBottom());
              }
              if (data.done) {
                this.messages[aiIdx].streaming = false;
                this.messages[aiIdx].id = data.message_id;
              }
              if (data.error) {
                this.messages[aiIdx].content = '(Error: ' + data.error + ')';
                this.messages[aiIdx].streaming = false;
              }
            } catch (e) { /* skip non-JSON lines */ }
          }
        }
      } catch (err) {
        this.messages[aiIdx].content = '(Network error)';
        this.messages[aiIdx].streaming = false;
      } finally {
        this.loading = false;
        this.$nextTick(() => this.scrollToBottom());
      }
    },

    scrollToBottom() {
      const el = this.$refs.msgContainer;
      if (el) el.scrollTop = el.scrollHeight;
    },

    async endSession() {
      if (!confirm('End this conversation? A summary will be generated.')) return;
      // Release microphone if recording
      if (this.recording) this.stopRecording();
      await fetch(`/thinker/api/conversations/${this.conversationId}/close`, { method: 'POST' });
      await this.newConversation();
    },

    // Voice input for chat
    async toggleRecording() {
      if (this.recording) { this.stopRecording(); }
      else { await this.startRecording(); }
    },

    async startRecording() {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const _chatMime = getSupportedMimeType();
        this.mediaRecorder = new MediaRecorder(stream, _chatMime ? { mimeType: _chatMime } : {});
        this.audioChunks = [];
        this.mediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) this.audioChunks.push(e.data); };
        this.mediaRecorder.onstop = async () => {
          this.transcribing = true;
          const _chatBlobType = getSupportedMimeType() || 'audio/webm';
          const blob = new Blob(this.audioChunks, { type: _chatBlobType });
          const fd = new FormData();
          fd.append('file', blob, 'recording.webm');
          try {
            const resp = await fetch('/thinker/api/voice/transcribe/stream', { method: 'POST', body: fd });
            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buf = '';
            while (true) {
              const { done, value } = await reader.read();
              if (done) break;
              buf += decoder.decode(value, { stream: true });
              const lines = buf.split('\n');
              buf = lines.pop() || '';
              for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                const p = line.slice(6);
                if (p === '[done]') continue;
                try { const d = JSON.parse(p); if (d.chunk) this.input += d.chunk; } catch (e) {}
              }
            }
          } catch (err) { alert('Speech recognition failed'); }
          this.transcribing = false;
          stream.getTracks().forEach(t => t.stop());
        };
        this.mediaRecorder.start();
        this.recording = true;
      } catch (err) { alert('Cannot access microphone'); }
    },

    stopRecording() {
      if (this.mediaRecorder && this.mediaRecorder.state !== 'inactive') this.mediaRecorder.stop();
      this.recording = false;
    },

    playTTS(messageId) {
      if (this._currentAudio) { this._currentAudio.pause(); this._currentAudio = null; }
      const audio = new Audio(`/thinker/api/voice/tts/${messageId}`);
      this._currentAudio = audio;
      audio.onended = () => { this._currentAudio = null; };
      audio.play().catch(() => { this._currentAudio = null; });
    },
  }));
});

// ── Rehearsal Tab ─────────────────────────────────────────────────

document.addEventListener('alpine:init', () => {
  Alpine.data('thinkerRehearsal', () => ({
    phase: 'preparing',
    rehearsalId: null,
    _rehearsalGeneration: 0,  // prevents stale callbacks from corrupting new sessions
    title: '',
    questionListPath: '',
    questionListPreview: '',
    questionListError: '',
    _questionListContent: '',
    _fileReading: false,
    questionInputMode: 'file',    // 'file' or 'paste' — paste mode for mobile
    pastedQuestionContent: '',    // raw text pasted on mobile

    // Presentation
    transcript: '',
    transcribingCount: 0,   // counter for concurrent STT chunks

    get transcribing() { return this.transcribingCount > 0; },
    elapsed: 0,
    _timer: null,
    _mediaRecorder: null,
    _audioChunksForArchive: [],
    audioLevel: 0,
    _audioCtx: null,
    failedChunks: [],       // chunks that failed after all retries
    failedChunkCount: 0,    // displayed in UI

    // Q&A
    currentQuestion: '',
    qaAnswerText: '',
    qaHistory: [],
    qaCount: 0,
    qaRecording: false,
    _qaMediaRecorder: null,
    _qaAudioChunks: [],
    _qaStream: null,

    // Summary
    summaryData: null,

    formatTime,

    handleFilePick(event) {
      const file = event.target.files[0];
      if (!file) return;
      if (file.size > 500 * 1024) {
        this.questionListError = '文件过大（最大 500KB）';
        return;
      }
      // Show filename + loading state in the path input
      this.questionListPath = file.name;
      this._questionListContent = '';  // clear until read completes
      this._fileReading = true;
      this.questionListPreview = '读取中...';
      const reader = new FileReader();
      reader.onload = (e) => {
        this.questionListPreview = e.target.result;
        this._questionListContent = e.target.result;
        this._fileReading = false;
      };
      reader.onerror = () => {
        this.questionListError = '文件读取失败，请重试';
        this._fileReading = false;
      };
      reader.readAsText(file, 'UTF-8');
    },

    async loadQuestionList() {
      this.questionListError = '';
      this.questionListPreview = '';
      if (!this.questionListPath) {
        this.questionListError = '请先输入问题清单的 .md 文件路径';
        return;
      }
      // If content was already loaded via file picker, preview immediately
      if (this._questionListContent) {
        this.questionListPreview = this._questionListContent;
        return;
      }
      try {
        const fd = new FormData();
        fd.append('title', '_preview_');
        fd.append('question_list_path', this.questionListPath);
        const resp = await fetch('/thinker/api/rehearsal/start', { method: 'POST', body: fd });
        const data = await resp.json();
        if (data.error) {
          this.questionListError = data.error;
          this.questionListPreview = '';
          return;
        }
        const r2 = await fetch('/thinker/api/rehearsal/' + data.id);
        const detail = await r2.json();
        this.questionListPreview = detail.question_list_content || '(empty)';
        // Delete the preview session
        await fetch('/thinker/api/rehearsal/' + data.id, { method: 'DELETE' });
      } catch (e) {
        this.questionListError = 'Failed to load: ' + e.message;
      }
    },

    async startRehearsal() {
      const fd = new FormData();
      fd.append('title', this.title);
      fd.append('question_list_path', this.questionListPath);
      // If file was picked locally, send content directly (no server read needed)
      if (this._questionListContent) {
        fd.append('question_list_content', this._questionListContent);
      }
      const resp = await fetch('/thinker/api/rehearsal/start', { method: 'POST', body: fd });
      const data = await resp.json();
      if (data.error) { alert(data.error); return; }
      this.rehearsalId = data.id;
      this._rehearsalGeneration++;
      this._beforeUnload = (e) => { e.preventDefault(); e.returnValue = ''; };
      window.addEventListener('beforeunload', this._beforeUnload);
      this.phase = 'presenting';
      this.startPresentationRecording();
    },

    async startPresentationRecording() {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        this._presentationStream = stream;  // saved so we can stop tracks later
        const _presMime = getSupportedMimeType();
        this._recordingMime = _presMime;  // store for chunk format detection
        this._mediaRecorder = new MediaRecorder(stream, _presMime ? { mimeType: _presMime } : {});
        this._audioChunksForArchive = [];

        this._mediaRecorder.ondataavailable = async (e) => {
          if (e.data.size > 0) {
            this._audioChunksForArchive.push(e.data);
            await this._sendChunkForSTT(e.data);
          }
        };

        this._mediaRecorder.onstop = () => {
          // Release microphone when recorder stops (normal end)
          if (this._presentationStream) {
            this._presentationStream.getTracks().forEach(t => t.stop());
            this._presentationStream = null;
          }
        };

        this._mediaRecorder.onerror = () => {
          // Release microphone on error (disconnect, phone call, etc.)
          if (this._presentationStream) {
            this._presentationStream.getTracks().forEach(t => t.stop());
            this._presentationStream = null;
          }
          alert('录音被中断（电话来电或麦克风断开）。请重新开始预演。');
          if (this._timer) clearInterval(this._timer);
          this.phase = 'preparing';
          this.transcribingCount = 0;
        };

        // Detect audio track ending (Bluetooth disconnect, system revoke)
        stream.getAudioTracks().forEach(track => {
          track.onended = () => {
            if (this.phase === 'presenting') {
              alert('麦克风连接已断开。请检查后重新开始预演。');
              if (this._timer) clearInterval(this._timer);
              this.phase = 'preparing';
              this.transcribingCount = 0;
            }
          };
        });

        this._mediaRecorder.start(30000); // 30-second chunks
        this.elapsed = 0;
        this._timer = setInterval(() => { this.elapsed++; }, 1000);

        // Audio level monitoring
        try {
          this._audioCtx = new AudioContext();
          const source = this._audioCtx.createMediaStreamSource(stream);
          const analyser = this._audioCtx.createAnalyser();
          analyser.fftSize = 256;
          source.connect(analyser);
          const dataArray = new Uint8Array(analyser.frequencyBinCount);
          const updateLevel = () => {
            if (this.phase !== 'presenting') return;
            analyser.getByteFrequencyData(dataArray);
            const avg = dataArray.reduce((a, b) => a + b, 0) / dataArray.length;
            this.audioLevel = Math.min(100, Math.round((avg / 128) * 100));
            requestAnimationFrame(updateLevel);
          };
          updateLevel();
        } catch (e) { /* AudioContext not available */ }
      } catch (err) {
        alert('Cannot access microphone: ' + err.message);
      }
    },

    async _sendChunkForSTT(blob, generation = this._rehearsalGeneration) {
      // Bail if the rehearsal was reset while this chunk was in-flight
      if (generation !== this._rehearsalGeneration) return;
      this.transcribingCount++;
      // Server handles its own error classification and retry.
      // Client only retries on network errors (fetch threw), not on server errors.
      let success = false;

      const _sendOnce = async () => {
        const fd = new FormData();
        fd.append('file', blob, 'chunk.opus');
        const _mime = this._recordingMime || '';
        const chunkFormat = _mime.includes('mp4') ? 'mp4'
                          : _mime.includes('ogg') ? 'ogg'
                          : 'webm';
        const resp = await fetch(
          `/thinker/api/rehearsal/${this.rehearsalId}/transcribe-chunk?format=${chunkFormat}`,
          { method: 'POST', body: fd },
        );
        const data = await resp.json();
        if (data.text) {
          this.transcript += (this.transcript ? ' ' : '') + data.text;
          success = true;
        } else if (data.error) {
          console.warn('Chunk STT server error:', data.error, data.info || '');
        }
      };

      // Try once, retry once on network failure only
      for (let attempt = 0; attempt < 2; attempt++) {
        try {
          await _sendOnce();
          break;
        } catch (e) {
          if (attempt === 0) {
            console.warn('Chunk STT network error, retrying once:', e.message || e);
            await new Promise(r => setTimeout(r, 2000));
          } else {
            console.error('Chunk STT failed after network retry:', e.message || e);
          }
        }
      }

      if (!success) {
        this.failedChunks.push(blob);
        this.failedChunkCount = this.failedChunks.length;
        console.error('Chunk STT failed after all retries, queued for recovery');
        // Mark gap in transcript
        this.transcript += '\n[...传输中断...]\n';
      }

      this.transcribingCount = Math.max(0, this.transcribingCount - 1);
    },

    async stopPresentation() {
      if (this._mediaRecorder && this._mediaRecorder.state !== 'inactive') {
        this._mediaRecorder.stop();
      }
      // Release microphone immediately (belt-and-suspenders with onstop handler)
      if (this._presentationStream) {
        this._presentationStream.getTracks().forEach(t => t.stop());
        this._presentationStream = null;
      }
      if (this._timer) clearInterval(this._timer);

      // Clean up audio context
      if (this._audioCtx) { this._audioCtx.close().catch(() => {}); this._audioCtx = null; }

      // Wait for all in-flight STT chunks to settle so the transcript is
      // complete before the LLM reads it for question generation.
      // Poll transcribingCount (decremented by _sendChunkForSTT on completion).
      while (this.transcribingCount > 0) {
        await new Promise(r => setTimeout(r, 300));
      }

      // Retry failed chunks synchronously (not fire-and-forget) so their
      // transcription results land in the DB before _fetchNextQuestion runs.
      if (this.failedChunks.length > 0) {
        const toRetry = [...this.failedChunks];
        this.failedChunks = [];
        this.failedChunkCount = 0;
        for (const chunk of toRetry) {
          await this._sendChunkForSTT(chunk);
        }
      }

      await fetch(`/thinker/api/rehearsal/${this.rehearsalId}/finish-presentation`, { method: 'POST' });

      // Save full audio in background (don't block Q&A start)
      this._saveFullAudio();

      this.phase = 'qa';
      // Prevent accidental data loss during Q&A
      if (!this._beforeUnload) {
        this._beforeUnload = (e) => { e.preventDefault(); e.returnValue = ''; };
        window.addEventListener('beforeunload', this._beforeUnload);
      }
      await this._fetchNextQuestion();
    },

    async _saveFullAudio() {
      if (this._audioChunksForArchive.length === 0) return;
      const fd = new FormData();
      this._audioChunksForArchive.forEach((chunk, i) => {
        fd.append(`chunk_${i}`, chunk, `chunk_${i}.opus`);
      });
      try {
        await fetch(`/thinker/api/rehearsal/${this.rehearsalId}/save-full-audio`, {
          method: 'POST', body: fd,
        });
      } catch (e) {
        console.error('Failed to save full audio:', e);
      }
    },

    async _fetchNextQuestion() {
      this.currentQuestion = '';
      try {
        const resp = await fetch(`/thinker/api/rehearsal/${this.rehearsalId}/next-question`);
        const data = await resp.json();
        if (data.question && data.question !== 'NO_MORE_QUESTIONS') {
          this.currentQuestion = data.question;
          // Speak the question via TTS so the presenter hears it
          this.playTTSForText(data.question);
        } else if (data.question === 'NO_MORE_QUESTIONS' || data.error) {
          alert('🎉 题库中的高质量问题已全部问完！可以结束问答生成总结了。');
        }
      } catch (e) {
        console.error('Fetch question failed:', e);
      }
    },

    async submitAnswer() {
      const answer = this.qaAnswerText.trim();
      if (!answer || !this.currentQuestion) return;

      const fd = new FormData();
      fd.append('question', this.currentQuestion);
      fd.append('answer_text', answer);
      try {
        await fetch(`/thinker/api/rehearsal/${this.rehearsalId}/answer`, { method: 'POST', body: fd });
      } catch (e) {
        alert('提交回答失败，请检查网络后重试。回答内容已保留在输入框中。');
        return;
      }

      this.qaHistory.push({ question: this.currentQuestion, answer });
      this.qaCount++;
      this.qaAnswerText = '';

      await this._fetchNextQuestion();
    },

    async toggleQAAnswerRecording() {
      if (this.qaRecording) { this._stopQAAnswerRecording(); }
      else { await this._startQAAnswerRecording(); }
    },

    async _startQAAnswerRecording() {
      try {
        this._qaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const _qaRecMime = getSupportedMimeType();
        this._qaMediaRecorder = new MediaRecorder(this._qaStream, _qaRecMime ? { mimeType: _qaRecMime } : {});
        this._qaAudioChunks = [];
        this._qaMediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) this._qaAudioChunks.push(e.data); };
        this._qaMediaRecorder.onstop = async () => {
          const _qaBlobType = getSupportedMimeType() || 'audio/webm';
          const blob = new Blob(this._qaAudioChunks, { type: _qaBlobType });
          const fd = new FormData();
          fd.append('question', this.currentQuestion);
          fd.append('file', blob, 'answer.webm');
          const resp = await fetch(`/thinker/api/rehearsal/${this.rehearsalId}/answer`, {
            method: 'POST', body: fd,
          });
          const data = await resp.json();
          if (data.answer) {
            this.qaHistory.push({ question: this.currentQuestion, answer: data.answer });
            this.qaCount++;
            await this._fetchNextQuestion();
          }
          this._qaStream.getTracks().forEach(t => t.stop());
          this.qaRecording = false;
        };
        this._qaMediaRecorder.start();
        this.qaRecording = true;
      } catch (err) { alert('Cannot access microphone'); }
    },

    _stopQAAnswerRecording() {
      if (this._qaMediaRecorder && this._qaMediaRecorder.state !== 'inactive') {
        this._qaMediaRecorder.stop();
      }
      // Release microphone immediately (belt-and-suspenders with onstop handler)
      if (this._qaStream) {
        this._qaStream.getTracks().forEach(t => t.stop());
        this._qaStream = null;
      }
    },

    async finishQA() {
      if (this.qaCount < 3) {
        if (!confirm(`你只完成了 ${this.qaCount} 轮问答。建议至少 3 轮以获得有意义的总结。确定要现在结束吗？`)) {
          return;
        }
      }
      this.phase = 'summarizing';
      try {
        const resp = await fetch(`/thinker/api/rehearsal/${this.rehearsalId}/finish-qa`, { method: 'POST' });
        if (!resp.ok) {
          const errData = await resp.json().catch(() => ({}));
          this.phase = 'qa';
          alert(errData.error || 'Summary generation failed. Please try again.');
          return;
        }
        await resp.json(); // consume the summary result
        // Load the full structured summary
        const r2 = await fetch(`/thinker/api/rehearsal/${this.rehearsalId}/summary`);
        this.summaryData = await r2.json();
        this.phase = 'completed';
        if (this._beforeUnload) {
          window.removeEventListener('beforeunload', this._beforeUnload);
          this._beforeUnload = null;
        }
      } catch (e) {
        this.phase = 'qa';
        console.error('Summary generation failed:', e);
        alert('总结生成失败：' + (e.message || '请重试'));
      }
    },

    resetRehearsal() {
      // ── Release microphone: stop all active recorders and streams ──
      if (this._mediaRecorder && this._mediaRecorder.state !== 'inactive') {
        this._mediaRecorder.stop();
      }
      if (this._presentationStream) {
        this._presentationStream.getTracks().forEach(t => t.stop());
        this._presentationStream = null;
      }
      if (this._qaMediaRecorder && this._qaMediaRecorder.state !== 'inactive') {
        this._qaMediaRecorder.stop();
      }
      if (this._qaStream) {
        this._qaStream.getTracks().forEach(t => t.stop());
        this._qaStream = null;
      }
      if (this._timer) clearInterval(this._timer);
      if (this._audioCtx) { this._audioCtx.close().catch(() => {}); this._audioCtx = null; }

      if (this._beforeUnload) {
        window.removeEventListener('beforeunload', this._beforeUnload);
        this._beforeUnload = null;
      }
      this.phase = 'preparing';
      this.rehearsalId = null;
      this.transcript = '';
      this.qaHistory = [];
      this.qaCount = 0;
      this.currentQuestion = '';
      this.summaryData = null;
      this._audioChunksForArchive = [];
      this._questionListContent = '';
      this.questionListPath = '';
      this.questionListPreview = '';
      this.questionListError = '';
      this.pastedQuestionContent = '';
      this.questionInputMode = 'file';
      this.failedChunks = [];
      this.failedChunkCount = 0;
      this.transcribingCount = 0;
    },

    playTTSForText(text) {
      if (!text) return;
      const audio = new Audio(`/thinker/api/rehearsal/${this.rehearsalId}/tts/question?text=${encodeURIComponent(text)}`);
      audio.play().catch(e => console.error('TTS play failed:', e));
    },
  }));
});

// ── Records Tab ───────────────────────────────────────────────────

document.addEventListener('alpine:init', () => {
  Alpine.data('thinkerRecords', () => ({
    records: [],
    searchQuery: '',
    recordType: '',
    selectedDetail: null,

    get summaryData() { return this.selectedDetail; },

    async loadRecords() {
      const params = new URLSearchParams();
      if (this.searchQuery) params.set('q', this.searchQuery);
      if (this.recordType) params.set('type', this.recordType);
      const resp = await fetch('/thinker/api/rehearsals?' + params.toString());
      this.records = await resp.json();
    },

    async viewDetail(id) {
      const resp = await fetch('/thinker/api/rehearsal/' + id + '/summary');
      if (!resp.ok) { alert('Failed to load detail'); return; }
      this.selectedDetail = await resp.json();
    },
  }));
});
