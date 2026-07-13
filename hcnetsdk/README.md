# HCNetSDK — Hikvision Device Network SDK (download separately)

This directory holds the **Hikvision HCNetSDK** ("Device Network SDK" for Linux 64-bit),
which this project depends on but **does not bundle**. The SDK is proprietary software
owned by *Hangzhou Hikvision Digital Technology Co., Ltd.* and is **not redistributable**
under its license, so you must download it yourself.

> Only `README.md` and `setup.sh` are committed to git. Everything else here
> (the `.so` files, the header, and the `.zip`) is git-ignored.
>
> Unlike the Dahua NetSDK (which ships Python bindings in a wheel), the Hikvision SDK
> ships only C `.so` libraries. This project's Python ctypes bindings for it live in
> `relay/backends/hikvision.py` (under source control).

## Setup

1. **Download** the Linux64 Device Network SDK from Hikvision (you accept their license
   agreement to download):
   - Primary: <https://www.hikvision.com/en/support/tools/hitools/?type=IP> — choose
     **Device Network SDK_Linux64** (ships as the `.zip` this project builds against).
   - Backup: <https://www.hikvision.com/mena-en/support/download/sdk/device-network-sdk--for-linux-64-bit-/>

   This project was built against
   **`EN-HCNetSDKV6.1.9.48_build20230410_linux64`** (newer builds should work too).

2. **Paste** the zip into this directory (`./hcnetsdk/`), e.g.:

   ```
   EN-HCNetSDKV6.1.9.48_build20230410_linux64.zip
   ```

3. **Run** the setup script — it extracts the libraries into place and verifies the result:

   ```bash
   ./hcnetsdk/setup.sh
   ```

## Expected layout (check at a glance)

After `setup.sh` runs, this directory should look like:

```
hcnetsdk/
├── README.md                 # this file        (committed)
├── setup.sh                  # extractor         (committed)
├── EN-HCNetSDK...linux64.zip  # you paste this    (git-ignored)
├── HCNetSDK.h                # header reference   (git-ignored)
├── libhcnetsdk.so            # ┐
├── libHCCore.so              # │
├── libcrypto.so.1.1          # │ from the zip     (git-ignored)
├── libssl.so.1.1             # │
├── libPlayCtrl.so, ...       # ┘
└── HCNetSDKCom/              # component libs     (git-ignored)
    ├── libHCPreview.so
    ├── libStreamTransClient.so
    └── ...
```

## License

The HCNetSDK is proprietary and subject to Hikvision's own license terms, which you
accept when you download it. Please read and comply with those terms (and any
applicable export-control laws) yourself. Because it is not freely redistributable, it
is not included in this repository and is not covered by this project's MIT license.
See [../THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
