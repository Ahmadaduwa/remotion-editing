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

// Circular progress initialization (element is inside render drawer)
let circleRadius = 42, circleCircumference = 42 * 2 * Math.PI;
if (renderCircleProgress) {
    circleRadius = renderCircleProgress.r.baseVal.value;
    circleCircumference = circleRadius * 2 * Math.PI;
    renderCircleProgress.style.strokeDasharray = `${circleCircumference} ${circleCircumference}`;
    renderCircleProgress.style.strokeDashoffset = circleCircumference;
}

function setProgress(percent) {
    const offset = circleCircumference - (percent / 100) * circleCircumference;
    renderCircleProgress.style.strokeDashoffset = offset;
    renderProgressPercent.innerText = `${Math.round(percent)}%`;
}

// ─── Step Management ───
function goToStep(stepNum) {
    // Map old steps 4 & 5 into the NLE workspace (step 3)
    if (stepNum === 4 || stepNum === 5) stepNum = 3;
    if (stepNum < 1 || stepNum > 3) return;

    state.currentStep = stepNum;

    // Update stepper UI (stepper now has only 3 steps)
    steps.forEach(step => {
        const num = parseInt(step.dataset.step);
        step.classList.remove('active', 'completed');
        if (num === stepNum) {
            step.classList.add('active');
        } else if (num < stepNum) {
            step.classList.add('completed');
        }
    });

    // Update active pane (step-pane-4/5 are hidden in HTML)
    stepPanes.forEach(pane => pane.classList.remove('active'));
    const targetPane = document.getElementById(`step-pane-${stepNum}`);
    if (targetPane) targetPane.classList.add('active');

    // Show projects panel only on step 1
    const projectsPanel = document.getElementById('projects-panel');
    if (projectsPanel) {
        projectsPanel.style.display = (stepNum === 1 && state.projects.length > 0) ? 'block' : 'none';
    }

    // Show/hide header project area
    const headerProjectArea = document.getElementById('header-project-area');
    if (headerProjectArea) {
        headerProjectArea.style.display = (stepNum > 1 && state.selectedProject) ? 'flex' : 'none';
    }

    // Run pane-specific loader
    onEnterStep(stepNum);
}

function onEnterStep(stepNum) {
    stopLivePreviewLoop();
    // Pause any legacy video elements
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
        // NLE workspace: initialize or refresh
        initNLE();
    }
}

