// ─── State Management ───
let state = {
    currentStep: 1,
    videos: [],
    projects: [],
    selectedVideo: null,       // { filename, size_mb, duration_seconds, source_type }
    selectedProject: null,     // project object from server
    overlays: [],              // list of overlay objects
    sfxList: [],               // list of available SFX from server
    clipRanges: [],            // list of clip range objects {start, end}
    activeOverlayIndex: null,  // overlay being edited
    renderPollInterval: null,
    transcribePollInterval: null
};

// ─── Toast System ───
function showToast(message, type = 'info') {
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        document.body.appendChild(container);
    }
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.innerHTML = `
        <span class="toast-message">${message}</span>
        <button class="toast-close" onclick="this.parentElement.remove()">×</button>
    `;
    container.appendChild(toast);
    setTimeout(() => toast.classList.add('show'), 50);
    setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 400);
    }, 4000);
}

// ─── Custom Confirm Modal ───
function showConfirmModal(message) {
    return new Promise((resolve) => {
        const overlay = document.getElementById('confirm-modal');
        const msgEl   = document.getElementById('confirm-modal-message');
        const okBtn   = document.getElementById('confirm-modal-ok');
        const cancelBtn = document.getElementById('confirm-modal-cancel');
        if (!overlay) { resolve(false); return; }

        msgEl.textContent = message;
        overlay.style.display = 'flex';

        function cleanup(result) {
            overlay.style.display = 'none';
            okBtn.removeEventListener('click', onOk);
            cancelBtn.removeEventListener('click', onCancel);
            overlay.removeEventListener('click', onOverlayClick);
            resolve(result);
        }
        function onOk()          { cleanup(true);  }
        function onCancel()      { cleanup(false); }
        function onOverlayClick(e) { if (e.target === overlay) cleanup(false); }

        okBtn.addEventListener('click', onOk);
        cancelBtn.addEventListener('click', onCancel);
        overlay.addEventListener('click', onOverlayClick);
    });
}

// ─── Live Preview and Visual Timeline Globals ───
let livePreviewLoopId = null;
let triggeredSFX = new Set();
let lastPreviewTime = 0;
let isDraggingTimeline = false;
let activeDragHandle = null; // 'left' or 'right'
let activeDragSegmentIdx = null;
let dragStartX = 0;
let dragStartVal = 0;
const PIXELS_PER_SECOND = 40; // 1 second = 40px
let previewBGMAudio = null; // Background Music preview instance

function getVideoSrcUrl(videoName) {
    if (!videoName) return '';
    const matching = state.videos.find(v => v.filename === videoName);
    if (matching && matching.source_type === 'upload') {
        return `/uploads/${videoName}`;
    }
    return `/input/${videoName}`;
}

function startLivePreviewLoop(videoElement, overlayElement) {
    stopLivePreviewLoop();
    if (!videoElement || !overlayElement) return;

    triggeredSFX.clear();
    lastPreviewTime = videoElement.currentTime;

    function tick() {
        if (!videoElement || !overlayElement) return;
        const currentTime = videoElement.currentTime;
        const isPlaying = !videoElement.paused;

        // Render captions overlay
        renderLiveCaptions(currentTime, overlayElement);

        // Dynamic Background Music (BGM) Preview with Auto-Ducking
        const bgmTrack = document.getElementById('bgm-track')?.value;
        const bgmVolume = parseFloat(document.getElementById('bgm-volume')?.value || "0.15");
        const enableDucking = document.getElementById('bgm-enable-ducking')?.checked;

        if (bgmTrack) {
            // Load BGM if not loaded or if track changed
            if (!previewBGMAudio) {
                previewBGMAudio = new Audio(bgmTrack);
                previewBGMAudio.loop = true;
            } else if (previewBGMAudio.src !== window.location.origin + '/' + bgmTrack && !previewBGMAudio.src.endsWith(bgmTrack)) {
                previewBGMAudio.pause();
                previewBGMAudio = new Audio(bgmTrack);
                previewBGMAudio.loop = true;
            }

            if (isPlaying) {
                if (previewBGMAudio.paused) {
                    previewBGMAudio.currentTime = currentTime % (previewBGMAudio.duration || 1);
                    previewBGMAudio.play().catch(e => console.log("BGM playback blocked by browser policy"));
                } else {
                    // Periodic drift check & synchronization
                    const expectedTime = currentTime % (previewBGMAudio.duration || 1);
                    if (Math.abs(previewBGMAudio.currentTime - expectedTime) > 0.3) {
                        previewBGMAudio.currentTime = expectedTime;
                    }
                }

                // Auto-Ducking volume calculation
                let targetVol = bgmVolume;
                if (enableDucking && state.selectedProject) {
                    const proj = state.selectedProject;
                    const transcript = proj.aligned_transcript || proj.raw_transcript;
                    if (transcript && transcript.segments) {
                        let minDistance = 9999.0;
                        for (const seg of transcript.segments) {
                            if (seg.words) {
                                for (const w of seg.words) {
                                    if (currentTime >= w.start && currentTime <= w.end) {
                                        minDistance = 0.0;
                                        break;
                                    }
                                    const distStart = Math.abs(currentTime - w.start);
                                    const distEnd = Math.abs(currentTime - w.end);
                                    minDistance = Math.min(minDistance, distStart, distEnd);
                                }
                            }
                            if (minDistance === 0.0) break;
                        }

                        if (minDistance === 0.0) {
                            targetVol = bgmVolume * 0.15; // Duck to 15%
                        } else if (minDistance < 0.5) {
                            const ratio = minDistance / 0.5;
                            targetVol = bgmVolume * (0.15 + 0.85 * ratio); // Smooth transition
                        }
                    }
                }
                previewBGMAudio.volume = targetVol;
            } else {
                if (!previewBGMAudio.paused) {
                    previewBGMAudio.pause();
                }
            }
        } else {
            if (previewBGMAudio) {
                previewBGMAudio.pause();
                previewBGMAudio = null;
            }
        }

        // Play active SFX triggers (only if playing)
        if (isPlaying) {
            if (Math.abs(currentTime - lastPreviewTime) > 0.5) {
                triggeredSFX.clear();
            }

            if (state.overlays && state.overlays.length > 0) {
                state.overlays.forEach((o, idx) => {
                    if (o.type === 'audio' || o.type === 'sfx') {
                        if (currentTime >= o.start && currentTime < o.start + 0.25) {
                            if (!triggeredSFX.has(idx)) {
                                triggeredSFX.add(idx);
                                const audio = new Audio(o.asset);
                                audio.volume = o.volume !== undefined ? o.volume : 1.0;
                                audio.play().catch(err => console.log("SFX play blocked by browser policy:", err));
                            }
                        } else if (currentTime < o.start - 0.2) {
                            triggeredSFX.delete(idx);
                        }
                    }
                });
            }
        }

        lastPreviewTime = currentTime;
        livePreviewLoopId = requestAnimationFrame(tick);
    }

    livePreviewLoopId = requestAnimationFrame(tick);
}

function stopLivePreviewLoop() {
    if (livePreviewLoopId) {
        cancelAnimationFrame(livePreviewLoopId);
        livePreviewLoopId = null;
    }
    if (previewBGMAudio) {
        previewBGMAudio.pause();
        previewBGMAudio = null;
    }
}

