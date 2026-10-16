"""Nanobot AI agent + voice channel, packaged as a reCamera App Center app.

kit.run imports this module and runs the single App subclass. The wrapper's
job is layout + lifecycle; nanobot and nanobot-channel-voice stay unmodified.

Layout (all under the app data dir, survives upgrades):
  <appdata>/nanobot/.nanobot/config.json   effective config, regenerated at start
  <appdata>/nanobot/nanobot.config.json    user overlay — edit freely (README.md)
  <appdata>/nanobot/webui_password.txt     generated LAN password when the
                                           webui_password knob is empty

Merge order: BASE_CONFIG <- existing config.json (WebUI state) <- user
overlay <- Configure-dialog knobs.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from kit.app import App, run_app
from kit.config import appdata_root

class _GateHandler(BaseHTTPRequestHandler):
    """Readiness gate on GATE_PORT (what App Center's Open UI links to).

    The gateway takes tens of seconds to boot (interpreter, onnxruntime,
    channel init); without this shim the launch redirect lands on a closed
    port and the browser shows ERR_EMPTY_RESPONSE. While 127.0.0.1:WEBUI_PORT
    refuses TCP, answer a self-refreshing 'starting' page; once it accepts,
    302 to it. The browser carries the launch URL's fragment (the bootstrap
    secret) across the redirect, so auto-login survives."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        try:
            with socket.create_connection(("127.0.0.1", WEBUI_PORT),
                                          timeout=2):
                pass
        except OSError:
            self._interstitial()
            return
        # Host may carry a port or brackets (IPv6); only the name matters.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0] or "127.0.0.1"
        scheme, port = "http", WEBUI_PORT
        try:  # per-request probe: self-heals if nginx/reload happens later
            with socket.create_connection(("127.0.0.1", RELAY_PORT),
                                          timeout=1):
                scheme, port = "https", RELAY_PORT
        except OSError:
            pass  # relay absent (old firmware, nginx down) -> plain HTTP
        self.send_response(302)
        self.send_header("Location", f"{scheme}://{host}:{port}/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _interstitial(self) -> None:
        page = (
            "<!doctype html><meta charset=utf-8>"
            "<meta http-equiv=refresh content=3>"
            "<title>nanobot is starting</title>"
            "<style>body{font-family:system-ui;display:grid;place-items:center;"
            "min-height:100vh;margin:0;background:#f7f8f5;color:#333}"
            "main{text-align:center}.spin{width:36px;height:36px;margin:0 auto "
            "16px;border:4px solid #d9e2c8;border-top-color:#8ab530;"
            "border-radius:50%;animation:s 1s linear infinite}"
            "@keyframes s{to{transform:rotate(360deg)}}</style>"
            "<main><div class=spin></div><h3>nanobot is starting</h3>"
            "<p>This page reloads automatically.</p></main>")
        body = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # keep the app log clean
        pass


APP_ID = "nanobot"
WEBUI_PORT = 18765  # the gateway's websocket channel serves here
GATE_PORT = 18766   # manifest.webui.port: the readiness gate in front of it
# nginx TLS relay for the WebUI (secure context for transcribe): the firmware
# serves https on WEBUI_PORT + 1000 (NGINX_APP_TLS_RELAY_PLAN.md). Probe it
# per request so a later nginx reload needs no app restart.
RELAY_PORT = WEBUI_PORT + 1000
OVERLAY_NAME = "nanobot.config.json"
PASSWORD_FILE = "webui_password.txt"

# Audio: the recamera_vqe chain ships as voice_defaults.json (the operator
# baseline beneath config.json, via NANOBOT_VOICE_DEFAULTS): ai_asr_vqe
# carries hardware AEC + beamforming, and the acodec loops speaker playback
# back as the AEC reference channels. Do not point these at plughw
# (exclusive).
BASE_CONFIG = {
    "channels": {
        "websocket": {"enabled": True, "host": "0.0.0.0"},
        # First-boot default only: once config.json exists, its value (the
        # WebUI channel toggle) wins — the panel is the single on/off entry.
        "voice": {"enabled": True},
    },
}

# Voice keys this wrapper owned before the defaults moved to
# NANOBOT_VOICE_DEFAULTS. Dropped from an existing config.json only while
# they still carry exactly the values we wrote — anything the user edited
# survives, and WebUI-panel state is never touched.
_LEGACY_VOICE = {
    "aec": "hardware",
    "audio": {
        "captureDevice": "plug:ai_asr_vqe",
        "playbackDevice": "plug:default",
        "sampleRate": 16000,
    },
    "stt": {"provider": "nanobot"},
    "tts": {
        "provider": "openai",
        "model": "gpt-4o-mini-tts",
        "voice": "alloy",
        "audioFormat": "wav",
    },
}
_LEGACY_TRANSCRIPTION = {"enabled": True, "provider": "groq"}


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _child_env(data: Path, proxy: str, app_dir: Path) -> dict:
    env = os.environ.copy()
    env["HOME"] = str(data)  # config/workspace/sessions/weights store
    # The voice channel's operator baseline: schema < this file < config.json.
    env["NANOBOT_VOICE_DEFAULTS"] = str(app_dir / "voice_defaults.json")
    # appmgr launches us under the per-release venv python; pin the child
    # PATH to its bin dir so a literal "python3" resolves to the SAME
    # interpreter (which owns the bundled wheels).
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + \
        env.get("PATH", "/usr/bin:/bin")
    # appmgr exports PYTHONPATH (kit + system site-packages) for SDK apps;
    # inherited by a venv child it would shadow bundled wheels with system
    # copies (old typing_extensions.pyc broke pydantic). nanobot needs neither
    # kit nor the SDK: the venv itself orders its site-packages correctly.
    env.pop("PYTHONPATH", None)
    if proxy:
        env.setdefault("HTTPS_PROXY", proxy)
        env.setdefault("HTTP_PROXY", proxy)
    return env


def _strip_legacy_voice(existing: dict) -> dict:
    out = dict(existing)
    for key, legacy in _LEGACY_VOICE.items():
        if out.get(key) == legacy:
            del out[key]
    return out


class NanobotApp(App):
    owns_loop = True
    # Streaming-audio contract (kit-app-patterns): we drive rknnlite directly
    # from the voice channel and never touch kit's model factory or frame
    # pipeline; the npu.rknn exclusive claim in the manifest is the lease.
    needs_model = False
    needs_frames = False

    def setup(self, config):
        super().setup(config)
        data = Path(appdata_root()) / APP_ID
        data.mkdir(parents=True, exist_ok=True)
        (data / ".nanobot").mkdir(exist_ok=True)
        self.data = data
        self.config_path = data / ".nanobot" / "config.json"
        self.overlay_path = data / OVERLAY_NAME
        self._webui_port = WEBUI_PORT
        self._proxy = ""
        self._password = ""
        self._child: subprocess.Popen | None = None

    def _knobs(self) -> dict:
        # kit auto-binds config_schema values as attributes.
        return {
            "webui_password": getattr(self, "webui_password", "") or "",
            "download_proxy": getattr(self, "download_proxy", "") or "",
            "webui_allow_remote_package_install": bool(getattr(
                self, "webui_allow_remote_package_install", True)),
            "debug_logs": bool(getattr(self, "debug_logs", False)),
        }

    def _load_existing(self) -> dict:
        """The live config.json: WebUI-persisted state (providers, models,
        the voice panel) rides above BASE and beneath the operator overlay,
        so an app restart never discards it."""
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except ValueError:
            return {}  # unreadable: rebuild from base, never crash-loop
        if not isinstance(data, dict):
            return {}
        return data

    def _load_overlay(self) -> dict:
        if not self.overlay_path.exists():
            return {}
        try:
            overlay = json.loads(self.overlay_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Loud on purpose: a broken overlay must not silently revert to
            # defaults while the user believes their settings applied.
            raise RuntimeError(
                f"invalid JSON in {self.overlay_path}: {exc}") from exc
        if not isinstance(overlay, dict):
            raise RuntimeError(
                f"{self.overlay_path} must contain a JSON object")
        return overlay

    def _webui_password(self, knob: str) -> tuple[str, bool]:
        store = self.data / PASSWORD_FILE
        if knob.strip():
            # Mirror the effective password: appmgr's webui launch endpoint
            # reads this file to attach the credential to the Open UI
            # redirect, whichever source the password came from.
            store.write_text(knob.strip() + "\n", encoding="utf-8")
            store.chmod(0o600)
            return knob.strip(), False
        try:
            existing = store.read_text(encoding="utf-8").strip()
        except OSError:
            existing = ""
        if existing:
            return existing, True
        generated = secrets.token_urlsafe(12)
        store.write_text(generated + "\n", encoding="utf-8")
        store.chmod(0o600)
        return generated, True

    def _build_effective_config(self) -> dict:
        knobs = self._knobs()
        self._webui_port = WEBUI_PORT  # manifest-declared; the card link reads it
        self._proxy = knobs["download_proxy"].strip()
        cfg = _deep_merge(BASE_CONFIG, self._load_existing())
        cfg = _deep_merge(cfg, self._load_overlay())
        # Transcription moved out of the shipped baseline with the voice
        # defaults; drop it only while it still carries our old values.
        if cfg.get("transcription") == _LEGACY_TRANSCRIPTION:
            del cfg["transcription"]
        ws = cfg.setdefault("channels", {}).setdefault("websocket", {})
        ws["enabled"] = True
        ws["host"] = "0.0.0.0"
        ws["port"] = self._webui_port
        password, generated = self._webui_password(knobs["webui_password"])
        self._password = password
        ws["tokenIssueSecret"] = password
        ws["websocketRequiresToken"] = True
        # Wrapper-managed gate: the WebUI has no editor for this flag and
        # every App Center session counts as remote, so without it all
        # plugin/optional-feature installs answer 403.
        cfg.setdefault("tools", {})["webuiAllowRemotePackageInstall"] = \
            knobs["webui_allow_remote_package_install"]
        if generated:
            print(f"[nanobot-app] Web UI password generated; see the app logs "
                  f"or {PASSWORD_FILE} in the app data dir", flush=True)
        voice = _strip_legacy_voice(cfg["channels"].get("voice") or {})
        cfg["channels"]["voice"] = voice
        self.config_path.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print(f"[nanobot-app] effective config written to {self.config_path}",
              flush=True)
        self._provision_skills(cfg)
        return cfg

    def _provision_skills(self, cfg: dict) -> None:
        """Sync bundled agent skills into the workspace (idempotent).

        nanobot discovers workspace skills at <workspace>/skills/<name>/.
        Replace each destination outright so upgrades also remove files the
        bundled skill dropped. `~` in agents.defaults.workspace anchors to
        the app data dir, exactly like the gateway child (HOME is pinned
        there)."""
        src_root = Path(__file__).resolve().parent / "skills"
        # kit/run.py imports this entry BY PATH from the app dir, so __file__
        # is the sanctioned way to reach bundled resources here (the kit puts
        # no app-dir env var in place).
        if not src_root.is_dir():
            return
        raw = str(cfg.get("agents", {}).get("defaults", {})
                  .get("workspace") or "").strip()
        if raw.startswith("~"):
            workspace = Path(str(self.data) + raw[1:])
        elif raw:
            workspace = Path(raw)
        else:
            workspace = self.data / ".nanobot" / "workspace"
        for src in sorted(src_root.iterdir()):
            if not (src / "SKILL.md").exists():
                continue
            dest = workspace / "skills" / src.name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
            print(f"[nanobot-app] skill installed: {src.name} -> {dest}",
                  flush=True)

    @staticmethod
    def _app_dir() -> Path:
        return Path(__file__).resolve().parent  # see the kit contract note above

    def _start_gate(self) -> None:
        try:
            server = ThreadingHTTPServer(("0.0.0.0", GATE_PORT), _GateHandler)
        except OSError as exc:
            # The gate is a convenience layer; a stale holder of the port
            # must not take the whole app down with it. Open UI then faces
            # a closed gate port during boot — the pre-gate behavior.
            print(f"[nanobot-app] readiness gate unavailable ({exc})",
                  flush=True)
            return
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def run(self):
        self._build_effective_config()
        env = _child_env(self.data, self._proxy, self._app_dir())
        print(f"[nanobot-app] starting nanobot gateway "
              f"(webui http://<device-ip>:{self._webui_port}/)", flush=True)
        self._start_gate()  # answers Open UI while the gateway boots
        # Same process group: appmgr stop signals reach the gateway directly.
        # The voice channel fetches the models its setup names by itself
        # (channels.voice.models.autoFetch), in the background, and reloads
        # once they land — nothing for the wrapper to orchestrate.
        # The argv list stays a literal at the call site: the package
        # validator's skill safety policy rejects computed executables.
        if self._knobs()["debug_logs"]:
            self._child = subprocess.Popen(
                ["python3", "-m", "nanobot", "gateway", "--foreground",
                 "--verbose", "--config", str(self.config_path)],
                env=env, cwd=str(self.data))
        else:
            self._child = subprocess.Popen(
                ["python3", "-m", "nanobot", "gateway", "--foreground",
                 "--config", str(self.config_path)],
                env=env, cwd=str(self.data))
        rc = self._child.wait()
        if rc:
            raise SystemExit(rc)


APP = NanobotApp()

if __name__ == "__main__":
    run_app(NanobotApp())
