"""Persistent settings (XDG), per-character settings and stream profiles."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import types
import typing
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path

from .knowledge import CATEGORIES

APP_ID = "chattoon"
# Version of the settings file format. Older files are migrated by Settings.load(); the
# original is copied to settings.json.v<old>.bak once before anything is overwritten.
CONFIG_VERSION = 3


def _xdg(var: str, fallback: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / fallback) / APP_ID


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config")


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share")


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache")


@dataclass
class Personality:
    preset: str = "comedian"  # friendly | comedian | troll | geek | chaotic | custom
    humor: int = 75  # 0..100
    sarcasm: int = 45
    irony: int = 45
    creativity: int = 70
    energy: int = 65
    formality: int = 15
    memes: int = 40
    geek: int = 40
    provocation: int = 35
    length: str = "short"  # short | medium
    custom_prompt: str = ""  # extra instructions written by the streamer


@dataclass
class CharacterSettings:
    personality: Personality = field(default_factory=Personality)
    voice_id: str = ""  # "" = default voice suggested for the character
    voice_rate: int = 0  # -100..100 (TTS BigLinux scale)
    voice_pitch: int = 0
    catchphrases: list[str] = field(default_factory=list)
    color: str = ""  # "" = character default
    curiosities: list[str] = field(default_factory=list)  # the streamer's own lines for spontaneous talk


@dataclass
class ChannelContext:
    channel_name: str = ""
    host_name: str = ""
    topic: str = ""
    description: str = ""
    allowed_topics: str = ""
    forbidden_topics: str = "política partidária, religião, conteúdo sexual"
    guests: str = ""
    links: str = ""
    catchphrases: str = ""
    promo_enabled: bool = True
    promo_messages: list[str] = field(default_factory=lambda: [
        "Deixa o like!", "Compartilha a live!", "Se inscreve no canal!",
    ])
    promo_every: int = 6  # at most one promo every N replies
    promo_cooldown: int = 300  # and never twice within this many seconds


@dataclass
class AISettings:
    host: str = "http://127.0.0.1:11434"
    model: str = ""
    temperature: float = 0.9
    max_tokens: int = 160
    context_window: int = 4096
    timeout: int = 60
    keep_alive: str = "15m"
    max_sentences: int = 3
    max_chars: int = 240
    reply_language: str = "pt-BR"
    memory_size: int = 4  # recent exchanges of other viewers sent as context (each costs prompt-eval time)
    adaptive: bool = True  # objective questions get little randomness, banter keeps "temperature"
    math_tool: bool = True  # arithmetic is computed exactly and handed to the model
    validate: bool = True  # one more try when a reply misses the verified result or copies the prompt
    reference: bool = True  # objective tech questions get entries of the verified reference sheet


@dataclass
class MemorySettings:
    """What the avatar remembers about viewers (docs/INTELLIGENCE.md)."""

    enabled: bool = True  # follow-ups within the live (RAM only)
    persist: bool = False  # also remember between lives (local SQLite file, never sent anywhere)
    retention_days: int = 30  # saved memories older than this are erased (0 = until erased by hand)
    summaries: bool = True  # while the chat is quiet, the model summarizes long conversations


@dataclass
class SpontaneousSettings:
    """Lines the avatar says on its own when the chat is quiet (docs/INTELLIGENCE.md)."""

    enabled: bool = False
    min_idle: int = 90  # seconds without chat messages before it may speak
    min_interval: int = 240  # seconds between two spontaneous lines
    frequency: str = "normal"  # low | normal | high (scales both times)
    intensity: str = "normal"  # calm | normal | lively (emotion strength and gestures)
    categories: list[str] = field(default_factory=lambda: ["invite", "curiosity", "context", "humor", "reflection",
                                                           "catchphrase"])
    use_ai: bool = True  # vary invitations and asides with the model (curiosities are always spoken as written)
    quiet_start: str = ""  # "23:00": no spontaneous lines from this time…
    quiet_end: str = ""  # …until this one ("" = no quiet hours)


@dataclass
class VoiceSettings:
    enabled: bool = True
    volume: int = 85  # 0..100
    audio_output: str = "desktop"  # desktop (app plays) | overlay (OBS browser source plays)


@dataclass
class ReadingSettings:
    """Read the viewer's name (and message) aloud before the reply."""

    mode: str = "name_message"  # off | name | name_message
    # say where the message came from: never | multi (only while 2+ platforms are live) | always
    platform: str = "multi"
    max_chars: int = 180  # longer messages are cut at a word boundary (never rewritten)
    pause: float = 0.5  # seconds of silence between the reading and the reply
    voice_id: str = ""  # "" = the character's own voice