function renderLiveCaptions(currentTime, overlayElement) {
    const proj = state.selectedProject;
    if (!proj) return;

    const transcript = proj.aligned_transcript || proj.raw_transcript;
    if (!transcript || !transcript.segments || transcript.segments.length === 0) {
        overlayElement.style.display = 'none';
        return;
    }

    const fontFamily = document.getElementById('style-font-family').value;
    const fontSize = document.getElementById('style-font-size').value;
    const textColor = document.getElementById('style-text-color').value;
    const highlightColor = document.getElementById('style-highlight-color').value;
    const bgType = document.getElementById('style-bg-type').value;
    const bgColor = document.getElementById('style-bg-color').value;
    const animation = document.getElementById('style-animation').value;

    const allWords = [];
    transcript.segments.forEach(seg => {
        if (seg.words) {
            seg.words.forEach(w => allWords.push(w));
        }
    });

    if (allWords.length === 0) {
        overlayElement.style.display = 'none';
        return;
    }

    let activeIdx = -1;
    for (let i = 0; i < allWords.length; i++) {
        if (currentTime >= allWords[i].start && currentTime <= allWords[i].end) {
            activeIdx = i;
            break;
        }
        if (i < allWords.length - 1 && currentTime > allWords[i].end && currentTime < allWords[i + 1].start) {
            activeIdx = i;
            break;
        }
    }

    if (activeIdx === -1) {
        overlayElement.style.display = 'none';
        return;
    }

    const WORDS_PER_GROUP = 5;
    const groupStart = Math.floor(activeIdx / WORDS_PER_GROUP) * WORDS_PER_GROUP;
    const groupEnd = Math.min(groupStart + WORDS_PER_GROUP, allWords.length);
    const groupWords = allWords.slice(groupStart, groupEnd);

    overlayElement.style.display = 'flex';
    overlayElement.style.fontFamily = `'${fontFamily}', sans-serif`;
    overlayElement.style.fontSize = `${fontSize}px`;
    overlayElement.innerHTML = '';
    
    if (bgType === 'card') {
        overlayElement.className = 'preview-subtitle-overlay-box style-card';
        overlayElement.style.backgroundColor = bgColor;
    } else if (bgType === 'outline') {
        overlayElement.className = 'preview-subtitle-overlay-box style-outline';
        overlayElement.style.backgroundColor = 'transparent';
    } else {
        overlayElement.className = 'preview-subtitle-overlay-box style-none';
        overlayElement.style.backgroundColor = 'transparent';
    }

    groupWords.forEach((word) => {
        const span = document.createElement('span');
        span.className = 'preview-word-span';
        span.innerText = word.word;
        
        const isWordActive = currentTime >= word.start && currentTime <= word.end;
        const isWordPast = currentTime > word.end;

        if (isWordActive) {
            span.style.color = highlightColor;
            span.classList.add('active');
            
            if (animation === 'pop') {
                span.classList.add('anim-pop');
            } else if (animation === 'bounce') {
                span.classList.add('anim-bounce');
            } else if (animation === 'glow') {
                span.classList.add('anim-glow');
                span.style.textShadow = `0 0 15px ${highlightColor}`;
            }
        } else {
            span.style.color = isWordPast ? 'rgba(255,255,255,0.6)' : textColor;
        }

        overlayElement.appendChild(span);
    });
}

function renderVisualTimeline() {
    const container = document.getElementById('timeline-tracks-area');
    const ruler = document.getElementById('timeline-ruler');
    if (!container || !ruler || !state.selectedProject) return;

    const transcript = state.selectedProject.raw_transcript;
    if (!transcript) {
        container.innerHTML = '<p class="subtitle text-center">No segments loaded. Run transcription first.</p>';
        return;
    }

    const duration = transcript.duration || 30.0;
    const timelineWidth = duration * PIXELS_PER_SECOND;
    container.style.width = `${timelineWidth}px`;
    ruler.style.width = `${timelineWidth}px`;

    ruler.innerHTML = '';
    for (let s = 0; s <= duration; s += 2) {
        const tick = document.createElement('div');
        tick.className = 'ruler-tick';
        tick.style.left = `${s * PIXELS_PER_SECOND}px`;
        tick.innerHTML = `<span class="tick-label">${s}s</span>`;
        ruler.appendChild(tick);
    }

    container.innerHTML = '';
    const segments = transcript.segments || [];

    segments.forEach((seg, idx) => {
        const block = document.createElement('div');
        block.className = 'timeline-segment-block';
        block.style.left = `${seg.start * PIXELS_PER_SECOND}px`;
        block.style.width = `${(seg.end - seg.start) * PIXELS_PER_SECOND}px`;
        
        block.innerHTML = `
            <div class="drag-handle handle-left" data-index="${idx}" data-side="left"></div>
            <div class="block-content">#${idx + 1}: ${seg.text}</div>
            <div class="drag-handle handle-right" data-index="${idx}" data-side="right"></div>
        `;
        
        block.addEventListener('click', () => {
            const video = document.getElementById('preview-video');
            if (video) {
                video.currentTime = seg.start;
            }
        });

        container.appendChild(block);
    });

    setupTimelineDragHandlers();
}

function setupTimelineDragHandlers() {
    const tracksArea = document.getElementById('timeline-tracks-area');
    if (!tracksArea) return;

    tracksArea.onmousedown = null;
    tracksArea.addEventListener('mousedown', (e) => {
        const handle = e.target.closest('.drag-handle');
        if (!handle) return;

        isDraggingTimeline = true;
        activeDragHandle = handle.dataset.side;
        activeDragSegmentIdx = parseInt(handle.dataset.index);
        
        const segments = state.selectedProject.raw_transcript.segments;
        const seg = segments[activeDragSegmentIdx];
        
        dragStartX = e.clientX;
        dragStartVal = activeDragHandle === 'left' ? seg.start : seg.end;

        document.addEventListener('mousemove', onTimelineMouseMove);
        document.addEventListener('mouseup', onTimelineMouseUp);
        
        e.preventDefault();
    });
}

function onTimelineMouseMove(e) {
    if (!isDraggingTimeline) return;

    const dx = e.clientX - dragStartX;
    const dt = dx / PIXELS_PER_SECOND;
    let newVal = parseFloat((dragStartVal + dt).toFixed(2));

    const segments = state.selectedProject.raw_transcript.segments;
    const seg = segments[activeDragSegmentIdx];
    const prevSeg = segments[activeDragSegmentIdx - 1];
    const nextSeg = segments[activeDragSegmentIdx + 1];

    if (activeDragHandle === 'left') {
        const minStart = prevSeg ? prevSeg.end + 0.1 : 0.0;
        const maxStart = seg.end - 0.2;
        newVal = Math.max(minStart, Math.min(newVal, maxStart));
        seg.start = newVal;
    } else {
        const minEnd = seg.start + 0.2;
        const maxEnd = nextSeg ? nextSeg.start - 0.1 : (state.selectedProject.raw_transcript.duration || 9999.0);
        newVal = Math.max(minEnd, Math.min(newVal, maxEnd));
        seg.end = newVal;
    }

    const blocks = document.querySelectorAll('.timeline-segment-block');
    const block = blocks[activeDragSegmentIdx];
    if (block) {
        block.style.left = `${seg.start * PIXELS_PER_SECOND}px`;
        block.style.width = `${(seg.end - seg.start) * PIXELS_PER_SECOND}px`;
    }

    const inputs = document.querySelectorAll('.subtitle-text-input');
    const row = inputs[activeDragSegmentIdx]?.closest('.subtitle-block-row');
    if (row) {
        const badge = row.querySelector('.subtitle-time-badge');
        if (badge) {
            badge.innerText = `${seg.start.toFixed(1)}s - ${seg.end.toFixed(1)}s`;
        }
    }
}

