import { Composition } from "remotion";
import { VideoComposition } from "./compositions/VideoComposition";
import type { VideoCompositionProps } from "./types";

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition
        id="VideoShort"
        component={VideoComposition as any}
        durationInFrames={300}
        fps={30}
        width={1080}
        height={1920}
        calculateMetadata={async ({ props }) => {
          const typedProps = props as Partial<VideoCompositionProps>;
          return {
            durationInFrames: typedProps.durationInFrames || 300,
            fps: typedProps.fps || 30,
            width: typedProps.width || 1080,
            height: typedProps.height || 1920,
            props: typedProps,
          };
        }}
      />
      <Composition
        id="VideoLong"
        component={VideoComposition as any}
        durationInFrames={300}
        fps={30}
        width={1920}
        height={1080}
        calculateMetadata={async ({ props }) => {
          const typedProps = props as Partial<VideoCompositionProps>;
          return {
            durationInFrames: typedProps.durationInFrames || 300,
            fps: typedProps.fps || 30,
            width: typedProps.width || 1920,
            height: typedProps.height || 1080,
            props: typedProps,
          };
        }}
      />
    </>
  );
};

