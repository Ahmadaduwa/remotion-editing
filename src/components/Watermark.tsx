/**
 * Watermark — Semi-transparent logo overlay.
 *
 * Renders an image at the specified corner, persistent throughout the video.
 * Small (~10% of frame width), semi-transparent.
 */
import React from "react";
import { Img, staticFile } from "remotion";

interface WatermarkProps {
  asset: string;
  position: "top" | "bottom-right" | "bottom" | "center";
}

const WATERMARK_POSITIONS: Record<string, React.CSSProperties> = {
  "bottom-right": { bottom: 24, right: 24 },
  "top": { top: 24, right: 24 },
  "bottom": { bottom: 24, left: "50%", transform: "translateX(-50%)" },
  "center": { top: "50%", left: "50%", transform: "translate(-50%, -50%)" },
};

export const Watermark: React.FC<WatermarkProps> = ({ asset, position }) => {
  const posStyle = WATERMARK_POSITIONS[position] || WATERMARK_POSITIONS["bottom-right"];

  // Try staticFile first (for assets in public/), fall back to direct path
  let src: string;
  try {
    src = staticFile(asset);
  } catch {
    src = asset;
  }

  return (
    <div
      style={{
        position: "absolute",
        ...posStyle,
        zIndex: 300,
        opacity: 0.6,
        pointerEvents: "none",
      }}
    >
      <Img
        src={src}
        style={{
          width: 80,
          height: "auto",
          filter: "drop-shadow(0 2px 4px rgba(0,0,0,0.5))",
        }}
      />
    </div>
  );
};
