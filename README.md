# Nanobot App for reCamera App Center

Personal AI agent ([nanobot](https://github.com/HKUDS/nanobot), iChizer0 fork
0.3.5) with its WebUI and the optional
[nanobot-channel-voice](https://github.com/iChizer0/nanobot-channel-voice)
voice channel, packaged as a single reCamera App Center v2 application and
running on the device's VQE audio chain (hardware AEC + beamforming).

This repository is both the **app source tree** (this directory packs
directly into the App Center artifact) and the **build workspace** for the
heavy dependencies (all Python deps ship as device-ready aarch64 wheels).

## Use

1. Install the package in App Center, start the app.
2. Open the Web UI: while running, the card's primary action opens it
   **already authenticated** (appmgr attaches the current password to the
   redirect; the page then scrubs it from the address bar). Direct visits to
   `http://<device-ip>:18765/` ask for the password: the `webui_password`
   knob, or — when empty — the generated one in the app logs /
   `webui_password.txt`.
3. First run: finish **Settings → Models** in the Web UI (pick a provider,
   paste the API key, choose a model). Nothing LLM-side works before this.
4. Voice is enabled by default with on-device engines; no keys needed.

## Bundled agent skill: recamera-local

The package ships the `recamera-local` agent skill (from
[reCamera-Intellisense](https://github.com/iChizer0/reCamera-Intellisense))
and installs it into the agent workspace on every start
(`<workspace>/skills/recamera-local/`). It teaches the agent to operate the
camera it runs on: snapshots, AI detection rules and realtime event waits,
recording triggers, clips/files, storage, ISP, GPIO, video encode, apps, and
result push. Everything is loopback — the skill reads the device API token
itself, zero configuration. Ask the agent things like "what do you see right
now" or "alert me when a person appears"; invoke it explicitly with
`$recamera-local`.

## Configuration

Two layers, merged at every start
(`BASE defaults < overlay < Configure-dialog knobs`):

- **Configure dialog** (App Center → Configure): `webui_password` and
  `webui_allow_remote_package_install` (Web UI — on by default; App Center
  sessions are never localhost, so turning it off blocks all plugin and
  optional-feature installs with a 403), and `download_proxy` (Network —
  weight downloads and outbound LLM calls). Both apply on restart. The Web
  UI port is fixed at **18765** (manifest-declared; the card's Open UI link
  reads it). Voice itself is configured only in the Web UI
  (Settings → Channels → Voice).
- **`nanobot.config.json` overlay** — the full nanobot config surface,
  deep-merged over the built-in defaults. Edit via SSH at
  `/userdata/local/appdata/nanobot/nanobot.config.json`; invalid JSON fails
  the app start loudly (see the app log).

The generated effective config is at
`/userdata/local/appdata/nanobot/.nanobot/config.json` — inspect it (or
`nanobot-voice config --config <that file>` for the canonical voice section)
when something behaves unexpectedly. Do not edit it: it is regenerated.

### Voice (on-device by default)

The shipped baseline runs the full voice stack **on the NPU**: wake word
("hey mycroft", strict mode), Silero VAD, Zipformer bilingual STT, Matcha
bilingual TTS — capture over the VQE chain (`ai_asr_vqe`, hardware AEC),
playback over `default` (its hardware loopback feeds the AEC reference).

First start downloads the four model weights (~200 MB, hash-verified) into
the app-private store **in the background**: the voice channel's own keeper
fetches what its setup names, retries with backoff, and reloads the pipeline
once the weights land — boot and the Web UI never wait on the network.
Progress and state live in the panel (Channels → Voice → Models row). The
two keys that carry a license notice (Matcha zh-en, openWakeWord
hey-mycroft) are pre-accepted in the shipped baseline
(`models.acceptNotices`; licensing in THIRD_PARTY_NOTICES.md). On a metered
network set the `download_proxy` knob, then restart. A cloud-engine setup
names no weights keys and fetches nothing.

**Configure voice in the Web UI**: Settings → Channels → Voice — the panel
validates every edit ("Resolved setup"), fetches/prunes models on Apply, and
its Reset returns to this app's shipped baseline. Panel edits persist across
restarts — including the channel on/off toggle, which is the single entry
for enabling voice (first boot defaults to on).

Cloud engines instead? Pick them in the panel, or paste a section into the
overlay — every block of
[EXAMPLE_CONFIGS.md](https://github.com/iChizer0/nanobot-channel-voice/blob/main/docs/EXAMPLE_CONFIGS.md)
works unchanged under `channels.voice`:

```json
{ "channels": { "voice": { "stt": { "provider": "nanobot" },
  "tts": { "provider": "openai", "apiKey": "sk-..." } } } }
```

with `transcription` keys resolved from `providers` (e.g.
`{ "providers": { "groq": { "apiKey": "gsk_..." } } }`).

Manage the weights store by hand: `ssh root@<device>` then
`HOME=/userdata/local/appdata/nanobot python3 -m nanobot_channel_voice.cli list`
(same command with `fetch <key>` / `prune`).

### Advanced

- Multiple instances / gateway ports: this app runs exactly one gateway on
  the fixed Web UI port 18765. The card's Open UI link points at a small
  readiness gate (18766) that shows a starting page until the gateway
  answers, then redirects.
- The overlay may also enable other nanobot channels (telegram, …) and MCP
  servers — see nanobot's `docs/configuration.md`.

## Development

### Layout

- Repository root — the app source tree: `app.py` (kit wrapper),
  `manifest.json` (App Center v2), `voice_defaults.json` (shipped voice
  baseline), `skills/` (bundled agent skills), `icon.png`,
  `THIRD_PARTY_NOTICES.md`, `app-requirements.txt` (exact wheel pins). This
  directory packs directly; `manifest.json`'s `package.exclude` keeps
  everything else out of the artifact.
- `lib/` — pinned upstream sources as git submodules (read-only references;
  wheels are built from these).
- `tools/` — wheelhouse scripts (`fix_wheel_tags.py`,
  `specialize_wheelhouse.py`).

### Rebuild

```sh
git submodule update --init
# 1) build the upstream wheels (nanobot's hatch hook builds the WebUI; needs bun)
cd lib/nanobot/webui && bun install && bun run build && cd ..
pip3 wheel . --no-deps -w ../../build/wheelhouse
cd ../../lib/nanobot-channel-voice && pip3 wheel . --no-deps -w ../../build/wheelhouse
# 2) dependency closure into build/wheelhouse (target: cp311 aarch64), then
python3 tools/fix_wheel_tags.py build/wheelhouse
python3 tools/specialize_wheelhouse.py   # run from build/
# 3) validate + package + sign (recamera-pro-ext-api skill scripts)
validate_app.py --mode publish --sdk-root <ext-api> --app-dir .
package_app.py --app-dir . --out build/dist --wheelhouse build/wheelhouse \
  --requirements app-requirements.txt --sdk-root <ext-api>
openssl dgst -sha256 -sign <owner_key> build/dist/nanobot-1.0.0-arm64.tar.gz \
  | openssl base64 -A > build/dist/nanobot-1.0.0-arm64.tar.gz.sig.b64
```

The wheelhouse also contains hand-built wheels (cpython-sqlite3,
cpython-bz2, recamera_local); `build/` (wheelhouse, dist, scratch) is
gitignored.

### Submodules

| Path | Upstream |
|---|---|
| `lib/nanobot` | [iChizer0/nanobot](https://github.com/iChizer0/nanobot) |
| `lib/nanobot-channel-voice` | [iChizer0/nanobot-channel-voice](https://github.com/iChizer0/nanobot-channel-voice) |
| `lib/reCamera-Intellisense` | [iChizer0/reCamera-Intellisense](https://github.com/iChizer0/reCamera-Intellisense) |

## Boundaries

- nanobot and nanobot-channel-voice ship as unmodified wheels under
  `wheels/`; this package adds only the wrapper (`app.py`) and the manifest.
- The voice channel never bundles model files; weights come from its index
  (hash-pinned) into the app-private store.
- The app claims the `npu.rknn` resource exclusively (required) for its
  on-device audio models and drives RKNNLite directly per the platform's
  streaming-audio carve-out (`needs_frames`/`needs_model` both false; the
  camera frame path stays with the firmware). Results may be published
  through the brokered `result.publish` lane. Audio capture goes through
  the firmware ALSA/VQE topology only.

## License

MIT (see LICENSE) for this packaging. Third-party components keep their own
licenses — see THIRD_PARTY_NOTICES.md.
