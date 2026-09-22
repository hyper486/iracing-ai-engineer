"""CPU inference DLLs only: never collect the wheel's optional cuDNN payload."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, copy_metadata

CPU_LIBRARIES = frozenset({"ctranslate2.dll", "libiomp5md.dll"})
binaries = [
    (source, destination)
    for source, destination in collect_dynamic_libs("ctranslate2")
    if Path(source).name.lower() in CPU_LIBRARIES
]
if {Path(source).name.lower() for source, _ in binaries} != CPU_LIBRARIES:
    raise RuntimeError("DESKTOP_CPU_SPEECH_LIBRARIES_MISSING")

datas = copy_metadata("ctranslate2")
hiddenimports = ["ctranslate2._ext", "ctranslate2.models"]
excludedimports = ["ctranslate2.converters", "ctranslate2.specs", "torch", "transformers"]
