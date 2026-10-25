"""Speech through TTS BigLinux (https://github.com/biglinux/tts-biglinux).

ChatToon does not ship its own speech engine. It imports TTS BigLinux's modules from
the installed package to reuse its voice catalog (RHVoice, espeak-ng, Piper, Kokoro),
its text normalisation (abbreviations, numbers, URLs, Markdown), its native
``tts_engine`` synthesizer and its Kokoro command builder. The only difference from the
TTS BigLinux app is the last step: instead of piping audio straight to ``aplay``,
ChatToon renders a WAV file so it can compute a mouth-movement envelope and play the
audio exactly once, in one place.
"""

from __future__ import annotations

import array
import importlib.machinery
import importlib.util
import json
import logging
import math
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

TTS_BIGLINUX_DIR = Path(os.environ.get("CHATTOON_TTS_BIGLINUX_DIR", "/usr/share/biglinux/tts-biglinux"))
TTS_ENGINE_DIR = Path(os.environ.get("CHATTOON_TTS_ENGINE_DIR", "/usr/lib/tts-biglinux"))
TTS_BIGLINUX_SETTINGS = Path.home() / ".config" / "biglinux-tts" / "settings.json"
ENVELOPE_FPS = 50  # mouth openness samples per second
# koko renders roughly as fast as the audio plays on a desktop CPU (~0.05 s per character)
KOKORO_TIMEOUT_BASE = 25.0
KOKORO_TIMEOUT_PER_CHAR = 0.15


class TTSError(Exception):
    """A synthesis problem. ``code`` lets the interface show a translated message."""

    def __init__(self, message: str, code: str = "", detail: str = ""):
        super().__init__(message)
        self.code = code
        self.detail = detail


class TTSCancelled(TTSError):
    def __init__(self) -> None:
        super().__init__("Speech synthesis was cancelled", "cancelled")


# Engines without real pitch control (the slider would do nothing).
_NO_PITCH = frozenset({"kokoro"})


@dataclass(frozen=True)
class Voice:
    id: str
    name: str
    language: str
    backend: str
    gender: str = ""
    quality: str = ""

    @property
    def label(self) -> str:
        engine = {"rhvoice": "RHVoice", "espeak-ng": "espeak-ng", "piper": "Piper", "kokoro": "Kokoro"}
        return f"{self.name} · {engine.get(self.backend, self.backend)}"

    @property
    def pitch_supported(self) -> bool:
        return self.backend not in _NO_PITCH

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "language": self.language, "backend": self.backend,
                "gender": self.gender, "quality": self.quality, "pitch": self.pitch_supported}


# Top-level names TTS BigLinux imports from its own directory. ``services`` and ``utils``
# have no __init__.py, so any regular ``utils.py`` elsewhere on sys.path (hermes-agent
# installs one with pip --user, for example) wins over them and the import fails.
_TTS_TOPLEVEL = ("config", "utils", "services")


def _from_tts_dir(module) -> bool:
    paths = list(getattr(module, "__path__", None) or []) + [getattr(module, "__file__", None) or ""]
    return any(p and Path(p).is_relative_to(TTS_BIGLINUX_DIR) for p in paths)


def _bind_tts_toplevel() -> None:
    """Load TTS BigLinux's top-level modules from its directory, whatever else is on sys.path."""
    for name in _TTS_TOPLEVEL:
        current = sys.modules.get(name)
        if current is not None and _from_tts_dir(current):
            continue
        spec = importlib.machinery.PathFinder.find_spec(name, [str(TTS_BIGLINUX_DIR)])
        if spec is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            if spec.loader is not None:
                spec.loader.exec_module(module)
        except BaseException:
            if current is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = current
            raise


@dataclass
class Utterance:
    id: str
    text: str
    path: Path
    duration: float
    envelope: list[float]  # 0..1 mouth openness at ENVELOPE_FPS
    synth_seconds: float = 0.0


