# TubeGrab

YouTube to MP4 / MP3 downloader and installable app (PWA).

**Made by Jhames Martin** · [Portfolio](https://jhamesmartinph.vercel.app) · [GitHub @jrmph13](https://github.com/jrmph13)

Paste a link, see the thumbnail, choose **Video MP4** (every resolution the video really has, 4K down to 144p, with file sizes) or **Audio MP3** (320 / 192 / 128 kbps), and download. Progress is real: it comes straight from the downloader, with speed, time left and the processing step (joining video and audio, converting to MP3).

Built with Flask, yt-dlp, Deno (solves YouTube's player challenges) and ffmpeg. Icons: [Lucide](https://lucide.dev) (ISC license, see `LICENSE-lucide.txt`).

MP3s include the video thumbnail as cover art.

## Install it as an app
- **Android (Chrome):** open the site, tap **Install app** at the top (or menu, then Install app). After installing,
  tap **Share** in the YouTube app and choose **TubeGrab**: it opens with the video already loaded.
- **iPhone/iPad (Safari):** tap **Install app** for the steps: Share, then **Add to Home Screen**.
- **Desktop (Chrome/Edge):** click **Install app** or the install icon in the address bar.

The app works offline for its own screens; downloading still needs the internet.

## Deploy to Render
1. Put this folder in a GitHub repo.
2. On render.com: **New → Blueprint**, pick the repo. It reads `render.yaml` and builds the Docker image (ffmpeg included).
3. Open `https://<name>.onrender.com/api/status`. Everything should say `true` (except `cookies` until you add them):
   `yt_dlp_ejs`, `deno`, `ffmpeg`. If `deno` is false, the YouTube downloads will fail, so redeploy.
4. Open your `https://<name>.onrender.com` link and paste a video.

### If you see "YouTube blocked this server with a bot check"
YouTube often blocks cloud servers like Render. Give the app cookies from a browser:
1. Install the **Get cookies.txt LOCALLY** extension (Chrome/Firefox).
2. Sign in to youtube.com (use a spare Google account), open any video, export cookies for youtube.com.
3. Render → your service → **Environment** → add `YTDLP_COOKIES` and paste the whole file contents → Save.

Cookies expire every few weeks; export again if the error returns.
If downloads break after a YouTube change, use **Manual Deploy → Clear build cache & deploy** to get the newest yt-dlp.

## Run on your computer
```bash
pip install -r requirements.txt   # needs ffmpeg installed too
python app.py                     # http://localhost:5000
```
or `docker build -t tubegrab . && docker run -p 10000:10000 tubegrab`

## Settings (environment variables)
| Name | Default | What it does |
|---|---|---|
| `MAX_DURATION_SECONDS` | 3600 | Longest video allowed |
| `JOB_TTL_SECONDS` | 1800 | How long finished files are kept for re-download |
| `MAX_ACTIVE_JOBS` | 3 | Downloads allowed at the same time (keeps the free plan from running out of memory) |
| `YTDLP_COOKIES` | none | Contents of cookies.txt |
| `YTDLP_COOKIES_FILE` | none | Or a path, e.g. a Render Secret File at `/etc/secrets/cookies.txt` |

## How it works
- `POST /api/info` reads title, thumbnail and the real list of formats.
- `GET /api/thumbnail/<video id>` downloads the best available thumbnail as a JPG (maxres, then sd, hq, mq).
- `POST /api/jobs` starts a download in the background; `GET /api/jobs/<id>` returns live progress; `POST /api/jobs/<id>/cancel` stops it; `GET /api/jobs/<id>/file` sends the finished file.
- Video downloads prefer H.264 + AAC so the MP4 plays on every phone and PC.
- The server runs one worker process (with many threads) so all requests see the same download jobs. Keep it that way, or move jobs to Redis before adding workers.

## Free plan limits
Render's free plan sleeps when idle (first visit takes 30–60 s) and has 512 MB RAM and limited disk, so 4K or very long videos may fail there. A paid instance handles them fine.

Only download videos you own or have permission to use.
