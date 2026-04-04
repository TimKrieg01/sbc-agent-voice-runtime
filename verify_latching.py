"""Proper silence simulation test."""
import numpy as np
import time
from src.services.speech.prosody import ProsodyEngine

p = ProsodyEngine()

print("Priming 5s baseline (steady speech)...")
for i in range(250): # 250 frames = 5s
    t = np.linspace(0, 0.02, 160)
    samples = (np.sin(2 * np.pi * 200 * t) * 8000).astype(np.int16)
    p.process_frame(samples.tobytes())
    if i % 7 == 0: p.compute_prosody()

print("Simulating pitch drop gesture...")
for i in range(10):
    freq = 200 - (i * 10)
    t = np.linspace(0, 0.02, 160)
    samples = (np.sin(2 * np.pi * freq * t) * 8000).astype(np.int16)
    p.process_frame(samples.tobytes())

res_speech = p.compute_prosody()
print(
    f"SPEECH: score={res_speech['turn_complete_score']} "
    f"(S:{res_speech['s_score']} P:{res_speech['p_score']} E:{res_speech['e_score']})"
)

print("\nSILENCE starts (Feeding 1s of actual silent frames)...")
for i in range(50): # 50 frames = 1s
    p.process_frame(b"\x00" * 320)

res_silence = p.compute_prosody()
print(f"SILENCE @ 1s: score={res_silence['turn_complete_score']} (S:{res_silence['s_score']} P:{res_silence['p_score']} E:{res_silence['e_score']})")

# Assertions
assert res_silence["turn_complete_score"] > 0.6, f"Score should be > 0.6 ({res_silence['turn_complete_score']})"
assert res_silence["p_score"] > 0.4, f"Pitch latching should preserve signal ({res_silence['p_score']})"
assert res_silence["e_score"] > 0.2, f"Energy drop should remain positively latched ({res_silence['e_score']})"

print("\n✓ Verification successful!")
