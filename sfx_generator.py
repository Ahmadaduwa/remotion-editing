import os
import math
import struct
import wave

def generate_wave(filename, frequency_func, duration=1.0, sample_rate=44100, volume_decay=3.0):
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    with wave.open(filename, "w") as wav_file:
        wav_file.setnchannels(1)  # Mono
        wav_file.setsampwidth(2) # 16-bit
        wav_file.setframerate(sample_rate)
        
        num_frames = int(duration * sample_rate)
        for i in range(num_frames):
            t = i / sample_rate
            freq = frequency_func(t, duration)
            
            # Volume envelope: quick fade-in, exponential decay
            env = 1.0
            if t < 0.01:
                env = t / 0.01
            env *= math.exp(-volume_decay * t)
            
            value = int(env * 32767 * math.sin(2 * math.pi * freq * t))
            data = struct.pack("<h", value)
            wav_file.writeframesraw(data)

def generate_noise(filename, duration=1.0, sample_rate=44100, volume_decay=3.0):
    import random
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    with wave.open(filename, "w") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        
        num_frames = int(duration * sample_rate)
        for i in range(num_frames):
            t = i / sample_rate
            cutoff = 200 + 1200 * math.sin(math.pi * t / duration)
            noise = random.uniform(-1.0, 1.0)
            env = 1.0
            if t < 0.05:
                env = t / 0.05
            env *= math.sin(math.pi * t / duration)
            
            value = int(env * 16384 * noise * (cutoff / 1400.0))
            data = struct.pack("<h", value)
            wav_file.writeframesraw(data)

def main():
    sfx_dir = "/home/aduwa/projects/editing/remotion-blank/assets/sfx"
    
    # 1. Ding (high pitch ring)
    ding_path = os.path.join(sfx_dir, "ding.wav")
    generate_wave(ding_path, lambda t, d: 880.0, duration=0.8, volume_decay=4.0)
    print(f"Generated {ding_path}")
    
    # 2. Whoosh (swept noise)
    whoosh_path = os.path.join(sfx_dir, "whoosh.wav")
    generate_noise(whoosh_path, duration=0.7)
    print(f"Generated {whoosh_path}")
    
    # 3. Pop (short bubble pop)
    pop_path = os.path.join(sfx_dir, "pop.wav")
    generate_wave(pop_path, lambda t, d: 600.0 + 300.0 * (1.0 - t/d), duration=0.15, volume_decay=15.0)
    print(f"Generated {pop_path}")
    
    # 4. Boom (deep sub drop)
    boom_path = os.path.join(sfx_dir, "boom.wav")
    generate_wave(boom_path, lambda t, d: 80.0 - 50.0 * (t/d), duration=1.2, volume_decay=2.0)
    print(f"Generated {boom_path}")

if __name__ == "__main__":
    main()
