# demucs_runner.py
import os
import subprocess


def _log(log_callback, message: str):
    if log_callback is not None:
        log_callback(message)


def run_demucs(audio_path: str, session_dir: str, log_callback=None) -> str:
    """
    Run demucs on the given audio file.
    Returns the directory containing separated stem WAV files.

    Supports:
      1) session_dir/separated/<model>/<track>/*.wav
      2) session_dir/<model>/<track>/*.wav
    """
    _log(log_callback, "Running Demucs...")
    cmd = [
        "demucs",
        "-o",
        session_dir,
        audio_path,
    ]
    _log(log_callback, "Running: " + " ".join(cmd))
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "demucs not found. Install with `pip install demucs` "
            "and ensure the `demucs` command is in PATH."
        ) from e

    assert process.stdout is not None
    try:
        for line in process.stdout:
            _log(log_callback, line.rstrip())
    finally:
        process.stdout.close()

    retcode = process.wait()
    if retcode != 0:
        raise RuntimeError(f"demucs failed with exit code {retcode}")

    separated_root = os.path.join(session_dir, "separated")
    if os.path.isdir(separated_root):
        base_root = separated_root
        _log(log_callback, f"Using Demucs 'separated' layout at: {separated_root}")
    else:
        base_root = session_dir
        _log(
            log_callback,
            "No 'separated' directory found – assuming layout: "
            "session_dir/<model>/<track>/*.wav",
        )

    model_dirs = [
        d for d in os.listdir(base_root)
        if os.path.isdir(os.path.join(base_root, d))
    ]
    if not model_dirs:
        raise FileNotFoundError(f"No model directories found under {base_root}")
    model_dir = os.path.join(base_root, model_dirs[0])

    track_dirs = [
        d for d in os.listdir(model_dir)
        if os.path.isdir(os.path.join(model_dir, d))
    ]
    if not track_dirs:
        raise FileNotFoundError(
            f"No track directories found inside Demucs output at {model_dir}"
        )
    stems_dir = os.path.join(model_dir, track_dirs[0])

    wavs = [
        f for f in os.listdir(stems_dir)
        if f.lower().endswith(".wav")
    ]
    if not wavs:
        raise FileNotFoundError(f"No stem WAV files found in {stems_dir}")

    _log(log_callback, f"Found stems directory: {stems_dir}")
    return stems_dir