function onTimelineMouseUp() {
    isDraggingTimeline = false;
    document.removeEventListener('mousemove', onTimelineMouseMove);
    document.removeEventListener('mouseup', onTimelineMouseUp);
    renderVisualTimeline();
    saveProjectSettingsToServer();
}

async function saveProjectSettingsToServer() {
    if (!state.selectedProject) return;
    
    const subtitle_style_settings = {
        fontFamily: document.getElementById('style-font-family').value,
        fontSize: parseInt(document.getElementById('style-font-size').value),
        color: document.getElementById('style-text-color').value,
        highlightColor: document.getElementById('style-highlight-color').value,
        backgroundType: document.getElementById('style-bg-type').value,
        backgroundColor: document.getElementById('style-bg-color').value,
        animation: document.getElementById('style-animation').value
    };
    
    const bgm_track = document.getElementById('bgm-track').value;
    const bgm_settings = bgm_track ? {
        asset: bgm_track,
        volume: parseFloat(document.getElementById('bgm-volume').value),
        enableDucking: document.getElementById('bgm-enable-ducking').checked
    } : null;
    
    try {
        const pid = state.selectedProject.project_id;
        await fetch(`${API_BASE}/v1/projects/${pid}/settings`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ subtitle_style_settings, bgm_settings })
        });
        
        state.selectedProject.subtitle_style = subtitle_style_settings;
        state.selectedProject.bgm_settings = bgm_settings;
    } catch(e) {
        console.error("Failed to sync settings with server", e);
    }
}

function populateSettingsForm() {
    const proj = state.selectedProject;
    if (!proj) return;

    const style = proj.subtitle_style || {};
    document.getElementById('style-font-family').value = style.fontFamily || "Noto Sans Thai";
    document.getElementById('style-font-size').value = style.fontSize || 44;
    document.getElementById('style-text-color').value = style.color || "#ffffff";
    document.getElementById('style-highlight-color').value = style.highlightColor || "#FFD700";
    document.getElementById('style-bg-type').value = style.backgroundType || "card";
    document.getElementById('style-bg-color').value = style.backgroundColor || "#000000";
    document.getElementById('style-animation').value = style.animation || "pop";

    const bgm = proj.bgm_settings || {};
    document.getElementById('bgm-track').value = bgm.asset || "";
    document.getElementById('bgm-volume').value = bgm.volume !== undefined ? bgm.volume : 0.15;
    document.getElementById('bgm-volume-value').innerText = `${Math.round((bgm.volume !== undefined ? bgm.volume : 0.15) * 100)}%`;
    document.getElementById('bgm-enable-ducking').checked = bgm.enableDucking !== false;
}

// ─── API URLs ───
const API_BASE = ''; // Same origin

// ─── DOM Elements ───
const steps = document.querySelectorAll('.step');
const stepPanes = document.querySelectorAll('.step-pane');
const dropZone = document.getElementById('drop-zone');
const fileInput = document.getElementById('file-input');
const uploadProgressContainer = document.getElementById('upload-progress-container');
const uploadProgressBar = document.getElementById('upload-progress-bar');
const uploadProgressLabel = document.getElementById('upload-progress-label');
const serverVideosList = document.getElementById('server-videos-list');
const projectSelect = document.getElementById('project-select');

// Transcription Page
const startTranscriptionBtn = document.getElementById('start-transcription-btn');
const transcriptionStatusText = document.getElementById('transcription-status-text');
const stageWhisper = document.getElementById('stage-whisper');
const stageThaiFix = document.getElementById('stage-thai-fix');
const stageAiCorrect = document.getElementById('stage-ai-correct');

// Subtitle Page
const subtitleSentencesList = document.getElementById('subtitle-sentences-list');
const applySubtitleEditsBtn = document.getElementById('apply-subtitle-edits-btn');
const metaVideoName = document.getElementById('meta-video-name');
const metaVideoDuration = document.getElementById('meta-video-duration');
const metaSegmentCount = document.getElementById('meta-segment-count');

// Overlays Page
const suggestOverlaysBtn = document.getElementById('suggest-overlays-btn');
const downloadOverlaysBtn = document.getElementById('download-overlays-btn');
const overlaysListTbody = document.getElementById('overlays-list-tbody');
const addOverlayBtn = document.getElementById('add-overlay-btn');
const overlayEditCard = document.getElementById('overlay-edit-card');
const overlayForm = document.getElementById('overlay-form');
const formOverlayType = document.getElementById('form-overlay-type');
const formOverlayStart = document.getElementById('form-overlay-start');
const formOverlayEnd = document.getElementById('form-overlay-end');
const formOverlayContent = document.getElementById('form-overlay-content');
const formOverlayAsset = document.getElementById('form-overlay-asset');
const formOverlayStyle = document.getElementById('form-overlay-style');
const formOverlayPosition = document.getElementById('form-overlay-position');
const formOverlayVolume = document.getElementById('form-overlay-volume');
const saveOverlayBtn = document.getElementById('save-overlay-btn');

// Render Page
const rangeCutsList = document.getElementById('range-cuts-list');
const addRangeCutBtn = document.getElementById('add-range-cut-btn');
const triggerRenderBtn = document.getElementById('trigger-render-btn');
const renderProgressSection = document.getElementById('render-progress-section');
const renderCircleProgress = document.getElementById('render-circle-progress');
const renderProgressPercent = document.getElementById('render-progress-percent');
const renderStatusLabel = document.getElementById('render-status-label');
const renderStatusDetails = document.getElementById('render-status-details');
const renderedOutputSection = document.getElementById('rendered-output-section');
const finalVideoPlayer = document.getElementById('final-video-player');
const finalVideoDownload = document.getElementById('final-video-download');

// Circular progress initialization
const circleRadius = renderCircleProgress.r.baseVal.value;
const circleCircumference = circleRadius * 2 * Math.PI;
renderCircleProgress.style.strokeDasharray = `${circleCircumference} ${circleCircumference}`;
renderCircleProgress.style.strokeDashoffset = circleCircumference;

function setProgress(percent) {
    const offset = circleCircumference - (percent / 100) * circleCircumference;
    renderCircleProgress.style.strokeDashoffset = offset;
    renderProgressPercent.innerText = `${Math.round(percent)}%`;
}

