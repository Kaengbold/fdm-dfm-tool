# Opening the 3MF in the slicer from the page

2026-11-18, for T-430. Drafted by Gemini Flash; the slicer parts were checked against the slicers' source
(GitHub `main`/`master` on that day). Browser behaviour not checked by hand yet: belongs to T-467.

## Result in one line

No one-click path exists for a file the page makes in the browser. What works: download it, then the
browser's "Always open files of this type" setting opens every later download in the slicer the computer uses for `.3mf`.

## URL schemes: need a web address, two of three restrict the site

| Slicer | Scheme | What `file=` must be | Checked in |
|---|---|---|---|
| PrusaSlicer | `prusaslicer://open?file=` | `https://` on `printables.com` or `thingiverse.com` only; otherwise "Download won't start" | `src/slic3r/GUI/Downloader.cpp`, `start_download` |
| Bambu Studio | `bambustudio://open?file=` | http(s); hosts outside a MakerWorld list ask "This file is not from a trusted site, do you want to open it anyway?" | `src/slic3r/GUI/GUI_App.cpp`, `is_trusted_model_download_url` |
| OrcaSlicer | `orcaslicer://open?file=` (also `prusaslicer`, `bambustudio`, `cura` prefixes) | any URL, downloaded with curl into the download folder set in the wizard | `src/slic3r/GUI/Downloader.cpp`, `start_download` |

All three download the file themselves. A `blob:` URL lives inside the browser tab, so no other program can
fetch it, and a static site (GitHub Pages) has no web address for a part made in the visitor's browser. Uploading
the part somewhere is out: no uploads is a project rule.

## Other paths

- **File System Access API** (`showDirectoryPicker`, handle kept in IndexedDB): Chrome / Edge / Opera on desktop only.
  It can save into a folder, but none of the three slicers watches a folder, so nothing opens.
- **"Always open files of this type"** (Chrome / Edge download list; Firefox: "Always open similar files"): after one
  right-click, each `.3mf` download opens in the default `.3mf` app. Safari: no such option. **Built:** one line in
  the "Next, in your slicer" card.
- **Web Share API with files**: on Windows and macOS the share sheet lists share targets (Mail, AirDrop, …), not
  desktop slicers. Doesn't help.
- **PWA file handlers** (T-449): open files *into* the installed app, not out of it. Doesn't help.
- **Dragging the file out** (Chrome's `DownloadURL` drag type): Chrome only, not checked whether a `blob:` URL works
  or whether the slicers take the drop. Worth one manual try (T-467) before building.
- **A small local helper** (localhost program that starts the slicer): works everywhere, but needs an install;
  fits local mode (T-450), not the static site.
