import numpy as np
import audioop
from src.services.speech.prosody import ProsodyEngine

p = ProsodyEngine()
t = np.linspace(0, 0.02, 160)
samples = (np.sin(2 * np.pi * 200 * t) * 8000).astype(np.int16)
pcm = samples.tobytes()

print(f"Input bytes len: {len(pcm)}")
feat = p.process_frame(pcm)
print(f"Processed features: {feat}")

# Check voiced baseline and recent window
print(f"Voiced energy history len: {len(p.voiced_energy_history)}")
print(f"Recent energy history len: {len(p.recent_energy)}")
print(f"Latest recent energy: {p.recent_energy[-1] if p.recent_energy else None}")