// ─── Step Management ───
function goToStep(stepNum) {
    if (stepNum < 1 || stepNum > 5) return;
    
    // Stop any active rendering polling if leaving step 5
    if (state.currentStep === 5 && stepNum !== 5) {
        clearInterval(state.renderPollInterval);
    }
    
    state.currentStep = stepNum;
    
    // Update active state in UI stepper
    steps.forEach(step => {
        const num = parseInt(step.dataset.step);
        step.classList.remove('active', 'completed');
        if (num === stepNum) {
            step.classList.add('active');
        } else if (num < stepNum) {
            step.classList.add('completed');
        }
    });

    // Update active pane
    stepPanes.forEach(pane => pane.classList.remove('active'));
    document.getElementById(`step-pane-${stepNum}`).classList.add('active');

    // Show projects panel only on step 1
    const projectsPanel = document.getElementById('projects-panel');
    if (projectsPanel) {
        projectsPanel.style.display = (stepNum === 1 && state.projects.length > 0) ? 'block' : 'none';
    }

    // Run pane-specific loader
    onEnterStep(stepNum);
}

function onEnterStep(stepNum) {
    stopLivePreviewLoop();
    const v3 = document.getElementById('preview-video');
    const v4 = document.getElementById('preview-video-step4');
    if (v3) v3.pause();
    if (v4) v4.pause();

    if (stepNum === 1) {
        loadVideos();
        loadProjects();
    } else if (stepNum === 2) {
        updateTranscriptionStageUI();
    } else if (stepNum === 3) {
        renderSubtitleEditor();
        renderVisualTimeline();
        if (state.selectedProject && v3) {
            v3.src = getVideoSrcUrl(state.selectedProject.video_name);
            startLivePreviewLoop(v3, document.getElementById('preview-subtitle-overlay'));
        }
    } else if (stepNum === 4) {
        loadSFXList();
        renderOverlaysTable();
        if (state.selectedProject && v4) {
            v4.src = getVideoSrcUrl(state.selectedProject.video_name);
            startLivePreviewLoop(v4, document.getElementById('preview-subtitle-overlay-step4'));
        }
    } else if (stepNum === 5) {
        renderRangeCuts();
        populateSettingsForm();
    }
}

// Bind stepper clicks
steps.forEach(step => {
    step.addEventListener('click', () => {
        const targetStep = parseInt(step.dataset.step);
        // Only allow switching to steps we have unlocked based on state
        if (targetStep === 1) {
            goToStep(1);
        } else if (targetStep === 2 && state.selectedVideo) {
            goToStep(2);
        } else if (targetStep === 3 && state.selectedProject && 
                  ['transcribed', 'ready', 'rendering', 'completed'].includes(state.selectedProject.status)) {
            goToStep(3);
        } else if (targetStep === 4 && state.selectedProject && 
                  ['transcribed', 'ready', 'rendering', 'completed'].includes(state.selectedProject.status)) {
            goToStep(4);
        } else if (targetStep === 5 && state.selectedProject && 
                  ['ready', 'rendering', 'completed'].includes(state.selectedProject.status)) {
            goToStep(5);
        }
    });
});

// ─── Step 1: Video Management ───

async function loadVideos() {
    serverVideosList.innerHTML = '<div class="loading-spinner"></div>';
    try {
        const response = await fetch(`${API_BASE}/v1/videos`);
        const data = await response.json();
        state.videos = data.videos;
        renderVideosList();
    } catch (e) {
        serverVideosList.innerHTML = `<p class="error">Failed to load videos: ${e.message}</p>`;
    }
}

async function loadProjects() {
    try {
        const response = await fetch(`${API_BASE}/v1/projects`);
        state.projects = await response.json();
        renderProjectsSelect();
        // If we're on step 1, show the projects panel
        if (state.currentStep === 1) {
            const projectsPanel = document.getElementById('projects-panel');
            if (projectsPanel) {
                projectsPanel.style.display = state.projects.length > 0 ? 'block' : 'none';
            }
        }
    } catch (e) {
        console.error("Failed to load projects", e);
    }
}

function renderVideosList() {
    if (state.videos.length === 0) {
        serverVideosList.innerHTML = '<p class="subtitle text-center">No video files on server. Upload one!</p>';
        return;
    }
    
    serverVideosList.innerHTML = '';
    state.videos.forEach(v => {
        const div = document.createElement('div');
        div.className = 'video-item';
        if (state.selectedVideo && state.selectedVideo.filename === v.filename) {
            div.classList.add('selected');
        }
        
        const dur = v.duration_seconds ? `${Math.round(v.duration_seconds)}s` : 'Unknown';
        const res = v.width && v.height ? `${v.width}x${v.height}` : '';
        
        div.innerHTML = `
            <div class="video-info">
                <span class="video-name">${v.filename}</span>
                <span class="video-meta">${v.size_mb} MB • ${dur} • ${res}</span>
            </div>
            <span class="source-badge ${v.source_type}">${v.source_type}</span>
        `;
        
        div.addEventListener('click', () => {
            document.querySelectorAll('.video-item').forEach(el => el.classList.remove('selected'));
            div.classList.add('selected');
            selectVideo(v);
        });
        
        serverVideosList.appendChild(div);
    });
}

function renderProjectsSelect() {
    // Keep hidden select for internal JS compatibility (backward compat for other listeners)
    projectSelect.innerHTML = '<option value="">-- Create New Project --</option>';
    state.projects.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.project_id;
        opt.text = `${p.video_name} (${p.status})`;
        if (state.selectedProject && state.selectedProject.project_id === p.project_id) {
            opt.selected = true;
        }
        projectSelect.appendChild(opt);
    });
    renderProjectsList();
}

