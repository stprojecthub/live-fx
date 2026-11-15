from pathlib import Path

from PyInstaller.utils.hooks import get_module_file_attribute


module_dir = Path(get_module_file_attribute("sounddevice")).parent
portaudio_dll = (
    module_dir
    / "_sounddevice_data"
    / "portaudio-binaries"
    / "libportaudio64bit.dll"
)

if not portaudio_dll.is_file():
    raise FileNotFoundError(f"Required PortAudio runtime not found: {portaudio_dll}")

binaries = [
    (str(portaudio_dll), str(Path("_sounddevice_data") / "portaudio-binaries"))
]
