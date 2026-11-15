# LIVE FX

LIVE FX is a real-time stereo effects processor for audio playing on your
computer. It routes audio from a virtual audio device through reverb, delay,
and echo, then sends the processed signal to your speakers or headphones.

The interface is styled like a retro studio rack and includes level meters,
a spectrum display, effect presets, and WAV recording.

## Contents

- [Features](#features)
- [How the audio system works](#how-the-audio-system-works)
- [Install the Windows app](#install-the-windows-app)
- [Set up audio routing](#set-up-audio-routing)
- [Use the app](#use-the-app)
- [Run from source](#run-from-source)
- [Build the Windows installer](#build-the-windows-installer)
- [Troubleshooting](#troubleshooting)
- [Project files](#project-files)
- [Release and licensing notes](#release-and-licensing-notes)

## Features

- Real-time stereo audio processing
- Freeverb-style reverb with room size, damping, pre-delay, and stereo width
- A single-repeat delay with optional ping-pong stereo
- Feedback echo with time, repeats, and tone controls
- Input trim, dry level, output level, limiter, bypass, and mute
- Ten built-in presets, plus save/load for user presets
- Tap tempo for setting echo time
- Input/output VU meters, signal/clip indicators, and a spectrum display
- Record processed audio to stereo 16-bit PCM WAV
- Remembers audio-device and effect settings between runs

## How the audio system works

LIVE FX does not capture all system audio by itself. A virtual audio cable
routes audio from an app or the operating system into LIVE FX:

```text
Browser / music player
        |
        v
Virtual cable playback endpoint
        |
        v
LIVE FX input -> gain -> reverb / delay / echo -> output gain -> limiter
        |
        v
LIVE FX output -> speakers / headphones
```

The application uses `sounddevice` and PortAudio to open one input/output
audio stream. NumPy performs the signal processing in the audio callback.
The internal processing chunk is 256 frames; the stream sample rate and device
buffer size are selected in the interface. Audio processing runs locally.

The effects are mixed with the dry signal. Bypass disables the effect mix;
output gain, limiter, and mute still apply. The limiter reduces peaks to help
avoid clipping.

Recording captures the processed output, not the unprocessed input. Files are
stereo 16-bit PCM WAV and are saved to:

- Windows: `%USERPROFILE%\LiveFX_Recordings`
- macOS/Linux: `~/LiveFX_Recordings`

The app stores settings in `~/.livefx_settings.json` (the user's home
directory). Uninstalling the Windows app does not remove these settings or
recordings.

## Install the Windows app

The Windows setup installs a standalone application; a separate Python
installation is not needed.

1. Open this repository's [Releases](../../releases) page.
2. Download the latest `LIVE-FX-Setup-<version>.exe`.
3. Run the installer and approve the Windows administrator prompt. LIVE FX is
   installed under Program Files.
4. Launch LIVE FX from the Start Menu. The installer also offers an optional
   desktop shortcut.
5. Install and configure a virtual audio cable as described below.
6. In LIVE FX, select the input, output, host API, sample rate, and buffer, then
   press **POWER ON**.

The installer targets 64-bit Windows 10 version 1809 or later. It does not
install third-party virtual audio routing software.

## Set up audio routing

Install one virtual audio routing device separately. Select its playback
endpoint as the source application's output, then select the corresponding
recording/monitor endpoint as LIVE FX's input.

### Windows: VB-CABLE

1. Install [VB-CABLE](https://vb-audio.com/Cable/) using its instructions.
2. In Windows Sound settings or the Volume Mixer, set the browser/music
   player's output to **CABLE Input**.
3. In LIVE FX, set **INPUT (CABLE)** to **CABLE Output**.
4. Set **OUTPUT (SPEAKERS)** to your speakers or headphones.
5. Select the same **HOST API** for both devices (for example, WASAPI), then
   press **POWER ON**.

### macOS: BlackHole

1. Install [BlackHole 2ch](https://existential.audio/blackhole/).
2. Set the system or source application's output to **BlackHole 2ch**.
3. In LIVE FX, select **BlackHole 2ch** as the input and your speakers or
   headphones as the output.
4. Select a host API/sample rate supported by both devices, then press
   **POWER ON**.

### Linux: PulseAudio null sink

Create a virtual sink in a terminal:

```sh
pactl load-module module-null-sink sink_name=fx
```

Route the source application to the `fx` sink. In LIVE FX, select **Monitor of
fx** as the input and your speakers/headphones as the output, then press
**POWER ON**. The `pactl` command creates a temporary sink; it may need to be
run again after logging out or restarting.

## Use the app

### Audio routing and performance

- **HOST API:** Choose the audio backend. Use the same host API for the input
  and output devices.
- **INPUT / OUTPUT:** Choose the virtual cable's monitor/recording endpoint
  and the physical playback device.
- **SAMPLE RATE:** Choose 44,100 or 48,000 Hz. Both devices must support the
  selected rate.
- **BUFFER:** Choose 256, 512, 1024, or 2048 frames. Lower values can reduce
  latency but may crackle; higher values are more tolerant of system load but
  add latency.

### Effect controls

| Section | Controls | Purpose |
| --- | --- | --- |
| Input | Trim, Dry | Adjust input gain and the unprocessed signal level. |
| Reverb | Mix, Size, Damp, Pre-delay, Width | Set the amount and character of the room effect. |
| Delay | Time, Mix, Ping-pong | Set the single-repeat delay and its stereo behavior. |
| Echo | Time, Repeats, Tone, Mix | Set feedback echo spacing, decay, tone, and level. |
| Output | Level, Limiter, Bypass, Mute | Set output gain and control the final signal. |

Use the preset selector to choose a starting point. **SAVE...** and **LOAD...**
store and restore effect parameters in JSON preset files. **TAP TEMPO** updates
the echo time from successive clicks.

### Knobs and keyboard shortcuts

- Drag a knob up/down or use the mouse wheel to change its value.
- Hold **Shift** while dragging or scrolling for finer adjustment.
- Double-click a knob to restore its default value.
- **F11:** Toggle full screen; **Esc:** Exit full screen.
- **Ctrl+P:** Power on/off.
- **Ctrl+B:** Bypass effects.
- **Ctrl+M:** Mute/unmute.
- **Ctrl+R:** Start/stop recording.

## Run from source

Running from source is useful for development or on macOS/Linux. This project
packages Windows only; other platforms require their own Python, Tk, and
PortAudio setup.

### Requirements

- Python 3
- NumPy and sounddevice (`requirements.txt`)
- Tkinter support in the Python installation
- A working PortAudio/audio-device setup
- A virtual audio routing device for processing system or browser audio

On Ubuntu/Debian, the Tk and PortAudio system packages are commonly available
as:

```sh
sudo apt install python3-tk libportaudio2
```

Run these commands from the repository root.

### Windows PowerShell

```sh
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

### macOS/Linux

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
```

To print the audio devices and host APIs detected by PortAudio:

```sh
# If the virtual environment is active:
python main.py --list
```

On Windows PowerShell, run
`.\.venv\Scripts\python.exe main.py --list`. On Linux or macOS, make sure the
Python interpreter has Tkinter enabled; some Python distributions require
installing Tk support separately.

## Build the Windows installer

Build on 64-bit Windows with 64-bit Python and Inno Setup 6 installed. From
the repository root, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\build_windows.ps1
```

The script creates an isolated `.build-venv`, installs the requirements from
`requirements-build.txt`, generates the multi-resolution icon, builds the
standalone windowed application, and compiles the installer. Output files are:

```text
dist\Live FX.exe
dist\LIVE-FX-Setup-<version>.exe
```

The release version is read from `packaging\VERSION`. Update that file before
a new release. The build script passes the version to both application and
installer metadata.

To build only the standalone app executable without compiling the installer:

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\build_windows.ps1 -SkipInstaller
```

`build/`, `dist/`, and local virtual environments are excluded by `.gitignore`.
For GitHub distribution, attach the setup executable to a GitHub Release rather
than committing build output to the source repository.

## Troubleshooting

### No audio reaches the output

- Confirm the source app is playing to the virtual cable's playback endpoint.
- Confirm LIVE FX uses the cable's corresponding recording/monitor endpoint as
  input and the actual speakers/headphones as output.
- Use the same host API for both selected devices.
- Confirm the selected sample rate is supported by both devices and press
  **POWER ON**.

### Audio crackles or drops out

Increase **BUFFER** (for example, from 256 to 512 or 1024). Close other
high-load audio applications and try 44,100 Hz if 48,000 Hz is unavailable.

### Device or sample-rate errors

Reconnect or enable the audio devices, select the correct host API, and choose
a sample rate supported by both input and output devices. If a device is
already in use by another application, close that application and try again.

### Source launch reports a missing dependency

Activate the virtual environment and run:

```sh
python -m pip install -r requirements.txt
```

Also verify that the Python installation includes Tkinter and that PortAudio
is available to sounddevice.

## Project files

```text
main.py                         GUI, audio stream, DSP, presets, recording
requirements.txt                Runtime Python dependencies
requirements-build.txt          Windows packaging dependencies
assets/live-fx.ico              Application and installer icon
packaging/VERSION               Release version
packaging/build_windows.ps1     Windows build entry point
packaging/live-fx.iss           Inno Setup installer definition
packaging/create_icon.py        Multi-resolution icon generator
packaging/hooks/hook-sounddevice.py
                                Bundle only the Windows x64 PortAudio runtime
```

## Release and licensing notes

The generated installer is not digitally signed. A public Windows release
should be signed with a code-signing certificate to establish publisher
identity and reduce SmartScreen warnings.

This repository currently has no `LICENSE` file. Choose and add a license
before publishing if you want to grant others permission to reuse or
redistribute the source.
