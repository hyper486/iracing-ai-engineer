"""Metadata for the pinned CPU voice stack, without caches or extra model assets.

Application inference explicitly disables VAD; the only model payload is the
four-file model plus manifest/license in desktop.spec. Existing official hooks
handle av, sounddevice, pygame and ONNX Runtime native libraries when reachable.
"""

from PyInstaller.utils.hooks import copy_metadata

datas = []
for distribution in (
    "faster-whisper",
    "huggingface-hub",
    "tokenizers",
    "av",
    "onnxruntime",
    "sounddevice",
    "pygame",
):
    datas += copy_metadata(distribution)

hiddenimports = ["faster_whisper.transcribe", "faster_whisper.tokenizer", "tokenizers.tokenizers"]
excludedimports = ["torch", "transformers", "tensorflow"]
