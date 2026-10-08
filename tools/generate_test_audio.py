import numpy as np
from scipy.io import wavfile

sample_rate = 44100
duration = 4.0 # seconds
t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)

# Left channel: 440 Hz tone, alternating
left = np.sin(2 * np.pi * 440 * t)
left[int(sample_rate):int(sample_rate*2)] = 0
left[int(sample_rate*3):] = 0

# Right channel: 880 Hz tone, alternating
right = np.sin(2 * np.pi * 880 * t)
right[:int(sample_rate)] = 0
right[int(sample_rate*2):int(sample_rate*3)] = 0

# Combine
stereo = np.column_stack((left, right))

# Reduce volume to avoid clipping
stereo *= 0.5

# Save to wav
wavfile.write('static/stereo_test.wav', sample_rate, stereo.astype(np.float32))
print("Saved stereo_test.wav to static folder.")