// Bind stepper clicks (3-step flow)
steps.forEach(step => {
    step.addEventListener('click', () => {
        const targetStep = parseInt(step.dataset.step);
        if (targetStep === 1) {
            goToStep(1);
        } else if (targetStep === 2 && state.selectedVideo) {
            goToStep(2);
        } else if (targetStep === 3 && state.selectedProject &&
                  ['transcribed', 'ready', 'rendering', 'completed'].includes(state.selectedProject.status)) {
            goToStep(3);
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
    
    // Enable auto-edit button
    const autoEditBtn = document.getElementById('auto-edit-btn');
    if (autoEditBtn) autoEditBtn.disabled = false;
    
    // Update auto-edit summary
    const summaryEl = document.getElementById('auto-edit-summary');
    if (summaryEl) {
        const dur = video.duration_seconds ? `${Math.round(video.duration_seconds)}s` : 'Unknown';
        summaryEl.innerText = `✅ Selected: ${video.filename} (${dur})`;
    }
    
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

async function applySubtitleEdits() {
    if (!state.selectedProject) return;
    
    const pid = state.selectedProject.project_id;
    const transcript = state.selectedProject.aligned_transcript || state.selectedProject.raw_transcript;
    if (!transcript || !transcript.segments) return;

    const segments = transcript.segments.map(seg => ({
        text: seg.text,
        start: seg.start,
        end: seg.end
    }));

    const btn = document.getElementById('apply-subtitle-edits-btn');
    if (btn) btn.innerText = 'Saving...';

    try {
        const response = await fetch(`${API_BASE}/v1/projects/${pid}/apply-edits`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ segments })
        });
        
        const result = await response.json();
        state.selectedProject.status = 'ready';
        state.selectedProject.aligned_transcript = result.aligned_transcript;
        
        // Save project settings to server as well
        await saveProjectSettingsToServer();

        if (btn) btn.innerText = 'Saved!';
        setTimeout(() => { if (btn) btn.innerText = '✓ Save Edits'; }, 1500);
        showToast("✓ All edits saved to server!", "success");
        renderNLETimeline();
    } catch (e) {
        showToast("Failed to save edits: " + e.message, "error");
        if (btn) btn.innerText = '✓ Save Edits';
    }
}

if (applySubtitleEditsBtn) {
    applySubtitleEditsBtn.addEventListener('click', applySubtitleEdits);
}

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

async function handleSuggestOverlays() {
    if (!state.selectedProject) return;
    
    const suggestBtn = document.getElementById('suggest-overlays-btn');
    if (suggestBtn) suggestBtn.innerText = 'Suggesting (Ollama)...';
    try {
        const pid = state.selectedProject.project_id;
        const response = await fetch(`${API_BASE}/v1/projects/${pid}/orchestrate`, {method: 'POST'});
        const data = await response.json();
        
        // Reload project to get newly created subtitles & overlays
        const responseProj = await fetch(`${API_BASE}/v1/projects/${pid}`);
        const proj = await responseProj.json();
        state.selectedProject = proj;
        state.overlays = proj.overlays || [];
        
        renderNLETimeline();
        showToast("✨ Overlays and SFX updated by AI Orchestrator!", "success");
        if (suggestBtn) suggestBtn.innerText = '✨ AI Suggest SFX';
    } catch (e) {
        showToast("Failed to fetch AI suggestions: " + e.message, "error");
        if (suggestBtn) suggestBtn.innerText = '✨ AI Suggest SFX';
    }
}

suggestOverlaysBtn.addEventListener('click', handleSuggestOverlays);

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
if (formOverlayType) {
    formOverlayType.addEventListener('change', () => {
        const isText = formOverlayType.value === 'text';
        const elEnd = document.getElementById('form-group-end');
        if (elEnd) elEnd.style.display = isText ? 'flex' : 'none';
        const elContent = document.getElementById('form-group-content');
        if (elContent) elContent.style.display = isText ? 'flex' : 'none';
        const elStyle = document.getElementById('form-group-style');
        if (elStyle) elStyle.style.display = isText ? 'flex' : 'none';
        const elPosition = document.getElementById('form-group-position');
        if (elPosition) elPosition.style.display = isText ? 'flex' : 'none';
        const elAsset = document.getElementById('form-group-asset');
        if (elAsset) elAsset.style.display = isText ? 'none' : 'flex';
        const elVolume = document.getElementById('form-group-volume');
        if (elVolume) elVolume.style.display = isText ? 'none' : 'flex';
    });

    // Trigger change to set correct visibility
    formOverlayType.dispatchEvent(new Event('change'));
}

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

function addRangeCut() {
    const lastRange = state.clipRanges[state.clipRanges.length - 1];
    const newStart = lastRange ? lastRange.end : 0.0;
    const dur = state.selectedProject?.aligned_transcript?.duration || 
                state.selectedProject?.raw_transcript?.duration || 30.0;
    state.clipRanges.push({ start: newStart, end: parseFloat(dur.toFixed(2)) });
}

function deleteRangeCut(index) {
    if (state.clipRanges.length > 1) {
        state.clipRanges.splice(index, 1);
        renderRangeCuts();
    } else {
        showToast("You must have at least one cut segment.", "error");
    }
}

async function triggerRender() {
    if (!state.selectedProject) return;

    const pid = state.selectedProject.project_id;
    const mode = document.querySelector('input[name="render-mode"]:checked')?.value || 'short';
    const subtitle_style = document.getElementById('render-subtitle-style')?.value || 'karaoke';
    const trim_silence = document.getElementById('render-trim-silence')?.checked ?? true;
    const auto_zoom = document.getElementById('render-auto-zoom')?.checked ?? true;
    const smart_crop = document.getElementById('render-smart-crop')?.checked ?? true;

    // New feature toggles from the NLE render drawer
    const enable_sfx     = document.getElementById('render-sfx-layer')?.checked ?? true;
    const enable_overlays= document.getElementById('render-text-overlays')?.checked ?? true;
    const enable_bgm_flag= document.getElementById('render-bgm')?.checked ?? true;
    const enable_ducking = document.getElementById('render-auto-ducking')?.checked ?? true;

    const subtitle_style_settings = {
        fontFamily:      document.getElementById('style-font-family')?.value || 'Noto Sans Thai',
        fontSize:        parseInt(document.getElementById('style-font-size')?.value || '44'),
        color:           document.getElementById('style-text-color')?.value || '#ffffff',
        highlightColor:  document.getElementById('style-highlight-color')?.value || '#FFD700',
        backgroundType:  document.getElementById('style-bg-type')?.value || 'card',
        backgroundColor: document.getElementById('style-bg-color')?.value || '#000000',
        animation:       document.getElementById('style-animation')?.value || 'pop',
    };

    const bgm_track = document.getElementById('bgm-track')?.value;
    const bgm_settings = (bgm_track && enable_bgm_flag) ? {
        asset:         bgm_track,
        volume:        parseFloat(document.getElementById('bgm-volume')?.value || '0.15'),
        enableDucking: enable_ducking,
    } : null;

    // Filter overlays based on toggles
    const filteredOverlays = (state.overlays || []).filter(o => {
        if ((o.type === 'audio' || o.type === 'sfx') && !enable_sfx) return false;
        if ((o.type === 'text' || o.type === 'overlay') && !enable_overlays) return false;
        return true;
    });

    const payload = {
        mode,
        subtitle_style,
        subtitle_style_settings,
        bgm_settings,
        trim_silence,
        auto_zoom,
        smart_crop,
        overlays:     filteredOverlays,
        clip_ranges:  state.clipRanges,
    };

    const btn = document.getElementById('trigger-render-btn');
    if (btn) { btn.disabled = true; btn.innerText = 'Submitting Job...'; }

    const progressSec = document.getElementById('render-progress-section');
    const outputSec   = document.getElementById('rendered-output-section');
    if (progressSec) progressSec.style.display = 'flex';
    if (outputSec)   outputSec.style.display   = 'none';

    try {
        const response = await fetch(`${API_BASE}/v1/projects/${pid}/render`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
        });
        const job = await response.json();

        setProgress(0);
        const lbl = document.getElementById('render-status-label');
        const det = document.getElementById('render-status-details');
        if (lbl) lbl.innerText = 'Queued';
        if (det) det.innerText = 'Waiting for rendering worker...';

        pollRenderJob(job.task_id);
    } catch (e) {
        showToast('Rendering job failed to start: ' + e.message, 'error');
        if (btn) { btn.disabled = false; btn.innerText = 'Render Final Video'; }
    }
}

// Keep legacy listener for backward compat (works if button is in DOM at page load)
if (triggerRenderBtn) {
    triggerRenderBtn.addEventListener('click', triggerRender);
}


function pollRenderJob(taskId) {
    clearInterval(state.renderPollInterval);
    state.renderPollInterval = setInterval(async () => {
        try {
            const response = await fetch(`${API_BASE}/v1/jobs/${taskId}`);
            const job = await response.json();

            const progress = job.progress_percent || 0;
            setProgress(progress);

            const lbl = document.getElementById('render-status-label');
            const det = document.getElementById('render-status-details');
            const progSec = document.getElementById('render-progress-section');
            const outSec  = document.getElementById('rendered-output-section');

            if (job.status === 'queued') {
                if (lbl) lbl.innerText = 'Queued';
                if (det) det.innerText = 'Job is in the processing queue';
            } else if (job.status === 'processing') {
                if (lbl) lbl.innerText = 'Rendering';
                if (det) det.innerText = `Stitching cuts & running Remotion renderer...`;
            } else if (job.status === 'completed') {
                clearInterval(state.renderPollInterval);
                if (lbl) lbl.innerText = 'Complete!';
                if (progSec) progSec.style.display = 'none';
                if (outSec)  outSec.style.display  = 'block';

                const filename = job.output_path.split('/').pop();
                const fvp = document.getElementById('final-video-player');
                const fvd = document.getElementById('final-video-download');
                if (fvp) fvp.src = `/output/${filename}`;
                if (fvd) { fvd.href = `/output/${filename}`; fvd.download = filename; }

                const btn = document.getElementById('trigger-render-btn');
                if (btn) { btn.disabled = false; btn.innerText = 'Render Final Video'; }

                showToast('🎉 Video rendered successfully!', 'success');
            } else if (job.status === 'failed') {
                clearInterval(state.renderPollInterval);
                if (lbl) lbl.innerText = 'Failed';
                if (det) det.innerText = `Error: ${job.error || 'Check server logs'}`;
                const btn = document.getElementById('trigger-render-btn');
                if (btn) { btn.disabled = false; btn.innerText = 'Render Final Video'; }
            }
        } catch (e) {
            console.error('Poller error', e);
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

// ══════════════════════════════════════════════════════════════════
//  NLE WORKSPACE MODULE — Professional Timeline Editor
// ══════════════════════════════════════════════════════════════════

const NLE = {
    zoom: 80,           // pixels per second
    duration: 0,        // video duration in seconds
    selectedBlock: null,
    selectedTrack: null,
    tool: 'select',
    sfxTriggered: new Set(),
    initialized: false,
    nleVideo: null,
};

const NLE_PX_PER_SEC_BASE = 80;

// ── INIT NLE ──────────────────────────────────────────────────────
function initNLE() {
    const proj = state.selectedProject;
    if (!proj) return;

    // Load project state elements
    state.overlays = proj.overlays || [];
    state.clipRanges = proj.clip_ranges || [];

    const video = document.getElementById('nle-preview-video');
    if (!video) return;

    NLE.nleVideo = video;

    // Set video source
    const videoSrc = getVideoSrcUrl(proj.video_name);
    if (!video.src || !video.src.endsWith(proj.video_name)) {
        video.src = videoSrc;
        video.load();
    }

    // Initialize on metadata load or immediately if already loaded
    const onMeta = () => {
        NLE.duration = video.duration || 0;
        renderNLETimeline();
    };

    if (video.readyState >= 1 && video.duration) {
        NLE.duration = video.duration;
        renderNLETimeline();
    } else {
        video.removeEventListener('loadedmetadata', onMeta);
        video.addEventListener('loadedmetadata', onMeta, { once: true });
    }

    // Transport controls
    document.getElementById('nle-play-pause').onclick = nleTogglePlay;
    document.getElementById('nle-goto-start').onclick = () => { video.currentTime = 0; NLE.sfxTriggered.clear(); };
    document.getElementById('nle-goto-end').onclick = () => { if (video.duration) video.currentTime = video.duration; };

    // Video events
    video.removeEventListener('timeupdate', nleOnTimeUpdate);
    video.addEventListener('timeupdate', nleOnTimeUpdate);
    video.addEventListener('ended', () => {
        document.getElementById('nle-play-pause').textContent = '▶';
    }, { once: false });

    // Space bar play/pause
    document.onkeydown = (e) => {
        if (e.code === 'Space' && e.target.tagName !== 'TEXTAREA' && e.target.tagName !== 'INPUT') {
            e.preventDefault();
            nleTogglePlay();
        }
    };

    // Zoom controls
    const zoomSlider = document.getElementById('tl-zoom-slider');
    if (zoomSlider && !zoomSlider._nlebound) {
        zoomSlider._nlebound = true;
        zoomSlider.oninput = () => {
            NLE.zoom = parseInt(zoomSlider.value);
            const mult = (NLE.zoom / NLE_PX_PER_SEC_BASE).toFixed(1);
            document.getElementById('tl-zoom-label').textContent = mult + '×';
            renderNLETimeline();
        };
        document.getElementById('tl-zoom-out').onclick = () => {
            zoomSlider.value = Math.max(30, NLE.zoom - 15);
            zoomSlider.dispatchEvent(new Event('input'));
        };
        document.getElementById('tl-zoom-in').onclick = () => {
            zoomSlider.value = Math.min(500, NLE.zoom + 15);
            zoomSlider.dispatchEvent(new Event('input'));
        };
    }

    // Timeline click to reposition playhead
    const scroll = document.getElementById('tl-tracks-scroll');
    if (scroll && !scroll._nlebound) {
        scroll._nlebound = true;
        scroll.addEventListener('click', (e) => {
            if (e.target.classList.contains('tl-block') ||
                e.target.classList.contains('tl-block-label') ||
                e.target.classList.contains('tl-resize-handle')) return;
            const rect = scroll.getBoundingClientRect();
            const x = e.clientX - rect.left + scroll.scrollLeft;
            const t = Math.max(0, Math.min(NLE.duration || 9999, x / NLE.zoom));
            video.currentTime = t;
            NLE.sfxTriggered.clear();
        });
    }

    // Inspector tabs
    document.querySelectorAll('.insp-tab').forEach(tab => {
        tab.onclick = () => {
            document.querySelectorAll('.insp-tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.insp-content').forEach(c => c.classList.remove('active'));
            tab.classList.add('active');
            const contentEl = document.getElementById('insp-tab-' + tab.dataset.tab);
            if (contentEl) contentEl.classList.add('active');
        };
    });

    // Inspector apply/delete
    const applyBtn = document.getElementById('insp-apply-btn');
    if (applyBtn) applyBtn.onclick = nleApplyBlockEdit;
    const deleteBtn = document.getElementById('insp-delete-btn');
    if (deleteBtn) deleteBtn.onclick = nleDeleteBlock;

    // NLE tool selection
    document.querySelectorAll('.nle-tool').forEach(btn => {
        btn.onclick = () => {
            document.querySelectorAll('.nle-tool').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            NLE.tool = btn.dataset.tool;
        };
    });

    // Render drawer button
    const openDrawerBtn = document.getElementById('open-render-drawer-btn');
    if (openDrawerBtn) openDrawerBtn.onclick = nleOpenRenderDrawer;

    const closeDrawerBtn = document.getElementById('render-drawer-close');
    if (closeDrawerBtn) closeDrawerBtn.onclick = nleCloseRenderDrawer;

    const drawerEl = document.getElementById('render-drawer');
    if (drawerEl) drawerEl.onclick = (e) => { if (e.target === drawerEl) nleCloseRenderDrawer(); };

    // Render button inside drawer
    const renderBtn = document.getElementById('trigger-render-btn');
    if (renderBtn) renderBtn.onclick = triggerRender;

    // Add range cut button
    const addRangeCutBtnNLE = document.getElementById('add-range-cut-btn');
    if (addRangeCutBtnNLE) addRangeCutBtnNLE.onclick = () => {
        addRangeCut();
        renderRangeCuts();
    };

    // AI Suggest overlays
    const suggestBtn = document.getElementById('suggest-overlays-btn');
    if (suggestBtn) suggestBtn.onclick = handleSuggestOverlays;

    // Apply subtitle edits
    const applyEditsBtn = document.getElementById('apply-subtitle-edits-btn');
    if (applyEditsBtn) applyEditsBtn.onclick = applySubtitleEdits;

    // Populate settings from project
    populateSettingsForm();
    renderRangeCuts();

    NLE.initialized = true;
}

// ── PLAY/PAUSE ────────────────────────────────────────────────────
function nleTogglePlay() {
    const video = document.getElementById('nle-preview-video');
    const btn = document.getElementById('nle-play-pause');
    if (!video) return;
    if (video.paused) {
        video.play().catch(() => {});
        if (btn) btn.textContent = '⏸';
        NLE.sfxTriggered.clear();

        // Start BGM preview
        const bgmTrack = document.getElementById('bgm-track')?.value;
        if (bgmTrack && !previewBGMAudio) {
            previewBGMAudio = new Audio(bgmTrack);
            previewBGMAudio.loop = true;
            previewBGMAudio.volume = parseFloat(document.getElementById('bgm-volume')?.value || '0.15');
        }
        if (previewBGMAudio && previewBGMAudio.paused) previewBGMAudio.play().catch(() => {});
    } else {
        video.pause();
        if (btn) btn.textContent = '▶';
        if (previewBGMAudio && !previewBGMAudio.paused) previewBGMAudio.pause();
    }
}

// ── TIME UPDATE → Playhead + Subtitles + SFX ──────────────────────
function nleOnTimeUpdate() {
    const video = document.getElementById('nle-preview-video');
    if (!video) return;
    const t = video.currentTime;

    // Timecode
    const tcEl = document.getElementById('nle-timecode');
    if (tcEl) tcEl.textContent = nleFormatTimecode(t);

    // Move playhead
    const ph = document.getElementById('tl-playhead');
    if (ph) ph.style.left = (t * NLE.zoom) + 'px';

    // Auto-scroll timeline to keep playhead in view
    const scroll = document.getElementById('tl-tracks-scroll');
    if (scroll && !video.paused) {
        const x = t * NLE.zoom;
        const sl = scroll.scrollLeft;
        const vw = scroll.clientWidth;
        if (x < sl + 40 || x > sl + vw - 80) {
            scroll.scrollLeft = Math.max(0, x - vw * 0.35);
        }
    }

    // Subtitle overlay
    if (document.getElementById('preview-sub-toggle')?.checked) {
        nleRenderSubtitleOverlay(t);
    } else {
        const subEl = document.getElementById('nle-sub-overlay');
        if (subEl) subEl.innerHTML = '';
    }

    // SFX trigger
    if (!video.paused && document.getElementById('preview-sfx-toggle')?.checked) {
        nleCheckSFX(t);
    }

    // BGM ducking
    if (previewBGMAudio) {
        const bgmVol = parseFloat(document.getElementById('bgm-volume')?.value || '0.15');
        const duck = document.getElementById('bgm-enable-ducking')?.checked;
        let targetVol = bgmVol;
        if (!video.paused) {
            if (duck && state.selectedProject) {
                const proj = state.selectedProject;
                const transcript = proj.aligned_transcript || proj.raw_transcript;
                if (transcript && transcript.segments) {
                    let minDistance = 9999.0;
                    for (const seg of transcript.segments) {
                        if (seg.words) {
                            for (const w of seg.words) {
                                if (t >= w.start && t <= w.end) {
                                    minDistance = 0.0;
                                    break;
                                }
                                const distStart = Math.abs(t - w.start);
                                const distEnd = Math.abs(t - w.end);
                                minDistance = Math.min(minDistance, distStart, distEnd);
                            }
                        }
                        if (minDistance === 0.0) break;
                    }
                    if (minDistance === 0.0) {
                        targetVol = bgmVol * 0.15;
                    } else if (minDistance < 0.5) {
                        const ratio = minDistance / 0.5;
                        targetVol = bgmVol * (0.15 + 0.85 * ratio);
                    }
                }
            }
            previewBGMAudio.volume = targetVol;
        }
    }
}

function nleFormatTimecode(secs) {
    const m = Math.floor(secs / 60);
    const s = Math.floor(secs % 60);
    const ms = Math.floor((secs % 1) * 1000);
    return `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}.${String(ms).padStart(3,'0')}`;
}

// ── SUBTITLE OVERLAY ──────────────────────────────────────────────
function nleRenderSubtitleOverlay(currentTime) {
    const overlayEl = document.getElementById('nle-sub-overlay');
    if (!overlayEl) return;

    const proj = state.selectedProject;
    if (!proj) { overlayEl.innerHTML = ''; return; }

    const transcript = proj.aligned_transcript || proj.raw_transcript;
    if (!transcript?.segments?.length) { overlayEl.innerHTML = ''; return; }

    const fontFamily = document.getElementById('style-font-family')?.value || 'Noto Sans Thai';
    const fontSize = parseInt(document.getElementById('style-font-size')?.value || '44');
    const textColor = document.getElementById('style-text-color')?.value || '#fff';
    const highlightColor = document.getElementById('style-highlight-color')?.value || '#FFD700';
    const scaledFontSize = Math.max(16, Math.round(fontSize * 0.5)); // scale down for preview

    const allWords = [];
    transcript.segments.forEach(seg => {
        if (seg.words) seg.words.forEach(w => allWords.push(w));
    });
    if (!allWords.length) { overlayEl.innerHTML = ''; return; }

    let activeIdx = -1;
    for (let i = 0; i < allWords.length; i++) {
        if (currentTime >= allWords[i].start && currentTime <= allWords[i].end + 0.05) {
            activeIdx = i; break;
        }
    }
    if (activeIdx === -1) {
        for (let i = allWords.length - 1; i >= 0; i--) {
            if (allWords[i].end < currentTime) { activeIdx = i; break; }
        }
    }
    if (activeIdx === -1) { overlayEl.innerHTML = ''; return; }

    const groupStart = Math.floor(activeIdx / 5) * 5;
    const group = allWords.slice(groupStart, Math.min(groupStart + 5, allWords.length));
    const isThai = group.some(w => /[\u0E00-\u0E7F]/.test(w.word));

    const html = group.map(w => {
        const isActive = currentTime >= w.start && currentTime <= w.end + 0.05;
        const color = isActive ? highlightColor : textColor;
        const scale = isActive ? 'scale(1.1)' : 'scale(1)';
        const clean = isThai ? w.word.replace(/\s+/g, '') : w.word;
        return `<span style="color:${color};transform:${scale};display:inline-block;transition:all 0.07s;font-weight:800;font-size:${scaledFontSize}px;font-family:${fontFamily};text-shadow:0 2px 5px rgba(0,0,0,0.9);${isThai ? 'margin:0;letter-spacing:0' : 'margin:0 2px'}">${clean}</span>${isThai ? '' : ' '}`;
    }).join('');

    overlayEl.innerHTML = `<div style="display:inline-block;background:rgba(0,0,0,0.55);border-radius:6px;padding:4px 10px;line-height:1.4">${html}</div>`;
}

// ── SFX TRIGGER ───────────────────────────────────────────────────
function nleCheckSFX(currentTime) {
    const overlays = state.overlays || [];
    overlays.forEach((o, idx) => {
        if (o.type !== 'audio' && o.type !== 'sfx') return;
        const key = `sfx_${idx}`;
        if (currentTime >= o.start && currentTime < o.start + 0.25) {
            if (!NLE.sfxTriggered.has(key)) {
                NLE.sfxTriggered.add(key);
                try {
                    const a = new Audio(o.asset);
                    a.volume = Math.min(1.0, o.volume ?? 1.0);
                    a.play().catch(() => {});
                } catch(e) {}
            }
        } else if (currentTime < o.start - 0.3) {
            NLE.sfxTriggered.delete(key);
        }
    });
}

// ── TIMELINE RENDERING ────────────────────────────────────────────
function renderNLETimeline() {
    const duration = NLE.duration || 60;
    const totalW = Math.max(800, duration * NLE.zoom + 200);

    // Size all track elements
    ['tl-ruler','tl-track-video','tl-track-subtitles','tl-track-overlays','tl-track-sfx'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.style.width = totalW + 'px';
    });

    nleRenderRuler(duration, totalW);
    nleRenderVideoTrack(duration, totalW);
    nleRenderSubtitleTrack(totalW);
    nleRenderOverlayTrack(totalW);
    nleRenderSFXTrack(totalW);
}

function nleRenderRuler(duration, totalW) {
    const ruler = document.getElementById('tl-ruler');
    if (!ruler) return;
    ruler.innerHTML = '';
    ruler.style.width = totalW + 'px';

    const pxPerSec = NLE.zoom;
    const interval = pxPerSec >= 200 ? 0.5 : pxPerSec >= 100 ? 1 : pxPerSec >= 50 ? 2 : pxPerSec >= 25 ? 5 : 10;
    const subDiv = 5;
    const subInterval = interval / subDiv;

    for (let t = 0; t <= duration + interval; t += subInterval) {
        const isMajor = Math.abs(t % interval) < 0.001;
        const x = t * pxPerSec;
        const tick = document.createElement('div');
        tick.className = 'tl-tick';
        tick.style.left = x + 'px';

        const line = document.createElement('div');
        line.className = 'tl-tick-line ' + (isMajor ? 'major' : 'minor');
        tick.appendChild(line);

        if (isMajor) {
            const lbl = document.createElement('div');
            lbl.className = 'tl-tick-label';
            lbl.textContent = nleFormatRulerTime(t);
            tick.appendChild(lbl);
        }
        ruler.appendChild(tick);
    }
}

function nleFormatRulerTime(secs) {
    const m = Math.floor(secs / 60);
    const s = Math.floor(secs % 60);
    return m > 0 ? `${m}:${String(s).padStart(2,'0')}` : `${s}s`;
}

function nleRenderVideoTrack(duration, totalW) {
    const track = document.getElementById('tl-track-video');
    if (!track) return;
    track.innerHTML = '';
    track.style.width = totalW + 'px';
    const proj = state.selectedProject;
    const block = nleCreateBlock({
        start: 0,
        end: duration,
        type: 'video',
        label: '▶ ' + (proj?.video_name || 'Video'),
    }, 'video', false);
    track.appendChild(block);
}

function nleRenderSubtitleTrack(totalW) {
    const track = document.getElementById('tl-track-subtitles');
    if (!track) return;
    track.innerHTML = '';
    track.style.width = totalW + 'px';

    const proj = state.selectedProject;
    if (!proj) return;
    const transcript = proj.aligned_transcript || proj.raw_transcript;
    if (!transcript?.segments) return;

    const allWords = [];
    transcript.segments.forEach(seg => {
        if (seg.words) seg.words.forEach(w => allWords.push(w));
    });

    const segments = nleGetSubtitleSegments(allWords);
    segments.forEach((seg, idx) => {
        const block = nleCreateBlock({
            start: seg.start,
            end: seg.end,
            type: 'subtitle',
            label: seg.text,
            segIndex: idx,
            segRef: seg,
        }, 'subtitle', true);
        track.appendChild(block);
    });
}

function nleRenderOverlayTrack(totalW) {
    const track = document.getElementById('tl-track-overlays');
    if (!track) return;
    track.innerHTML = '';
    track.style.width = totalW + 'px';

    const overlays = (state.overlays || []).filter(o => o.type === 'text' || o.type === 'watermark' || o.type === 'overlay');
    overlays.forEach((o, idx) => {
        const endTime = (o.end && o.end > 0) ? o.end : o.start + 2.0;
        const block = nleCreateBlock({
            start: o.start,
            end: endTime,
            type: 'overlay',
            label: o.content || 'Text',
            overlayIdx: idx,
        }, 'overlay', true);
        track.appendChild(block);
    });
}

function nleRenderSFXTrack(totalW) {
    const track = document.getElementById('tl-track-sfx');
    if (!track) return;
    track.innerHTML = '';
    track.style.width = totalW + 'px';

    const sfxItems = (state.overlays || []).filter(o => o.type === 'audio' || o.type === 'sfx');
    sfxItems.forEach((o, idx) => {
        const x = o.start * NLE.zoom;
        const block = document.createElement('div');
        block.className = 'tl-block tl-block-sfx';
        block.style.left = x + 'px';
        block.style.width = '28px';
        block.title = (o.asset || '').split('/').pop().replace(/\.[^.]+$/, '');
        block.innerHTML = '🔊';
        block.style.fontSize = '14px';
        block.onclick = () => {
            // Preview click: play SFX
            try {
                const a = new Audio(o.asset);
                a.volume = o.volume ?? 1.0;
                a.play().catch(() => {});
            } catch(e) {}
            nleSelectBlock(block, { start: o.start, end: o.start + 0.5, type: 'sfx', label: block.title }, 'sfx');
        };
        track.appendChild(block);
    });
}

// ── BLOCK FACTORY ─────────────────────────────────────────────────
function nleCreateBlock(blockData, trackType, resizable) {
    const el = document.createElement('div');
    el.className = `tl-block tl-block-${trackType}`;

    const x = blockData.start * NLE.zoom;
    const w = Math.max(18, (blockData.end - blockData.start) * NLE.zoom);
    el.style.left = x + 'px';
    el.style.width = w + 'px';

    const lbl = document.createElement('span');
    lbl.className = 'tl-block-label';
    lbl.textContent = blockData.label || '';
    el.appendChild(lbl);

    if (resizable) {
        const lh = document.createElement('div');
        lh.className = 'tl-resize-handle left';
        lh.addEventListener('mousedown', (e) => { e.stopPropagation(); nleInitResize(e, el, blockData, 'left'); });
        el.appendChild(lh);

        const rh = document.createElement('div');
        rh.className = 'tl-resize-handle right';
        rh.addEventListener('mousedown', (e) => { e.stopPropagation(); nleInitResize(e, el, blockData, 'right'); });
        el.appendChild(rh);
    }

    el.addEventListener('click', (e) => {
        e.stopPropagation();
        nleSelectBlock(el, blockData, trackType);
    });

    el._nleData = blockData;
    return el;
}

// ── RESIZE HANDLE DRAG ────────────────────────────────────────────
function nleInitResize(e, el, blockData, side) {
    e.preventDefault();
    const startX = e.clientX;
    const origStart = blockData.start;
    const origEnd   = blockData.end;

    document.querySelectorAll('.tl-resize-handle').forEach(h => h.classList.remove('resizing'));
    e.currentTarget.classList.add('resizing');

    const onMove = (e2) => {
        const dx = (e2.clientX - startX) / NLE.zoom;
        if (side === 'left') {
            blockData.start = Math.max(0, Math.min(origEnd - 0.1, origStart + dx));
        } else {
            blockData.end = Math.max(blockData.start + 0.1, Math.min(NLE.duration || 9999, origEnd + dx));
        }
        el.style.left  = (blockData.start * NLE.zoom) + 'px';
        el.style.width = Math.max(18, (blockData.end - blockData.start) * NLE.zoom) + 'px';

        // Live update inspector timing
        if (NLE.selectedBlock === blockData) {
            const si = document.getElementById('insp-start');
            const ei = document.getElementById('insp-end');
            if (si) si.value = blockData.start.toFixed(2);
            if (ei) ei.value = blockData.end.toFixed(2);
        }
    };

    const onUp = () => {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        document.querySelectorAll('.tl-resize-handle').forEach(h => h.classList.remove('resizing'));

        // Apply change to transcript data if subtitle block
        if (blockData.type === 'subtitle' && blockData.segRef) {
            nleRedistributeWords(blockData.segRef, blockData.start, blockData.end);
        }
        showToast('⏱ Timing adjusted', 'success');
    };

    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
}

// ── BLOCK SELECT → INSPECTOR ──────────────────────────────────────
function nleSelectBlock(el, blockData, trackType) {
    document.querySelectorAll('.tl-block.selected').forEach(b => b.classList.remove('selected'));
    el.classList.add('selected');
    NLE.selectedBlock = blockData;
    NLE.selectedTrack = trackType;

    // Show block form
    const emptyEl = document.getElementById('insp-empty-state');
    const formEl  = document.getElementById('insp-block-form');
    if (emptyEl) emptyEl.style.display = 'none';
    if (formEl)  formEl.style.display = 'flex';

    // Badge color
    const badge = document.getElementById('insp-block-badge');
    const colors = { video:'#2563eb', subtitle:'#059669', overlay:'#d97706', sfx:'#7c3aed' };
    if (badge) {
        badge.textContent = trackType.toUpperCase();
        badge.style.background = colors[trackType] || '#6b7280';
    }

    // Fields
    const textEl  = document.getElementById('insp-text');
    const startEl = document.getElementById('insp-start');
    const endEl   = document.getElementById('insp-end');
    if (textEl) { textEl.value = blockData.label || ''; textEl.disabled = (trackType === 'video'); }
    if (startEl) startEl.value = blockData.start.toFixed(2);
    if (endEl)   endEl.value   = blockData.end.toFixed(2);

    // Show/hide Pixabay search panel for overlay block type
    const pixabaySec = document.getElementById('insp-pixabay-section');
    if (pixabaySec) {
        if (trackType === 'overlay') {
            pixabaySec.style.display = 'block';
            
            // Populate key input if saved in localStorage or use default user key
            const savedKey = localStorage.getItem('pixabay_api_key') || '55845643-094149992ad8aa500c1909466';
            const keyInput = document.getElementById('pixabay-api-key-input');
            if (keyInput) keyInput.value = savedKey;

            // Show current selection preview if any
            const assetUrl = blockData.asset || '';
            const previewWrap = document.getElementById('pixabay-selected-preview');
            const previewName = document.getElementById('pixabay-selected-filename');
            if (assetUrl.startsWith('http')) {
                if (previewWrap) previewWrap.style.display = 'flex';
                if (previewName) previewName.innerText = assetUrl.split('/').pop().split('?')[0];
            } else {
                if (previewWrap) previewWrap.style.display = 'none';
            }
        } else {
            pixabaySec.style.display = 'none';
        }
    }

    // Switch to Edit tab
    document.querySelectorAll('.insp-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.insp-content').forEach(c => c.classList.remove('active'));
    const editTab  = document.querySelector('.insp-tab[data-tab="edit"]');
    const editContent = document.getElementById('insp-tab-edit');
    if (editTab) editTab.classList.add('active');
    if (editContent) editContent.classList.add('active');
}

// ── INSPECTOR APPLY ───────────────────────────────────────────────
function nleApplyBlockEdit() {
    const bd = NLE.selectedBlock;
    if (!bd) return;

    const newText  = document.getElementById('insp-text')?.value  || '';
    const newStart = parseFloat(document.getElementById('insp-start')?.value || '0');
    const newEnd   = parseFloat(document.getElementById('insp-end')?.value   || '0');

    if (isNaN(newStart) || isNaN(newEnd) || newStart >= newEnd) {
        showToast('⚠ Invalid timing values', 'error'); return;
    }

    bd.label = newText;
    const oldStart = bd.start, oldEnd = bd.end;
    bd.start = newStart;
    bd.end   = newEnd;

    if (bd.type === 'subtitle' && bd.segRef) {
        nleRedistributeWords(bd.segRef, newStart, newEnd);
        bd.segRef.text = newText;
    }
    if (bd.type === 'overlay' && bd.overlayIdx !== undefined && state.overlays[bd.overlayIdx]) {
        state.overlays[bd.overlayIdx].start   = newStart;
        state.overlays[bd.overlayIdx].end     = newEnd;
        state.overlays[bd.overlayIdx].content = newText;
    }

    renderNLETimeline();
    showToast('✓ Block updated', 'success');
}

// ── INSPECTOR DELETE ──────────────────────────────────────────────
function nleDeleteBlock() {
    const bd = NLE.selectedBlock;
    if (!bd || NLE.selectedTrack === 'video') return;

    if (NLE.selectedTrack === 'overlay' && bd.overlayIdx !== undefined) {
        state.overlays.splice(bd.overlayIdx, 1);
    } else if (NLE.selectedTrack === 'sfx' && bd.overlayIdx !== undefined) {
        state.overlays.splice(bd.overlayIdx, 1);
    }

    NLE.selectedBlock = null;
    const emptyEl = document.getElementById('insp-empty-state');
    const formEl  = document.getElementById('insp-block-form');
    if (emptyEl) emptyEl.style.display = 'flex';
    if (formEl)  formEl.style.display  = 'none';

    renderNLETimeline();
    showToast('�� Block deleted', 'info');
}

// ── SUBTITLE SEGMENTATION ─────────────────────────────────────────
function nleGetSubtitleSegments(allWords, wordsPerSeg = 5) {
    const segs = [];
    for (let i = 0; i < allWords.length; i += wordsPerSeg) {
        const group = allWords.slice(i, i + wordsPerSeg);
        if (!group.length) continue;
        const isThai = group.some(w => /[\u0E00-\u0E7F]/.test(w.word));
        segs.push({
            start: group[0].start,
            end:   group[group.length - 1].end,
            text:  group.map(w => isThai ? w.word.replace(/\s+/g,'') : w.word).join(isThai ? '' : ' '),
            words: group,
        });
    }
    return segs;
}

function nleRedistributeWords(seg, newStart, newEnd) {
    const origDur = seg.end - seg.start;
    const newDur  = newEnd  - newStart;
    const scale   = origDur > 0 ? newDur / origDur : 1;
    if (seg.words) {
        seg.words.forEach(w => {
            const relS = (w.start - seg.start) * scale;
            const relE = (w.end   - seg.start) * scale;
            w.start = Math.max(0, newStart + relS);
            w.end   = Math.max(w.start + 0.01, newStart + relE);
        });
    }
    seg.start = newStart;
    seg.end   = newEnd;
}

// ── RENDER DRAWER ─────────────────────────────────────────────────
function nleOpenRenderDrawer() {
    const drawer = document.getElementById('render-drawer');
    if (drawer) {
        drawer.style.display = 'flex';
        // Init progress ring inside drawer
        const circ = document.getElementById('render-circle-progress');
        if (circ && !circ._initDone) {
            circ._initDone = true;
            const r = circ.r.baseVal.value;
            const c = r * 2 * Math.PI;
            circ.style.strokeDasharray  = `${c} ${c}`;
            circ.style.strokeDashoffset = c;
            circleCircumference = c;
        }
        renderRangeCuts();
    }
}

function nleCloseRenderDrawer() {
    const drawer = document.getElementById('render-drawer');
    if (drawer) drawer.style.display = 'none';
}

// Override setProgress to also initialize the SVG ring if needed
const _origSetProgress = setProgress;
function setProgress(percent) {
    const circ = document.getElementById('render-circle-progress');
    if (circ) {
        const r = circ.r.baseVal.value;
        const c = r * 2 * Math.PI;
        const offset = c - (percent / 100) * c;
        circ.style.strokeDasharray  = `${c} ${c}`;
        circ.style.strokeDashoffset = offset;
    }
    const pct = document.getElementById('render-progress-percent');
    if (pct) pct.innerText = `${Math.round(percent)}%`;
}


// ── PIXABAY INTEGRATION ───────────────────────────────────────────
function togglePixabayKeyInput() {
    const wrapper = document.getElementById('pixabay-key-wrapper');
    if (wrapper) {
        wrapper.style.display = wrapper.style.display === 'none' ? 'flex' : 'none';
    }
}

async function searchPixabay() {
    const keyInput = document.getElementById('pixabay-api-key-input');
    const queryInput = document.getElementById('pixabay-search-input');
    const typeSelect = document.getElementById('pixabay-search-type');
    const grid = document.getElementById('pixabay-results-grid');

    if (!queryInput || !grid) return;

    const key = (keyInput && keyInput.value.trim()) || localStorage.getItem('pixabay_api_key') || '55845643-094149992ad8aa500c1909466';
    localStorage.setItem('pixabay_api_key', key); // Save key
    if (keyInput && !keyInput.value) keyInput.value = key;

    const query = queryInput.value.trim();
    if (!query) {
        showToast("🔍 Enter a search query first", "info");
        return;
    }

    grid.innerHTML = '<span style="grid-column:span 3; font-size:10px; color:var(--text-secondary); text-align:center; padding:10px 0;">Searching...</span>';

    try {
        const type = typeSelect ? typeSelect.value : 'photo';
        let url = `https://pixabay.com/api/?key=${key}&q=${encodeURIComponent(query)}&per_page=12`;
        if (type === 'video') {
            url = `https://pixabay.com/api/videos/?key=${key}&q=${encodeURIComponent(query)}&per_page=12`;
        }

        const resp = await fetch(url);
        if (!resp.ok) {
            throw new Error(`Pixabay API error: ${resp.status}`);
        }

        const data = await resp.json();
        const hits = data.hits || [];
        grid.innerHTML = '';

        if (hits.length === 0) {
            grid.innerHTML = '<span style="grid-column:span 3; font-size:10px; color:var(--text-muted); text-align:center; padding:10px 0;">No results found.</span>';
            return;
        }

        hits.forEach(hit => {
            const item = document.createElement('div');
            item.style.position = 'relative';
            item.style.cursor = 'pointer';
            item.style.borderRadius = '4px';
            item.style.overflow = 'hidden';
            item.style.background = '#1a202c';
            item.style.height = '50px';
            item.style.border = '1px solid var(--nle-border)';
            item.style.transition = 'border-color 0.12s';
            item.title = hit.tags || '';

            let previewUrl = hit.previewURL || '';
            let targetUrl = hit.webformatURL || '';

            if (type === 'video') {
                previewUrl = hit.userImageURL || 'https://pixabay.com/static/img/logo_square.png'; // fallback preview
                targetUrl = hit.videos?.tiny?.url || hit.videos?.medium?.url || '';
            }

            item.innerHTML = `<img src="${previewUrl}" style="width:100%; height:100%; object-fit:cover;">`;
            
            item.onclick = async () => {
                // Remove active classes
                grid.querySelectorAll('div').forEach(d => d.style.borderColor = 'var(--nle-border)');
                item.style.borderColor = 'var(--color-primary)';

                // Update selected filename preview
                const pdiv = document.getElementById('pixabay-selected-preview');
                const pf = document.getElementById('pixabay-selected-filename');
                if (pdiv) pdiv.style.display = 'flex';
                if (pf) pf.innerText = targetUrl.split('/').pop().split('?')[0];

                // Download the asset to the server locally
                let localAsset = targetUrl;
                try {
                    const dlResp = await fetch(`${API_BASE}/v1/download-asset?url=${encodeURIComponent(targetUrl)}`);
                    if (dlResp.ok) {
                        const dlData = await dlResp.json();
                        localAsset = dlData.local_path;
                    }
                } catch (e) {
                    console.warn("Failed to download asset to server, using remote URL", e);
                }

                // Update selected block data
                if (NLE.selectedBlock) {
                    NLE.selectedBlock.asset = localAsset;
                    NLE.selectedBlock.type = type === 'video' ? 'video' : 'image';
                    NLE.selectedBlock.label = query; // update label to match query

                    // Update corresponding overlay list element in state
                    const idx = NLE.selectedBlock.overlayIdx;
                    if (idx !== undefined && state.overlays[idx]) {
                        state.overlays[idx].asset = localAsset;
                        state.overlays[idx].type = type === 'video' ? 'video' : 'image';
                        state.overlays[idx].content = query;
                    }
                    
                    // Render NLE
                    renderNLETimeline();
                    showToast(`✓ Downloaded & selected Pixabay ${type}!`, 'success');
                }
            };

            grid.appendChild(item);
        });

    } catch (e) {
        console.error("Pixabay search error", e);
        grid.innerHTML = `<span style="grid-column:span 3; font-size:10px; color:#ef4444; text-align:center; padding:10px 0;">Error: ${e.message}</span>`;
    }
}

// Bind search actions
const searchBtn = document.getElementById('pixabay-search-btn');
if (searchBtn) searchBtn.onclick = searchPixabay;
const searchInp = document.getElementById('pixabay-search-input');
if (searchInp) {
    searchInp.onkeydown = (e) => {
        if (e.key === 'Enter') {
            searchPixabay();
        }
    }
}

// ══════════════════════════════════════════════════════════════════
//  AUTO-EDIT BUTTON — One-click transcribe + orchestrate + render
// ══════════════════════════════════════════════════════════════════

async function handleAutoEdit() {
    const video = state.selectedVideo;
    if (!video) {
        showToast("⚠ Select or upload a video first.", "error");
        return;
    }

    const mode = document.getElementById('auto-edit-mode')?.value || 'short';
    const clipCount = parseInt(document.getElementById('auto-edit-clip-count')?.value || '3');
    const statusEl = document.getElementById('auto-edit-summary');
    const btn = document.getElementById('auto-edit-btn');
    if (btn) { btn.disabled = true; btn.innerText = '⏳ Processing...'; }
    if (statusEl) statusEl.innerText = '🚀 Starting auto-edit pipeline...';

    try {
        // 1. Create project
        let projectId;
        const projResp = await fetch(`${API_BASE}/v1/projects`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ video_name: video.filename, source_type: video.source_type })
        });
        const project = await projResp.json();
        projectId = project.project_id;
        state.selectedProject = project;
        if (statusEl) statusEl.innerText = '📝 Project created, starting transcription...';

        // 2. Call auto-edit endpoint (transcribes + orchestrates with AI)
        const autoResp = await fetch(`${API_BASE}/v1/projects/${projectId}/transcribe`, {
            method: 'POST'
        });
        const autoData = await autoResp.json();
        if (statusEl) statusEl.innerText = '🔊 Transcribing with Whisper...';

        // 3. Poll until transcription is ready
        await new Promise((resolve, reject) => {
            const poll = setInterval(async () => {
                try {
                    const resp = await fetch(`${API_BASE}/v1/projects/${projectId}`);
                    const proj = await resp.json();
                    state.selectedProject = proj;
                    if (proj.status === 'ready') {
                        clearInterval(poll);
                        state.overlays = proj.overlays || [];
                        if (statusEl) statusEl.innerText = '✅ Transcription & AI orchestration complete!';
                        resolve();
                    } else if (proj.status === 'failed') {
                        clearInterval(poll);
                        reject(new Error('Transcription failed'));
                    } else {
                        if (statusEl) statusEl.innerText = `⏳ Processing... (${proj.status})`;
                    }
                } catch (e) {
                    // Continue polling
                }
            }, 2000);
        });

        // 4. Orchestrate for AI overlays/SFX/BGM if not already set
        if (!state.overlays || state.overlays.length === 0) {
            try {
                await fetch(`${API_BASE}/v1/projects/${projectId}/orchestrate`, { method: 'POST' });
                const resp = await fetch(`${API_BASE}/v1/projects/${projectId}`);
                const proj = await resp.json();
                state.selectedProject = proj;
                state.overlays = proj.overlays || [];
            } catch (e) {
                console.warn("Orchestration failed, continuing with defaults", e);
            }
        }

        // 5. Set clip ranges
        if (state.selectedProject.render_plan && state.selectedProject.render_plan.clip_ranges) {
            state.clipRanges = state.selectedProject.render_plan.clip_ranges;
        } else {
            state.clipRanges = [{ start: 0, end: Math.min(video.duration_seconds || 60, 30) }];
        }

        // 6. Navigate to NLE editor
        loadProjects();
        goToStep(3);
        showToast('✅ Auto-edit complete! Review in the editor, then export.', 'success');
    } catch (e) {
        showToast("❌ Auto-edit failed: " + e.message, "error");
        if (statusEl) statusEl.innerText = '❌ Error: ' + e.message;
    } finally {
        if (btn) { btn.disabled = false; btn.innerText = '⚡ Auto Edit Selected Video'; }
    }
}

// Bind auto-edit button
const autoEditBtn = document.getElementById('auto-edit-btn');
if (autoEditBtn) {
    autoEditBtn.addEventListener('click', handleAutoEdit);
}
