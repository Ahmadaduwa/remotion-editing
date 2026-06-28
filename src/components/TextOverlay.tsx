/**
 * TextOverlay — Animated text overlays (hook, CTA, default).
 *
 * Supports three styles:
 * - "hook": Bold scale-in with spring animation (for attention-grabbing openers)
 * - "cta": Pulsing effect (for call-to-action)
 * - "default": Fade in/out
 *
 * Position maps to safe areas on screen.
 */
import React from "react";
import {
  useCurrentFrame,
  useVideoConfig,
  spring,
  interpolate,
  Easing,
} from "remotion";
import type { OverlayProps } from "../types";

interface TextOverlayComponentProps {
  overlay: OverlayProps;
  totalDurationSeconds: number;
}

const POSITION_MAP: Record<string, React.CSSProperties> = {
  top: { top: "10%", left: 0, right: 0 },
  center: { top: "45%", left: 0, right: 0 },
  bottom: { bottom: "20%", left: 0, right: 0 },
  "bottom-right": { bottom: "10%", right: "5%" },
};

export const TextOverlay: React.FC<TextOverlayComponentProps> = ({
  overlay,
  totalDurationSeconds,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const currentTime = frame / fps;

  const startTime = overlay.start;
  const endTime = overlay.end === -1 ? totalDurationSeconds : overlay.end;

  // Not visible yet or already gone
  if (currentTime < startTime || currentTime > endTime) {
    return null;
  }

  const relativeFrame = (currentTime - startTime) * fps;
  const durationFrames = (endTime - startTime) * fps;
  const exitStartFrame = durationFrames - fps * 0.3; // Start exit 0.3s before end

  let opacity = 1;
  let scale = 1;
  let extraStyle: React.CSSProperties = {};

  if (overlay.style === "hook") {
    // Bold spring entrance
    const springVal = spring({
      frame: relativeFrame,
      fps,
      config: { stiffness: 120, damping: 12, mass: 0.8 },
    });
    scale = springVal;
    opacity = interpolate(relativeFrame, [0, fps * 0.1], [0, 1], {
      extrapolateRight: "clamp",
    });
    // Exit
    if (relativeFrame > exitStartFrame) {
      opacity = interpolate(
        relativeFrame,
        [exitStartFrame, durationFrames],
        [1, 0],
        { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
      );
    }
    extraStyle = {
      fontSize: 64,
      fontWeight: 900,
      color: "#FFFFFF",
      textShadow:
        "0 0 30px rgba(255, 215, 0, 0.6), 0 4px 8px rgba(0,0,0,0.8)",
      letterSpacing: "2px",
      textTransform: "uppercase" as const,
    };
  } else if (overlay.style === "cta") {
    // Pulsing effect
    const pulse = interpolate(
      relativeFrame % (fps * 0.8),
      [0, fps * 0.4, fps * 0.8],
      [1, 1.08, 1],
      { extrapolateRight: "clamp" }
    );
    scale = pulse;
    // Fade in
    opacity = interpolate(relativeFrame, [0, fps * 0.2], [0, 1], {
      extrapolateRight: "clamp",
    });
    if (relativeFrame > exitStartFrame) {
      opacity = interpolate(
        relativeFrame,
        [exitStartFrame, durationFrames],
        [1, 0],
        { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
      );
    }
    extraStyle = {
      fontSize: 48,
      fontWeight: 700,
      color: "#FF6B35",
      textShadow: "0 2px 6px rgba(0,0,0,0.7)",
      background: "linear-gradient(135deg, rgba(255,107,53,0.2), rgba(255,215,0,0.2))",
      borderRadius: 12,
      padding: "12px 28px",
      border: "2px solid rgba(255,107,53,0.4)",
    };
  } else {
    // Default: simple fade in/out
    opacity = interpolate(relativeFrame, [0, fps * 0.3], [0, 1], {
      extrapolateRight: "clamp",
    });
    if (relativeFrame > exitStartFrame) {
      opacity = interpolate(
        relativeFrame,
        [exitStartFrame, durationFrames],
        [1, 0],
        { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
      );
    }
    extraStyle = {
      fontSize: 44,
      fontWeight: 600,
      color: "#FFFFFF",
      textShadow: "0 2px 4px rgba(0,0,0,0.6)",
    };
  }

  const positionStyle = POSITION_MAP[overlay.position] || POSITION_MAP.top;

  return (
    <div
      style={{
        position: "absolute",
        ...positionStyle,
        display: "flex",
        justifyContent:
          overlay.position === "bottom-right" ? "flex-end" : "center",
        alignItems: "center",
        zIndex: 200,
        opacity,
        transform: `scale(${scale})`,
        pointerEvents: "none",
      }}
    >
      <div
        style={{
          fontFamily: "'Inter', 'Noto Sans Thai', sans-serif",
          textAlign: "center",
          ...extraStyle,
        }}
      >
        {overlay.content}
      </div>
    </div>
  );
};
