"""Desktop app tests (offscreen, software rendering, no QtWebEngine)."""

import json
import os
import re
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ["CHATTOON_NO_WEBENGINE"] = "1"

from PySide6.QtCore import QCoreApplication, QEventLoop  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from chattoon.core.config import Settings  # noqa: E402
from chattoon.core.engine import ChatToonEngine  # noqa: E402
from chattoon.core.overlay_server import OverlayServer  # noqa: E402

from .test_engine import FakeOllama, FakePlayer, FakeTTS  # noqa: E402

QML = Path(__file__).resolve().parent.parent / "chattoon" / "gui" / "qml"


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def pump(seconds=0.3, until=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        if until and until():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def backend(qapp, tmp_path):
    from chattoon.gui.backend import Backend

    s = Settings(first_run_done=True)
    s.chat.min_interval = 0
    web = tmp_path / "web" / "overlay"
    web.mkdir(parents=True)
    (web / "index.html").write_text("x")
    engine = ChatToonEngine(s, tts=FakeTTS(tmp_path), player=FakePlayer(), ollama=FakeOllama(),
                            server=OverlayServer("t", 0, tmp_path / "web"))

    async def services():
        report = {"ollama": {"ok": True, "version": "test", "models": ["gemma3:4b", "x:1b"]},
                  "tts": {"ok": True, "voices": [engine.tts.voice.__dict__]}, "player": True}
        engine.bus.emit("services", report)
        return report

    engine.check_services = services
    b = Backend(settings=s, engine=engine)
    b.toasts = []
    b.toast.connect(lambda m, k: b.toasts.append((m, k)))
    assert pump(5, lambda: b.runtimeState["overlayUrl"] != "")
    yield b
    b.shutdown()


def test_backend_autoselects_installed_model_and_reports_services(backend):
    assert pump(2, lambda: backend.services["ollama"]["ok"])
    assert backend.config["ai"]["model"] == "gemma3:4b"  # recommended and installed
    assert backend.runtimeState["overlayUrl"].startswith("http://127.0.0.1:")


def test_set_config_paths_types_and_persistence(backend):
    backend.setConfig("ai.temperature", "0.4")
    backend.setConfig("chat.max_queue", 12.7)
    backend.setConfig("context.promo_messages", "Like!\n\n  Inscreva-se!  ")
    backend.setConfig("current.personality.humor", 10)
    backend.setConfig("does.not.exist", 1)
    saved = Settings.load()
    assert saved.ai.temperature == 0.4 and saved.chat.max_queue == 12
    assert saved.context.promo_messages == ["Like!", "Inscreva-se!"]
    p = saved.characters["worm"].personality
    assert p.humor == 10 and p.preset == "custom"  # moving a dial switches to Custom
    backend.applyPreset("geek")
    assert backend.config["current"]["personality"]["preset"] == "geek"


def test_set_config_takes_a_javascript_array(backend):
    # The "What it may say" checkboxes send a JS array: it arrives as a QJSValue, and
    # 3.0.0 saved it as the text "<PySide6.QtQml.QJSValue object …>".
    from PySide6.QtQml import QJSEngine
    js = QJSEngine()
    backend.setConfig("spontaneous.categories", js.evaluate('["humor", "cta"]'))
    assert Settings.load().spontaneous.categories == ["humor", "cta"]


def test_broken_spontaneous_categories_heal_on_load(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"spontaneous": {"categories": ["<PySide6.QtQml.QJSValue object at 0x7f>"]}}))
    assert Settings.load(path).spontaneous.categories == Settings().spontaneous.categories
    path.write_text(json.dumps({"spontaneous": {"categories": ["humor", "bogus"]}}))
    assert Settings.load(path).spontaneous.categories == ["humor"]
    path.write_text(json.dumps({"spontaneous": {"categories": []}}))  # all unchecked on purpose
    assert Settings.load(path).spontaneous.categories == []


def test_select_character(backend):
    backend.selectCharacter("tux")
    assert backend.config["character"] == "tux"
    assert pump(2, lambda: "pinguim" in backend.engine.selector.config.names)


def test_demo_flow_through_backend(backend):
    backend.start()
    assert pump(3, lambda: backend.runtimeState["state"] == "running")
    backend.submitMessage("oi minhoca", "Ana")
    assert pump(8, lambda: len(backend.history) == 1)
    assert backend.history[0]["reply"] == "Resposta para oi minhoca!" and backend.history[0]["read"]
    # new installations read "name + message" first: two utterances, one after the other
    # (the start report also renders a silent "Oi!" to prove the voice works; it is never played)
    assert [t for t in backend.engine.tts.texts if t != "Oi!"] == ["Ana disse: oi minhoca.", "Resposta para oi minhoca!"]
    assert backend.stats["spoken"] == 1 and backend.stats["read"] == 1
    assert len(backend.engine.player.played) == 2 and backend.engine.player.max_active == 1
    backend.emergencyStop()
    assert pump(3, lambda: backend.runtimeState["state"] == "emergency")


