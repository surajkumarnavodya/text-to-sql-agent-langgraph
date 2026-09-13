"""Standalone entry point: one-time download of the configured Piper voice model.

Usage (from repo root, with the venv activated):

    python scripts\\download_voice_model.py              # uses Settings.tts_voice
    python scripts\\download_voice_model.py en_US-amy-medium
    python scripts\\download_voice_model.py --force

Downloads `<voice>.onnx` + `<voice>.onnx.json` from the public
`rhasspy/piper-voices` model repository (via `piper.download_voices
.download_voice`, the same function `python -m piper.download_voices`
itself calls) into `voice/models/`, the default location
`voice.tts._get_piper_voice` looks for (see `Settings
.tts_voice_model_path` if you want a different location). Safe to re-run:
skips files that already exist unless `--force` is passed. Only needed
once per voice, same shape as `ollama pull` -- faster-whisper's own model
downloads itself automatically on first use and needs no separate script.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from piper.download_voices import download_voice  # noqa: E402

from config.settings import configure_logging, get_settings  # noqa: E402

logger = logging.getLogger(__name__)

_MODELS_DIR = Path(__file__).resolve().parent.parent / "voice" / "models"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "voice",
        nargs="?",
        default=None,
        help="Voice name like 'en_US-lessac-medium' (default: Settings.tts_voice / TTS_VOICE).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if the model files already exist.",
    )
    args = parser.parse_args()

    configure_logging()
    settings = get_settings()
    voice_name = args.voice or settings.tts_voice

    _MODELS_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading Piper voice %r into %s", voice_name, _MODELS_DIR)
    download_voice(voice_name, _MODELS_DIR, force_redownload=args.force)
    logger.info(
        "Done. Set TTS_VOICE=%s in .env if this isn't already the configured voice.",
        voice_name,
    )


if __name__ == "__main__":
    main()
