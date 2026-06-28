import os
import math
import struct
import wave

SAMPLE_RATE = 44100

def synth_ambient_pad(filename, duration=30.0):
    """Generates a warm, ambient synth pad chord progression."""
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    
    # 4-chord progression: Fmaj7 -> G6 -> Em7 -> Am7
    chords = [
        [174.61, 220.00, 261.63, 329.63], # Fmaj7
        [196.00, 246.94, 293.66, 329.63], # G6
        [164.81, 196.00, 246.94, 293.66], # Em7
        [220.00, 261.63, 329.63, 392.00]  # Am7
    ]
    chord_duration = duration / len(chords) # 7.5 seconds per chord
    
    with wave.open(filename, "w") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(SAMPLE_RATE)
        
        num_frames = int(duration * SAMPLE_RATE)
        for i in range(num_frames):
            t = i / SAMPLE_RATE
            chord_idx = min(int(t / chord_duration), len(chords) - 1)
            active_notes = chords[chord_idx]
            
            # Chord attack/release envelope (7.5s cycle)
            local_t = t % chord_duration
            # Soft linear attack (1.5s) and release (1.5s)
            env = 1.0
            attack_len = 1.5
            release_len = 1.5
            if local_t < attack_len:
                env = local_t / attack_len
            elif local_t > (chord_duration - release_len):
                env = (chord_duration - local_t) / release_len
            
            # Synthesize chord notes (sine waves + some warm harmonics)
            sample = 0.0
            for note in active_notes:
                # Fundamental
                sample += math.sin(2 * math.pi * note * t)
                # Soft 2nd harmonic
                sample += 0.3 * math.sin(2 * math.pi * (note * 2) * t)
                # Soft 3rd harmonic
                sample += 0.15 * math.sin(2 * math.pi * (note * 3) * t)
            
            # Normalize and scale
            sample = (sample / len(active_notes)) * env
            value = int(sample * 16384) # Keep volume soft so it acts as background
            data = struct.pack("<h", value)
            wav_file.writeframesraw(data)

def synth_chill_lofi(filename, duration=30.0):
    """Generates a chill lofi loop with ambient pads and a slow arpeggio melody."""
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    
    chords = [
        [174.61, 220.00, 261.63, 329.63], # Fmaj7
        [196.00, 246.94, 293.66, 329.63], # G6
        [164.81, 196.00, 246.94, 293.66], # Em7
        [220.00, 261.63, 329.63, 392.00]  # Am7
    ]
    chord_duration = duration / len(chords)
    
    with wave.open(filename, "w") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(SAMPLE_RATE)
        
        num_frames = int(duration * SAMPLE_RATE)
        for i in range(num_frames):
            t = i / SAMPLE_RATE
            chord_idx = min(int(t / chord_duration), len(chords) - 1)
            active_notes = chords[chord_idx]
            
            # --- Pad Layer ---
            local_t = t % chord_duration
            pad_env = 1.0
            if local_t < 1.0:
                pad_env = local_t
            elif local_t > (chord_duration - 1.0):
                pad_env = chord_duration - local_t
                
            pad_sample = 0.0
            for note in active_notes:
                pad_sample += math.sin(2 * math.pi * note * t)
            pad_sample = (pad_sample / len(active_notes)) * pad_env * 0.4
            
            # --- Melodic Arpeggio Layer (0.6 seconds per note) ---
            arp_tempo = 0.6
            arp_step = int(t / arp_tempo)
            # Pick a note from the current chord based on step
            note_idx = arp_step % len(active_notes)
            arp_note = active_notes[note_idx] * 2.0 # Pitch up one octave for melody
            
            arp_local_t = t % arp_tempo
            # Soft pluck envelope
            arp_env = math.exp(-8.0 * arp_local_t)
            
            # Pluck waveform (triangle-ish approximation using harmonics)
            arp_sample = math.sin(2 * math.pi * arp_note * t) * arp_env * 0.25
            
            # Rhythmic soft kick drum (every 1.2 seconds)
            kick_tempo = 1.2
            kick_local_t = t % kick_tempo
            kick_env = math.exp(-15.0 * kick_local_t)
            kick_freq = 150.0 * math.exp(-30.0 * kick_local_t) # frequency sweep
            kick_sample = math.sin(2 * math.pi * kick_freq * kick_local_t) * kick_env * 0.15
            
            # Mix signals
            total_sample = pad_sample + arp_sample + kick_sample
            # Limit maximum range
            total_sample = max(-1.0, min(1.0, total_sample))
            
            value = int(total_sample * 20000)
            data = struct.pack("<h", value)
            wav_file.writeframesraw(data)

def main():
    bgm_dir = "/home/aduwa/projects/editing/remotion-blank/assets/bgm"
    
    ambient_path = os.path.join(bgm_dir, "ambient_pad.wav")
    print("Synthesizing ambient pad...")
    synth_ambient_pad(ambient_path)
    print(f"Synthesized: {ambient_path}")
    
    lofi_path = os.path.join(bgm_dir, "chill_lofi.wav")
    print("Synthesizing chill lofi...")
    synth_chill_lofi(lofi_path)
    print(f"Synthesized: {lofi_path}")

if __name__ == "__main__":
    main()
