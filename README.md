# RIO × SOCIAL MEDIA DOWNLOADER (YouTube + Instagram + Facebook + TikTok + Pinterest)

One Flask site. Paste a link from **YouTube, Instagram, Facebook, TikTok or Pinterest**; the site works out which one it is, then you
save the video (with or without the Rio ghost watermark), an MP3, the original file, the cover image or a details TXT.
Theme: black and white until a link is pasted. As soon as a link is recognised the whole page fades to that platform's colours (YouTube red, Instagram pink, Facebook blue, TikTok cyan, Pinterest red) and the matching pill lights up. The neutral colours are `:root` and the platform ones are `:root[data-site="..."]`, both at the top of `static/style.css`; `static/app.js` (`markSite`) sets `data-site`.

## Installable app (PWA)

The site can be installed on phones and desktops: Android Chrome shows an **Install app** button on the page (iPhone: Share > Add to Home Screen).
Once installed on Android, **Share > RIO Downloader** from the YouTube / TikTok / Instagram app opens the link here and looks it up at once.
Files: `/manifest.webmanifest` and `/sw.js` (routes in `app.py`, worker in `templates/sw.js`), icons in `static/icons/`.
The service worker only caches the page shell; lookups, downloads and the counter always go to the server. It needs HTTPS (Render gives that).

The big **download counter** is the same number as the header pill (`/api/stats`). Without Upstash it is kept in a temp file, which Render's
free plan wipes on restart: set `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` to keep it for good.

## Folder layout (Flask needs exactly this)

```
app.py
requirements.txt
render.yaml
templates/index.html
static/app.js
static/style.css
static/watermark.png     <- ghost logo burned into videos
static/ghost.png         <- logo in the header / hero
static/favicon.png
static/icons/*.png       <- app icons (PWA)
templates/sw.js          <- service worker
```

## Run locally

```bash
pip install -r requirements.txt
pip install deno         # JavaScript runtime that yt-dlp needs for YouTube (or have Node.js installed)
python app.py            # http://localhost:5000
```

## Deploy on Render

Push this folder to GitHub, create a **Web Service** from the repo (render.yaml is picked up).

* Build command: `pip install -r requirements.txt && (pip install deno || echo "deno wheel not installed")`
* Start command: `gunicorn app:app --workers 1 --threads 16 --timeout 120 --worker-tmp-dir /dev/shm`
* Health check path: `/healthz`

## Links that work