function renderProjectsList() {
    const panel = document.getElementById('projects-panel');
    const grid = document.getElementById('projects-list-grid');
    const headerLabel = document.getElementById('header-project-label');
    if (!panel || !grid) return;

    // Update header label
    if (headerLabel) {
        if (state.selectedProject) {
            headerLabel.textContent = `${state.selectedProject.video_name} (${state.selectedProject.status})`;
        } else {
            headerLabel.textContent = '-- No Project --';
        }
    }

    if (state.projects.length === 0) {
        panel.style.display = 'none';
        return;
    }

    panel.style.display = 'block';

    const STATUS_COLORS = {
        'uploaded': '#3b82f6',
        'transcribing': '#f59e0b',
        'transcribed': '#10b981',
        'ready': '#8b5cf6',
        'rendering': '#f59e0b',
        'completed': '#10b981',
        'error': '#ef4444'
    };

    grid.innerHTML = '';
    state.projects.forEach(p => {
        const isSelected = state.selectedProject && state.selectedProject.project_id === p.project_id;
        const statusColor = STATUS_COLORS[p.status] || '#9ca3af';
        const date = new Date(p.created_at).toLocaleString();

        const card = document.createElement('div');
        card.className = 'project-card' + (isSelected ? ' selected' : '');
        card.innerHTML = `
            <div class="project-card-info">
                <div class="project-card-name">${p.video_name}</div>
                <div class="project-card-meta">
                    <span class="project-status-badge" style="background: ${statusColor}22; color: ${statusColor}; border: 1px solid ${statusColor}44;">${p.status}</span>
                    <span class="project-card-date">${date}</span>
                </div>
            </div>
            <div class="project-card-actions">
                <button type="button" class="btn btn-outline btn-small project-load-btn" data-pid="${p.project_id}">
                    ${isSelected ? '✓ Active' : 'Load'}
                </button>
                <button type="button" class="btn btn-danger btn-small project-delete-btn" data-pid="${p.project_id}" title="Delete project">
                    🗑
                </button>
            </div>
        `;

        // Load button
        card.querySelector('.project-load-btn').addEventListener('click', async (e) => {
            e.preventDefault();
            if (isSelected) return;
            try {
                const resp = await fetch(`${API_BASE}/v1/projects/${p.project_id}`);
                const proj = await resp.json();
                state.selectedProject = proj;
                populateSettingsForm();
                state.selectedVideo = { filename: proj.video_name, source_type: 'input' };
                renderProjectsList();

                if (proj.status === 'uploaded') {
                    goToStep(2);
                } else if (proj.status === 'transcribing') {
                    goToStep(2);
                    pollProjectTranscription();
                } else if (proj.status === 'transcribed') {
                    goToStep(3);
                } else if (['ready', 'rendering', 'completed'].includes(proj.status)) {
                    state.overlays = proj.overlays || [];
                    goToStep(4);
                }
            } catch (err) {
                showToast("Error loading project: " + err.message, "error");
            }
        });

        // Delete button
        card.querySelector('.project-delete-btn').addEventListener('click', async (e) => {
            e.preventDefault();
            e.stopPropagation();
            const confirmed = await showConfirmModal(`ลบโปรเจกต์ "${p.video_name}"?\nการลบจะไม่สามารถกู้คืนได้`);
            if (!confirmed) return;
            try {
                const resp = await fetch(`${API_BASE}/v1/projects/${p.project_id}`, { method: 'DELETE' });
                if (!resp.ok) throw new Error(await resp.text());
                showToast(`Project "${p.video_name}" deleted.`, 'info');
                // If we deleted the active project, reset state
                if (state.selectedProject && state.selectedProject.project_id === p.project_id) {
                    state.selectedProject = null;
                    state.selectedVideo = null;
                }
                await loadProjects();
            } catch (err) {
                showToast("Error deleting project: " + err.message, "error");
            }
        });

        grid.appendChild(card);
    });
}


projectSelect.addEventListener('change', async (e) => {
    const pid = e.target.value;
    if (pid) {
        try {
            const resp = await fetch(`${API_BASE}/v1/projects/${pid}`);
            const proj = await resp.json();
            state.selectedProject = proj;
            populateSettingsForm();
            
            // Re-align selected video metadata
            state.selectedVideo = {
                filename: proj.video_name,
                source_type: 'input' // default
            };
            
            // Advance steps based on status
            if (proj.status === 'uploaded') {
                goToStep(2);
            } else if (proj.status === 'transcribing') {
                goToStep(2);
                pollProjectTranscription();
            } else if (proj.status === 'transcribed') {
                goToStep(3);
            } else if (['ready', 'rendering', 'completed'].includes(proj.status)) {
                state.overlays = proj.overlays || [];
                goToStep(4);
            }
        } catch (err) {
            showToast("Error loading project: " + err.message, "error");
        }
    } else {
        state.selectedProject = null;
        state.selectedVideo = null;
        renderVideosList();
    }
});

async function selectVideo(video) {
    state.selectedVideo = video;
    
    // Automatically create project on server
    try {
        const response = await fetch(`${API_BASE}/v1/projects`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                video_name: video.filename,
                source_type: video.source_type
            })
        });
        state.selectedProject = await response.json();
        loadProjects();
        // Transition to step 2 (transcription)
        goToStep(2);
    } catch (e) {
        showToast("Failed to initialize project: " + e.message, "error");
    }
}

// Drag & Drop Upload
dropZone.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => {
    if (fileInput.files.length > 0) {
        uploadFile(fileInput.files[0]);
    }
});

dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropZone.classList.add('dragover');
});

dropZone.addEventListener('dragleave', () => {
    dropZone.classList.remove('dragover');
});

dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('dragover');
    if (e.dataTransfer.files.length > 0) {
        uploadFile(e.dataTransfer.files[0]);
    }
});

function uploadFile(file) {
    const xhr = new XMLHttpRequest();
    const formData = new FormData();
    formData.append('file', file);
    
    uploadProgressContainer.style.display = 'block';
    uploadProgressBar.style.width = '0%';
    uploadProgressLabel.innerText = 'Uploading 0%';
    
    xhr.upload.addEventListener('progress', (e) => {
        if (e.lengthComputable) {
            const percent = (e.loaded / e.total) * 100;
            uploadProgressBar.style.width = `${percent}%`;
            uploadProgressLabel.innerText = `Uploading ${Math.round(percent)}%`;
        }
    });
    
    xhr.addEventListener('load', async () => {
        if (xhr.status === 200) {
            const result = JSON.parse(xhr.responseText);
            uploadProgressLabel.innerText = 'Upload complete!';
            await loadVideos();
            
            // Select this newly uploaded video
            const matchingVideo = state.videos.find(v => v.filename === result.filename);
            if (matchingVideo) {
                selectVideo(matchingVideo);
            }
        } else {
            showToast('Upload failed: ' + xhr.responseText, "error");
            uploadProgressContainer.style.display = 'none';
        }
    });
    
    xhr.addEventListener('error', () => {
        showToast('Upload failed due to connection error', "error");
        uploadProgressContainer.style.display = 'none';
    });
    
    xhr.open('POST', `${API_BASE}/v1/upload`);
    xhr.send(formData);
}

// ─── Step 2: Transcription ───

function updateTranscriptionStageUI() {
    if (!state.selectedProject) return;
    
    const status = state.selectedProject.status;
    
    stageWhisper.className = 'stage-item';
    stageThaiFix.className = 'stage-item';
    stageAiCorrect.className = 'stage-item';
    
    if (status === 'uploaded') {
        stageWhisper.classList.add('pending');
        stageThaiFix.classList.add('pending');
        stageAiCorrect.classList.add('pending');
        startTranscriptionBtn.style.display = 'inline-flex';
        transcriptionStatusText.innerText = 'Ready to begin transcription pipeline.';
    } else if (status === 'transcribing') {
        stageWhisper.classList.add('running');
        stageThaiFix.classList.add('pending');
        stageAiCorrect.classList.add('pending');
        startTranscriptionBtn.style.display = 'none';
        transcriptionStatusText.innerText = 'Running Faster-Whisper on CPU/GPU... This may take a minute.';
    } else if (status === 'transcribed' || ['ready', 'rendering', 'completed'].includes(status)) {
        stageWhisper.classList.add('completed');
        stageThaiFix.classList.add('completed');
        stageAiCorrect.classList.add('completed');
        startTranscriptionBtn.style.display = 'none';
        transcriptionStatusText.innerHTML = `AI processing complete! <br><button class="btn btn-success" onclick="goToStep(3)" style="margin-top: 15px;">Open Human Loop Subtitle Editor</button>`;
    } else if (status === 'failed') {
        transcriptionStatusText.innerText = 'AI processing failed. Please check backend logs.';
        startTranscriptionBtn.style.display = 'inline-flex';
    }
}

