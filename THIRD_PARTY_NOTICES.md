# Third-Party Notices

This project is a thin wrapper around third-party software. The wrapper code is
MIT-licensed (see [LICENSE](LICENSE)); the components below are **not** and are
**not redistributed** in this repository.

## Dahua NetSDK (`NetSDK/`) — proprietary, NOT included

The `NetSDK/` directory (Python bindings and `Libs/linux64/*.so`) is the
**Dahua Network SDK**, proprietary software owned by
**Zhejiang Dahua Technology Co., Ltd.**

- **Not bundled here.** It is git-ignored and must be obtained directly from Dahua.
- **License:** proprietary, subject to Dahua's own license terms (which you accept
  when you download it). Because it is not freely redistributable, it is not
  included here and is not covered by this project's MIT license.
- **Where to get it & how to install it:** see [NetSDK/README.md](NetSDK/README.md).

Please review Dahua's license terms and any applicable export-control laws yourself
before downloading and using the SDK; this project makes no representations about them.

## Hikvision HCNetSDK (`hcnetsdk/`) — proprietary, NOT included

The `hcnetsdk/` directory (the `*.so` libraries and `HCNetSDKCom/`) is the
**Hikvision Device Network SDK** (HCNetSDK), proprietary software owned by
**Hangzhou Hikvision Digital Technology Co., Ltd.**

- **Not bundled here.** It is git-ignored and must be obtained directly from Hikvision.
  (This project's own Python ctypes bindings for it live in `relay/backends/hikvision.py`
  and *are* MIT-licensed; only the SDK's binaries are proprietary.)
- **License:** proprietary, subject to Hikvision's own license terms (which you accept
  when you download it). Because it is not freely redistributable, it is not
  included here and is not covered by this project's MIT license.
- **Where to get it & how to install it:** see [hcnetsdk/README.md](hcnetsdk/README.md).

Please review Hikvision's license terms and any applicable export-control laws yourself
before downloading and using the SDK; this project makes no representations about them.

## Other bundled-at-build-time components

- **MediaMTX** (downloaded in the Docker image): MIT License —
  https://github.com/bluenviron/mediamtx
- **FFmpeg** (system package in the Docker image): LGPL/GPL depending on build —
  https://ffmpeg.org
