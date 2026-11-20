# Hikvision private protocol Python port

## Goal

Replace the SDK dependency for the Hikvision operations the relay uses. The Python client must authenticate on the private device port, discover channels, and deliver main and substream bytes to the existing MPEG-PS pipeline. The recorder configured in `.env` is the live acceptance test on macOS ARM64. Dahua continues to use its existing backend.

## 2026-11-20

- The installed `libhcnetsdk.so` is an x86-64 Linux ELF library. The test machine is macOS ARM64, so a Python `ctypes` wrapper around this file cannot make it portable.
- The configured private SDK port accepts TCP connections. The standard RTSP ports tested timed out. The HTTPS endpoint returned HTML for ISAPI paths. Use the private port and existing `.env` credentials for acceptance testing.
- Scope the replacement to the operations the relay calls. Keep FFmpeg and MediaMTX as the existing deployment tools for stream copying and serving RTSP.
- Inspect the reference SDK locally and capture a successful login and live preview from the configured recorder. Keep emulator files, private packet captures, and camera samples under the ignored `tmp/` directory. Keep credentials and raw captures out of git.
- Baseline tests on macOS ARM64 have 69 passes and four failures caused by the absent Dahua capture fixture `tests/fixtures/ch3_main.dhav`.

### Reference capture setup

- The existing backend calls `NET_DVR_Login_V40` and uses TCP private mode with `NET_DVR_RealPlay_V40`. Model login, channel numbering, and preview against that path. The repository’s previous tests identify channels 7 and 8 as candidates.
- Capture each TCP direction separately through a loopback forwarder. This keeps byte boundaries and reconnect behavior observable without collecting traffic for other devices. TCP `recv()` boundaries are arbitrary and must not be treated as protocol packet boundaries.
- A local userspace emulator boots AMD64 Linux Python on ARM64. The SDK reference hits two emulator limitations: Linux `libffi` callback trampoline allocation fails, and SDK initialization stalls during its OpenSSL work. A callback is optional for collecting wire bytes. Trying a Rosetta-backed userspace runner for initialization.
- The Python environment is already synced. `uv run --no-cache --no-sync` uses that environment when the workspace sandbox blocks uv’s default cache path.
- Rosetta userspace boots Ubuntu Python, but the reference library still fails in `NET_DVR_Init`. Use an AMD64 Docker container for the reference capture with the user's approval. The port and live acceptance test will run directly on the host with `uv`.

### First authenticated capture

- The AMD64 SDK authenticated successfully and returned analog channels 1–16 and IP channels 33–50. Both known substreams produced H.264 at 352×288. Closing the SDK under emulation raised `SIGBUS` after the samples were written.
- Login is a two-message exchange on one TCP connection. The first client message carries a per-run RSA public key. The recorder sends an RSA-encrypted challenge and a separate ASCII salt. Preview starts on another TCP connection using the resulting session identifiers.
- The recorder sends length-prefixed RTP packets for the tested substreams. The SDK assembles and muxes them as MPEG-PS before invoking the relay callback. Account for this conversion in the port so the existing FFmpeg input contract still works.
- Recomputed both login proofs offline and matched the SDK capture byte for byte. For this recorder, the RSA plaintext is a 32-byte challenge. The proof is HMAC-MD5 of the username and of the lowercase SHA-256 hex digest of `username + salt + password`, using the challenge as key. Each login generates a fresh RSA key.
- Main streams also use RTP on this recorder. The captured main streams decoded at 960×1080 and 1280×720. The tested streams used single H.264 NAL units and FU-A fragments, with separate recorder metadata packets.

### Native login and preview

- The preview command checksum matches the SDK capture offline. It uses the session ID, recorder nonce, clock from the first reply, client identifier, and four rounds of Rijndael keyed by the first 16 challenge bytes.
- Native macOS ARM64 Python authenticated using the existing `.env`, discovered the same 34 channels, and received a 40-byte `IMKH` header plus 24,000 bytes of live preview data from channel 7 main. The runtime did not load the SDK.
- Next acceptance check is to turn the private RTP frames into program-stream packets, publish with the existing relay, and decode RTSP through a viewer.

### Relay verification

- Replayed the captured RTP through the Python MPEG-PS muxer and decoded the expected H.264 resolutions for all three reference captures. RTP sequence numbers wrap at 16 bits and timestamps at 32 bits. Treat the TCP length prefix, RTP padding, and RTP extension as separate fields. Drop an incomplete access unit after a sequence gap.
- Ran `relay run` on macOS ARM64 with one shared Python login and two live camera previews. FFmpeg published both via RTSP to loopback MediaMTX. A separate FFmpeg reader decoded channel 7 main at 960×1080 and channel 8 sub at 352×288. A decoded channel 7 frame was visually checked. Clips and temporary configs are in `tmp/`.
- Added regression tests for backend selection, login proof, command checksum, split TCP reads, H.264 fragment reassembly, and HEVC stream type detection. The checksum vector also matched a synthetic reference call in the SDK.
- `HIKVISION_TRANSPORT=python` is the default for `VENDOR=hikvision` and `VENDOR=hik`. `HIKVISION_TRANSPORT=sdk` selects the existing native transport on a compatible Linux host. The Python implementation covers the observed IPv4 legacy challenge and private TCP RTP mode. HEVC and G.711 handling have no live-device coverage. Other authentication modes and high-byte IP channel counts require a separate recorder for verification.
- The normal `uv run --locked python -m relay parse` command detected all four working channel 7 and 8 variants and recorded their H.264 resolutions. The capped Hikvision probe now follows the recorder's channel numbering, starting at channel 1 on this unit.
- Verified on-demand startup with MediaMTX `runOnDemand` invoking `relay serve`. The first RTSP viewer triggered the Python login and decoded a six-second channel 7 main clip at 960×1080.
- The test suite passed 75 cases. The four pre-existing Dahua cases still require the missing DHAV capture fixture.

### Prebuilt image

- The single runtime image defaults to the Python Hikvision transport and copies the architecture-matched MediaMTX 1.21.4 executable from the upstream image. Both local SDK directories are excluded from the build context.
- The GHCR workflow builds Linux AMD64 and ARM64 from `main` and `v*` tags. The workflow tracks the current GitHub Actions major tags. The root Compose file pulls `ghcr.io/jackblk/nvr-rtsp-relay:latest` using the existing `.env` and `config/` layout.
- The optional read-only SDK mounts are commented out in Compose. Uncomment the matching mount when supplying an SDK built for the container architecture. The entrypoint checks for the Python wrapper and `libdhnetsdk.so` in Dahua mode, or `libhcnetsdk.so` in native Hikvision mode, before starting MediaMTX.
- Built both image variants locally and decoded 24 RTSP frames from live Hikvision channel 7 main (H.264, 960×1080) through each variant using its default command. Both variants exclude the vendor SDKs and exit with status 1 and a mode-specific error when the configured SDK is missing.
- Validated the workflow and Compose configuration. The full `uv run pytest -q` run passed 75 cases. Four existing tests failed because `tests/fixtures/ch3_main.dhav` is absent from the checkout.
