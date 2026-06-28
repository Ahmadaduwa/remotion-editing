/**
 * TypeScript interfaces for Remotion inputProps.
 * These match the Python Pydantic models in api.py.
 */

export interface WordTimestamp {
  word: string;
  start: number; // seconds
  end: number;   // seconds
}

export interface OverlayProps {
  type: "text" | "watermark" | "audio" | "sfx";
  content?: string;
  asset?: string;
  position?: "top" | "center" | "bottom" | "bottom-right";
  start: number;  // seconds
  end?: number;    // seconds (-1 = entire duration)
  style?: "hook" | "cta" | "default";
  volume?: number;
}

export interface SubtitleStyleSettings {
  fontFamily: string;
  color: string;
  highlightColor: string;
  fontSize: number;
  animation: "glow" | "bounce" | "pop" | "none";
  backgroundType: "card" | "outline" | "none";
  backgroundColor: string;
}

export interface BgmSettings {
  asset?: string;
  volume: number;
  enableDucking: boolean;
}

export interface VideoCompositionProps {
  src: string;
  fps: number;
  durationInFrames: number;
  width: number;
  height: number;
  mode: "short" | "long";
  subtitleStyle: "karaoke" | "static" | "none";
  subtitleStyleSettings?: SubtitleStyleSettings;
  bgmSettings?: BgmSettings;
  subtitles: WordTimestamp[];
  overlays: OverlayProps[];
  autoZoom: boolean;
}
