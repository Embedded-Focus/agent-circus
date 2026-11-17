# Vendored xterm.js

These files are copied from the npm packages below so the browser shell
works offline and no third-party code is loaded at runtime.

| Package | Version | Tarball | integrity (sha512, base64) |
|---|---|---|---|
| `@xterm/xterm` | 6.0.0 | `https://registry.npmjs.org/@xterm/xterm/-/xterm-6.0.0.tgz` | `TQwDdQGtwwDt+2cgKDLn0IRaSxYu1tSUjgKarSDkUM0ZNiSRXFpjxEsvc/Zgc5kq5omJ+V0a8/kIM2WD3sMOYg==` |
| `@xterm/addon-fit` | 0.11.0 | `https://registry.npmjs.org/@xterm/addon-fit/-/addon-fit-0.11.0.tgz` | `jYcgT6xtVYhnhgxh3QgYDnnNMYTcf8ElbxxFzX0IZo+vabQqSPAjC3c1wJrKB5E19VwQei89QCiZZP86DCPF7g==` |

Copied files (SHA-256 after the edit below), vendored on 2026-01-01:

| File | Source in the package | SHA-256 |
|---|---|---|
| `xterm.js` | `@xterm/xterm/package/lib/xterm.js` | `98d0973151aff2991d335b1adbbdac2e14da26341abe329d677d4c0034402bdf` |
| `xterm.css` | `@xterm/xterm/package/css/xterm.css` | `854a7c0fb70e8b1a083c16797ab827299fb18744f5ad34f227b48337e33293c6` |
| `addon-fit.js` | `@xterm/addon-fit/package/lib/addon-fit.js` | `4df3789fa7bff5d545ba38462a8cd2f7460ba971021bce3f81c2870f937a201e` |
| `LICENSE.xterm` | `@xterm/xterm/package/LICENSE` | `b569f629d00f2626a8100df2a1798210535621e42164dfd426a6fe5aac7b0ccd` |
| `LICENSE.addon-fit` | `@xterm/addon-fit/package/LICENSE` | `e256f01188af527e4d06d21d06fbf785ae9c50d4b328bf03cbe0ba7f0aa4228f` |

Edit made while copying: the trailing `//# sourceMappingURL=...` line
was dropped from each `.js` file (the source maps are not shipped).
The two MIT license texts differ in their copyright header, so both are
kept.

## How to update

1. Read `https://registry.npmjs.org/@xterm/xterm/latest` and
   `https://registry.npmjs.org/@xterm/addon-fit/latest` (fields
   `version`, `dist.tarball`, `dist.integrity`).
2. Download each tarball into an empty directory outside the
   repository; verify it against `dist.integrity` (sha512, base64)
   before extracting.
3. Copy `package/lib/xterm.js`, `package/css/xterm.css` and
   `package/lib/addon-fit.js` here, dropping the `sourceMappingURL`
   line, and refresh `LICENSE.xterm` / `LICENSE.addon-fit`.
4. Update the tables above (versions, integrity values, file hashes,
   date) and the hashes in `tests/test_msb_circus_vendor.py` stay
   derived from this file.
