# Remotion Auto-Edit Development Roadmap (Features 2-5)

This document outlines the technical designs, implementation steps, and file structures for the selected features planned for the next development phase.

---

## 📌 Roadmap Summary
1. **[Feature 2]** Real-time HTML5 Preview Player
2. **[Feature 3]** Subtitle Styling & Animation Presets
3. **[Feature 4]** Background Music & Auto-Ducking
4. **[Feature 5]** Visual Subtitle Timeline Adjuster

---

## 🎥 [Feature 2] Real-time HTML5 Preview Player
To minimize rendering wait times, this feature simulates Remotion's final output in the web browser in real-time.

### 🛠️ Changes & Tasks:
1. **Frontend (UI/Layout):**
   - Add a video preview container `<div class="preview-container">` in Steps 3 and 4.
   - Include a `<video id="preview-player">` and a overlay container `<div class="html-subtitles-overlay">` absolute-positioned on top of the video player.
2. **Frontend (Logic - `app.js`):**
   - Bind to the player's `timeupdate` event.
   - Dynamically render the subtitles and highlighted words inside `html-subtitles-overlay` according to the current video playback head (`currentTime`).
   - Trigger custom sound effects using `new Audio(sfx_url).play()` when the video playback crosses any defined SFX timestamp.
3. **Live Sync:**
   - Synchronize text edits and timestamp modifications instantly so the preview player accurately represents the latest editor state.

---

## 🎨 [Feature 3] Subtitle Styling & Animation Presets
Give users the ability to customize text overlays and subtitle styles directly from the web interface.

### 🛠️ Changes & Tasks:
1. **Database Schema (`db.py`):**
   - Update `projects` and `jobs` tables to store subtitle styling parameters in a JSON object (font family, colors, drop shadows, animation choices).
2. **FastAPI Endpoints (`api.py`):**
   - Update `RenderProjectRequest` and `JobRequest` schemas to accept styling properties.
3. **Remotion Components (`src/`):**
   - Update [src/types.ts](file:///home/aduwa/projects/editing/remotion-blank/src/types.ts) to support style parameters.
   - Refactor [KaraokeSubtitles.tsx](file:///home/aduwa/projects/editing/remotion-blank/src/components/KaraokeSubtitles.tsx) and [StaticSubtitles.tsx](file:///home/aduwa/projects/editing/remotion-blank/src/components/StaticSubtitles.tsx):
     * Read and apply dynamic styles like `fontFamily`, `color`, and text outline.
     * Incorporate CSS animation keyframes (e.g., spring pop, scale bounce) for currently active words.
4. **Frontend UI Panel:**
   - Add a **Subtitle Styling** panel in Step 3 or Step 5:
     - Popular Thai fonts: *Kanit, Prompt, Noto Sans Thai* (loaded via Google Fonts).
     - Color pickers for primary text and karaoke highlight color.
     - Animation presets: *Default Glow, Bounce Up, Slide In, Word Pop*.

---

## 🎵 [Feature 4] Background Music & Auto-Ducking
Add stock music tracks behind the speech, with automatic volume attenuation (ducking) during active voice parts.

### 🛠️ Changes & Tasks:
1. **Assets & Storage:**
   - Create a directory for stock music files, e.g., `/app/assets/bgm/`.
   - Provide standard royalty-free background tracks (e.g., lofi.mp3, cinematic.wav, upbeat.mp3).
2. **Remotion Logic (`src/compositions/VideoComposition.tsx`):**
   - Add an `<Audio>` layer for the background music track.
   - Map transcription speech windows to dynamically calculate the music volume (Auto-Ducking):
     ```typescript
     // Dynamically adjust bgm volume based on whether speech is active
     const volume = interpolate(
       currentTime,
       speechTimeWindows, // Derived from Whisper word timestamps
       isSpeaking ? 0.08 : 0.35 // Duck to 8% during speech, restore to 35% during silence
     );
     ```
3. **Frontend UI:**
   - Add a **Background Music** selection menu in the render configuration screen (Step 5).
   - Add volume control sliders and a toggle for auto-ducking.

---

## ⏱️ [Feature 5] Visual Subtitle Timeline Adjuster
Provide a drag-and-drop visual editor to fine-tune subtitle segments and alignment.

### 🛠️ Changes & Tasks:
1. **Frontend (UI/UX - Step 3):**
   - Add a scrollable timeline container `<div class="timeline-scroll-container">` beneath the preview video player.
   - Render subtitle segments as horizontal block elements whose widths are proportional to their duration.
2. **Interaction:**
   - Implement drag-and-resize handles on the blocks (using vanilla JS handlers or a lightweight library like `interact.js`).
   - Users can:
     * Resize edges to adjust subtitle `start` and `end` times.
     * Drag entire blocks to shift their positions on the timeline.
3. **Backend Integration:**
   - Trigger a timing alignment API call `/v1/projects/{project_id}/apply-edits` when a block is resized/moved, redistributing word timestamps within the updated boundaries.

---

## 🛠️ Phase-by-Phase Implementation Plan
* **Phase 1:** Build **Feature 2 (Real-time Preview Player)** first. This is a prerequisite to verify styling, timings, and SFX in subsequent phases without rendering overhead.
* **Phase 2:** Build **Feature 3 (Subtitle Style UI & Remotion)** to support custom styles.
* **Phase 3:** Build **Feature 4 (Background Music & Auto-Ducking)** to add audio layers.
* **Phase 4:** Build **Feature 5 (Timeline UI)** to enable visual timing adjustment.