startTranscriptionBtn.addEventListener('click', async () => {
    if (!state.selectedProject) return;
    
    try {
        const pid = state.selectedProject.project_id;
        const response = await fetch(`${API_BASE}/v1/projects/${pid}/transcribe`, {method: 'POST'});
        const data = await response.json();
        
        state.selectedProject.status = 'transcribing';
        updateTranscriptionStageUI();
        pollProjectTranscription();
    } catch (e) {
        showToast("Failed to start transcription: " + e.message, "error");
    }
});

function pollProjectTranscription() {
    clearInterval(state.transcribePollInterval);
    state.transcribePollInterval = setInterval(async () => {
        if (!state.selectedProject) {
            clearInterval(state.transcribePollInterval);
            return;
        }
        
        try {
            const pid = state.selectedProject.project_id;
            const response = await fetch(`${API_BASE}/v1/projects/${pid}`);
            const proj = await response.json();
            state.selectedProject = proj;
            
            updateTranscriptionStageUI();
            
            if (proj.status !== 'transcribing') {
                clearInterval(state.transcribePollInterval);
                loadProjects();
            }
        } catch (e) {
            console.error("Polling error", e);
        }
    }, 3000);
}

// ─── Step 3: Human loop Subtitles ───

function renderSubtitleEditor() {
    if (!state.selectedProject) return;
    
    metaVideoName.innerText = state.selectedProject.video_name;
    
    const transcript = state.selectedProject.raw_transcript;
    if (!transcript) {
        subtitleSentencesList.innerHTML = '<p class="error">No transcription loaded.</p>';
        return;
    }
    
    const duration = transcript.duration || 0;
    metaVideoDuration.innerText = `${Math.round(duration)} seconds`;
    
    const segments = transcript.segments || [];
    metaSegmentCount.innerText = segments.length;
    
    subtitleSentencesList.innerHTML = '';
    
    // The user edits plain text lines. We represent them as text inputs map 1-to-1 with segments.
    // So the human edits the segments individually to preserve 1-to-1 timing bounds.
    segments.forEach((seg, idx) => {
        const row = document.createElement('div');
        row.className = 'subtitle-block-row';
        
        row.innerHTML = `
            <div class="subtitle-time-badge">${seg.start.toFixed(1)}s - ${seg.end.toFixed(1)}s</div>
            <input type="text" class="subtitle-text-input" data-index="${idx}" value="${seg.text.replace(/"/g, '&quot;')}">
        `;
        
        subtitleSentencesList.appendChild(row);
    });
}

applySubtitleEditsBtn.addEventListener('click', async () => {
    if (!state.selectedProject) return;
    
    const inputs = document.querySelectorAll('.subtitle-text-input');
    const segments = [];
    inputs.forEach(input => {
        const idx = parseInt(input.dataset.index);
        const seg = state.selectedProject.raw_transcript.segments[idx];
        segments.push({
            text: input.value,
            start: seg.start,
            end: seg.end
        });
    });
    
    try {
        const pid = state.selectedProject.project_id;
        applySubtitleEditsBtn.innerText = 'Applying...';
        
        const response = await fetch(`${API_BASE}/v1/projects/${pid}/apply-edits`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ segments })
        });
        
        const result = await response.json();
        state.selectedProject.status = 'ready';
        state.selectedProject.aligned_transcript = result.aligned_transcript;
        
        applySubtitleEditsBtn.innerText = 'Saved!';
        setTimeout(() => applySubtitleEditsBtn.innerText = 'Apply Subtitle Edits', 1500);
        
        loadProjects();
        goToStep(4);
    } catch (e) {
        showToast("Failed to apply edits: " + e.message, "error");
        applySubtitleEditsBtn.innerText = 'Apply Subtitle Edits';
    }
});

// ─── Step 4: Overlays Timeline ───

async function loadSFXList() {
    if (!state.selectedProject) return;
    try {
        const pid = state.selectedProject.project_id;
        const response = await fetch(`${API_BASE}/v1/projects/${pid}/sfx`);
        const data = await response.json();
        state.sfxList = data.sfx;
        
        // Populate SFX dropdown
        formOverlayAsset.innerHTML = '';
        state.sfxList.forEach(sfx => {
            const opt = document.createElement('option');
            opt.value = sfx.asset;
            opt.text = sfx.name;
            formOverlayAsset.appendChild(opt);
        });
    } catch (e) {
        console.error("SFX loading error", e);
    }
}

suggestOverlaysBtn.addEventListener('click', async () => {
    if (!state.selectedProject) return;
    
    suggestOverlaysBtn.innerText = 'Suggesting (Ollama)...';
    try {
        const pid = state.selectedProject.project_id;
        const response = await fetch(`${API_BASE}/v1/projects/${pid}/suggest-overlays`, {method: 'POST'});
        const data = await response.json();
        
        state.overlays = data.overlays || [];
        renderOverlaysTable();
        
        suggestOverlaysBtn.innerText = 'Suggest Overlays (AI)';
    } catch (e) {
        showToast("Failed to fetch AI suggestions: " + e.message, "error");
        suggestOverlaysBtn.innerText = 'Suggest Overlays (AI)';
    }
});

downloadOverlaysBtn.addEventListener('click', () => {
    if (state.overlays.length === 0) {
        showToast("No overlays to download.", "error");
        return;
    }
    const dataStr = "data:text/json;charset=utf-8," + encodeURIComponent(JSON.stringify(state.overlays, null, 2));
    const downloadAnchor = document.createElement('a');
    downloadAnchor.setAttribute("href",     dataStr);
    downloadAnchor.setAttribute("download", `overlays_${state.selectedProject.project_id.slice(0,8)}.json`);
    document.body.appendChild(downloadAnchor);
    downloadAnchor.click();
    downloadAnchor.remove();
});

function renderOverlaysTable() {
    overlaysListTbody.innerHTML = '';
    
    if (state.overlays.length === 0) {
        overlaysListTbody.innerHTML = '<tr><td colspan="6" class="text-center subtitle">No text or SFX overlays defined. Click AI Suggest!</td></tr>';
        const container = document.getElementById('overlay-timeline-tracks-area');
        if (container) container.innerHTML = '';
        return;
    }
    
    // Sort overlays by start time
    state.overlays.sort((a,b) => a.start - b.start);
    
    state.overlays.forEach((o, index) => {
        const tr = document.createElement('tr');
        tr.className = 'overlay-row';
        if (state.activeOverlayIndex === index) {
            tr.classList.add('selected');
        }
        
        const isText = o.type === 'text';
        const typeBadge = `<span class="overlay-badge ${o.type}">${o.type}</span>`;
        const timeCell = isText ? `${o.start.toFixed(1)}s - ${o.end && o.end !== -1 ? o.end.toFixed(1) + 's' : 'end'}` : `${o.start.toFixed(1)}s`;
        const contentCell = isText ? `"${o.content}"` : o.asset.split('/').pop();
        const styleCell = isText ? `${o.style} / ${o.position}` : '-';
        const volumeCell = isText ? '-' : `Vol: ${o.volume ?? 1.0}`;
        
        tr.innerHTML = `
            <td>${typeBadge}</td>
            <td>${timeCell}</td>
            <td style="font-weight: 500;">${contentCell}</td>
            <td>${styleCell}</td>
            <td>${volumeCell}</td>
            <td>
                <button class="btn btn-outline btn-small" onclick="editOverlay(${index})">Edit</button>
                <button class="btn btn-danger btn-small" onclick="deleteOverlay(${index})">Delete</button>
            </td>
        `;
        overlaysListTbody.appendChild(tr);
    });

    renderVisualOverlaysTimeline();
}