@dataclass
class AnimationSettings:
    """Idle life of the avatar while nobody is talking to it."""

    idle: bool = True
    intensity: str = "normal"  # subtle | normal | lively
    frequency: str = "normal"  # low | normal | high
    reduce_motion: bool = False  # no jumps, spins or fast gestures


@dataclass
class ChatSettings:
    url: str = ""  # YouTube URL of ChatToon 2.x; migrated to a stream source (kept for older versions)
    # smart | balanced | weighted | all | mentions | questions | moderators | members | random | manual
    selection: str = "smart"
    user_cooldown: int = 60  # seconds before answering the same person again
    max_queue: int = 40
    message_ttl: int = 120  # older messages are dropped
    min_interval: int = 4  # seconds of silence between replies
    max_message_chars: int = 300
    skip_backlog: bool = True  # ignore messages that existed before connecting
    ignore_commands: bool = True  # "!cmd" messages belong to other bots
    blocked_words: str = ""  # comma separated, messages containing them are dropped
    superchat_priority: bool = True  # paid messages of any platform (Super Chat, Bits, gifts…) go first
    fairness: bool = True  # share replies between platforms instead of answering only the busiest chat
    include_events: bool = False  # gifts, follows and subscriptions without text may be answered too
    priority_viewers: str = ""  # comma separated names ("ana" or "twitch:ana") answered first


# option names that could carry credentials never enter settings files or profiles
SECRET_KEY = re.compile(r"token|secret|password|passwd|cookie|session|api.?key|bearer|verifier|auth.?code", re.I)
PLATFORMS = ("youtube", "twitch", "kick", "tiktok", "facebook", "instagram", "x")


@dataclass
class SourceConfig:
    """One chat being read: a platform plus the channel, URL or live it points to."""

    id: str = ""
    platform: str = "youtube"
    target: str = ""  # URL, channel name, @handle or id typed by the streamer
    label: str = ""  # optional display name
    enabled: bool = True
    priority: int = 1  # 1..5: share of attention in the "weighted" selection mode
    options: dict[str, str] = field(default_factory=dict)  # platform specific, never secrets

    def __post_init__(self) -> None:
        if self.platform not in PLATFORMS:
            self.platform = "youtube"
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", self.id or ""):
            self.id = f"{self.platform}-{secrets.token_hex(3)}"
        self.priority = min(5, max(1, int(self.priority)))
        self.options = {str(k): str(v) for k, v in self.options.items() if not SECRET_KEY.search(str(k))}


@dataclass
class PlatformApps:
    """Public ids of developer apps used for official logins. Secrets (client secrets,
    tokens) live in the system keyring, never here."""

    twitch_client_id: str = ""
    kick_client_id: str = ""
    kick_webhook_port: int = 8790  # local receiver for Kick webhooks (only when enabled)


@dataclass
class StreamSettings:
    sources: list[SourceConfig] = field(default_factory=list)
    apps: PlatformApps = field(default_factory=PlatformApps)
    migrated: bool = False  # True once ChatToon 2.x's single YouTube URL became a source


