from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
WORKER_DIR = ROOT / "deploy" / "termux" / "phone-worker"
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

from teto_renderer import TetoRenderer
from teto_renderer.errors import TetoResourceError


def _write_wav(path: Path, *, frames: int = 2205) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(22050)
        audio.writeframes(b"\x00\x00" * frames)


class TetoRendererTests(unittest.TestCase):
    def _assets(self, root: Path) -> tuple[Path, Path]:
        voicebank = root / "voicebank"
        voicebank.mkdir()
        _write_wav(voicebank / "te.wav")
        _write_wav(voicebank / "to.wav")
        (voicebank / "oto.ini").write_text(
            "te.wav=て,0,20,0,0,0\n"
            "to.wav=と,0,20,0,0,0\n",
            encoding="utf-8",
        )
        (voicebank / "character.txt").write_text("name=Kasane Teto Test\n", encoding="utf-8")

        resampler = root / "fake_resampler.py"
        resampler.write_text(
            "#!/usr/bin/env python3\n"
            "import shutil, sys\n"
            "shutil.copyfile(sys.argv[1], sys.argv[2])\n",
            encoding="utf-8",
        )
        resampler.chmod(resampler.stat().st_mode | stat.S_IXUSR)
        return voicebank, resampler

    def _env(self, voicebank: Path, resampler: Path, cache: Path) -> dict[str, str]:
        return {
            "PHONE_WORKER_TETO_ENABLED": "true",
            "PHONE_WORKER_TETO_VOICEBANK_DIR": str(voicebank),
            "PHONE_WORKER_TETO_RESAMPLER_COMMAND": str(resampler),
            "PHONE_WORKER_TETO_MIN_ALIASES": "1",
            "PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR": str(cache),
            "PHONE_WORKER_TETO_MAX_CHARACTERS": "180",
            "PHONE_WORKER_TETO_MAX_PHONEMES": "32",
            "PHONE_WORKER_TETO_MAX_AUDIO_SECONDS": "5",
        }

    def test_status_and_render_with_external_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            voicebank, resampler = self._assets(root)
            with patch.dict(os.environ, self._env(voicebank, resampler, root / "cache"), clear=False):
                renderer = TetoRenderer(resource_guard=lambda: {"ok": True})
                status = renderer.status(force=True)
                self.assertTrue(status["ready"])
                self.assertEqual(status["aliases"], 2)

                result = renderer.synthesize("teto", timeout_seconds=10)

                self.assertEqual(result["audio_format"], "wav")
                self.assertEqual(result["voicebank"], "Kasane Teto Test")
                self.assertGreater(result["rendered_phonemes"], 0)
                self.assertTrue(bytes(result["audio"]).startswith(b"RIFF"))
                self.assertLessEqual(len(result["audio"]), 8 * 1024 * 1024)

    def test_resource_guard_blocks_without_starting_resampler(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            voicebank, resampler = self._assets(root)
            with patch.dict(os.environ, self._env(voicebank, resampler, root / "cache"), clear=False):
                renderer = TetoRenderer(resource_guard=lambda: {"ok": False, "reason": "build ativo"})
                self.assertTrue(renderer.status(force=True)["ready"])
                with self.assertRaisesRegex(TetoResourceError, "build ativo"):
                    renderer.synthesize("teto")


if __name__ == "__main__":
    unittest.main()
