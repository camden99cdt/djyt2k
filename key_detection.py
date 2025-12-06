# key_detection.py
import numpy as np
import librosa
from librosa import util

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
    Frame-wise chroma pipeline with tuning correction, smoothing, and
    energy-based masking. Returns weighted correlations for major/minor
    keys along with the best key estimate and confidence.
    """

    # Load audio at native sample rate and trim silence
    y, sr = librosa.load(audio_path, sr=None, mono=True)
    yt, _ = librosa.effects.trim(y)

    if yt.size == 0:
        raise ValueError("Audio appears to be silent after trimming.")

    hop_length = 2048

    # Compute chroma with tuning correction via nearest-neighbor filtering
    chroma = librosa.feature.chroma_cqt(y=yt, sr=sr, hop_length=hop_length)
    chroma_filtered = librosa.decompose.nn_filter(
        chroma, aggregate=np.median, metric="cosine", width=9
    )
    chroma_tuned = np.minimum(chroma, chroma_filtered)
    chroma_smooth = librosa.decompose.nn_filter(
        chroma_tuned, aggregate=np.mean, metric="cosine", width=7
    )
    chroma_smooth = util.normalize(chroma_smooth, axis=0)

    # Frame energy for masking/weighting
    rms = librosa.feature.rms(y=yt, frame_length=4096, hop_length=hop_length)[0]
    frame_count = min(chroma_smooth.shape[1], rms.shape[0])
    chroma_smooth = chroma_smooth[:, :frame_count]
    frame_weights = rms[:frame_count]

    # Ignore quiet frames
    energy_threshold = np.percentile(frame_weights, 25)
    frame_weights = np.where(frame_weights > energy_threshold, frame_weights, 0.0)

    # Normalize key profiles once for correlation
    normalized_major_profiles = [util.normalize(np.roll(major_profile, i)) for i in range(12)]
    normalized_minor_profiles = [util.normalize(np.roll(minor_profile, i)) for i in range(12)]

    major_scores = np.zeros(12, dtype=float)
    minor_scores = np.zeros(12, dtype=float)
    total_weight = 0.0

    for idx in range(frame_count):
        weight = float(frame_weights[idx])
        if weight <= 0:
            continue

        chroma_vec = chroma_smooth[:, idx]
        norm = np.linalg.norm(chroma_vec)
        if norm == 0:
            continue

        chroma_vec /= norm

        frame_major_corr = [float(np.dot(profile, chroma_vec)) for profile in normalized_major_profiles]
        frame_minor_corr = [float(np.dot(profile, chroma_vec)) for profile in normalized_minor_profiles]

        major_scores += weight * np.array(frame_major_corr)
        minor_scores += weight * np.array(frame_minor_corr)
        total_weight += weight

    if total_weight == 0:
        raise ValueError("No energetic frames available for key estimation.")

    major_scores /= total_weight
    minor_scores /= total_weight

    all_scores = [(*item, "Maj") for item in enumerate(major_scores)] + [
        (*item, "min") for item in enumerate(minor_scores)
    ]
    all_scores.sort(key=lambda item: item[1], reverse=True)

    best_index, best_score, best_mode = all_scores[0]
    second_score = all_scores[1][1] if len(all_scores) > 1 else 0.0
    confidence = max(0.0, (best_score - second_score) / (abs(best_score) + 1e-6))

    return major_scores, minor_scores, int(best_index), best_mode, float(best_score), confidence


def _determine_key(major_correlations, minor_correlations):
    """
    Choose the key (major or minor) with the highest correlation.
    Returns (pitch_class, 'Maj' or 'min', score, confidence).
    """
    # The new _detect_key_profiles returns the full tuple; short-circuit if provided.
    if isinstance(major_correlations, tuple) and len(major_correlations) == 6:
        return major_correlations

    major_scores = major_correlations
    minor_scores = minor_correlations

    best_mode = "Maj"
    best_index = int(np.argmax(major_scores))
    best_score = float(major_scores[best_index])

    minor_best_index = int(np.argmax(minor_scores))
    minor_best_score = float(minor_scores[minor_best_index])

    if minor_best_score > best_score:
        best_mode = "min"
        best_index = minor_best_index
        best_score = minor_best_score

    combined_scores = list(major_scores) + list(minor_scores)
    combined_scores.sort(reverse=True)
    second_score = combined_scores[1] if len(combined_scores) > 1 else 0.0
    confidence = max(0.0, (best_score - second_score) / (abs(best_score) + 1e-6))

    return major_scores, minor_scores, best_index, best_mode, best_score, confidence


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

        (
            major_corr,
            minor_corr,
            key_index,
            key_mode,
            best_score,
            confidence,
        ) = _detect_key_profiles(audio_path)

        # Check for relative major/minor ambiguity
        relative_index = (key_index + 9) % 12 if key_mode == "Maj" else (key_index + 3) % 12
        relative_score = (
            minor_corr[relative_index] if key_mode == "Maj" else major_corr[relative_index]
        )

        if abs(best_score - relative_score) < 0.02:
            if log_callback:
                log_callback(
                    "Key confidence is low; relative major/minor scores are very close."
                )
            if relative_score > best_score:
                key_index = relative_index
                key_mode = "min" if key_mode == "Maj" else "Maj"
                best_score = relative_score

        key_pc = chroma_labels[key_index]
        key_str = f"{key_pc} {key_mode.lower()}"
        if log_callback:
            log_callback(f"Detected key: {key_str} (confidence: {confidence:.2f})")
        return key_str

    except Exception as e:
        if log_callback:
            log_callback(f"Key detection failed: {e}")
        return None
