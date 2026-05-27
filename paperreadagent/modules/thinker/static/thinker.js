/**
 * thinker.js — Thinker 浮动侧边栏 Alpine.js 交互逻辑
 * 依赖：Alpine.js、HTMX（已由 base.html 加载）
 */

document.addEventListener('alpine:init', () => {
  Alpine.data('thinkerPanel', () => ({
    open: false,
    minimized: false,
    conversationId: null,
    messages: [],
    input: '',
    loading: false,
    pendingQuestion: null,
    questionPollInterval: null,
    relatedNotes: [],

    async init() {
      // 恢复最近活跃会话
      const resp = await fetch('/thinker/api/conversations');
      const convs = await resp.json();
      if (convs.length > 0 && convs[0].status === 'active') {
        this.conversationId = convs[0].id;
        await this.loadMessages();
      } else {
        // 创建新会话
        const fd = new FormData();
        fd.append('mode', 'chat');
        const cr = await fetch('/thinker/api/conversations', { method: 'POST', body: fd });
        const c = await cr.json();
        this.conversationId = c.id;
      }
      this.startPolling();
    },

    async loadMessages() {
      const resp = await fetch(`/thinker/api/conversations/${this.conversationId}/messages`);
      this.messages = await resp.json();
      this.$nextTick(() => this.scrollToBottom());
    },

    async send() {
      const text = this.input.trim();
      if (!text || this.loading) return;

      this.loading = true;
      this.messages.push({ role: 'user', content: text });
      this.input = '';
      this.$nextTick(() => this.scrollToBottom());

      // 添加一个空的 assistant 消息占位
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
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            const payload = line.slice(6);
            if (payload.startsWith('[error]')) {
              this.messages[aiIdx].content = '（出了点问题，请重试）';
              this.messages[aiIdx].streaming = false;
              continue;
            }
            try {
              const data = JSON.parse(payload);
              if (data.chunk) {
                this.messages[aiIdx].content += data.chunk;
                this.$nextTick(() => this.scrollToBottom());
              }
              if (data.done) {
                this.messages[aiIdx].streaming = false;
                this.messages[aiIdx].id = data.message_id;
                if (data.message_id) {
                  this.loadRelatedNotes(data.message_id);
                  // 自动朗读 AI 回复
                  this.playTTS(data.message_id);
                }
              }
              if (data.error) {
                this.messages[aiIdx].content = '（出了点问题：' + data.error + '）';
                this.messages[aiIdx].streaming = false;
              }
            } catch (e) {
              // 忽略非 JSON 行
            }
          }
        }
      } catch (err) {
        this.messages[aiIdx].content = '（网络错误，请重试）';
        this.messages[aiIdx].streaming = false;
      } finally {
        this.loading = false;
        this.$nextTick(() => this.scrollToBottom());
      }
    },

    scrollToBottom() {
      const el = this.$el.querySelector('.chat-messages');
      if (el) el.scrollTop = el.scrollHeight;
    },

    toggle() { this.open = !this.open; },
    minimize() { this.minimized = !this.minimized; },

    async pause(minutes = 30) {
      const fd = new FormData();
      fd.append('minutes', minutes);
      await fetch(`/thinker/api/conversations/${this.conversationId}/pause`, { method: 'POST', body: fd });
    },

    handleKeydown(e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        this.send();
      }
    },

    // ── 语音 ──────────────────────────────────────────────

    recording: false,
    transcribing: false,
    mediaRecorder: null,
    audioChunks: [],

    async toggleRecording() {
      if (this.recording) {
        this.stopRecording();
      } else {
        await this.startRecording();
      }
    },

    async startRecording() {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        this.mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm;codecs=opus' });
        this.audioChunks = [];

        this.mediaRecorder.ondataavailable = (e) => {
          if (e.data.size > 0) this.audioChunks.push(e.data);
        };

        this.mediaRecorder.onstop = async () => {
          this.transcribing = true;
          this.input = '';
          const blob = new Blob(this.audioChunks, { type: 'audio/webm' });
          const fd = new FormData();
          fd.append('file', blob, 'recording.webm');
          try {
            const resp = await fetch('/thinker/api/voice/transcribe/stream', { method: 'POST', body: fd });
            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
              const { done, value } = await reader.read();
              if (done) break;
              buffer += decoder.decode(value, { stream: true });
              const lines = buffer.split('\n');
              buffer = lines.pop() || '';
              for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                const payload = line.slice(6);
                if (payload === '[done]') continue;
                try {
                  const d = JSON.parse(payload);
                  if (d.chunk) {
                    this.input += d.chunk;
                  }
                } catch (e) { /* skip */ }
              }
            }
            if (!this.input.trim()) {
              alert('没有识别到语音内容，请重试');
            }
          } catch (err) {
            this.input = '';
            alert('语音转写失败，请检查网络连接');
          }
          this.transcribing = false;
          stream.getTracks().forEach(t => t.stop());
        };

        this.mediaRecorder.start();
        this.recording = true;
      } catch (err) {
        alert('无法访问麦克风，请在浏览器设置中允许本网站使用麦克风');
      }
    },

    stopRecording() {
      if (this.mediaRecorder && this.mediaRecorder.state !== 'inactive') {
        this.mediaRecorder.stop();
      }
      this.recording = false;
    },

    _currentAudio: null,

    playTTS(messageId) {
      // 停掉之前正在播放的
      if (this._currentAudio) {
        this._currentAudio.pause();
        this._currentAudio = null;
      }
      const audio = new Audio(`/thinker/api/voice/tts/${messageId}`);
      this._currentAudio = audio;
      audio.onended = () => { this._currentAudio = null; };
      audio.play().catch(err => { console.error('TTS 播放失败:', err); this._currentAudio = null; });
    },

    // ── 主动提问轮询 ──────────────────────────────────────────

    pendingQuestion: null,
    questionPollInterval: null,

    startPolling() {
      const poll = async () => {
        if (!this.conversationId || document.hidden) return;
        try {
          const resp = await fetch(`/thinker/api/questions/pending?conversation_id=${this.conversationId}`);
          const data = await resp.json();
          if (data && data.question) {
            this.pendingQuestion = data;
          }
        } catch (e) {
          // 静默失败
        }
      };
      this.questionPollInterval = setInterval(poll, 30000);
      // 标签页隐藏时暂停轮询
      document.addEventListener('visibilitychange', () => {
        if (!document.hidden) poll();
      });
    },

    async dismissQuestion() {
      if (this.pendingQuestion) {
        try {
          await fetch(`/thinker/api/questions/${this.pendingQuestion.id}/dismiss`, { method: 'POST' });
        } catch (e) {
          // ignore
        }
        this.pendingQuestion = null;
      }
    },

    async endSession() {
      if (!confirm('结束本次对话？小思会为你生成一份摘要。')) return;
      try {
        await fetch(`/thinker/api/conversations/${this.conversationId}/close`, { method: 'POST' });
        // 创建新会话，清空界面
        const fd = new FormData();
        fd.append('mode', 'chat');
        const cr = await fetch('/thinker/api/conversations', { method: 'POST', body: fd });
        const c = await cr.json();
        this.conversationId = c.id;
        this.messages = [];
        this.relatedNotes = [];
        this.input = '';
      } catch (e) {
        console.error('结束对话失败:', e);
      }
    },

    async loadRelatedNotes(messageId) {
      try {
        const resp = await fetch(`/thinker/api/messages/${messageId}/related`);
        this.relatedNotes = await resp.json();
      } catch (e) {
        // 静默
      }
    }
  }));
});
