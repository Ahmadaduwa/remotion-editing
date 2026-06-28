import React from "react";
import { useCurrentFrame, useVideoConfig, interpolate } from "remotion";
import type { WordTimestamp, SubtitleStyleSettings } from "../types";

interface StaticSubtitlesProps {
  subtitles: WordTimestamp[];
  styleSettings?: SubtitleStyleSettings;
}

// Group words into sentence-like chunks (~8-12 words each)
function groupIntoSentences(words: WordTimestamp[]): {
  text: string;
  start: number;
  end: number;
}[] {
  if (!words || words.length === 0) return [];

  const sentences: { text: string; start: number; end: number }[] = [];
  let currentWords: WordTimestamp[] = [];

  for (const word of words) {
    currentWords.push(word);

    // Break on sentence-ending punctuation or after ~10 words
    const endsWithPunctuation = /[.!?。！？]$/.test(word.word);
    const isLongEnough = currentWords.length >= 10;
    const hasGap =
      currentWords.length > 3 &&
      words.indexOf(word) < words.length - 1 &&
      words[words.indexOf(word) + 1].start - word.end > 0.5;

    if (endsWithPunctuation || isLongEnough || hasGap) {
      sentences.push({
        text: currentWords.map((w) => w.word).join(" "),
        start: currentWords[0].start,
        end: currentWords[currentWords.length - 1].end,
      });
      currentWords = [];
    }
  }

  // Don't forget remaining words
  if (currentWords.length > 0) {
    sentences.push({
      text: currentWords.map((w) => w.word).join(" "),
      start: currentWords[0].start,
      end: currentWords[currentWords.length - 1].end,
    });
  }

  return sentences;
}

export const StaticSubtitles: React.FC<StaticSubtitlesProps> = ({
  subtitles,
  styleSettings,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const currentTime = frame / fps;

  if (!subtitles || subtitles.length === 0) return null;

  const sentences = groupIntoSentences(subtitles);

  // Find current sentence
  const currentSentence = sentences.find(
    (s) => currentTime >= s.start - 0.1 && currentTime <= s.end + 0.3
  );

  if (!currentSentence) return null;

  // Fade in animation
  const opacity = interpolate(
    currentTime,
    [currentSentence.start - 0.1, currentSentence.start + 0.2],
    [0, 1],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );

  // Apply style preferences
  const activeFontFamily = styleSettings?.fontFamily
    ? `'${styleSettings.fontFamily}', sans-serif`
    : "'Inter', 'Noto Sans Thai', sans-serif";
  const activeColor = styleSettings?.color || "#FFFFFF";
  const activeFontSize = styleSettings?.fontSize || 36;
  const activeBgType = styleSettings?.backgroundType || "card";
  const activeBgColor = styleSettings?.backgroundColor || "rgba(0, 0, 0, 0.65)";

  const cardStyle: React.CSSProperties = activeBgType === "card" ? {
    backgroundColor: activeBgColor,
    borderRadius: 8,
    padding: "10px 24px",
  } : {};

  let textShadow = "0 1px 3px rgba(0,0,0,0.6)";
  if (activeBgType === "outline") {
    textShadow += `, -2px -2px 0 #000000, 2px -2px 0 #000000, -2px 2px 0 #000000, 2px 2px 0 #000000, 0px -2px 0 #000000, 0px 2px 0 #000000, -2px 0px 0 #000000, 2px 0px 0 #000000`;
  }

  return (
    <div
      style={{
        position: "absolute",
        bottom: "8%",
        left: 0,
        right: 0,
        display: "flex",
        justifyContent: "center",
        zIndex: 100,
        opacity,
      }}
    >
      <div
        style={{
          maxWidth: "90%",
          ...cardStyle,
        }}
      >
        <span
          style={{
            fontSize: activeFontSize,
            fontWeight: 600,
            fontFamily: activeFontFamily,
            color: activeColor,
            textShadow: textShadow,
            lineHeight: 1.5,
          }}
        >
          {currentSentence.text}
        </span>
      </div>
    </div>
  );
};
