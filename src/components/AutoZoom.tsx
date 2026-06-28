/**
 * AutoZoom — Ken Burns effect wrapper for video segments.
 *
 * Slowly zooms and pans to add dynamic visual movement, resetting on each clip.
 * Uses interpolate() with Easing.inOut for smooth motion.
 */
import React from "react";
import {
  useCurrentFrame,
  useVideoConfig,
  interpolate,
  Easing,
} from "remotion";

interface AutoZoomProps {
  children: React.ReactNode;
  clipIndex: number;
}

export const AutoZoom: React.FC<AutoZoomProps> = ({ children, clipIndex }) => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();

  // Alternate pan direction based on clip index to keep visual variety
  const zoomType = clipIndex % 4;

  let scale = 1.0;
  let translateX = 0;
  let translateY = 0;

  // Zoom scale from 1.0 to 1.15 over the duration of the clip
  scale = interpolate(
    frame,
    [0, durationInFrames],
    [1.0, 1.15],
    {
      extrapolateRight: "clamp",
      easing: Easing.inOut(Easing.ease),
    }
  );

  if (zoomType === 1) {
    // Pan slightly right
    translateX = interpolate(
      frame,
      [0, durationInFrames],
      [0, 20],
      { extrapolateRight: "clamp", easing: Easing.inOut(Easing.ease) }
    );
  } else if (zoomType === 2) {
    // Pan slightly left
    translateX = interpolate(
      frame,
      [0, durationInFrames],
      [0, -20],
      { extrapolateRight: "clamp", easing: Easing.inOut(Easing.ease) }
    );
  } else if (zoomType === 3) {
    // Pan slightly up
    translateY = interpolate(
      frame,
      [0, durationInFrames],
      [0, -20],
      { extrapolateRight: "clamp", easing: Easing.inOut(Easing.ease) }
    );
  } else {
    // Pan slightly down
    translateY = interpolate(
      frame,
      [0, durationInFrames],
      [0, 20],
      { extrapolateRight: "clamp", easing: Easing.inOut(Easing.ease) }
    );
  }

  return (
    <div
      style={{
        width: "100%",
        height: "100%",
        transform: `scale(${scale}) translate(${translateX}px, ${translateY}px)`,
        transformOrigin: "center center",
      }}
    >
      {children}
    </div>
  );
};