@dataclass
class OverlaySettings:
    port: int = 8765
    token: str = ""
    # HTTPS address of a reverse proxy on this computer that forwards to the overlay
    # (e.g. "https://example.org:8443"); "" keeps the overlay reachable only locally
    public_address: str = ""
    show_bubble: bool = True
    show_author: bool = True
    show_platform: bool = True  # small "Twitch • Ana" tag on the viewer's message
    scale: float = 1.0
    position_x: float = 0.0  # -1..1 (left..right)
    position_y: float = -0.15  # -1..1 (bottom..top)
    rotation: float = 0.0  # degrees around Y
    camera_zoom: float = 1.0
    light: float = 1.0
    shadows: bool = True
    quality: str = "high"  # low | medium | high
    fps: int = 60
    bubble_seconds: float = 2.5  # bubble stays this long after speech ends
    background: str = "transparent"  # transparent | green (chroma key)
    # Local network (docs/ARCHITECTURE.md): off by default. When on, the overlay also
    # listens on this one IPv4 address of this computer (never 0.0.0.0) and only answers
    # computers of that network.
    lan_enabled: bool = False
    lan_address: str = ""

    def __post_init__(self) -> None:
        if not self.token:
            self.token = secrets.token_urlsafe(12)


@dataclass
class DisplaySettings:
    """Where the character appears (docs/ARCHITECTURE.md)."""

    mode: str = "obs"  # obs | window | both
    window_open: bool = False  # the desktop window was open when ChatToon closed
    size: str = "medium"  # small | medium | large | custom
    width: int = 440
    height: int = 660
    x: int = -1  # -1 = let ChatToon/the compositor place it
    y: int = -1
    screen: str = ""  # output name the window was on
    keep_above: bool = True
    click_through: bool = False
    locked: bool = False
    skip_taskbar: bool = False
    show_bubble: bool = True


