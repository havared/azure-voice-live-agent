/* ═══════════════════════════════════════════════════════════════════
   Voice Live Agent — Application Logic
   Real-time voice session over WebSocket with PCM16 audio
   ═══════════════════════════════════════════════════════════════════ */

// ── State ───────────────────────────────────────────────────────────
let ws = null;
let audioCtx = null;
let playbackCtx = null;
let mediaStream = null;
let processor = null;
let nextPlayTime = 0;
let activeSources = [];
let isAgentSpeaking = false;
let playbackMuted = false;
let sessionActive = false;
let timerInterval = null;
let timerSeconds = 0;
let audioLevelInterval = null;
let currentRms = 0;

// Recording state
let recordedUserChunks = [];
let recordedAgentChunks = [];
let recordingBlobUrl = null;

const BARGE_IN_RMS_THRESHOLD = 0.015;
const AUDIO_BAR_COUNT = 5;

// ── DOM Helpers ─────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);

function setStatus(text, state = '') {
    const el = $('statusLine');
    el.className = 'status-line' + (state ? ' ' + state : '');

    let iconHtml = '';
    if (state === 'processing') {
        iconHtml = '<div class="spinner"></div>';
    } else if (state === 'active') {
        iconHtml = `<div class="waveform">
      <div class="waveform-bar"></div><div class="waveform-bar"></div>
      <div class="waveform-bar"></div><div class="waveform-bar"></div>
      <div class="waveform-bar"></div>
    </div>`;
    }

    el.innerHTML = iconHtml + '<span>' + escapeHtml(text) + '</span>';
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function getTimestamp() {
    const now = new Date();
    return now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function addMessage(text, type) {
    const emptyState = $('emptyState');
    if (emptyState) emptyState.style.display = 'none';

    $('clearBtn').style.display = '';

    const container = document.createElement('div');
    container.className = 'msg msg-' + type;

    const timestamp = getTimestamp();

    if (type === 'status') {
        container.innerHTML = `<div class="msg-bubble">${escapeHtml(text)}</div>`;
    } else {
        const label = type === 'user' ? 'You' : 'Agent';
        const labelSide = type === 'user' ? 'right' : 'left';
        container.innerHTML = `
      <div class="msg-label">
        <span>${label}</span>
        <span class="msg-timestamp">${timestamp}</span>
      </div>
      <div class="msg-bubble">${escapeHtml(text)}</div>
    `;
    }

    const transcriptEl = $('transcript');
    transcriptEl.appendChild(container);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

function clearTranscript() {
    const transcript = $('transcript');
    transcript.innerHTML = `
    <div class="conversation-empty" id="emptyState">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" width="40" height="40">
        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
      </svg>
      <p>Start a session to begin the conversation</p>
    </div>
  `;
    $('clearBtn').style.display = 'none';
}

function showToast(message, isError = false) {
    const toast = $('toast');
    toast.textContent = message;
    toast.className = 'toast' + (isError ? ' toast-error' : '');

    // Trigger show
    requestAnimationFrame(() => {
        toast.classList.add('show');
    });

    setTimeout(() => {
        toast.classList.remove('show');
    }, 3500);
}

function updateConnectionBadge(state) {
    const badge = $('connectionBadge');
    const text = $('connectionText');
    const dot = badge.querySelector('.status-dot');

    badge.className = 'badge';
    dot.className = 'status-dot';

    switch (state) {
        case 'connected':
            badge.classList.add('badge-success');
            dot.classList.add('pulse');
            text.textContent = 'Connected';
            break;
        case 'connecting':
            badge.classList.add('badge-warning');
            dot.classList.add('pulse');
            text.textContent = 'Connecting…';
            break;
        case 'error':
            badge.classList.add('badge-destructive');
            text.textContent = 'Error';
            break;
        default:
            badge.classList.add('badge-outline');
            text.textContent = 'Disconnected';
    }
}

// ── Timer ───────────────────────────────────────────────────────────
function startTimer() {
    timerSeconds = 0;
    updateTimerDisplay();
    timerInterval = setInterval(() => {
        timerSeconds++;
        updateTimerDisplay();
    }, 1000);
}

function stopTimer() {
    if (timerInterval) {
        clearInterval(timerInterval);
        timerInterval = null;
    }
}

function updateTimerDisplay() {
    const mins = Math.floor(timerSeconds / 60).toString().padStart(2, '0');
    const secs = (timerSeconds % 60).toString().padStart(2, '0');
    $('sessionTimer').textContent = mins + ':' + secs;
}

// ── Audio Level Visualizer ──────────────────────────────────────────
function startAudioLevel() {
    const bars = $('audioLevel').querySelectorAll('.audio-bar');
    audioLevelInterval = setInterval(() => {
        for (let i = 0; i < bars.length; i++) {
            const base = currentRms * 200;
            const jitter = Math.random() * 4;
            const height = Math.max(3, Math.min(20, base + jitter));
            bars[i].style.height = height + 'px';
        }
    }, 100);
}

function stopAudioLevel() {
    if (audioLevelInterval) {
        clearInterval(audioLevelInterval);
        audioLevelInterval = null;
    }
    const bars = $('audioLevel').querySelectorAll('.audio-bar');
    for (const bar of bars) bar.style.height = '3px';
}

// ── PCM Conversion ──────────────────────────────────────────────────
function float32ToPcm16(f32) {
    const buf = new Int16Array(f32.length);
    for (let i = 0; i < f32.length; i++) {
        const s = Math.max(-1, Math.min(1, f32[i]));
        buf[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    return buf;
}

function pcm16ToFloat32(pcm) {
    const f = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) f[i] = pcm[i] / 32768.0;
    return f;
}

// ── Playback Scheduler ─────────────────────────────────────────────
function playChunk(arrayBuffer) {
    // Record agent audio
    const pcmForRecord = new Int16Array(arrayBuffer);
    const f32ForRecord = pcm16ToFloat32(pcmForRecord);
    recordedAgentChunks.push(new Float32Array(f32ForRecord));

    if (!playbackCtx || playbackMuted) return;
    const pcm = new Int16Array(arrayBuffer);
    const f32 = pcm16ToFloat32(pcm);
    const buf = playbackCtx.createBuffer(1, f32.length, 24000);
    buf.getChannelData(0).set(f32);
    const src = playbackCtx.createBufferSource();
    src.buffer = buf;
    src.connect(playbackCtx.destination);
    const now = playbackCtx.currentTime;
    const t = Math.max(now + 0.02, nextPlayTime);
    src.start(t);
    nextPlayTime = t + buf.duration;

    activeSources.push(src);
    src.onended = () => {
        const idx = activeSources.indexOf(src);
        if (idx !== -1) activeSources.splice(idx, 1);
    };
}

function flushPlayback() {
    for (const src of activeSources) {
        try { src.stop(); } catch (e) { /* ignore */ }
    }
    activeSources = [];
    nextPlayTime = 0;
    isAgentSpeaking = false;
    playbackMuted = true;
}

// ── Session Management ─────────────────────────────────────────────
function toggleSession() {
    if (sessionActive) {
        stopSession();
    } else {
        startSession();
    }
}

async function startSession() {
    const micBtn = $('micBtn');
    micBtn.disabled = true;
    sessionActive = true;

    updateConnectionBadge('connecting');
    setStatus('Connecting to server…', 'processing');

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(proto + '//' + location.host + '/ws/voice');
    ws.binaryType = 'arraybuffer';

    ws.onopen = async () => {
        updateConnectionBadge('connected');
        setStatus('Connected — requesting microphone…', 'processing');

        try {
            mediaStream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    sampleRate: 24000,
                    channelCount: 1,
                    echoCancellation: true,
                    noiseSuppression: true,
                },
            });

            audioCtx = new AudioContext({ sampleRate: 24000 });
            playbackCtx = new AudioContext({ sampleRate: 24000 });
            nextPlayTime = 0;

            const source = audioCtx.createMediaStreamSource(mediaStream);
            processor = audioCtx.createScriptProcessor(4096, 1, 1);

            processor.onaudioprocess = (e) => {
                const inputData = e.inputBuffer.getChannelData(0);

                // Record user audio
                recordedUserChunks.push(new Float32Array(inputData));

                // Calculate RMS for audio level viz
                let sumSq = 0;
                for (let i = 0; i < inputData.length; i++) sumSq += inputData[i] * inputData[i];
                currentRms = Math.sqrt(sumSq / inputData.length);

                if (ws && ws.readyState === WebSocket.OPEN) {
                    ws.send(float32ToPcm16(inputData).buffer);
                }

                if (isAgentSpeaking) {
                    if (currentRms > BARGE_IN_RMS_THRESHOLD) {
                        flushPlayback();
                        if (ws && ws.readyState === WebSocket.OPEN) {
                            ws.send(JSON.stringify({ type: 'barge_in' }));
                        }
                    }
                }
            };

            source.connect(processor);
            processor.connect(audioCtx.destination);

            // Show session UI
            micBtn.classList.add('active');
            micBtn.disabled = false;
            $('sessionInfo').style.display = '';
            startTimer();
            startAudioLevel();

            setStatus('Session active — speak now', 'active');
            addMessage('Voice session started', 'status');
        } catch (err) {
            setStatus('Microphone error: ' + err.message, 'error');
            showToast('Microphone access denied', true);
            stopSession();
        }
    };

    ws.onmessage = (event) => {
        if (event.data instanceof ArrayBuffer) {
            playChunk(event.data);
        } else {
            const m = JSON.parse(event.data);

            switch (m.type) {
                case 'session_started':
                    addMessage('Session ' + m.session_id, 'status');
                    break;

                case 'clear_playback':
                    flushPlayback();
                    break;

                case 'user_transcript':
                    addMessage(m.text, 'user');
                    break;

                case 'agent_transcript':
                case 'agent_text':
                    addMessage(m.text, 'agent');
                    break;

                case 'status':
                    if (m.status === 'agent_speaking') {
                        isAgentSpeaking = true;
                        playbackMuted = false;
                        setStatus('Agent speaking…', 'active');
                    } else if (m.status === 'listening') {
                        isAgentSpeaking = false;
                        setStatus('Listening…', 'active');
                    } else if (m.status === 'processing') {
                        setStatus('Processing…', 'processing');
                    } else if (m.status === 'ready') {
                        isAgentSpeaking = false;
                        setStatus('Session active — speak now', 'active');
                    }
                    break;

                case 'error':
                    addMessage('Error: ' + m.message, 'status');
                    showToast(m.message, true);
                    break;

                case 'pong':
                    break;

                default:
                    break;
            }
        }
    };

    ws.onclose = () => {
        updateConnectionBadge('disconnected');
        if (sessionActive) {
            setStatus('Disconnected from server');
            addMessage('Session disconnected', 'status');
        }
        cleanup();
    };

    ws.onerror = () => {
        updateConnectionBadge('error');
        setStatus('Connection error', 'error');
        showToast('WebSocket connection failed', true);
    };
}

function cleanup() {
    flushPlayback();
    stopTimer();
    stopAudioLevel();

    if (processor) { processor.disconnect(); processor = null; }
    if (audioCtx) { audioCtx.close(); audioCtx = null; }
    if (playbackCtx) { playbackCtx.close(); playbackCtx = null; }
    if (mediaStream) { mediaStream.getTracks().forEach((t) => t.stop()); mediaStream = null; }

    const micBtn = $('micBtn');
    micBtn.classList.remove('active');
    micBtn.disabled = false;
    $('sessionInfo').style.display = 'none';
    sessionActive = false;
    currentRms = 0;
}

function stopSession() {
    // Build recording before cleanup clears state
    buildRecording();

    if (ws) { ws.close(); ws = null; }
    cleanup();
    updateConnectionBadge('disconnected');
    setStatus('Session ended');
    addMessage('Session ended', 'status');
}

// ── Recording Helpers ───────────────────────────────────────────────
function concatFloat32Arrays(chunks) {
    let totalLength = 0;
    for (const chunk of chunks) totalLength += chunk.length;
    const result = new Float32Array(totalLength);
    let offset = 0;
    for (const chunk of chunks) {
        result.set(chunk, offset);
        offset += chunk.length;
    }
    return result;
}

function mergeAndMixAudio(userChunks, agentChunks) {
    const user = concatFloat32Arrays(userChunks);
    const agent = concatFloat32Arrays(agentChunks);
    const length = Math.max(user.length, agent.length);
    const mixed = new Float32Array(length);

    for (let i = 0; i < length; i++) {
        const u = i < user.length ? user[i] : 0;
        const a = i < agent.length ? agent[i] : 0;
        mixed[i] = Math.max(-1, Math.min(1, u + a));
    }
    return mixed;
}

function encodeWav(float32, sampleRate) {
    const numSamples = float32.length;
    const bytesPerSample = 2;
    const dataBytes = numSamples * bytesPerSample;
    const buffer = new ArrayBuffer(44 + dataBytes);
    const view = new DataView(buffer);

    // RIFF header
    writeString(view, 0, 'RIFF');
    view.setUint32(4, 36 + dataBytes, true);
    writeString(view, 8, 'WAVE');

    // fmt sub-chunk
    writeString(view, 12, 'fmt ');
    view.setUint32(16, 16, true);           // sub-chunk size
    view.setUint16(20, 1, true);            // PCM format
    view.setUint16(22, 1, true);            // mono
    view.setUint32(24, sampleRate, true);    // sample rate
    view.setUint32(28, sampleRate * bytesPerSample, true); // byte rate
    view.setUint16(32, bytesPerSample, true); // block align
    view.setUint16(34, 16, true);           // bits per sample

    // data sub-chunk
    writeString(view, 36, 'data');
    view.setUint32(40, dataBytes, true);

    // PCM samples
    let offset = 44;
    for (let i = 0; i < numSamples; i++) {
        const s = Math.max(-1, Math.min(1, float32[i]));
        const val = s < 0 ? s * 0x8000 : s * 0x7FFF;
        view.setInt16(offset, val, true);
        offset += 2;
    }
    return new Blob([buffer], { type: 'audio/wav' });
}

function writeString(view, offset, str) {
    for (let i = 0; i < str.length; i++) {
        view.setUint8(offset + i, str.charCodeAt(i));
    }
}

function buildRecording() {
    // Release previous recording URL
    if (recordingBlobUrl) {
        URL.revokeObjectURL(recordingBlobUrl);
        recordingBlobUrl = null;
    }

    if (recordedUserChunks.length === 0 && recordedAgentChunks.length === 0) {
        $('downloadBtn').style.display = 'none';
        return;
    }

    const mixed = mergeAndMixAudio(recordedUserChunks, recordedAgentChunks);
    const wavBlob = encodeWav(mixed, 24000);
    recordingBlobUrl = URL.createObjectURL(wavBlob);

    // Reset chunk buffers for next session
    recordedUserChunks = [];
    recordedAgentChunks = [];

    // Show download button
    const btn = $('downloadBtn');
    btn.style.display = '';
    btn.classList.add('download-ready');
    showToast('Recording ready — click Download to save');
}

function downloadRecording() {
    if (!recordingBlobUrl) {
        showToast('No recording available', true);
        return;
    }
    const a = document.createElement('a');
    a.href = recordingBlobUrl;
    const now = new Date();
    const ts = now.toISOString().replace(/[:.]/g, '-').slice(0, 19);
    a.download = 'voice-session-' + ts + '.wav';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
}

// ── Logout ──────────────────────────────────────────────────────────
async function handleLogout() {
    if (sessionActive) stopSession();
    try {
        await fetch('/auth/logout', { method: 'POST' });
    } catch (e) { /* ignore */ }
    window.location.href = '/login';
}
