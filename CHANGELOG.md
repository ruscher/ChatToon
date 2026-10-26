# Changelog

## 3.0.1 — 2026-10-25

**Fixes**
- TTS BigLinux was reported as not installed when another Python package put a top-level
  `utils.py` on the path (hermes-agent does, with pip --user): its `utils/` and `services/`
  folders are now always loaded from the TTS BigLinux directory.
- Settings › Spontaneous lines › What it may say: the checkboxes were saved as
  "<PySide6.QtQml.QJSValue object …>", so none stayed checked and no spontaneous line was
  spoken. Broken saved values go back to the default categories.

## 3.0.0 — 2026-10-17

**Where the character appears**
- New: the character in a transparent window on the desktop (KDE Plasma Wayland and X11), with
  drag, resize, keep above, lock, clicks-through, screens, a remembered position, the character's
  icon and its own right-click panel. OBS, the window, or both, always in sync and with one voice.
- OBS on another computer: a real local-network mode (opt-in, one private address, same subnet
  only, firewall rule on request) and a 5-step internet assistant that tests DNS, port,
  certificate, proxy, token and WebSocket. New secret address on demand.

**AI and voice without a terminal**
- Ollama: real state (not installed, stopped, starting, running, error, remote server), install
  from the official repositories with recommended GPU acceleration, start/stop/restart, start
  with the computer, other Ollama instances found on this computer.
- Every installed model is listed with its capabilities; "in use", "installed" and "loaded in
  memory" are separate; models that cannot chat say why; a chosen model is never replaced
  silently. Suggestions with real download sizes, progress, speed and cancel.
- Voice engines (Piper, Kokoro, RHVoice, espeak-ng) with their real state; install engines and
  voices through TTS BigLinux; Kokoro voice downloads; restore voice defaults.
- Long Kokoro texts are rendered in pieces (koko crashed past ~510 tokens).

**Starting a live**
- Start now checks what is ready, starts only when nothing essential is missing, proves that the
  model answers and the voice speaks, and shows what the character will do. System health and
  the last report are on the Dashboard.

**Privacy and safety**
- Live privacy mode (on by default): addresses, IPs, domains and logins hidden on every page;
  secret codes and the public IP always hidden; reveal for 15 s after a warning.
- The public IP is looked up only with consent. The overlay address never reaches the logs.

**Interface**
- Simple and advanced modes, a 6-step first-run wizard, restore defaults per section, a scrolling
  side menu for small windows, reduce motion also for the interface, clearer errors for Ollama and
  the voice (translated by code), "personagem" everywhere in Portuguese.
- Kick: the unofficial public socket was retired by Kick (verified); the app says so and points to
  the official webhook mode.

**Performance** (same profile, against 2.3.0): first frame 1.96 → 1.72 s, memory 523 → 482 MB,
idle CPU with the 3D preview 18 → 9.5 %. Pages load on first visit; the in-app preview runs at
30 fps.

**Project**
- Settings format version 3; a 2.x settings file is copied once to `settings.json.v2.bak`.
- GPL-3.0 licence text, AppStream metainfo, explicit dependencies in the PKGBUILD, new
  screenshots, documentation consolidated in `docs/`. The historical prototypes left the tree
  (they remain in the Git history, commit `fdb7a4f`).

## 2.3.0 — 2026-10-15
Correct answers before humour (local classification, exact calculator, verified reference sheet
and a check of every reply: 29 % → 90 % with `qwen3.5:2b`), per-viewer memory with optional
persistence, emotions that fit the conversation, spontaneous lines, stream context filled from
the platforms, generation diagnostics, and a fix for Assisted mode stalling after a rejection.

## 2.2.2
The character never speaks or shows pieces of the reply's JSON again.

## 2.2.1
Overlay reachable from another computer through an HTTPS reverse proxy; only OBS sources receive
the voice, so opening the link in a browser no longer takes it from OBS.

## 2.2.0
Multistream: YouTube, Twitch, Kick, TikTok, Facebook, Instagram and X at the same time, a unified
live chat, fair queue across platforms, the platform said in the reading and shown in the bubble,
logins in the system keyring, automatic migration from 2.1.

## 2.1.0
Reading the viewer's name and message before the reply, Kokoro voices, Tux with a top hat, the
Cat, the Owl and the Dog, idle animation with states and gestures, a new Characters page.

## 2.0.0
The ChatToon application: PySide6/QML desktop interface, three 3D characters, local AI with
Ollama, TTS BigLinux voice, a secure OBS overlay, Portuguese translation and an Arch/BigLinux
package.

## 1.x
Pedro Bezerra's original prototype (InfoRecarga): Python and Node.js scripts with the 3D
snake/caterpillar.
