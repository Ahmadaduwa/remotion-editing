/**
 * VideoComposition — Main video assembly component.
 *
 * Renders the preprocessed video (using OffthreadVideo), overlays subtitles,
 * handles custom text overlays, optional watermarks, and applies optional Ken Burns zoom.
 */
import React from "react";
import { AbsoluteFill, OffthreadVideo, staticFile, useVideoConfig, Audio, Sequence, useCurrentFrame, Img } from "remotion";
import type { VideoCompositionProps } from "../types";
import { KaraokeSubtitles } from "../components/KaraokeSubtitles";
import { StaticSubtitles } from "../components/StaticSubtitles";
import { TextOverlay } from "../components/TextOverlay";
import { Watermark } from "../components/Watermark";
import { AutoZoom } from "../components/AutoZoom";

export const VideoComposition: React.FC<VideoCompositionProps> = ({
  src,
  subtitleStyle,
  subtitleStyleSettings,
  bgmSettings,
  subtitles,
  overlays,
  autoZoom,
  mode,
}) => {
  const frame = useCurrentFrame();
  const { durationInFrames, fps } = useVideoConfig();
  const currentTime = frame / fps;
  const totalDurationSeconds = durationInFrames / fps;
  const isShort = mode === "short";

  // Use a placeholder video when no input source is provided during smoke tests.
  const videoSrc = staticFile(src || "assets/videos/placeholder.mp4");

  // Dynamic BGM volume calculation with smooth Auto-Ducking
  let bgmVolume = bgmSettings?.volume !== undefined ? bgmSettings.volume : 0.0;
  if (bgmSettings && bgmSettings.asset && bgmVolume > 0) {
    if (bgmSettings.enableDucking && subtitles && subtitles.length > 0) {
      // Calculate closest distance in time to any subtitle word
      let minDistance = 9999.0;
      for (const w of subtitles) {
        if (currentTime >= w.start && currentTime <= w.end) {
          minDistance = 0.0;
          break;
        }
        const distStart = Math.abs(currentTime - w.start);
        const distEnd = Math.abs(currentTime - w.end);
        minDistance = Math.min(minDistance, distStart, distEnd);
      }

      // Smooth 500ms volume transition
      if (minDistance === 0.0) {
        bgmVolume = bgmVolume * 0.15; // Ducked to 15%
      } else if (minDistance < 0.5) {
        const ratio = minDistance / 0.5; // 0 to 1
        bgmVolume = bgmVolume * (0.15 + 0.85 * ratio); // transition smoothly
      }
    }
  }

  // Video element (with optional AutoZoom wrapper)
  const videoElement = (
    <OffthreadVideo
      src={videoSrc}
      style={{
        width: "100%",
        height: "100%",
        objectFit: "cover",
      }}
    />
  );

  return (
    <AbsoluteFill style={{ backgroundColor: "#000000" }}>
      {/* 1. Base Video Layer */}
      {autoZoom ? (
        <AutoZoom clipIndex={0}>{videoElement}</AutoZoom>
      ) : (
        videoElement
      )}

      {/* 2. Subtitles Layer */}
      {subtitleStyle === "karaoke" && (
        <KaraokeSubtitles subtitles={subtitles} isShort={isShort} styleSettings={subtitleStyleSettings} />
      )}
      {subtitleStyle === "static" && (
        <StaticSubtitles subtitles={subtitles} styleSettings={subtitleStyleSettings} />
      )}

      {/* 3. Text Overlays */}
      {overlays &&
        overlays
          .filter((o) => o.type === "text")
          .map((overlay, index) => (
            <TextOverlay
              key={`text-overlay-${index}`}
              overlay={overlay}
              totalDurationSeconds={totalDurationSeconds}
            />
          ))}

      {/* 4. Watermark Overlays */}
      {overlays &&
        overlays
          .filter((o) => o.type === "watermark")
          .map((watermark, index) => (
            <Watermark
              key={`watermark-${index}`}
              asset={watermark.asset || "assets/logo.png"}
              position={watermark.position || "bottom-right"}
            />
          ))}

      {/* 4.5. Image/Video Visual Overlays */}
      {overlays &&
        overlays
          .filter((o) => o.type === "image" || o.type === "video")
          .map((item, index) => {
            const startFrame = Math.round(item.start * fps);
            const durationInFrames = item.end !== undefined && item.end !== -1
              ? Math.round((item.end - item.start) * fps)
              : Math.round(2 * fps); // default 2 seconds

            const src = staticFile(item.asset || "");

            return (
              <Sequence
                key={`visual-overlay-${index}`}
                from={startFrame}
                durationInFrames={durationInFrames}
              >
                <AbsoluteFill style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', pointerEvents: 'none' }}>
                  {item.type === "image" ? (
                    <Img
                      src={src}
                      style={{
                        width: item.width || "50%",
                        height: "auto",
                        maxWidth: "90%",
                        borderRadius: 12,
                        boxShadow: "0 10px 30px rgba(0,0,0,0.6)",
                        border: "2px solid rgba(255,255,255,0.2)",
                      }}
                    />
                  ) : (
                    <OffthreadVideo
                      src={src}
                      style={{
                        width: item.width || "50%",
                        height: "auto",
                        maxWidth: "90%",
                        borderRadius: 12,
                        boxShadow: "0 10px 30px rgba(0,0,0,0.6)",
                        border: "2px solid rgba(255,255,255,0.2)",
                      }}
                      muted
                    />
                  )}
                </AbsoluteFill>
              </Sequence>
            );
          })}

      {/* 5. Sound Effects (Audio Overlays) */}
      {overlays &&
        overlays
          .filter((o) => o.type === "audio" || o.type === "sfx")
          .map((audio, index) => {
            const startFrame = Math.round(audio.start * fps);
            const durationInFrames = audio.end !== undefined && audio.end !== -1
              ? Math.round((audio.end - audio.start) * fps)
              : undefined;
            return (
              <Sequence
                key={`audio-overlay-${index}`}
                from={startFrame}
                durationInFrames={durationInFrames}
              >
                <Audio
                  src={staticFile(audio.asset || "")}
                  volume={audio.volume !== undefined ? audio.volume : 1.0}
                />
              </Sequence>
            );
          })}

      {/* 6. Background Music Track */}
      {bgmSettings && bgmSettings.asset && bgmVolume > 0 && (
        <Audio
          src={staticFile(bgmSettings.asset)}
          volume={bgmVolume}
        />
      )}
    </AbsoluteFill>
  );
};