def test_profiles_save_load_export_without_token(backend, tmp_path):
    backend.setConfig("context.channel_name", "BigLinux")
    assert backend.saveProfile("Sexta Linux")
    assert "Sexta Linux" in backend.profiles
    backend.setConfig("context.channel_name", "Outro")
    assert backend.loadProfile("Sexta Linux")
    assert backend.config["context"]["channel_name"] == "BigLinux"
    out = tmp_path / "p.json"
    assert backend.exportProfile(str(out))
    data = json.loads(out.read_text())
    assert "token" not in data["settings"]["overlay"]
    assert not backend.loadProfile(str(tmp_path / "missing.json"))
    assert backend.toasts[-1][1] == "error"


def test_main_qml_loads_every_page_without_warnings(backend):
    from chattoon.gui.app import create_engine

    warnings = []
    engine = create_engine(backend, warnings)
    window = engine.rootObjects()[0]
    for page in range(11):
        window.setProperty("currentPage", page)
        pump(0.15)
    backend.setConfig("theme", "dark")
    backend.setConfig("mode", "assisted")
    pump(0.2)
    window.close()
    del engine
    assert warnings == []


def test_first_run_wizard_opens_for_new_users(qapp, backend):
    from chattoon.gui.app import create_engine

    backend.settings.first_run_done = False
    warnings = []
    engine = create_engine(backend, warnings)
    pump(0.3)
    backend.finishFirstRun()
    assert Settings.load().first_run_done
    del engine
    assert warnings == []


def test_icon_provider(qapp):
    from PySide6.QtCore import QSize

    from chattoon.gui.icons import IconProvider

    img = IconProvider().requestImage("emergency/b49cff", QSize(), QSize(24, 24))
    assert (img.width(), img.height()) == (24, 24)
    assert any(img.pixelColor(x, y).alpha() > 200 for x in range(24) for y in range(24))


def test_no_hardcoded_portuguese_in_qml():
    # interface strings are English + qsTr(); Portuguese lives in the translation file
    suspicious = re.compile(r'(?<![\w.])text:\s*"[^"]*[ãõçáéíóúâêô][^"]*"')
    offenders = [f"{p.name}:{i}" for p in QML.rglob("*.qml") for i, line in enumerate(p.read_text().splitlines(), 1)
                 if suspicious.search(line) and "qsTr" not in line]
    assert offenders == []


# -- theme contrast (WCAG AA) ---------------------------------------------

def _tokens(mode):
    out = {}
    for name, dark, light in re.findall(r'property color (\w+): dark \? "(#[0-9a-fA-F]{6})" : "(#[0-9a-fA-F]{6})"',
                                        (QML / "Theme.qml").read_text()):
        out[name] = dark if mode == "dark" else light
    return out


def _lum(h):
    c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _ratio(a, b):
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _mix(fg, bg, alpha):
    f = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
    g = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * alpha + y * (1 - alpha)):02x}" for x, y in zip(f, g))


@pytest.mark.parametrize("mode", ["dark", "light"])
@pytest.mark.parametrize("token", ["text", "textMuted", "textFaint", "accent", "accent2", "success", "warning", "danger"])
def test_theme_text_contrast(mode, token):
    t = _tokens(mode)
    card = "#211e2d" if mode == "dark" else "#fdfcff"
    chip = _mix(t[token], card, 0.15 if mode == "dark" else 0.12)
    places = [("background", t["bg"]), ("sidebar", t["sidebar"]), ("card", card)]
    if token != "textFaint":  # chips never use the faint tone
        places.append(("chip", chip))
    for where, bg in places:
        assert _ratio(t[token], bg) >= 4.5, f"{token} on {where} ({mode}): {_ratio(t[token], bg):.2f}"


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_text_on_accent_buttons_is_readable(mode):
    src = (QML / "Theme.qml").read_text()
    ink = re.search(r'accentText: dark \? "(#\w{6})" : "(#\w{6})"', src).groups()[0 if mode == "dark" else 1]
    t = _tokens(mode)
    for stop in ("accent", "accent2"):  # both ends of the primary gradient
        assert _ratio(ink, t[stop]) >= 4.5, f"button text on {stop} ({mode}): {_ratio(ink, t[stop]):.2f}"


