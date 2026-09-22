# PyInstaller input: dependency payloads plus six explicitly allowlisted model files.
from pathlib import Path

repo_root = Path(SPECPATH).resolve().parent
model_files = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
model_data = [
    (str(repo_root / "models" / "faster-whisper-medium" / filename), "voice-model")
    for filename in model_files
] + [
    (str(repo_root / "packaging" / "voice-model.json"), "voice-model"),
    (str(repo_root / "packaging" / "WHISPER-LICENSE.txt"), "voice-model"),
]

analysis = Analysis(
    [str(repo_root / "scripts" / "run_desktop.py")],
    pathex=[str(repo_root / "src")],
    binaries=[],
    datas=model_data,
    hiddenimports=[
        "irsdk", "tkinter", "tkinter.ttk", "tkinter.filedialog",
        "sounddevice", "pygame", "pygame.joystick", "pygame.event", "pygame.display",
        "faster_whisper", "ctranslate2", "tokenizers", "av",
    ],
    hookspath=[str(repo_root / "packaging" / "hooks")],
    runtime_hooks=[],
    excludes=[
        "IPython", "ipykernel", "jupyter", "notebook", "matplotlib",
        "PyQt5", "PyQt6", "PySide2", "PySide6", "webview",
        "torch", "torchaudio", "tensorflow", "transformers", "jax", "jaxlib",
        "nvidia", "cupy", "triton", "ctranslate2.converters", "ctranslate2.specs",
    ],
    noarchive=False,
    optimize=0,
)
archive = PYZ(analysis.pure)
# Passing binaries/data to EXE, with no COLLECT, creates the one-file bundle.
executable = EXE(
    archive,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="AEIS-Engineer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
    uac_admin=False,
    uac_uiaccess=False,
)