| Site | Links |
|---|---|
| YouTube | `youtube.com/watch?v=...`, `youtu.be/...`, `youtube.com/shorts/...` |
| Instagram | `instagram.com/reel/...`, `/p/...` (video posts), `/tv/...`, `instagr.am/...` |
| Facebook | `facebook.com/watch/?v=...`, `/reel/...`, `/<page>/videos/...`, `/share/v/...`, `/share/r/...`, `fb.watch/...` |
| TikTok | `tiktok.com/@user/video/...`, `vm.tiktok.com/...`, `tiktok.com/t/...` |
| Pinterest | `pinterest.com/pin/...` (and regional sites such as `pinterest.co.uk`), `pin.it/...`. Video pins give MP4 + MP3 (the page data, Pinterest's pin API and HLS playlists are all tried, so a video pin is never turned into its cover picture); image pins give the original-size picture in the Document tab. |

Only **public** videos work. Not supported: private accounts, stories, friends/group posts, photo-only posts (incl. TikTok slideshows),
playlists and channels, live streams and premieres, age-restricted or members-only YouTube videos, YouTube videos
longer than `MAX_DURATION`.

## How downloads work

| Tab | What you get | How |
|---|---|---|
| Video | MP4 in the chosen quality. Watermark switch + corner picker. | **YouTube:** always a job (yt-dlp downloads picture + sound, ffmpeg joins them, no re-encode; watermark = one extra encode). **Instagram / Facebook / TikTok:** direct stream, or a job when a quality is picture-only or the watermark is on. |
| Audio | Real **MP3** with title/creator tags. | ffmpeg job. |
| Document | **Original file** (no watermark, no re-encode), **cover image**, **details TXT**. | Same as Video, or a direct download. |

Every download starts with `POST /api/prepare`, so any failure shows as a message in the page.
Files are named `youtube_...`, `instagram_...`, `facebook_...`, `tiktok_...` or `pinterest_...`.

## IMPORTANT: YouTube and Instagram block cloud servers

YouTube and Instagram answer requests from data-centre IPs (Render, AWS, ...) with "Sign in to confirm you're
not a bot" or "login required". That is the most common reason for "it doesn't work", and it is not a bug in the app.
(Facebook is much more relaxed.) For YouTube the app already makes a second try with other player clients when
it sees the bot message, which sometimes helps. If it still fails, use one of these:

1. **Cookies.** Export a `cookies.txt` (Netscape format) from a browser that is logged in to a **throw-away**
   account (extension "Get cookies.txt LOCALLY"). On Render: *Environment > Secret Files*.
   * YouTube: file name `cookies.txt` (read automatically from `/etc/secrets/cookies.txt`), or set `COOKIES_FILE`,
     or paste the content into `YOUTUBE_COOKIES`.
   * Instagram: file name `instagram_cookies.txt` (read automatically from `/etc/secrets/instagram_cookies.txt`),
     or set `INSTAGRAM_COOKIES_FILE`, or paste the content into `INSTAGRAM_COOKIES`.

   Never use a main account (it can get flagged or restricted), never commit cookies to GitHub, and re-export now
   and then because they expire.
2. **A residential proxy** in `PROXY_URL` (e.g. `http://user:pass@host:port`).
3. Host the app somewhere with a home IP (a Raspberry Pi at home works well).

To see what the server has, set `DEBUG_ERRORS=1` and open `/api/debug/env`: it shows the yt-dlp version, whether
`deno` / `node` and ffmpeg were found, and which sites have cookies loaded. Remove `DEBUG_ERRORS` afterwards.

## Environment variables (all optional)

| Name | Default | Meaning |
|---|---|---|
| `MAX_HEIGHT` | `1080` | Tallest YouTube quality offered. Above 1080 the file is an MKV and much bigger. |
| `MAX_DURATION` | `1800` | Longest YouTube video accepted, in seconds. |
| `MAX_SOURCE_MB` | `250` | Largest download accepted. |
| `PARALLEL_JOBS` | `2` | Downloads running at once (watermark encoding is always one at a time). |
| `MAX_ACTIVE_JOBS` | `5` | Jobs allowed in the queue + running. |
| `COOKIES_FILE` / `YOUTUBE_COOKIES` | `/etc/secrets/cookies.txt` | YouTube cookies (see above). |
| `INSTAGRAM_COOKIES_FILE` / `INSTAGRAM_COOKIES` | `/etc/secrets/instagram_cookies.txt` | Instagram cookies (see above). |
| `TIKTOK_PROVIDERS` (or `PROVIDERS`) | `own,tikwm,ytdlp` | Order of TikTok lookup providers. `own,ytdlp` never uses the third-party tikwm API. |
| `PROXY_URL` | - | Proxy for all requests. |
| `WATERMARK` | `1` | `0` switches the watermark feature off. |
| `WATERMARK_POSITION` | `br` | Default corner: `br`, `bl`, `tr`, `tl`. |
| `WATERMARK_SIZE` | `0.24` | Logo width as a share of the video width. |
| `WATERMARK_OPACITY` | `0.92` | 0.1 to 1. |
| `WATERMARK_IMAGE` | `static/watermark.png` | Use another PNG (transparent background). |
| `FFMPEG_PRESET` | `ultrafast` | `veryfast` gives smaller files but uses more CPU. |
| `RATE_LIMIT` / `RATE_WINDOW` | `12` / `60` | Requests per visitor per window (seconds). |
| `GLOBAL_RATE_LIMIT` | `240` | Requests per window for the whole site. |
| `PROXY_HOPS` | `2` | See below. |
| `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN` | - | Keeps the download counter across restarts. |
| `COUNTER_KEY` | `rio_downloads` | Counter name (one shared counter for all five sites). |
| `DEBUG_ERRORS` | - | `1` shows the real error in the UI and enables `/api/debug/ip` and `/api/debug/env`. |

### Rate limit and `PROXY_HOPS`

`X-Forwarded-For` can be forged by visitors, so the app trusts only the entry added by your own proxies,
counted from the right. On Render this is normally 2. To check: set `DEBUG_ERRORS=1`, open `/api/debug/ip`
and confirm `seen_as` is **your** IP, then remove `DEBUG_ERRORS`.

## Notes

* **These sites change constantly.** If lookups suddenly fail, redeploy so `yt-dlp` updates (it is not
  version-pinned on purpose). Render: Manual Deploy > "Clear build cache & deploy".
* Render's free plan sleeps after 15 min idle (first request is slow) and wipes temp files on restart.
  Long YouTube videos use disk space and time on the free plan; lower `MAX_DURATION` if needed.
* Replace `static/watermark.png` with any transparent PNG to rebrand.
* Only save videos you made, videos whose licence allows it, or videos you have permission to use.
  The terms of YouTube, Instagram, Facebook, TikTok and Pinterest do not allow downloading other people's videos without permission.