@dataclass
class Settings:
    config_version: int = CONFIG_VERSION
    character: str = "worm"  # worm | alien | tux | cat | owl | dog
    mode: str = "demo"  # demo | assisted | auto
    ui_language: str = "system"  # system | en | pt_BR
    theme: str = "system"  # system | dark | light
    first_run_done: bool = False
    advanced: bool = False  # show the technical settings (network, inference, diagnostics…)
    # Live privacy mode: hide addresses, IPs and account names everywhere in the interface
    # (tokens and the public IP are always masked; this hides the rest too)
    privacy_mode: bool = True
    ai: AISettings = field(default_factory=AISettings)
    voice: VoiceSettings = field(default_factory=VoiceSettings)
    chat: ChatSettings = field(default_factory=ChatSettings)
    streams: StreamSettings = field(default_factory=StreamSettings)
    overlay: OverlaySettings = field(default_factory=OverlaySettings)
    display: DisplaySettings = field(default_factory=DisplaySettings)
    reading: ReadingSettings = field(default_factory=ReadingSettings)
    animation: AnimationSettings = field(default_factory=AnimationSettings)
    context: ChannelContext = field(default_factory=ChannelContext)
    memory: MemorySettings = field(default_factory=MemorySettings)
    spontaneous: SpontaneousSettings = field(default_factory=SpontaneousSettings)
    characters: dict[str, CharacterSettings] = field(default_factory=dict)

    def character_settings(self, character_id: str | None = None) -> CharacterSettings:
        from .characters import CHARACTERS  # local import: avoids a cycle

        cid = character_id or self.character
        if cid not in self.characters:
            base = CHARACTERS.get(cid)
            self.characters[cid] = base.default_settings(self.ai.reply_language) if base else CharacterSettings()
        return self.characters[cid]

    def stream_sources(self) -> list[SourceConfig]:
        """Configured sources; ChatToon 2.x settings (one YouTube URL) still work unmigrated."""
        if self.streams.sources or self.streams.migrated:
            return self.streams.sources
        url = self.chat.url.strip()
        return [SourceConfig(id="youtube-main", platform="youtube", target=url)] if url else []

    # -- persistence -----------------------------------------------------

    @classmethod
    def file(cls) -> Path:
        return config_dir() / "settings.json"

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        path = path or cls.file()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        settings = from_dict(cls, data)
        old = data.get("config_version", 2) if isinstance(data, dict) else CONFIG_VERSION
        if isinstance(old, int) and old < CONFIG_VERSION:
            backup = path.with_name(f"{path.name}.v{old}.bak")
            if not backup.exists():  # keep the 2.x file as it was, once
                try:
                    shutil.copy2(path, backup)
                    os.chmod(backup, 0o600)
                except OSError:
                    pass
            settings.config_version = CONFIG_VERSION
        if isinstance(data, dict) and "reading" not in data:
            # Saved before message reading existed: keep answering directly, as the
            # streamer is used to. New installations read "name + message" by default.
            settings.reading.mode = "off"
        if isinstance(data, dict) and "streams" not in data:
            # ChatToon 2.x read one YouTube chat: it becomes the first source, nothing to redo.
            settings.streams.sources = settings.stream_sources()
            settings.streams.migrated = True
        sp = settings.spontaneous
        valid = [c for c in sp.categories if c in CATEGORIES]
        if valid != sp.categories:
            # 3.0.0 saved the checkboxes as "<PySide6.QtQml.QJSValue object …>": back to the defaults
            sp.categories = valid or SpontaneousSettings().categories
        return settings

    def save(self, path: Path | None = None) -> None:
        path = path or self.file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)

    # -- profiles (export/import without secrets) -------------------------

    def export_profile(self) -> dict:
        data = asdict(self)
        data["overlay"].pop("token", None)  # the overlay token is a local secret
        for local in ("lan_enabled", "lan_address"):  # this computer's network, not the stream's
            data["overlay"].pop(local, None)
        data.pop("first_run_done", None)
        data.pop("config_version", None)
        for source in data["streams"]["sources"]:  # belt and braces: options never hold secrets
            source["options"] = {k: v for k, v in source["options"].items() if not SECRET_KEY.search(k)}
        return {"chattoon_profile": 1, "settings": data}

    def import_profile(self, profile: dict) -> Settings:
        if not isinstance(profile, dict) or profile.get("chattoon_profile") != 1:
            raise ValueError("not a ChatToon profile")
        data = dict(profile.get("settings") or {})
        overlay = data.setdefault("overlay", {})
        if not isinstance(overlay, dict):
            overlay = data["overlay"] = {}
        overlay["token"] = self.overlay.token
        overlay["lan_enabled"], overlay["lan_address"] = self.overlay.lan_enabled, self.overlay.lan_address
        data["first_run_done"] = self.first_run_done
        return from_dict(Settings, data)


def profiles_dir() -> Path:
    return config_dir() / "profiles"


# -- tolerant (de)serialization ------------------------------------------------

def from_dict(cls, data):
    """Build dataclass ``cls`` from ``data`` ignoring unknown keys and bad types."""
    if not isinstance(data, dict):
        return cls()
    hints = typing.get_type_hints(cls)
    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value = _coerce(hints[f.name], data[f.name])
        if value is not _INVALID:
            kwargs[f.name] = value
    return cls(**kwargs)


_INVALID = object()


def _coerce(tp, value):
    origin = typing.get_origin(tp)
    if is_dataclass(tp):
        return from_dict(tp, value) if isinstance(value, dict) else _INVALID
    if origin is dict:
        _, vt = typing.get_args(tp)
        if not isinstance(value, dict):
            return _INVALID
        out = {}
        for k, v in value.items():
            cv = _coerce(vt, v)
            if cv is not _INVALID:
                out[str(k)] = cv
        return out
    if origin is list:
        (it,) = typing.get_args(tp)
        if not isinstance(value, list):
            return _INVALID
        return [c for c in (_coerce(it, v) for v in value) if c is not _INVALID]
    if origin in (typing.Union, types.UnionType):
        return value
    if tp is bool:
        return value if isinstance(value, bool) else _INVALID
    if tp in (int, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return _INVALID
        return tp(value)
    if tp is str:
        return value if isinstance(value, str) else _INVALID
    return value
