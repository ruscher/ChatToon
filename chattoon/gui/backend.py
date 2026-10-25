"""Bridge between QML and the engine (which runs in its own asyncio thread)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path

import aiohttp
from PySide6.QtCore import Property, QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtQml import QJSValue

from ..core import extras, knowledge
from ..core.brain import apply_preset
from ..core.config import AISettings, AnimationSettings, DisplaySettings, OverlaySettings, ReadingSettings, VoiceSettings
from ..core.credentials import REDACTOR
from ..core.privacy import scrub
from ..core.characters import CHARACTERS, GESTURES, get_character
from ..core.config import PLATFORMS as PLATFORM_IDS
from ..core.config import Settings, SourceConfig, profiles_dir
from ..core.credentials import open_store
from ..core.engine import ChatToonEngine
from ..core.events import EventBus
from ..core.ollama import RECOMMENDED_MODELS
from ..core.runtime import Runtime
from ..core.streams import PLATFORMS
from ..core.streams.base import ConnectorError
from ..core.streams.twitch import TwitchAuth, client_id
from ..core.streams.urls import UrlError, detect, source_key
from .ai_center import OllamaCenter
from .character_window import CharacterWindow
from .connectivity import Connectivity
from .feed import ChatFeedFilter, ChatFeedModel, row_from_message
from .session_report import SessionReport
from .voice_center import VoiceCenter

log = logging.getLogger(__name__)

# settings that require the engine to rebuild its selector/overlay state
_ENGINE_SECTIONS = {"chat", "overlay", "ai", "mode", "character", "animation", "reading", "streams", "memory",
                    "spontaneous"}

# sections that "Restore defaults" may reset — never secrets, the model, the host or the token
_RESETTABLE = {
    "ai.generation": ("ai", AISettings, ("temperature", "max_tokens", "context_window", "timeout", "keep_alive",
                                         "max_sentences", "max_chars", "memory_size", "adaptive", "math_tool",
                                         "validate", "reference")),
    "overlay.appearance": ("overlay", OverlaySettings, ("show_bubble", "show_author", "show_platform", "scale",
                                                        "position_x", "position_y", "rotation", "camera_zoom", "light",
                                                        "shadows", "quality", "fps", "bubble_seconds", "background")),
    "display": ("display", DisplaySettings, ("size", "width", "height", "x", "y", "screen", "keep_above",
                                             "click_through", "locked", "skip_taskbar", "show_bubble")),
    "reading": ("reading", ReadingSettings, ("mode", "platform", "max_chars", "pause", "voice_id")),
    "animation": ("animation", AnimationSettings, ("idle", "intensity", "frequency", "reduce_motion")),
    "voice": ("voice", VoiceSettings, ("enabled", "volume", "audio_output")),
}

# secrets the interface may store: credential key -> allowed fields (nothing else is accepted)
_SECRETS = {"kick/app": ("client_secret",), "meta/token": ("access_token",), "x/bearer": ("bearer_token",),
            "tiktok/sign": ("api_key",)}


class Backend(QObject):
    configChanged = Signal()
    servicesChanged = Signal()
    runtimeChanged = Signal()  # state, phase, chat status, overlay
    statsChanged = Signal()
    queueChanged = Signal()
    historyChanged = Signal()
    sourcesChanged = Signal()
    accountsChanged = Signal()
    loginChanged = Signal()
    memoryChanged = Signal()  # who the avatar remembers (Live chat › Memory)
    memoryViewChanged = Signal()  # one viewer's conversation or a search
    diagnosticsChanged = Signal()  # latest generations (AI page)
    metadataChanged = Signal()  # what the platforms publish (Stream context)
    suggestionChanged = Signal()  # the assistant's proposal for one field
    probeFinished = Signal(str, "QVariantMap")  # source id (or "draft"), result
    toast = Signal(str, str)  # message, kind (info | success | error)
    _event = Signal(str, "QVariant")  # engine thread -> GUI thread

    def __init__(self, settings: Settings | None = None, engine: ChatToonEngine | None = None, parent=None):
        super().__init__(parent)
        self.settings = settings or Settings.load()
        self.bus = EventBus()
        self.engine = engine or ChatToonEngine(self.settings, self.bus)
        if engine is not None:
            self.bus = engine.bus
        self.runtime = Runtime()
        self._services: dict = {"ollama": {"ok": None, "models": []}, "tts": {"ok": None, "voices": []}}
        self._state = {"state": "stopped", "phase": "idle", "chat": "stopped", "chatDetail": "",
                       "overlayUrl": "", "publicOverlayUrl": "", "lanOverlayUrl": "", "lanError": "", "port": 0,
                       "obs": 0, "preview": 0, "desktop": 0, "browser": 0, "lastError": "", "lastErrorSource": ""}
        self._stats: dict = {}
        self._queue: dict = {"pending": [], "suggestions": []}
        self._history: list[dict] = []
        self._sources: list[dict] = []
        self._connected: list[str] = []
        self._accounts: dict = {"store": "", "encrypted": False}
        self._login: dict = {"platform": "", "state": "idle"}
        self._login_future = None
        self._memory: dict = {"stats": {}, "participants": []}
        self._memory_view: dict = {"mode": "", "key": "", "items": [], "notes": [], "query": ""}
        self._diagnostics: list[dict] = []
        self._metadata: dict = {"sources": [], "time": 0, "loading": False}
        self._suggestion: dict = {"field": "", "loading": False}
        self._closing = False
        self._missing_model = ""
        self._memory_timer = QTimer(self, singleShot=True, interval=1200)  # a burst of replies → one refresh
        self._memory_timer.timeout.connect(self.refreshMemory)
        self._feed = ChatFeedModel(self)
        self._feed_filter = ChatFeedFilter(self._feed, self)
        self._feed_pending: list[dict] = []
        self._feed_timer = QTimer(self, singleShot=True, interval=80)  # batch bursts of messages
        self._feed_timer.timeout.connect(self._flush_feed)
        self._secrets: set[str] = set()
        self.remember_secret(self.settings.overlay.token)
        self.ai = OllamaCenter(self)
        self.voices = VoiceCenter(self)
        self.net = Connectivity(self)
        self.session = SessionReport(self)
        self.window = CharacterWindow(self)
        self._event.connect(self._on_event)
        self.bus.subscribe(lambda kind, data: self._event.emit(kind, data))
        self.runtime.start()
        self._boot_future = self.runtime.submit(self._boot())

    async def _boot(self) -> None:
        ctx = self.engine.streams.ctx
        if ctx.credentials is None:
            ctx.credentials = await asyncio.to_thread(open_store)  # D-Bus may block: never on the GUI thread
        ctx.open_browser = lambda url: self.bus.emit("open_url", {"url": url})
        self.engine.refresh_sources()
        await self._refresh_accounts()
        try:
            await self.engine.startup()
        except OSError as e:
            self.bus.emit("error", {"source": "overlay", "message": str(e), "code": "overlay-port"})
        await self.engine.check_services()
        if not self._closing:
            self.ai._set("_status", await self.ai.service.status(self.settings.ai.host), self.ai.serviceChanged)

    def shutdown(self) -> None:
        self._closing = True
        self._memory_timer.stop()
        self.window.shutdown()
        self.settings.save()
        self._boot_future.cancel()  # closing during start-up must not leave a dangling task
        self.runtime.stop(self._shutdown())

    async def _shutdown(self) -> None:
        await self.engine.shutdown()
        await self.ai.shutdown()  # an `ollama serve` started by ChatToon ends with it

    # -- privacy ---------------------------------------------------------------------------

    def remember_secret(self, value: str) -> None:
        """Never show or log this value (overlay token, public IP…)."""
        if value:
            self._secrets.add(value)
            REDACTOR.add(value)

    def notify(self, message: str, kind: str = "info") -> None:
        """Every toast goes through here: tokens, the public IP and (in live privacy mode)
        every address are masked before anything reaches the screen."""
        if message:
            self.toast.emit(scrub(message, tuple(self._secrets), self.settings.privacy_mode), kind)

    # -- events from the engine (GUI thread) -------------------------------

    def _on_event(self, kind: str, data) -> None:
        data = data or {}
        if kind == "services":
            self._services = data
            self._autoselect()
            self.servicesChanged.emit()
        elif kind == "state":
            self._state["state"] = data.get("state", "stopped")
            self.runtimeChanged.emit()
        elif kind == "phase":
            self._state["phase"] = data.get("phase", "idle")
            self.runtimeChanged.emit()
        elif kind == "chat_status":
            self._state["chat"] = data.get("state", "")
            self._state["chatDetail"] = data.get("detail", "")
            self.runtimeChanged.emit()
            if data.get("state") == "error":
                self.notify(data.get("detail", ""), "error")
        elif kind == "overlay":
            self._state["overlayUrl"] = data.get("url", "")
            self._state["publicOverlayUrl"] = data.get("publicUrl", "")
            self._state["lanOverlayUrl"] = data.get("lanUrl", "")
            self._state["lanError"] = data.get("lanError", "")
            self._state["port"] = data.get("port", 0)
            self.runtimeChanged.emit()
            self.window.changed.emit()  # the window follows a new address (new token)
        elif kind == "overlay_clients":
            self._state["obs"] = data.get("obs", 0)
            self._state["preview"] = data.get("preview", 0)
            self._state["desktop"] = data.get("desktop", 0)
            self._state["browser"] = data.get("browser", 0)
            self.runtimeChanged.emit()
        elif kind == "stats":
            self._stats = data
            self.statsChanged.emit()
        elif kind == "queue":
            self._queue = data
            self.queueChanged.emit()
        elif kind == "message":
            data = dict(data, receivedAt=time.time())
            self._feed_pending.append(row_from_message(data))
            if not self._feed_timer.isActive():
                self._feed_timer.start()
        elif kind == "message_state":
            self._message_state(data)
        elif kind == "sources":
            self._sources = data.get("sources", [])
            self._connected = data.get("connected", [])
            self.sourcesChanged.emit()
        elif kind == "accounts":
            self._accounts = data
            self.accountsChanged.emit()
        elif kind == "login":
            self._login = data
            self.loginChanged.emit()
            if data.get("state") == "done":
                self.toast.emit(self.tr("Account connected: %1").replace("%1", data.get("login", "")), "success")
            elif data.get("state") == "error":
                self.toast.emit(self._login_error(data.get("code", ""), data.get("detail", "")), "error")
        elif kind == "open_url":
            QDesktopServices.openUrl(QUrl(data.get("url", "")))
        elif kind == "history":
            self._history = ([data] + self._history)[:300]
            self.historyChanged.emit()
            self._memory_timer.start()
        elif kind == "memory":
            self._memory = data
            self.memoryChanged.emit()
        elif kind == "memory_view":
            self._memory_view = data
            self.memoryViewChanged.emit()
        elif kind == "diagnostics":
            self._diagnostics = ([data] + self._diagnostics)[:20]
            self.diagnosticsChanged.emit()
        elif kind == "metadata":
            self._metadata = dict(data, loading=False)
            self.metadataChanged.emit()
        elif kind == "suggestion":
            self._suggestion = dict(data, loading=False)
            self.suggestionChanged.emit()
        elif kind == "error":
            message = scrub(self._error_text(data), tuple(self._secrets), self.settings.privacy_mode)
            self._state["lastError"] = message
            self._state["lastErrorSource"] = data.get("source", "")
            self.runtimeChanged.emit()
            self.notify(message, "error")
        elif kind == "probe":
            self.probeFinished.emit(data.get("id", ""), data)
        elif kind == "toast":
            self.toast.emit(self.tr("Saved in the keyring"), "success")

    def _flush_feed(self) -> None:
        rows, self._feed_pending = self._feed_pending, []
        self._feed.append(rows)

    def _message_state(self, data: dict) -> None:
        uid, state = data.get("id", ""), data.get("state", "")
        changes = {"status": state, "reason": data.get("reason", "")}
        if data.get("reply"):
            changes["reply"] = data["reply"]
        if data.get("memory"):
            changes["memory"] = int(data["memory"])
        for row in self._feed_pending:  # not shown yet: patch the pending row
            if row["uid"] == uid:
                row.update(changes)
                return
        self._feed.update(uid, **changes)

    def _autoselect(self) -> None:
        """Pick a sensible model the first time only. A model the person chose is never
        replaced silently: if it disappears, the AI page and the start report say so."""
        ollama = self._services.get("ollama", {})
        models = ollama.get("models", [])
        if models and not self.settings.ai.model:
            preferred = next((m for m in RECOMMENDED_MODELS if m in models), models[0])
            self.settings.ai.model = preferred
            self.settings.save()
            self.configChanged.emit()
        missing = bool(ollama.get("ok") and self.settings.ai.model and self.settings.ai.model not in models)
        if missing and self._missing_model != self.settings.ai.model:
            self.notify(self.tr("The model %1 is no longer available. Choose another one on the AI page.")
                        .replace("%1", self.settings.ai.model), "error")
        self._missing_model = self.settings.ai.model if missing else ""

    # -- properties --------------------------------------------------------

    def _config(self) -> dict:
        data = asdict(self.settings)
        data["current"] = asdict(self.settings.character_settings())
        return data

    config = Property("QVariantMap", _config, notify=configChanged)
    services = Property("QVariantMap", lambda self: self._services, notify=servicesChanged)
    runtimeState = Property("QVariantMap", lambda self: self._state, notify=runtimeChanged)
    stats = Property("QVariantMap", lambda self: self._stats, notify=statsChanged)
    queue = Property("QVariantMap", lambda self: self._queue, notify=queueChanged)
    history = Property("QVariantList", lambda self: self._history, notify=historyChanged)
    sources = Property("QVariantList", lambda self: self._sources, notify=sourcesChanged)
    connectedPlatforms = Property("QVariantList", lambda self: self._connected, notify=sourcesChanged)  # noqa: N815
    accounts = Property("QVariantMap", lambda self: self._accounts, notify=accountsChanged)
    login = Property("QVariantMap", lambda self: self._login, notify=loginChanged)
    memory = Property("QVariantMap", lambda self: self._memory, notify=memoryChanged)
    memoryView = Property("QVariantMap", lambda self: self._memory_view, notify=memoryViewChanged)  # noqa: N815
    diagnostics = Property("QVariantList", lambda self: self._diagnostics, notify=diagnosticsChanged)
    metadata = Property("QVariantMap", lambda self: self._metadata, notify=metadataChanged)
    suggestion = Property("QVariantMap", lambda self: self._suggestion, notify=suggestionChanged)
    feed = Property(QObject, lambda self: self._feed_filter, constant=True)
    aiCenter = Property(QObject, lambda self: self.ai, constant=True)  # noqa: N815
    voiceCenter = Property(QObject, lambda self: self.voices, constant=True)  # noqa: N815
    connectivity = Property(QObject, lambda self: self.net, constant=True)
    sessionReport = Property(QObject, lambda self: self.session, constant=True)  # noqa: N815
    characterWindow = Property(QObject, lambda self: self.window, constant=True)  # noqa: N815
    feedModel = Property(QObject, lambda self: self._feed, constant=True)  # noqa: N815

    def _platforms(self) -> list[dict]:
        notes = {
            "youtube": self.tr("Public live chat, no login. Paste a live link or your @channel to follow every new live."),
            "twitch": self.tr("Official EventSub with a Twitch login, or read-only guest mode without login."),
            "kick": self.tr("Official webhooks with your own Kick app and a public HTTPS address. The unofficial public socket was retired by Kick in October 2026."),
            "tiktok": self.tr("Unofficial TikTokLive library; uses the Euler Stream signing service. Optional install."),
            "facebook": self.tr("Facebook Pages only, with a token from your own Meta app. Personal profiles cannot be read."),
            "instagram": self.tr("Professional accounts linked to a Facebook Page, with a token from your own Meta app."),
            "x": self.tr("No API exists for live video or Spaces chat. Can read replies to a post with your own paid API key."),
        }
        accepts = {
            "youtube": self.tr("youtube.com/live/…, youtu.be/… or @channel"),
            "twitch": self.tr("twitch.tv/channel or just the channel name"),
            "kick": self.tr("kick.com/channel or just the channel name"),
            "tiktok": self.tr("tiktok.com/@user/live or @user"),
            "facebook": self.tr("facebook.com/YourPage or the link of a live video"),
            "instagram": self.tr("instagram.com/user or @user"),
            "x": self.tr("Link of the post that announces the live"),
        }
        return [dict(info, id=pid, note=notes.get(pid, ""), accepts=accepts.get(pid, info["accepts"]))
                for pid, info in PLATFORMS.items()]

    platforms = Property("QVariantList", _platforms, constant=True)

    def _error_text(self, data: dict) -> str:
        """Engine errors carry a code: show them in the interface language."""
        code, detail, message = data.get("code", ""), data.get("detail", ""), data.get("message", "")
        known = {
            "no-model": self.tr("Choose an Ollama model first."),
            "kokoro-unavailable": self.tr("Kokoro: %1"),
            "kokoro-missing": self.tr("This TTS BigLinux version cannot use Kokoro voices. Update the tts-biglinux package."),
            "kokoro-timeout": self.tr("Kokoro took too long to speak this text. Try a shorter reply or another voice."),
            "kokoro-failed": self.tr("Kokoro could not speak this text: %1"),
            "no-audio": self.tr("The voice engine produced no audio."),
            "invalid-wav": self.tr("The voice engine produced an invalid audio file."),
            "unsupported": self.tr("The voice engine %1 is not supported."),
            "missing": self.tr("%1 is not installed."),
            "timeout": self.tr("%1 took too long to answer."),
            "failed": self.tr("The voice engine %1 failed."),
            "no-spontaneous": self.tr("No spontaneous line is available for this category. Add curiosities or fill in "
                                      "the stream topic."),
            "no-sources": self.tr("Add a streaming platform (and enable it) before starting, or use Demo mode."),
            "ai-unreachable": self.tr("Ollama is not answering. Start it on the AI page."),
            "ai-timeout": self.tr("The model %1 took too long to answer. Try a smaller model or a longer timeout."),
            "ai-model-missing": self.tr("The model %1 is not installed in Ollama. Choose another one on the AI page."),
            "ai-empty": self.tr("The model %1 gave an empty answer. Try again or choose another model."),
            "ai-failed": self.tr("The AI could not answer: %1"),
            "tts-missing": self.tr("TTS BigLinux is not installed. Install it on the Voice page."),
            "tts-broken": self.tr("TTS BigLinux could not be loaded. Reinstall the tts-biglinux package."),
            "no-text": self.tr("There was nothing to say after cleaning the text."),
            "voice-missing": self.tr("The voice %1 is not installed anymore. Choose another one on the Voice page."),
            "no-player": self.tr("No audio player was found. Install pipewire-audio (or pulseaudio-utils)."),
            "overlay-port": self.tr("No free port for the overlay. Close the program that uses it and restart ChatToon."),
            "keyring": self.tr("The system keyring refused the operation: %1"),
        }
        text = known.get(code)
        return text.replace("%1", detail or message) if text else message

    def _characters(self) -> list[dict]:
        lang = self.settings.ai.reply_language
        info = {
            "worm": (self.tr("Cartoon earthworm"), self.tr("Ironic, funny and a bit of a provocateur.")),
            "alien": (self.tr("Alien explorer"), self.tr("Curious, tech-savvy and unpredictable.")),
            "tux": (self.tr("Penguin in a top hat"), self.tr("Geeky, elegant and a free software champion.")),
            "cat": (self.tr("Orange cat"), self.tr("Sarcastic, clever and independent.")),
            "owl": (self.tr("Purple owl"), self.tr("Wise, observant and good-humoured.")),
            "dog": (self.tr("Beagle puppy"), self.tr("Cheerful, playful and full of energy.")),
        }
        return [{"id": c.id, "name": c.name, "color": c.color, "preset": c.personality.preset,
                 "species": info.get(c.id, ("", ""))[0], "tagline": info.get(c.id, ("", ""))[1],
                 "persona": c.text(c.persona, lang), "example": c.text(c.examples, lang)[0][1],
                 "exampleIn": c.text(c.examples, lang)[0][0],
                 "gestures": [{"id": g, "label": self._gesture_label(g)} for g in GESTURES.get(c.id, ())]}
                for c in CHARACTERS.values()]

    def _gesture_label(self, gesture: str) -> str:
        labels = {
            "lookAround": self.tr("Look around"), "stretch": self.tr("Stretch"), "wiggle": self.tr("Wiggle"),
            "peek": self.tr("Hide in the dirt"), "headBob": self.tr("Head bob"), "dig": self.tr("Dig"),
            "scan": self.tr("Scan"), "beep": self.tr("Beep"), "wave": self.tr("Wave"), "orbit": self.tr("Spin in the air"),
            "probe": self.tr("Investigate"), "tipHat": self.tr("Tip the hat"), "adjustHat": self.tr("Adjust the hat"),
            "flap": self.tr("Flap"), "waddle": self.tr("Waddle"), "bow": self.tr("Bow"), "bellyPat": self.tr("Belly pat"),
            "tailWag": self.tr("Wag the tail"), "earTwitch": self.tr("Twitch an ear"), "slowBlink": self.tr("Slow blink"),
            "headTilt": self.tr("Tilt the head"), "curiousLook": self.tr("Curious look"), "silentMeow": self.tr("Silent meow"),
            "pawRaise": self.tr("Raise a paw"), "faceWash": self.tr("Wash the face"), "headTurn": self.tr("Turn the head"),
            "wingOpen": self.tr("Open the wings"), "wingFlap": self.tr("Flap the wings"), "sway": self.tr("Sway"),
            "ruffle": self.tr("Ruffle the feathers"), "hoot": self.tr("Hoot"), "surprisePuff": self.tr("Startle"),
            "techExcited": self.tr("Tech excitement"), "earFlop": self.tr("Flop the ears"), "pant": self.tr("Pant"),
            "tongueOut": self.tr("Tongue out"), "hop": self.tr("Hop"), "joy": self.tr("Joy"),
            "curiousSniff": self.tr("Sniff around"),
        }
        return labels.get(gesture, gesture)

    characters = Property("QVariantList", _characters, constant=True)

    # -- settings ----------------------------------------------------------

    @Slot(str, "QVariant")
    def setConfig(self, path: str, value) -> None:  # noqa: N802
        """Update a setting by dotted path ("ai.model"); "current.*" targets the selected character."""
        parts = path.split(".")
        if parts[0] == "current":
            target, attrs = self.settings.character_settings(), parts[1:]
        else:
            target, attrs = self.settings, parts
        try:
            for attr in attrs[:-1]:
                target = getattr(target, attr)
            name = attrs[-1]
            current = getattr(target, name)
        except AttributeError:
            log.warning("unknown setting %s", path)
            return
        if isinstance(value, QJSValue):  # a JavaScript array from QML, e.g. spontaneous.categories
            value = value.toVariant()
        try:
            if isinstance(current, bool):
                value = bool(value)
            elif isinstance(current, (int, float)):
                value = type(current)(float(value))
            elif isinstance(current, list):
                value = [str(v).strip() for v in (value if isinstance(value, list) else str(value).splitlines())
                         if str(v).strip()]
            else:
                value = str(value)
        except (TypeError, ValueError):
            return
        if value == current:
            return
        setattr(target, name, value)
        if parts[:2] == ["current", "personality"] and name not in ("preset", "custom_prompt", "length"):
            self.settings.character_settings().personality.preset = "custom"  # a slider moved
        self.settings.save()
        self.configChanged.emit()
        if parts[0] in _ENGINE_SECTIONS or parts[0] == "current":
            self.runtime.submit(self._apply())

    async def _apply(self) -> None:
        self.engine.apply_settings()

    @Slot(str)
    def applyPreset(self, preset: str) -> None:  # noqa: N802
        apply_preset(self.settings.character_settings().personality, preset)
        self.settings.save()
        self.configChanged.emit()

    @Slot(str)
    def selectCharacter(self, character_id: str) -> None:  # noqa: N802
        self.settings.character = get_character(character_id).id
        self.settings.save()
        self.configChanged.emit()
        self.runtime.submit(self.engine.set_character(character_id))

    # -- streaming sources ---------------------------------------------------

    def _url_error(self, code: str) -> str:
        return {
            "empty": self.tr("Paste a link or type the channel."),
            "unknown": self.tr("This link is not from a supported platform."),
            "invalid": self.tr("This link or name is not valid for this platform."),
            "short-link": self.tr("Short links cannot be checked. Open it in the browser and paste the full address."),
            "ambiguous": self.tr("Choose the platform for this name."),
            "unsupported": self.tr("X Spaces are audio rooms without a readable chat."),
        }.get(code, self.tr("This link or name is not valid for this platform."))

    @Slot(str, str, str, result="QVariantMap")
    def detectSource(self, text: str, platform: str, editing: str = "") -> dict:  # noqa: N802
        """Recognise a pasted link: platform, canonical target and duplicates."""
        try:
            found = detect(text, platform if platform in PLATFORM_IDS else "")
        except UrlError as e:
            return {"ok": False, "code": e.code, "error": self._url_error(e.code)}
        duplicate = next((s for s in self.settings.stream_sources()
                          if s.id != editing and source_key(s.platform, s.target) == found.key), None)
        return {"ok": duplicate is None, "platform": found.platform, "target": found.target, "display": found.display,
                "kind": found.kind, "code": "duplicate" if duplicate else "",
                "error": self.tr("This chat is already in the list.") if duplicate else ""}

    def _save_sources(self, sources: list[SourceConfig]) -> None:
        self.settings.streams.sources = sources
        self.settings.streams.migrated = True
        self.settings.save()
        self.configChanged.emit()
        self.runtime.submit(self._apply())

    @Slot(str, str, str, bool, "QVariantMap", result=str)
    def addSource(self, platform: str, target: str, label: str, enabled: bool, options) -> str:  # noqa: N802
        check = self.detectSource(target, platform, "")
        if not check["ok"]:
            self.toast.emit(check["error"], "error")
            return ""
        source = SourceConfig(platform=check["platform"], target=check["target"], label=label.strip()[:60],
                              enabled=enabled, options={str(k): str(v) for k, v in dict(options or {}).items()})
        self._save_sources([*self.settings.stream_sources(), source])
        self.toast.emit(self.tr("%1 added").replace("%1", PLATFORMS[source.platform]["label"]), "success")
        return source.id

    @Slot(str, "QVariantMap", result=bool)
    def updateSource(self, source_id: str, changes) -> bool:  # noqa: N802
        sources = list(self.settings.stream_sources())
        source = next((s for s in sources if s.id == source_id), None)
        if source is None:
            return False
        changes = dict(changes or {})
        if "target" in changes or "platform" in changes:
            check = self.detectSource(str(changes.get("target", source.target)),
                                      str(changes.get("platform", source.platform)), source_id)
            if not check["ok"]:
                self.toast.emit(check["error"], "error")
                return False
            source.platform, source.target = check["platform"], check["target"]
        if "label" in changes:
            source.label = str(changes["label"]).strip()[:60]
        if "enabled" in changes:
            source.enabled = bool(changes["enabled"])
        if "priority" in changes:
            source.priority = min(5, max(1, int(float(changes["priority"]))))
        if "options" in changes:
            source.options = SourceConfig(options={str(k): str(v) for k, v in dict(changes["options"]).items()}).options
        self._save_sources(sources)
        return True

    @Slot(str)
    def removeSource(self, source_id: str) -> None:  # noqa: N802
        self._save_sources([s for s in self.settings.stream_sources() if s.id != source_id])

    @Slot(str)
    def connectSource(self, source_id: str) -> None:  # noqa: N802
        self.runtime.loop.call_soon_threadsafe(self.engine.connect_source, source_id)

    @Slot(str)
    def disconnectSource(self, source_id: str) -> None:  # noqa: N802
        self.runtime.loop.call_soon_threadsafe(self.engine.disconnect_source, source_id)

    @Slot(str)
    @Slot(str, str, str, "QVariantMap")
    def testSource(self, source_id: str, platform: str = "", target: str = "", options=None) -> None:  # noqa: N802
        """Check a saved source (by id) or the one being typed in the dialog (id "draft")."""
        if source_id == "draft":
            source = SourceConfig(id="draft", platform=platform, target=target,
                                  options={str(k): str(v) for k, v in dict(options or {}).items()})
        else:
            source = next((s for s in self.settings.stream_sources() if s.id == source_id), None)
            if source is None:
                return

        async def run() -> None:
            result = await self.engine.probe_source(source)
            self.bus.emit("probe", {"id": source_id, **result.to_dict()})

        self.runtime.submit(run())

    # -- accounts and secrets -------------------------------------------------

    async def _refresh_accounts(self) -> None:
        store = self.engine.streams.ctx.credentials

        def read() -> dict:
            twitch = store.get("twitch/account") if store else None
            extras.activate()
            return {
                "store": getattr(store, "backend", ""), "encrypted": bool(getattr(store, "encrypted", False)),
                "twitch": {"connected": bool(twitch), "login": (twitch or {}).get("login", ""),
                           "clientId": bool(client_id(self.settings.streams.apps))},
                "kick": {"secret": bool(store and store.has("kick/app"))},
                "meta": {"token": bool(store and store.has("meta/token"))},
                "x": {"token": bool(store and store.has("x/bearer"))},
                "tiktok": {"installed": importlib.util.find_spec("TikTokLive") is not None,
                           "key": bool(store and store.has("tiktok/sign")),
                           "install": " && ".join(" ".join(c) for c in extras.install_commands("tiktok"))},
            }

        try:
            self.bus.emit("accounts", await asyncio.to_thread(read))
        except Exception as e:  # noqa: BLE001 - a locked keyring must not break the app
            log.warning("cannot read the keyring: %s", type(e).__name__)

    @Slot(str, str, str)
    def setSecret(self, key: str, field: str, value: str) -> None:  # noqa: N802
        """Store a pasted secret in the keyring (never in settings files)."""
        if field not in _SECRETS.get(key, ()) or not value.strip():
            return
        store = self.engine.streams.ctx.credentials

        async def run() -> None:
            try:
                await asyncio.to_thread(store.set, key, {field: value.strip()})
                self.bus.emit("toast", {"message": "saved"})
            except OSError as e:
                self.bus.emit("error", {"source": "accounts", "message": str(e), "code": "keyring",
                                        "detail": e.strerror or type(e).__name__})
            await self._refresh_accounts()

        self.runtime.submit(run())

    @Slot(str)
    def clearSecret(self, key: str) -> None:  # noqa: N802
        if key not in _SECRETS:
            return
        store = self.engine.streams.ctx.credentials

        async def run() -> None:
            await asyncio.to_thread(store.delete, key)
            await self._refresh_accounts()

        self.runtime.submit(run())

    def _login_error(self, code: str, detail: str) -> str:
        return {
            "no-client-id": self.tr("Set the Twitch Client ID first."),
            "auth-denied": self.tr("The login was not approved."),
            "auth-expired": self.tr("The login code expired. Try again."),
        }.get(code, detail or self.tr("The login failed."))

    @Slot()
    def loginTwitch(self) -> None:  # noqa: N802
        """Device Code login: show a code, open twitch.tv/activate, wait for approval."""
        if self._login_future and not self._login_future.done():
            return
        auth = TwitchAuth(self.engine.streams.ctx.credentials, client_id(self.settings.streams.apps))

        async def run() -> None:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
                    device = await auth.start(http)
                    self.bus.emit("login", {"platform": "twitch", "state": "waiting", "code": device["user_code"],
                                            "url": device["verification_uri"]})
                    self.bus.emit("open_url", {"url": device["verification_uri"]})
                    account = await auth.finish(http, device)
                self.bus.emit("login", {"platform": "twitch", "state": "done", "login": account.get("login", "")})
                await self._refresh_accounts()
                self.engine.reconnect_platform("twitch")
            except asyncio.CancelledError:
                self.bus.emit("login", {"platform": "twitch", "state": "idle"})
                raise
            except ConnectorError as e:
                self.bus.emit("login", {"platform": "twitch", "state": "error", "code": e.code, "detail": str(e)})
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
                self.bus.emit("login", {"platform": "twitch", "state": "error", "code": "network",
                                        "detail": type(e).__name__})

        self._login_future = self.runtime.submit(run())

    @Slot()
    def cancelLogin(self) -> None:  # noqa: N802
        if self._login_future:
            self._login_future.cancel()
        self._login = {"platform": "", "state": "idle"}
        self.loginChanged.emit()

    @Slot()
    def logoutTwitch(self) -> None:  # noqa: N802
        auth = TwitchAuth(self.engine.streams.ctx.credentials, client_id(self.settings.streams.apps))

        async def run() -> None:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
                await auth.revoke(http)
            await self._refresh_accounts()
            self.engine.reconnect_platform("twitch")

        self.runtime.submit(run())

    @Slot()
    def refreshAccounts(self) -> None:  # noqa: N802
        self.runtime.submit(self._refresh_accounts())

    # -- feed ------------------------------------------------------------------

    @Slot()
    def clearFeed(self) -> None:  # noqa: N802
        self._feed.clear()

    @Slot()
    def finishFirstRun(self) -> None:  # noqa: N802
        self.settings.first_run_done = True
        self.settings.save()
        self.configChanged.emit()

    # -- live controls -----------------------------------------------------

    @Slot()
    def start(self) -> None:
        """Start through the report: essentials are checked first (docs/ARCHITECTURE.md)."""
        self.session.startLive()

    @Slot()
    def startQuietly(self) -> None:  # noqa: N802
        """Same checks, without opening the report (typing to the avatar in Demo mode)."""
        self.session.startLive(False)

    @Slot(str)
    def setDisplayMode(self, mode: str) -> None:  # noqa: N802
        """Where the character appears: OBS, a window on the desktop, or both."""
        if mode not in ("obs", "window", "both"):
            return
        self.settings.display.mode = mode
        self.settings.save()
        self.configChanged.emit()
        if mode == "obs":
            self.window.hide()
        else:
            self.window.show()

    @Slot(str)
    def resetSection(self, name: str) -> None:  # noqa: N802
        """Restore the defaults of one section only (other settings and credentials stay)."""
        found = _RESETTABLE.get(name)
        if found is None:
            return
        attr, cls, fields = found
        target, fresh = getattr(self.settings, attr), cls()
        for f in fields:
            setattr(target, f, getattr(fresh, f))
        if name == "voice":
            cs = self.settings.character_settings()
            cs.voice_id, cs.voice_rate, cs.voice_pitch = "", 0, 0
        self.settings.save()
        self.configChanged.emit()
        self.runtime.submit(self._apply())
        self.notify(self.tr("Defaults restored"), "success")

    @Slot()
    def pause(self) -> None:
        self.runtime.loop.call_soon_threadsafe(self.engine.pause)

    @Slot()
    def resume(self) -> None:
        self.runtime.loop.call_soon_threadsafe(self.engine.resume)

    @Slot()
    def stop(self) -> None:
        self.runtime.submit(self.engine.stop())

    @Slot()
    def emergencyStop(self) -> None:  # noqa: N802
        self.runtime.submit(self.engine.emergency_stop())

    @Slot()
    def interrupt(self) -> None:
        self.runtime.submit(self.engine.interrupt())

    @Slot()
    def clearQueue(self) -> None:  # noqa: N802
        self.runtime.loop.call_soon_threadsafe(self.engine.clear_queue)

    @Slot(str, str)
    def submitMessage(self, text: str, author: str) -> None:  # noqa: N802
        if text.strip():
            self.runtime.loop.call_soon_threadsafe(self.engine.submit, text, author.strip() or self.tr("You"))

    @Slot(str)
    @Slot(str, str)
    def testReply(self, text: str, author: str = "") -> None:  # noqa: N802
        """Simulate a viewer message (read aloud + AI reply), whatever the mode."""
        if text.strip():
            self.runtime.submit(self.engine.test_reply(text, author.strip() or self.tr("Viewer")))

    @Slot(str, str)
    def say(self, text: str, emotion: str) -> None:
        self.runtime.submit(self.engine.say(text, emotion or "happy"))

    @Slot(str)
    def previewVoice(self, voice_id: str) -> None:  # noqa: N802
        sample = self.tr("Hi! This is how I sound. Ready for the live stream?")
        self.runtime.submit(self.engine.say(sample, "happy", voice_id))

    @Slot(str)
    def react(self, emotion: str) -> None:
        self.runtime.submit(self.engine.react(emotion))

    @Slot(str)
    def gesture(self, name: str) -> None:
        self.runtime.submit(self.engine.gesture(name))

    @Slot()
    def previewReading(self) -> None:  # noqa: N802
        self.runtime.submit(self.engine.preview_reading())

    @Slot(result=str)
    def readingSample(self) -> str:  # noqa: N802
        """What the current reading settings would say for a sample message."""
        return self.engine.reading_sample()

    @Slot(str, result=str)
    def voiceFor(self, character_id: str) -> str:  # noqa: N802
        """Name of the voice a character speaks with ("" while TTS BigLinux is unavailable)."""
        if not self._services.get("tts", {}).get("ok"):
            return ""
        voice = self.engine.voice_for(get_character(character_id))
        return voice.label if voice else ""

    @Slot(str)
    def approve(self, item_id: str) -> None:
        self.runtime.submit(self.engine.approve(item_id))

    @Slot(str)
    def reject(self, item_id: str) -> None:
        self.runtime.loop.call_soon_threadsafe(self.engine.reject, item_id)

    @Slot()
    def refreshServices(self) -> None:  # noqa: N802
        self.runtime.submit(self.engine.check_services())

    @Slot(str)
    def pullModel(self, model: str) -> None:  # noqa: N802
        self.ai.pullModel(model)

    # -- memory (Live chat › Memory) -------------------------------------------
    # Runs on the engine's loop (never a worker thread): the memory is only touched there.

    @Slot()
    def refreshMemory(self) -> None:  # noqa: N802
        if self._closing:
            return

        async def run() -> None:
            self.bus.emit("memory", self.engine.memory_overview())
        self.runtime.submit(run())

    @Slot(str)
    def openConversation(self, key: str) -> None:  # noqa: N802
        async def run() -> None:
            view = self.engine.memory_conversation(key)
            self.bus.emit("memory_view", {"mode": "viewer", "key": key, "items": view["exchanges"],
                                          "notes": view["notes"], "query": ""})
        self.runtime.submit(run())

    @Slot(str)
    def searchMemory(self, query: str) -> None:  # noqa: N802
        query = query.strip()[:80]
        if not query:
            self._memory_view = {"mode": "", "key": "", "items": [], "notes": [], "query": ""}
            self.memoryViewChanged.emit()
            return

        async def run() -> None:
            items = self.engine.memory_search(query)
            self.bus.emit("memory_view", {"mode": "search", "key": "", "items": items, "notes": [], "query": query})
        self.runtime.submit(run())

    @Slot(str)
    def forgetViewer(self, key: str) -> None:  # noqa: N802
        async def run() -> None:
            self.engine.forget_viewer(key)
            self.bus.emit("memory_view", {"mode": "", "key": "", "items": [], "notes": [], "query": ""})
            self.bus.emit("memory", self.engine.memory_overview())
        self.runtime.submit(run())
        self.toast.emit(self.tr("Memories of this viewer erased"), "success")

    @Slot()
    def clearMemory(self) -> None:  # noqa: N802
        async def run() -> None:
            self.engine.clear_memory()
            self.bus.emit("memory_view", {"mode": "", "key": "", "items": [], "notes": [], "query": ""})
            self.bus.emit("memory", self.engine.memory_overview())
        self.runtime.submit(run())
        self.toast.emit(self.tr("All memories erased"), "success")

    # -- stream context assistant ------------------------------------------------

    @Slot(bool)
    def fetchMetadata(self, force: bool = False) -> None:  # noqa: N802
        self._metadata = dict(self._metadata, loading=True)
        self.metadataChanged.emit()
        self.runtime.submit(self.engine.stream_metadata(force))

    @Slot(str, "QVariantList")
    def suggestContext(self, field_name: str, source_ids=None) -> None:  # noqa: N802
        self._suggestion = {"field": field_name, "loading": True}
        self.suggestionChanged.emit()
        ids = [str(s) for s in (source_ids or [])]

        async def run() -> None:
            try:
                await self.engine.suggest_context(field_name, ids or None)
            except Exception:  # noqa: BLE001 - shown in the dialog, never a crash
                log.exception("context suggestion failed")
                self.bus.emit("suggestion", {"field": field_name, "error": "ai-failed"})
        self.runtime.submit(run())

    @Slot(str, str)
    def applySuggestion(self, field_name: str, value: str) -> None:  # noqa: N802
        if field_name in ("channel_name", "host_name", "topic", "description", "guests", "links", "catchphrases",
                          "allowed_topics", "forbidden_topics", "promo_messages"):
            self.setConfig(f"context.{field_name}", value)
            self.toast.emit(self.tr("Suggestion applied"), "success")

    # -- characters: what they say on their own ------------------------------------

    @Slot(str, result="QVariantList")
    def curiosities(self, character_id: str) -> list:  # noqa: N802
        lang = self.settings.ai.reply_language
        return [{"text": line.text, "kind": line.kind, "category": line.category}
                for line in knowledge.lines(get_character(character_id).id, lang)]

    @Slot(str)
    def testSpontaneous(self, category: str = "") -> None:  # noqa: N802
        self.runtime.submit(self.engine.spontaneous_now(category))

    # -- desktop helpers ---------------------------------------------------

    @Slot(str)
    def copyText(self, text: str) -> None:  # noqa: N802
        QGuiApplication.clipboard().setText(text)
        secret = any(x in text for x in self._secrets)
        self.toast.emit(self.tr("Copied. It contains a secret code: paste it only in OBS.") if secret
                        else self.tr("Copied to the clipboard"), "success")

    @Slot(str)
    def openUrl(self, url: str) -> None:  # noqa: N802
        QDesktopServices.openUrl(QUrl(url))

    @Slot()
    def clearHistory(self) -> None:  # noqa: N802
        self._history = []
        self.engine.history.clear()
        self.historyChanged.emit()

    @Slot(str, result=bool)
    def exportHistory(self, file_url: str) -> bool:  # noqa: N802
        path = Path(QUrl(file_url).toLocalFile() or file_url)
        try:
            path.write_text(json.dumps(list(reversed(self._history)), ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            self.notify(self.tr("Could not save the file: %1").replace("%1", e.strerror or str(e)), "error")
            return False
        self.toast.emit(self.tr("History saved"), "success")
        return True

    # -- profiles ----------------------------------------------------------

    def _profile_names(self) -> list[str]:
        folder = profiles_dir()
        return sorted(p.stem for p in folder.glob("*.json")) if folder.is_dir() else []

    profiles = Property("QVariantList", _profile_names, notify=configChanged)

    @Slot(str, result=bool)
    def saveProfile(self, name: str) -> bool:  # noqa: N802
        safe = "".join(c for c in name if c.isalnum() or c in " -_").strip()[:60]
        if not safe:
            return False
        folder = profiles_dir()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{safe}.json").write_text(json.dumps(self.settings.export_profile(), ensure_ascii=False, indent=2),
                                             encoding="utf-8")
        self.configChanged.emit()
        self.toast.emit(self.tr("Profile saved"), "success")
        return True

    @Slot(str, result=bool)
    def loadProfile(self, name_or_url: str) -> bool:  # noqa: N802
        path = Path(QUrl(name_or_url).toLocalFile()) if "://" in name_or_url else profiles_dir() / f"{name_or_url}.json"
        try:
            imported = self.settings.import_profile(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            self.toast.emit(self.tr("Could not load the profile: %1").replace("%1", str(e)), "error")
            return False
        for field_name in imported.__dataclass_fields__:
            setattr(self.settings, field_name, getattr(imported, field_name))
        self.settings.save()
        self.configChanged.emit()
        self.runtime.submit(self._apply())
        self.toast.emit(self.tr("Profile loaded"), "success")
        return True

    @Slot(str, result=bool)
    def exportProfile(self, file_url: str) -> bool:  # noqa: N802
        path = Path(QUrl(file_url).toLocalFile() or file_url)
        try:
            path.write_text(json.dumps(self.settings.export_profile(), ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            self.notify(self.tr("Could not save the file: %1").replace("%1", e.strerror or str(e)), "error")
            return False
        self.toast.emit(self.tr("Profile exported (without secrets)"), "success")
        return True