function renderVisualOverlaysTimeline() {
    const container = document.getElementById('overlay-timeline-tracks-area');
    const ruler = document.getElementById('overlay-timeline-ruler');
    if (!container || !ruler || !state.selectedProject) return;

    const duration = (state.selectedProject.raw_transcript && state.selectedProject.raw_transcript.duration) || 30.0;
    const timelineWidth = duration * PIXELS_PER_SECOND;
    container.style.width = `${timelineWidth}px`;
    ruler.style.width = `${timelineWidth}px`;

    // Render ticks
    ruler.innerHTML = '';
    for (let s = 0; s <= duration; s += 2) {
        const tick = document.createElement('div');
        tick.className = 'ruler-tick';
        tick.style.left = `${s * PIXELS_PER_SECOND}px`;
        tick.innerHTML = `<span class="tick-label">${s}s</span>`;
        ruler.appendChild(tick);
    }

    container.innerHTML = '';

    // Render track visual elements
    state.overlays.forEach((o, index) => {
        const block = document.createElement('div');
        block.className = `timeline-overlay-block timeline-overlay-${o.type}`;
        if (state.activeOverlayIndex === index) {
            block.classList.add('active');
        }

        const isText = o.type === 'text';
        const startSec = o.start;
        const endSec = isText ? (o.end !== -1 ? o.end : duration) : (o.start + 1.5);
        const widthSec = Math.max(0.3, endSec - startSec);

        block.style.left = `${startSec * PIXELS_PER_SECOND}px`;
        block.style.width = `${widthSec * PIXELS_PER_SECOND}px`;
        block.style.top = isText ? '8px' : '48px';

        const label = isText ? `Text: "${o.content}"` : `🔊 ${o.asset.split('/').pop()}`;
        block.innerText = label;
        block.title = `${o.type} overlay at ${o.start.toFixed(1)}s`;

        block.addEventListener('click', () => {
            editOverlay(index);
        });

        container.appendChild(block);
    });
}

// Overlay Form Logic
formOverlayType.addEventListener('change', () => {
    const isText = formOverlayType.value === 'text';
    document.getElementById('form-group-end').style.display = isText ? 'flex' : 'none';
    document.getElementById('form-group-content').style.display = isText ? 'flex' : 'none';
    document.getElementById('form-group-style').style.display = isText ? 'flex' : 'none';
    document.getElementById('form-group-position').style.display = isText ? 'flex' : 'none';
    document.getElementById('form-group-asset').style.display = isText ? 'none' : 'flex';
    document.getElementById('form-group-volume').style.display = isText ? 'none' : 'flex';
});

// Trigger change to set correct visibility
formOverlayType.dispatchEvent(new Event('change'));

addOverlayBtn.addEventListener('click', () => {
    state.activeOverlayIndex = null;
    overlayForm.reset();
    formOverlayType.dispatchEvent(new Event('change'));
    saveOverlayBtn.innerText = 'Add Overlay';
    overlayEditCard.querySelector('h3').innerText = 'Add Overlay';
    renderVisualOverlaysTimeline();
});

function editOverlay(idx) {
    state.activeOverlayIndex = idx;
    const o = state.overlays[idx];
    
    formOverlayType.value = o.type;
    formOverlayType.dispatchEvent(new Event('change'));
    
    formOverlayStart.value = o.start;
    if (o.type === 'text') {
        formOverlayEnd.value = o.end;
        formOverlayContent.value = o.content;
        formOverlayStyle.value = o.style;
        formOverlayPosition.value = o.position;
    } else {
        formOverlayAsset.value = o.asset;
        formOverlayVolume.value = o.volume ?? 1.0;
    }
    
    saveOverlayBtn.innerText = 'Update Overlay';
    overlayEditCard.querySelector('h3').innerText = 'Edit Overlay';
    renderVisualOverlaysTimeline();
}

function deleteOverlay(idx) {
    if (confirm("Delete this overlay?")) {
        state.overlays.splice(idx, 1);
        renderOverlaysTable();
    }
}

overlayForm.addEventListener('submit', (e) => {
    e.preventDefault();
    
    const type = formOverlayType.value;
    const start = parseFloat(formOverlayStart.value);
    
    let newOverlay = { type, start };
    
    if (type === 'text') {
        newOverlay.end = parseFloat(formOverlayEnd.value);
        newOverlay.content = formOverlayContent.value;
        newOverlay.style = formOverlayStyle.value;
        newOverlay.position = formOverlayPosition.value;
        newOverlay.asset = "";
    } else {
        newOverlay.end = -1;
        newOverlay.asset = formOverlayAsset.value;
        newOverlay.volume = parseFloat(formOverlayVolume.value);
        newOverlay.style = "default";
        newOverlay.position = "center";
        newOverlay.content = "";
    }
    
    if (state.activeOverlayIndex !== null) {
        // Edit existing
        state.overlays[state.activeOverlayIndex] = newOverlay;
    } else {
        // Add new
        state.overlays.push(newOverlay);
    }
    
    // Save to projects overlays
    saveProjectOverlaysToServer();
    
    renderOverlaysTable();
    overlayForm.reset();
    formOverlayType.dispatchEvent(new Event('change'));
    state.activeOverlayIndex = null;
    saveOverlayBtn.innerText = 'Add Overlay';
    overlayEditCard.querySelector('h3').innerText = 'Add Overlay';
});

async function saveProjectOverlaysToServer() {
    if (!state.selectedProject) return;
    try {
        const pid = state.selectedProject.project_id;
        await fetch(`${API_BASE}/v1/projects/${pid}/suggest-overlays`, {
            // Re-using endpoint logic but updating list directly
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(state.overlays)
        });
    } catch(e) {
        console.error("Failed to sync overlays", e);
    }
}

// ─── Step 5: Render final composition ───

function renderRangeCuts() {
    rangeCutsList.innerHTML = '';
    
    if (state.clipRanges.length === 0) {
        // Default range is the entire duration
        const dur = state.selectedProject.aligned_transcript ? state.selectedProject.aligned_transcript.duration : 30.0;
        state.clipRanges = [{ start: 0.0, end: parseFloat(dur.toFixed(2)) }];
    }
    
    state.clipRanges.forEach((range, index) => {
        const div = document.createElement('div');
        div.className = 'range-cut-item';
        div.innerHTML = `
            <div class="form-group" style="margin-bottom:0;">
                <label>Start (s)</label>
                <input type="number" class="range-start-input" data-index="${index}" step="0.1" value="${range.start}">
            </div>
            <div class="form-group" style="margin-bottom:0;">
                <label>End (s)</label>
                <input type="number" class="range-end-input" data-index="${index}" step="0.1" value="${range.end}">
            </div>
            <button class="btn btn-danger btn-small" style="margin-top:20px;" onclick="deleteRangeCut(${index})">Remove</button>
        `;
        rangeCutsList.appendChild(div);
    });
    
    // Bind inputs to save back
    document.querySelectorAll('.range-start-input').forEach(el => {
        el.addEventListener('change', (e) => {
            const idx = parseInt(e.target.dataset.index);
            state.clipRanges[idx].start = parseFloat(e.target.value);
        });
    });
    document.querySelectorAll('.range-end-input').forEach(el => {
        el.addEventListener('change', (e) => {
            const idx = parseInt(e.target.dataset.index);
            state.clipRanges[idx].end = parseFloat(e.target.value);
        });
    });
}