def test_shutdown_right_after_start_is_clean(qapp, tmp_path, capfd):
    from chattoon.gui.backend import Backend

    web = tmp_path / "web" / "overlay"
    web.mkdir(parents=True)
    (web / "index.html").write_text("x")
    engine = ChatToonEngine(Settings(), tts=FakeTTS(tmp_path), player=FakePlayer(), ollama=FakeOllama(),
                            server=OverlayServer("t", 0, tmp_path / "web"))

    async def slow_services():
        import asyncio
        await asyncio.sleep(5)

    engine.check_services = slow_services
    b = Backend(settings=Settings(), engine=engine)
    b.shutdown()  # boot still running
    import gc
    gc.collect()
    assert "Task was destroyed but it is pending" not in capfd.readouterr().err


def test_six_characters_with_gestures_and_species(backend):
    chars = backend.characters
    assert [c["id"] for c in chars] == ["worm", "alien", "tux", "cat", "owl", "dog"]
    for c in chars:
        assert c["species"] and c["tagline"] and c["gestures"], c["id"]
        assert all(g["label"] and g["id"].isalpha() for g in c["gestures"])
    tux = next(c for c in chars if c["id"] == "tux")
    assert "tipHat" in [g["id"] for g in tux["gestures"]]
    thumbs = QML / "assets" / "characters"
    assert all((thumbs / f"{c['id']}.png").exists() for c in chars)


def test_animation_and_reading_settings_persist_and_reach_the_overlay(backend):
    backend.setConfig("animation.intensity", "lively")
    backend.setConfig("animation.reduce_motion", True)
    backend.setConfig("reading.mode", "name")
    backend.setConfig("reading.pause", 1.2)
    saved = Settings.load()
    assert (saved.animation.intensity, saved.animation.reduce_motion) == ("lively", True)
    assert (saved.reading.mode, saved.reading.pause) == ("name", 1.2)
    assert pump(2, lambda: backend.engine.server._snapshot.get("config", {}).get("reduceMotion") is True)
    cfg = backend.engine.server._snapshot["config"]
    assert cfg["intensity"] == "lively" and cfg["idle"] is True
    assert backend.readingSample() == "Pergunta de Maria."
    backend.setConfig("reading.mode", "name_message")
    assert backend.readingSample().startswith("Maria perguntou: ")
    backend.setConfig("reading.mode", "off")
    assert backend.readingSample() == ""


def test_engine_errors_are_shown_in_the_interface_language(backend):
    backend.engine.bus.emit("error", {"source": "tts", "message": "Kokoro took too long", "code": "kokoro-timeout"})
    backend.engine.bus.emit("error", {"source": "tts", "message": "x", "code": "kokoro-unavailable",
                                      "detail": "Voice not installed"})
    backend.engine.bus.emit("error", {"source": "chat", "message": "Something else"})
    assert pump(2, lambda: len(backend.toasts) >= 3)
    texts = [m for m, _ in backend.toasts[-3:]]
    assert texts[0].startswith("Kokoro took too long to speak this text")
    assert texts[1] == "Kokoro: Voice not installed" and texts[2] == "Something else"


def test_gesture_slot_broadcasts_only_safe_names(backend):
    sent = []
    original = backend.engine.server.broadcast

    async def spy(event, keep=False):
        sent.append(event)
        await original(event, keep)

    backend.engine.server.broadcast = spy
    backend.gesture("tipHat")
    backend.gesture("<script>")
    assert pump(2, lambda: any(e.get("type") == "gesture" for e in sent))
    pump(0.3)
    assert [e["name"] for e in sent if e.get("type") == "gesture"] == ["tipHat"]


def test_character_cards_select_on_click_but_not_from_the_expand_button(backend):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest

    from chattoon.gui.app import create_engine

    warnings = []
    engine = create_engine(backend, warnings)
    window = engine.rootObjects()[0]
    window.setProperty("currentPage", 1)
    pump(0.5)

    def find(item, name):
        if item.objectName() == name:
            return item
        for child in item.childItems():
            found = find(child, name)
            if found is not None:
                return found
        return None

    def click(name):
        click_in(window.contentItem(), name)  # Loader/Repeater items live in the visual tree

    def click_in(root, name):
        item = find(root, name)
        assert item is not None, name
        pos = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
        pump(0.4)

    assert backend.config["character"] == "worm"
    click("expand-owl")
    assert backend.config["character"] == "worm"  # expanding only opens the enlarged view
    assert find(window.contentItem(), "zoom-close") is not None  # the enlarged view is open
    click("zoom-close")
    click("card-owl")
    assert backend.config["character"] == "owl"
    window.close()
    del engine
    assert warnings == []
