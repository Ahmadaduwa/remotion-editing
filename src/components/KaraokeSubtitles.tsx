import React from "react";
import {
  useCurrentFrame,
  useVideoConfig,
  interpolate,
} from "remotion";
import type { WordTimestamp, SubtitleStyleSettings } from "../types";

interface KaraokeSubtitlesProps {
  subtitles: WordTimestamp[];
  isShort: boolean;
  styleSettings?: SubtitleStyleSettings;
}

const WORDS_PER_GROUP = 5;

export const KaraokeSubtitles: React.FC<KaraokeSubtitlesProps> = ({
  subtitles,
  isShort,
  styleSettings,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const currentTime = frame / fps;

  if (!subtitles || subtitles.length === 0) return null;

  // Find the current word index
  let currentWordIndex = -1;
  for (let i = 0; i < subtitles.length; i++) {
    if (currentTime >= subtitles[i].start && currentTime <= subtitles[i].end) {
      currentWordIndex = i;
      break;
    }
    // If between words, show the upcoming word's group
    if (
      i < subtitles.length - 1 &&
      currentTime > subtitles[i].end &&
      currentTime < subtitles[i + 1].start
    ) {
      currentWordIndex = i;
      break;
    }
  }

  if (currentWordIndex === -1) {
    if (subtitles.length > 0 && currentTime < subtitles[0].start) {
      return null;
    }
    if (
      subtitles.length > 0 &&
      currentTime > subtitles[subtitles.length - 1].end
    ) {
      return null;
    }
    return null;
  }

  // Calculate group boundaries
  const groupStart =
    Math.floor(currentWordIndex / WORDS_PER_GROUP) * WORDS_PER_GROUP;
  const groupEnd = Math.min(groupStart + WORDS_PER_GROUP, subtitles.length);
  const groupWords = subtitles.slice(groupStart, groupEnd);

  // Apply style preferences
  const activeFontFamily = styleSettings?.fontFamily
    ? `'${styleSettings.fontFamily}', sans-serif`
    : "'Inter', 'Noto Sans Thai', sans-serif";
  const activeColor = styleSettings?.color || "#FFFFFF";
  const activeHighlightColor = styleSettings?.highlightColor || "#FFD700";
  const activeFontSize = styleSettings?.fontSize || (isShort ? 54 : 36);
  const activeBgType = styleSettings?.backgroundType || "card";
  const activeBgColor = styleSettings?.backgroundColor || "rgba(0, 0, 0, 0.7)";
  const activeAnimation = styleSettings?.animation || "pop";

  const bottomOffset = isShort ? "22%" : "12%";

  // CSS card background vs outline style
  const cardStyle: React.CSSProperties = activeBgType === "card" ? {
    backgroundColor: activeBgColor,
    borderRadius: 16,
    padding: "16px 28px",
    backdropFilter: "blur(8px)",
  } : {};

  const isThai = subtitles.some(w => /[\u0e00-\u0e7f]/.test(w.word));

  return (
    <div
      style={{
        position: "absolute",
        bottom: bottomOffset,
        left: 0,
        right: 0,
        display: "flex",
        justifyContent: "center",
        alignItems: "center",
        zIndex: 100,
      }}
    >
      <div
        style={{
          maxWidth: "85%",
          display: "flex",
          flexWrap: "wrap",
          justifyContent: "center",
          gap: isThai ? "6px 0px" : "6px 10px",
          ...cardStyle,
        }}
      >
        {groupWords.map((word, i) => {
          const globalIndex = groupStart + i;
          const isActive =
            currentTime >= word.start && currentTime <= word.end;
          const isPast = currentTime > word.end;

          const wordDuration = word.end - word.start;
          const midPoint = word.start + Math.min(0.08, wordDuration * 0.5);

          // Apply active word animations
          let transform = "scale(1)";
          if (isActive) {
            if (activeAnimation === "pop") {
              const scale = wordDuration > 0.001
                ? interpolate(
                    currentTime,
                    [word.start, midPoint, word.end],
                    [1.0, 1.18, 1.05],
                    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
                  )
                : 1.15;
              transform = `scale(${scale})`;
            } else if (activeAnimation === "bounce") {
              const y = wordDuration > 0.001
                ? interpolate(
                    currentTime,
                    [word.start, midPoint, word.end],
                    [0, -12, 0],
                    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
                  )
                : -5;
              transform = `translateY(${y}px) scale(1.05)`;
            }
          }

          // Text Outline vs Default shadow
          let textShadow = "0 2px 4px rgba(0,0,0,0.8)";
          if (isActive && activeAnimation === "glow") {
            textShadow = `0 0 20px ${activeHighlightColor}b3, 0 2px 4px rgba(0,0,0,0.8)`;
          }
          if (activeBgType === "outline") {
            textShadow += `, -2px -2px 0 #000000, 2px -2px 0 #000000, -2px 2px 0 #000000, 2px 2px 0 #000000, 0px -2px 0 #000000, 0px 2px 0 #000000, -2px 0px 0 #000000, 2px 0px 0 #000000`;
          }

          return (
            <span
              key={`${globalIndex}-${word.word}`}
              style={{
                fontSize: activeFontSize,
                fontWeight: 800,
                fontFamily: activeFontFamily,
                color: isActive
                  ? activeHighlightColor
                  : isPast
                    ? "rgba(255, 255, 255, 0.6)"
                    : activeColor,
                textShadow: textShadow,
                transform: transform,
                transition: "color 0.05s ease, transform 0.08s ease-out",
                display: "inline-block",
                lineHeight: 1.4,
              }}
            >
              {word.word}
            </span>
          );
        })}
      </div>
    </div>
  );
};
