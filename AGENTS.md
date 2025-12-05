# AGENTS.md

## Overview

This application provides a full workflow for:

- Downloading audio from YouTube using **yt-dlp**
- Optionally separating stems via **Demucs**
- Playing audio with:
  - Individual stem toggles + an “All” (full mix) mode
  - Time-stretching (speed) with pitch preserved
  - Pitch shifting in semitone increments
  - Master volume control
  - Waveform visualization + seek bar
  - Automatic key detection on the original mix (with UI transposition based on pitch shift)

Technologies used:

- **Python 3**
- **Tkinter** for the graphical interface
- **sounddevice**, **librosa**, **numpy**, **soundfile** for audio
- **yt-dlp** and **Demucs** for download + separation

The codebase is intentionally modular. Agents should preserve and strengthen this separation of concerns.

---

# Directory Structure

audio_player.py
audio_session.py
demucs_runner.py
downloader.py
gui.py
key_detection.py
main.py
playback_engine.py
requirements.txt

--

Below is the purpose, expectations, and boundaries of each module.

---

# Entry Point

## `main.py`

### Responsibilities
- Creates the Tkinter root window.
- Instantiates the main GUI application (`gui.py`).
- Enters the Tk main loop.

### Constraints
- Must not perform heavy operations.
- Should not contain business logic or audio engine calls.

---

# GUI Layer

## `gui.py`

### Responsibilities
The main application class orchestrating:

- Entire user interface flow
- URL handling, thumbnail display, log view
- Download / Separation pipeline
- Playback controls & waveform integration
- Routing user actions to the audio engine
- Updating UI elements based on playback

### Behaviors
- Runs downloads, Demucs, and DSP-heavy work **in background threads**
- Updates the GUI using `root.after(...)`
- Uses `audio_player.AudioPlayer` for playback control
- Manages stems vs. “All” mode logic
- Updates:
  - Time display
  - Playback cursor
  - Waveform rendering
  - Speed/pitch/volume display
  - Key transposition display

### Must Preserve
- No blocking operations on the Tk thread
- No DSP inside the GUI — delegate to `audio_session.py`
- Compatibility with the public interfaces of:
  - `downloader.py`
  - `demucs_runner.py`
  - `audio_player.py`

---

# Audio Engine Layer

## `audio_player.py`

### Responsibilities
High-level audio controller sitting between GUI and audio engine:

- Owns an `AudioSession`
- Owns a `PlaybackEngine` (sounddevice-based)
- Exposes simple GUI-facing playback API:
  - `play()`, `pause()`, `stop()`
  - `seek(seconds)`
  - `get_position()`, `get_duration()`
  - `set_master_volume(value)`
  - `set_tempo_rate(value)`
  - `set_pitch_semitones(value)`
  - `set_active_stems(set)`
  - `set_play_all(bool)`
- Provides audio chunks via `_pull_audio()` to the sounddevice callback

### Must Preserve
- No heavy DSP inside callbacks
- Never block inside the audio callback
- Always use `audio_session` for DSP-generated buffers

---

## `audio_session.py`

### Responsibilities
The central DSP state machine.

Maintains:

- Original audio data (mix + stems)
- Current processed audio (tempo- & pitch-modified)
- Pending processed audio to be activated when ready
- Sample rate, sample count, envelopes, active stems

Key Methods:

### `request_tempo_pitch_change(new_rate, new_pitch)`
- Spawns a background worker thread
- Applies heavyweight DSP (`librosa.time_stretch`, `pitch_shift`)
- Populates `pending_*` data structures

### `maybe_swap_pending(current_position_seconds)`
- Called during audio callback
- If new config is ready:
  - Swaps current ↔ pending audio
  - Computes **new playhead sample index preserving fractional track position**
    (e.g., 30s of a 2min track → 60s of a 4min slowed track)

### `get_chunk(start, frames)`
- Fast numpy slicing + summation
- No DSP — only reads from already-processed buffers

### Must Preserve
- All DSP happens asynchronously
- `get_chunk` stays lightweight
- Fractional-time invariants for seamless tempo changes
- All stems are resampled to match mix sample rate

---

# Playback Layer

## `playback_engine.py`

### Responsibilities
Low-level wrapper around `sounddevice`:

- Manages audio output stream
- Calls back into `audio_player` to pull audio frames
- Handles underruns and stream lifecycle gracefully

### Must Preserve
- Avoid blocking in callbacks
- No DSP in callback
- Keep API stable for `audio_player.py`

---

# Download / Separation Layer

## `downloader.py`

### Responsibilities
- Retrieves metadata from YouTube using yt-dlp
- Downloads audio to a local directory
- Ensures output is compatible with `librosa` & `soundfile`
- Provides metadata: title, thumbnail URL, etc.

### Must Preserve
- Interface compatibility with `gui.py`
- Use of `log_callback` for reporting progress

---

## `demucs_runner.py`

### Responsibilities
- Executes Demucs to generate separated stems
- Returns the correct stem directory path
  - Your version stores stems *without* a "separated/" folder
- Logs output and error cases

### Must Preserve
- Compatibility with your Demucs directory layout
- Correct discovery of stems folder

---

# Key Detection Layer

## `key_detection.py`

### Responsibilities
- Detects musical key of the **original downloaded audio**
- Provides:
  - `detect_key_string(path, log_callback)`
  - Pitch-class tables (`CHROMA_LABELS`, `FLAT_TO_SHARP`)
- GUI uses detected key + semitone shift to compute displayed key
  - No re-analysis required when pitch slider changes

### Must Preserve
- Only analyze the original mix
- Pitch changes only affect UI transposition

---

# Behavioral Guarantees

Agents must maintain the following invariants:

### **1. Tempo/pitch rebuilds are asynchronous**
No heavy DSP in the audio callback or GUI thread.

### **2. Fractional progression preserved on tempo changes**
Playback must not “jump” incorrectly when speed changes.

### **3. Sample-rate consistency**
All stems must match the mix’s sample rate.

### **4. Skip separation mode**
Behavior:
- Only the mix is loaded
- Stems UI disabled/hidden
- Audio engine in mix-only mode

### **5. “All” vs stems mode**
These modes are mutually exclusive.

### **6. GUI never blocks**
All network, Demucs, and DSP processes run in threads.

### **7. Interfaces remain stable**
Do not change function signatures used across modules without updating all call sites.

---

# Prohibited Changes

Agents should **NOT**:

- Add heavy DSP into `audio_player` or `playback_engine`
- Add blocking code in GUI (`gui.py`)
- Modify Demucs output paths without updating the runner logic
- Remove resampling logic
- Recompute key on every pitch change
- Put network or file I/O inside sounddevice callbacks

---

# Running the Application

Install dependencies:

```bash
pip install -r requirements.txt

Run:

python main.py

