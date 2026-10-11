# Brand assets

The Magehand logo (wordmark) and app icon. The README uses
`logo/logo-1024.png`.

| Path | Use |
| --- | --- |
| `logo/logo-{256,512,1024,1536}.png` | wordmark logo (3:2) at standard widths |
| `logo/logo.webp` | wordmark logo, 1536 px, small file |
| `logo/logo.svg` | wordmark logo, vector trace of the artwork (see below) |
| `icon/icon-{16,32,48,64,128,192,256,512,1024}.png` | app icon (square) at standard sizes |
| `icon/favicon.ico` | multi-size ICO: 16, 32, 48, 64, 128, 256 |
| `icon/apple-touch-icon.png` | 180 px, for `<link rel="apple-touch-icon">` |
| `icon/icon.icns` | macOS icon |
| `icon/icon.webp` | app icon, 512 px, small file |
| `icon/icon.svg` | app icon, vector trace of the artwork (see below) |

The artwork is raster (glows and gradients), so every size is resampled from
the original PNGs and the SVGs are automatic traces of them, not hand-drawn
vectors. They are good for scaling up and for places that want SVG; for tiny
sizes use the PNGs. The icon's corners are opaque (dark), as in the original.