addRangeCutBtn.addEventListener('click', () => {
    const lastRange = state.clipRanges[state.clipRanges.length - 1];
    const newStart = lastRange ? lastRange.end : 0.0;
    const dur = state.selectedProject.aligned_transcript ? state.selectedProject.aligned_transcript.duration : 30.0;
    state.clipRanges.push({ start: newStart, end: parseFloat(dur.toFixed(2)) });
    renderRangeCuts();
});

function deleteRangeCut(index) {
    if (state.clipRanges.length > 1) {
        state.clipRanges.splice(index, 1);
        renderRangeCuts();
    } else {
        showToast("You must have at least one cut segment.", "error");
    }
}

triggerRenderBtn.addEventListener('click', async () => {
    if (!state.selectedProject) return;
    
    const pid = state.selectedProject.project_id;
    const mode = document.querySelector('input[name="render-mode"]:checked').value;
    const subtitle_style = document.getElementById('render-subtitle-style').value;
    const trim_silence = document.getElementById('render-trim-silence').checked;
    const auto_zoom = document.getElementById('render-auto-zoom').checked;
    const smart_crop = document.getElementById('render-smart-crop').checked;
    
    const subtitle_style_settings = {
        fontFamily: document.getElementById('style-font-family').value,
        fontSize: parseInt(document.getElementById('style-font-size').value),
        color: document.getElementById('style-text-color').value,
        highlightColor: document.getElementById('style-highlight-color').value,
        backgroundType: document.getElementById('style-bg-type').value,
        backgroundColor: document.getElementById('style-bg-color').value,
        animation: document.getElementById('style-animation').value
    };
    
    const bgm_track = document.getElementById('bgm-track').value;
    const bgm_settings = bgm_track ? {
        asset: bgm_track,
        volume: parseFloat(document.getElementById('bgm-volume').value),
        enableDucking: document.getElementById('bgm-enable-ducking').checked
    } : null;

    const payload = {
        mode,
        subtitle_style,
        subtitle_style_settings,
        bgm_settings,
        trim_silence,
        auto_zoom,
        smart_crop,
        overlays: state.overlays,
        clip_ranges: state.clipRanges
    };
    
    triggerRenderBtn.disabled = true;
    triggerRenderBtn.innerText = 'Submitting Job...';
    
    renderProgressSection.style.display = 'flex';
    renderedOutputSection.style.display = 'none';
    
    try {
        const response = await fetch(`${API_BASE}/v1/projects/${pid}/render`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        });
        const job = await response.json();
        
        setProgress(0);
        renderStatusLabel.innerText = 'Queued';
        renderStatusDetails.innerText = 'Waiting for rendering worker...';
        
        pollRenderJob(job.task_id);
    } catch (e) {
        showToast("Rendering job failed to start: " + e.message, "error");
        triggerRenderBtn.disabled = false;
        triggerRenderBtn.innerText = 'Render Final Video';
    }
});

function pollRenderJob(taskId) {
    clearInterval(state.renderPollInterval);
    state.renderPollInterval = setInterval(async () => {
        try {
            const response = await fetch(`${API_BASE}/v1/jobs/${taskId}`);
            const job = await response.json();
            
            const progress = job.progress_percent || 0;
            setProgress(progress);
            
            if (job.status === 'queued') {
                renderStatusLabel.innerText = 'Queued';
                renderStatusDetails.innerText = 'Job is in the processing queue';
            } else if (job.status === 'processing') {
                renderStatusLabel.innerText = 'Rendering';
                renderStatusDetails.innerText = `Stitching cuts & running Remotion renderer (stage details available in server)`;
            } else if (job.status === 'completed') {
                clearInterval(state.renderPollInterval);
                renderStatusLabel.innerText = 'Complete!';
                renderProgressSection.style.display = 'none';
                
                // Show output
                renderedOutputSection.style.display = 'block';
                
                // Remotion output files are written to /app/output
                // The backend uvicorn serves from /app/public and does not directly mount output files.
                // However, FastAPI can expose static files from output!
                // Wait, uvicorn doesn't expose OUTPUT_DIR directly as static files.
                // But in api.py, the endpoint `/v1/jobs/{task_id}` details returns `"output_path"`.
                // Can we expose it?
                // Yes! Let's check how the video file is accessed.
                // FastAPI's `/v1/outputs` lists output files. We can download output files by creating an endpoint!
                // Wait! Let's see if we have an endpoint for downloading files or if uvicorn can mount output folder.
                // In api.py, we mounted:
                // `app.mount("/", StaticFiles(directory="/app/public", html=True), name="public")`
                // But `/app/public/assets` is symlinked to `/app/assets`.
                // Can we symlink /app/output or add a route for `/output`?
                // Let's check if api.py has `/v1/jobs/{task_id}` which returns output_path.
                // We should expose `/output` directory as static files as well so the browser can play them!
                // Let's add `/output` mount to api.py:
                // `app.mount("/output", StaticFiles(directory="/app/output"), name="output")`
                // That way, we can download the final output directly at `/output/{filename}`!
                // Wait, did we do that? Yes, we can add it or did we already do it?
                // In api.py, we only mounted:
                // `app.mount("/", StaticFiles(directory="/app/public", html=True), name="public")`
                // So let's add `app.mount("/output", StaticFiles(directory="/app/output"), name="output")` right before `/` mount!
                // This is extremely important so that `final-video-player` can play the video!
                
                const filename = job.output_path.split('/').pop();
                finalVideoPlayer.src = `/output/${filename}`;
                finalVideoDownload.href = `/output/${filename}`;
                
                triggerRenderBtn.disabled = false;
                triggerRenderBtn.innerText = 'Render Final Video';
            } else if (job.status === 'failed') {
                clearInterval(state.renderPollInterval);
                renderStatusLabel.innerText = 'Failed';
                renderStatusDetails.innerText = `Error: ${job.error || 'Check server logs'}`;
                triggerRenderBtn.disabled = false;
                triggerRenderBtn.innerText = 'Render Final Video';
            }
        } catch (e) {
            console.error("Poller error", e);
        }
    }, 2000);
}

// Bind auto-save change listeners to Style/BGM forms
const stylingInputs = [
    'style-font-family', 'style-font-size', 'style-text-color',
    'style-highlight-color', 'style-bg-type', 'style-bg-color', 'style-animation',
    'bgm-track', 'bgm-volume', 'bgm-enable-ducking'
];
stylingInputs.forEach(id => {
    const el = document.getElementById(id);
    if (el) {
        el.addEventListener('change', () => {
            if (id === 'bgm-volume') {
                document.getElementById('bgm-volume-value').innerText = `${Math.round(el.value * 100)}%`;
            }
            saveProjectSettingsToServer();
        });
    }
});

// Initial Load
goToStep(1);