class BigLinuxTTS:
    """Adapter around the installed TTS BigLinux package."""

    def __init__(self, workdir: Path | None = None):
        self.workdir = Path(workdir or tempfile.mkdtemp(prefix="chattoon-tts-"))
        self.workdir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.workdir, 0o700)
        self._lock = threading.Lock()  # the native engine is not re-entrant
        self._voices: list[Voice] | None = None
        self._modules = None
        self._kokoro = None
        self._proc: subprocess.Popen | None = None  # engine process running right now
        self._proc_lock = threading.Lock()
        self._epoch = 0  # cancel() bumps it: every synthesis requested before is dropped
        self._job_epoch = 0  # epoch of the synthesis holding self._lock
        self.error = ""

    # -- integration ------------------------------------------------------

    def _load(self):
        if self._modules is not None:
            return self._modules
        if not TTS_BIGLINUX_DIR.is_dir():
            self.error = "TTS BigLinux is not installed (package tts-biglinux)."
            raise TTSError(self.error, "tts-missing")
        for p in (str(TTS_BIGLINUX_DIR), str(TTS_ENGINE_DIR)):
            if p not in sys.path:
                sys.path.append(p)  # append: never shadow ChatToon's own modules
        try:
            _bind_tts_toplevel()
            from services import tts_service, voice_manager  # type: ignore[import-not-found]
            from services.text_processor import process_text  # type: ignore[import-not-found]
        except Exception as e:  # noqa: BLE001 - any import failure means "unusable"
            self.error = f"Could not load TTS BigLinux: {e}"
            raise TTSError(self.error, "tts-broken", str(e)[:200]) from e
        try:
            import tts_engine  # type: ignore[import-not-found]
        except ImportError:
            tts_engine = None
        self._modules = (voice_manager, tts_service, process_text, tts_engine)
        return self._modules

    def _kokoro_service(self):
        """TTS BigLinux's Kokoro helpers (model, voices.bin, koko command line)."""
        if self._kokoro is None:
            self._load()
            try:
                from services import kokoro_voice_service  # type: ignore[import-not-found]
            except Exception as e:  # noqa: BLE001 - older TTS BigLinux without Kokoro support
                raise TTSError(f"This TTS BigLinux version has no Kokoro support: {e}", "kokoro-missing",
                               str(e)) from e
            self._kokoro = kokoro_voice_service
        return self._kokoro

    @property
    def available(self) -> bool:
        try:
            self._load()
            return True
        except TTSError:
            return False

    def voices(self, refresh: bool = False) -> list[Voice]:
        if self._voices is None or refresh:
            vm = self._load()[0]
            catalog = vm.discover_voices()
            usable = {k for k, v in vm.engine_availability(catalog).items() if getattr(v, "value", v) == "ready"}
            self._voices = [
                Voice(v.voice_id, v.name, v.language, v.backend, v.gender or "", v.quality or "")
                for v in catalog.voices if v.backend in usable
            ]
        return self._voices

    def find(self, voice_id: str) -> Voice | None:
        return next((v for v in self.voices() if v.id == voice_id), None)

    def user_default(self) -> Voice | None:
        """The voice chosen in the TTS BigLinux app, if any."""
        try:
            data = json.loads(TTS_BIGLINUX_SETTINGS.read_text(encoding="utf-8"))
            speech = data.get("speech", data)
            return self.find(speech.get("voice_id", ""))
        except (OSError, ValueError, AttributeError):
            return None

    def suggest(self, hints: tuple[str, ...], language: str = "pt-BR") -> Voice | None:
        voices = self.voices()
        lang = language.lower()
        same_lang = [v for v in voices if v.language.lower() == lang] or \
                    [v for v in voices if v.language.lower().split("-")[0] == lang.split("-")[0]]
        for hint in hints:
            for v in same_lang:
                if hint.casefold() in (v.id + " " + v.name).casefold():
                    return v
        default = self.user_default()
        if default and default in same_lang:
            return default
        return same_lang[0] if same_lang else (voices[0] if voices else None)

    # -- synthesis --------------------------------------------------------

    def prepare_text(self, text: str, language: str) -> str:
        process_text = self._load()[2]
        text = re.sub(r"[\U0001F000-\U0001FAFF☀-➿️]", " ", text)
        processed = process_text(text, expand_abbreviations=True, process_special_chars=False,
                                 process_urls=False, strip_formatting=True, normalize_numbers=True,
                                 language=language)
        return " ".join((processed or "").split())

    def prewarm(self, voice: Voice) -> None:
        """Load a Piper model ahead of time (first synthesis drops from ~1.6 s to ~0.1 s).

        Kokoro runs one ``koko`` process per text: a tiny render pulls its 90 MB model into
        the page cache, so the first viewer does not also pay for the disk read."""
        engine = self._load()[3]
        if voice.backend == "piper" and engine is not None and hasattr(engine, "load_piper"):
            model = voice.id.removeprefix("piper:")
            if not model.endswith(".onnx") and os.path.isfile(model + ".onnx"):
                model += ".onnx"
            with self._lock:
                engine.load_piper(model)
        elif voice.backend == "kokoro":
            path = self.workdir / "prewarm.wav"
            with self._lock:
                self._job_epoch = self._epoch
                try:
                    self._kokoro_render(".", voice, 0, path)
                except TTSError:
                    pass  # reported for real on the first reply
                finally:
                    path.unlink(missing_ok=True)

    def cancel(self) -> None:
        """Stop the synthesis in progress (thread-safe). The waiting call raises TTSCancelled."""
        with self._proc_lock:
            self._epoch += 1
            proc = self._proc
        if proc is not None:
            _kill(proc)

    def synthesize(self, text: str, voice: Voice, rate: int = 0, pitch: int = 0, volume: int = 85,
                   utterance_id: str = "") -> Utterance:
        """Render ``text`` to a WAV file (blocking: call from a worker thread)."""
        _, svc, _, engine = self._load()
        spoken = self.prepare_text(text, voice.language)
        if not spoken:
            raise TTSError("Nothing to say after text cleanup", "no-text")
        uid = utterance_id or f"u{int(time.time() * 1000)}"
        path = self.workdir / f"{uid}.wav"
        started = time.monotonic()
        epoch = self._epoch
        with self._lock:
            self._job_epoch = epoch
            try:
                if self._is_cancelled():
                    raise TTSCancelled()
                if voice.backend == "piper":
                    self._piper(svc, engine, spoken, voice, rate, pitch, volume, path)
                elif voice.backend == "espeak-ng":
                    self._espeak(svc, engine, spoken, voice, rate, pitch, volume, path)
                elif voice.backend == "rhvoice":
                    self._rhvoice(spoken, voice, rate, pitch, volume, path)
                elif voice.backend == "kokoro":
                    self._kokoro_synth(spoken, voice, rate, volume, path)
                else:
                    raise TTSError(f"Voice engine '{voice.backend}' is not supported by ChatToon yet",
                                   "unsupported", voice.backend)
                if self._is_cancelled():
                    raise TTSCancelled()
                if not path.exists() or path.stat().st_size < 100:
                    raise TTSError("The voice engine produced no audio", "no-audio")
                # Every engine ends as 16-bit PCM: one format for the envelope, pw-play and OBS.
                to_pcm16(path)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        duration, envelope = analyse_wav(path)
        if duration <= 0.05:
            path.unlink(missing_ok=True)
            raise TTSError("The voice engine produced no audio", "no-audio")
        return Utterance(uid, text, path, duration, envelope, time.monotonic() - started)

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)

    def _piper(self, svc, engine, text, voice, rate, pitch, volume, path) -> None:
        model = voice.id.removeprefix("piper:")
        if not model.endswith(".onnx") and os.path.isfile(model + ".onnx"):
            model += ".onnx"
        if not os.path.isfile(model):
            raise TTSError("The selected Piper voice is not installed", "voice-missing", voice.name)
        args = (svc.piper_length_scale(rate), svc.piper_noise_scale(pitch), 0.8, svc.volume_factor(volume))
        if engine is not None:
            wav = engine.synthesize_piper(text, model, *args)
            if wav and len(wav) >= 100:
                self._write(path, bytes(wav))
                return
        from services.voice_manager import _find_piper_binary  # type: ignore[import-not-found]

        binary = _find_piper_binary()
        if not binary:
            raise TTSError("Piper is not installed", "missing", "Piper")
        self._run([binary, "--model", model, "--output_file", str(path), "--length_scale", f"{args[0]:.2f}",
                   "--noise_scale", f"{args[1]:.3f}", "--noise_w", "0.8"], text)

    def _espeak(self, svc, engine, text, voice, rate, pitch, volume, path) -> None:
        name = voice.id.removeprefix("espeak-") or "pt-br"
        wpm, esp_pitch, amp = svc.espeak_wpm(rate), svc.espeak_pitch(pitch), svc.espeak_volume(volume)
        if engine is not None and hasattr(engine, "synthesize_espeak"):
            wav = engine.synthesize_espeak(text, name, wpm, esp_pitch, amp)
            if wav and len(wav) >= 100:
                self._write(path, bytes(wav))
                return
        self._run(["espeak-ng", "-v", name, "-s", str(wpm), "-p", str(esp_pitch), "-a", str(amp),
                   "-w", str(path), "--stdin"], text)

    def _rhvoice(self, text, voice, rate, pitch, volume, path) -> None:
        # Same parameter mapping TTS BigLinux uses for RHVoice-test.
        self._run(["RHVoice-test", "-p", voice.id, "-r", str(max(20, min(300, 100 + rate))),
                   "-t", str(max(20, min(200, 100 + pitch))), "-v", str(max(0, min(100, volume))),
                   "-o", str(path)], text)

    # -- Kokoro (package biglinux-kokoro-tts, ``koko`` binary) -------------
    #
    # ``koko pipe`` (what the TTS BigLinux app uses) plays every sentence itself while
    # it writes the WAV, so ChatToon would be heard twice. ``koko text`` only renders
    # the file. The command line comes from TTS BigLinux (build_koko_command), so the
    # model, voices.bin, language and speed are exactly what the TTS BigLinux app uses.

    def _kokoro_synth(self, text, voice, rate, volume, path) -> None:
        if volume <= 0:
            raise TTSError("The voice volume is zero", "no-audio")
        chunks = self._kokoro_chunks(text)
        temps: list[Path] = []
        try:
            for i, chunk in enumerate(chunks):
                if self._is_cancelled():
                    raise TTSCancelled()
                raw = path.with_suffix(f".koko{i}.wav")
                part = path.with_suffix(f".part{i}.wav")
                temps += [raw, part]
                self._kokoro_render(chunk, voice, rate, raw)
                to_pcm16(raw, part if len(chunks) > 1 else path, gain=_volume_gain(volume))
            if len(chunks) > 1:
                _join_wavs([path.with_suffix(f".part{i}.wav") for i in range(len(chunks))], path)
        finally:
            for f in temps:
                f.unlink(missing_ok=True)

    def _kokoro_chunks(self, text: str) -> list[str]:
        """koko panics past ~510 tokens in one call ("index out of bounds"): long texts are
        rendered piece by piece. prepare_koko_text already cuts them at sentence boundaries
        (KOKO_MAX_SEGMENT_CHARS); the pieces are packed back up to that size."""
        kvs = self._kokoro_service()
        limit = int(getattr(kvs, "KOKO_MAX_SEGMENT_CHARS", 220))
        chunks: list[str] = []
        for piece in (ln.strip() for ln in kvs.prepare_koko_text(text).split("\n")):
            if not piece:
                continue
            if chunks and len(chunks[-1]) + 1 + len(piece) <= limit:
                chunks[-1] += " " + piece
            else:
                chunks.append(piece)
        return chunks or [text]

    def _kokoro_render(self, text: str, voice: Voice, rate: int, output: Path) -> None:
        kvs = self._kokoro_service()
        problem = kvs.koko_problem(voice.id)  # binary, model, voices.bin, this voice
        if problem:
            raise TTSError(problem, "kokoro-unavailable", problem)
        # koko cuts sentences at every punctuation sign and fails on punctuation-only
        # pieces ("Sério?!"); prepare_koko_text fixes that and splits long sentences.
        prepared = " ".join(kvs.prepare_koko_text(text).split("\n"))
        if not any(c.isalnum() for c in prepared):
            raise TTSError("Nothing to say after text cleanup", "no-audio")
        cmd = kvs.build_koko_command(voice.id, speed=kvs.kokoro_speed(rate), text=prepared, output=str(output))
        if "pipe" in cmd or "text" not in cmd:  # never a mode that plays audio by itself
            raise TTSError("This TTS BigLinux version cannot render Kokoro to a file", "kokoro-failed",
                           "koko text")
        timeout = min(300.0, KOKORO_TIMEOUT_BASE + len(prepared) * KOKORO_TIMEOUT_PER_CHAR)
        try:
            code, err = self._exec(cmd, None, timeout, cwd=kvs.koko_workdir())
        except subprocess.TimeoutExpired as e:
            raise TTSError("Kokoro took too long to speak this text", "kokoro-timeout") from e
        if code != 0:
            detail = _engine_error(err) or f"exit code {code}"
            raise TTSError(f"Kokoro failed: {detail}", "kokoro-failed", detail)
        if not output.exists() or output.stat().st_size < 100:
            raise TTSError("The voice engine produced no audio", "no-audio")

    # -- processes ----------------------------------------------------------

    def _run(self, cmd: list[str], text: str, timeout: float = 120) -> None:
        if not shutil.which(cmd[0]) and not os.path.isfile(cmd[0]):
            raise TTSError(f"{cmd[0]} is not installed", "missing", cmd[0])
        # Text goes through stdin, never through a shell or the command line.
        try:
            code, err = self._exec(cmd, text.encode("utf-8"), timeout)
        except subprocess.TimeoutExpired as e:
            raise TTSError(f"{cmd[0]} took too long", "timeout", cmd[0]) from e
        if code != 0:
            raise TTSError(_engine_error(err) or f"{cmd[0]} failed", "failed", cmd[0])

    def _exec(self, cmd: list[str], stdin: bytes | None, timeout: float, cwd: str | None = None) -> tuple[int, bytes]:
        """Run an engine (argv list, no shell) that cancel() can kill. Returns (code, stderr)."""
        with self._proc_lock:
            if self._is_cancelled():
                raise TTSCancelled()
            # Own process group: a cancel/timeout also takes down helpers the engine started.
            try:
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, cwd=cwd,
                                        start_new_session=True)
            except OSError as e:
                raise TTSError(f"Could not start {os.path.basename(cmd[0])}: {e.strerror or e}", "failed",
                               os.path.basename(cmd[0])) from e
            self._proc = proc
        try:
            _, err = proc.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(proc)
            raise
        except BaseException:
            _kill(proc)
            raise
        finally:
            with self._proc_lock:
                self._proc = None
        if self._is_cancelled():
            raise TTSCancelled()
        return proc.returncode, err or b""

    def _is_cancelled(self) -> bool:
        return self._job_epoch != self._epoch

    def cleanup(self, keep: int = 20) -> None:
        """Delete old WAV files, keeping the newest ``keep``."""
        files = []
        for f in self.workdir.glob("*.wav"):
            try:
                files.append((f.stat().st_mtime, f))
            except OSError:
                pass  # removed meanwhile
        for _, old in sorted(files, reverse=True)[keep:]:
            old.unlink(missing_ok=True)

    def shutdown(self) -> None:
        self.cancel()
        shutil.rmtree(self.workdir, ignore_errors=True)


