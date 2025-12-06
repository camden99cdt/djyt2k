# key_detection.py
import numpy as np
import librosa

# Key profiles for major and minor keys (Krumhansl-Schmuckler).
major_profile = np.array([6.35, 2.23, 3.48, 2.33,
                          4.38, 4.09, 2.52, 5.19,
                          2.39, 3.66, 2.29, 2.88])

minor_profile = np.array([6.33, 2.68, 3.52, 5.38,
                          2.60, 3.53, 2.54, 4.75,
                          3.98, 2.69, 3.34, 3.17])

# Pitch class labels
chroma_labels = ['C', 'C#', 'D', 'D#', 'E', 'F',
                 'F#', 'G', 'G#', 'A', 'A#', 'B']


def _detect_key_profiles(audio_path: str):
    """
    Core detection logic derived from your script:
    compute correlations against major/minor key profiles
    using a chroma CQT over 10-second segments.
    """
    # Load audio at native sample rate
    y, sr = librosa.load(audio_path, sr=None, mono=True)

    # Trim leading/trailing silence
    yt, _ = librosa.effects.trim(y)

    if yt.size == 0:
        raise ValueError("Audio appears to be silent after trimming.")

    segment_length = sr * 10
    num_segments = max(1, len(yt) // segment_length)

    chroma_mean_total = np.zeros(12, dtype=float)

    for i in range(num_segments):
        start = i * segment_length
        end = start + segment_length
        segment = yt[start:end]

        if segment.size == 0:
            continue

        chroma = librosa.feature.chroma_cqt(y=segment, sr=sr)
        chroma_mean_total += np.mean(chroma, axis=1)

    # Average across segments
    chroma_mean_total /= num_segments

    # Normalize
    norm = np.linalg.norm(chroma_mean_total)
    if norm == 0:
        raise ValueError("Chromagram is zero; cannot normalize.")
    chroma_mean_total /= norm

    # Correlate with rotated key profiles
    major_correlations = [
        np.corrcoef(np.roll(major_profile, i), chroma_mean_total)[0, 1]
        for i in range(12)
    ]
    minor_correlations = [
        np.corrcoef(np.roll(minor_profile, i), chroma_mean_total)[0, 1]
        for i in range(12)
    ]

    return major_correlations, minor_correlations


def _determine_key(major_correlations, minor_correlations):
    """
    Choose the key (major or minor) with the highest correlation.
    Returns (pitch_class, 'Major' or 'Minor')
    """
    major_key = int(np.argmax(major_correlations))
    minor_key = int(np.argmax(minor_correlations))

    if max(major_correlations) > max(minor_correlations):
        return chroma_labels[major_key], "Maj"
    else:
        return chroma_labels[minor_key], "min"


def detect_key_string(audio_path: str, log_callback=None) -> str | None:
    """
    High-level helper:
      - Runs key detection on the ORIGINAL file
      - Returns a human-readable string like 'F major'
      - Returns None on failure

    This is intended to be called once per download and cached.
    """
    try:
        if log_callback:
            log_callback("Analyzing song key (Krumhansl–Schmuckler)...")

        major_corr, minor_corr = _detect_key_profiles(audio_path)
        key_pc, key_type = _determine_key(major_corr, minor_corr)

        # Convert to "F major" / "D minor"
        key_str = f"{key_pc} {key_type.lower()}"
        if log_callback:
            log_callback(f"Detected key: {key_str}")
        return key_str

    except Exception as e:
        if log_callback:
            log_callback(f"Key detection failed: {e}")
        return None