# koko writes progress and the text being read to stderr; only real errors are reported.
_ENGINE_NOISE = ("WARNING", "TOKENIZE", "shape_style", "phonemes", "PHONEMIZE", "CALLING PHONEMIZE",
                 "Processing", "Audio saved", "Time taken", "Words per second", "Detected language",
                 "Language detection", "Using ", "MANUAL LANGUAGE", "Manual language", "voice styles")


def _engine_error(stderr: bytes) -> str:
    lines = [ln.strip() for ln in stderr.decode("utf-8", "replace").splitlines()]
    useful = [ln for ln in lines if ln and not ln.startswith(_ENGINE_NOISE)]
    return " ".join(useful[-3:])[-300:]


def _kill(proc: subprocess.Popen) -> None:
    """Terminate an engine and its process group, and reap it (no zombies)."""
    if proc.poll() is not None:
        return
    for sig, wait in ((signal.SIGTERM, 1.0), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.send_signal(sig)
            except OSError:
                pass
        try:
            proc.wait(wait)
            return
        except subprocess.TimeoutExpired:
            continue


def _join_wavs(parts: list[Path], dst: Path) -> None:
    """Concatenate 16-bit PCM WAV files with the same format (pieces of one Kokoro line)."""
    tmp = dst.with_suffix(".join")
    with wave.open(str(parts[0]), "rb") as first:
        params = first.getparams()
    with wave.open(str(tmp), "wb") as out:
        out.setparams(params)
        for part in parts:
            with wave.open(str(part), "rb") as w:
                out.writeframes(w.readframes(w.getnframes()))
    os.chmod(tmp, 0o600)
    tmp.replace(dst)


def _volume_gain(volume: int) -> float:
    """Same scale as TTS BigLinux (50 = unchanged, 100 = twice as loud, 0 = mute)."""
    if volume <= 0:
        return 0.0
    return max(0.2, min(2.0, volume / 50.0))


# -- WAV normalisation ---------------------------------------------------------

_PCM, _FLOAT, _EXTENSIBLE = 1, 3, 0xFFFE


def _read_wav(path: Path) -> tuple[int, int, int, int, bytes]:
    """(format, channels, rate, bits, sample bytes) of a RIFF WAV, tolerating the
    streaming headers engines write (data size 0 or 0xFFFFFFFF) and truncated files."""
    data = path.read_bytes()
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise TTSError("The voice engine produced an invalid audio file", "invalid-wav")
    pos, fmt, samples = 12, None, None
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = pos + 8
        if cid == b"fmt " and size >= 16:
            tag, channels, rate, _, _, bits = struct.unpack("<HHIIHH", data[body:body + 16])
            if tag == _EXTENSIBLE and size >= 40:
                tag = struct.unpack("<H", data[body + 24:body + 26])[0]  # sub-format GUID
            fmt = (tag, channels, rate, bits)
        elif cid == b"data":
            end = len(data) if size in (0, 0xFFFFFFFF) or body + size > len(data) else body + size
            samples = data[body:end]
            break
        pos = body + size + (size & 1)
    if fmt is None or samples is None or fmt[1] < 1 or fmt[2] < 1000:
        raise TTSError("The voice engine produced an invalid audio file", "invalid-wav")
    return (*fmt, samples)


def to_pcm16(src: Path, dst: Path | None = None, gain: float = 1.0) -> Path:
    """Rewrite ``src`` as mono 16-bit PCM (in place, or to ``dst``), applying ``gain``.

    Kokoro writes stereo 32-bit float (WAVE_FORMAT_EXTENSIBLE), which Python's ``wave``
    module, the lip-sync analysis and some players cannot read. The gain is limited so
    the loudest sample never clips."""
    dst = dst or src
    tag, channels, rate, bits, raw = _read_wav(src)
    if tag == _PCM and bits == 16 and channels == 1 and gain == 1.0:
        if dst != src:
            shutil.copyfile(src, dst)
        return dst
    width = bits // 8
    usable = len(raw) - len(raw) % (width * channels)
    raw = raw[:usable]
    if tag == _FLOAT and bits == 32:
        values = array.array("f", raw)
        scale = 1.0
    elif tag == _FLOAT and bits == 64:
        values = array.array("d", raw)
        scale = 1.0
    elif tag == _PCM and bits == 16:
        values = array.array("h", raw)
        scale = 1 / 32768
    elif tag == _PCM and bits == 32:
        values = array.array("i", raw)
        scale = 1 / 2147483648
    elif tag == _PCM and bits == 8:
        values = array.array("f", (b - 128 for b in raw))
        scale = 1 / 128
    elif tag == _PCM and bits == 24:
        values = array.array("i", (int.from_bytes(raw[i:i + 3], "little", signed=True)
                                   for i in range(0, len(raw), 3)))
        scale = 1 / 8388608
    else:
        raise TTSError("The voice engine produced an unsupported audio format", "invalid-wav", f"{tag}/{bits}")
    if sys.byteorder == "big" and values.typecode in "hifd":
        values.byteswap()
    if not values:
        raise TTSError("The voice engine produced no audio", "no-audio")
    try:
        import numpy as np  # fast path; numpy comes with TTS BigLinux's Kokoro tools
    except ImportError:
        np = None
    if np is not None:
        a = np.frombuffer(values, dtype=np.dtype(values.typecode)).astype(np.float32) * scale
        mono = a.reshape(-1, channels).mean(axis=1) if channels > 1 else a
        mono = np.nan_to_num(mono)
        peak = float(np.abs(mono).max()) if mono.size else 0.0
        g = min(gain, 0.98 / peak) if peak > 0 else gain
        pcm = (np.clip(mono * g, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    else:
        mono = [sum(values[i:i + channels]) * scale / channels for i in range(0, len(values), channels)] \
            if channels > 1 else [v * scale for v in values]
        mono = [0.0 if v != v else v for v in mono]  # NaN
        peak = max((abs(v) for v in mono), default=0.0)
        g = min(gain, 0.98 / peak) if peak > 0 else gain
        out = array.array("h", (int(max(-1.0, min(1.0, v * g)) * 32767) for v in mono))
        if sys.byteorder == "big":
            out.byteswap()
        pcm = out.tobytes()
    tmp = dst.with_suffix(".part")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    os.chmod(tmp, 0o600)
    tmp.replace(dst)
    return dst


def analyse_wav(path: Path, fps: int = ENVELOPE_FPS) -> tuple[float, list[float]]:
    """Duration and normalised loudness envelope (0..1) of a 16-bit PCM WAV."""
    with wave.open(str(path), "rb") as w:
        rate, channels, width, frames = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(frames)
    duration = frames / rate if rate else 0.0
    if width != 2 or not raw:
        return duration, []
    samples = array.array("h", raw[: len(raw) - len(raw) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    step = max(1, int(rate / fps)) * channels
    levels = []
    for i in range(0, len(samples), step):
        window = samples[i:i + step]
        if window:
            levels.append(math.sqrt(sum(s * s for s in window) / len(window)))
    if not levels:
        return duration, []
    ref = sorted(levels)[int(len(levels) * 0.95)] or 1.0  # 95th percentile, robust to peaks
    gate = ref * 0.08
    return duration, [round(min(1.0, max(0.0, (v - gate) / (ref - gate))), 3) if ref > gate else 0.0
                      for v in levels]


class AudioPlayer:
    """Plays one WAV at a time on the desktop (PipeWire/PulseAudio/ALSA)."""

    CANDIDATES = (["pw-play"], ["paplay"], ["aplay", "-q"])

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self.command = next((c for c in self.CANDIDATES if shutil.which(c[0])), None)

    @property
    def available(self) -> bool:
        return self.command is not None

    def play(self, path: Path) -> None:
        """Start playback (stopping anything still playing)."""
        if not self.command:
            raise TTSError("No audio player found (pipewire, pulseaudio-utils or alsa-utils)", "no-player")
        with self._lock:
            self._stop_locked()
            self._proc = subprocess.Popen([*self.command, str(path)], stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)

    def wait(self, timeout: float | None = None) -> bool:
        """Block until playback ends. True if it finished on its own."""
        proc = self._proc
        if proc is None:
            return True
        try:
            return proc.wait(timeout) == 0
        except subprocess.TimeoutExpired:
            return False

    @property
    def playing(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(1)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
