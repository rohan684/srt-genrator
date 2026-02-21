import os
import re
import tempfile
import subprocess
import requests
from flask import Flask, request

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_KEY")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

processed_updates = set()

@app.route("/", methods=["GET"])
def home():
    return "SRT Bot Running! v4", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    update_id = data.get("update_id")
    if update_id in processed_updates:
        return "OK", 200
    processed_updates.add(update_id)

    message = data.get("message")
    if not message:
        return "OK", 200

    chat_id = message["chat"]["id"]

    try:
        # ===== VIDEO UPLOAD =====
        if "video" in message or "document" in message:
            media = message.get("video") or message.get("document")
            file_size_mb = media.get("file_size", 0) / (1024 * 1024)
            if file_size_mb > 20:
                send_message(chat_id, "⚠️ File too large. Please send under 20MB.")
                return "OK", 200
            send_message(chat_id, "📥 Video received. Generating subtitles...")
            file_id = media["file_id"]
            file_url = get_telegram_file_url(file_id)
            handle_video_url(chat_id, file_url)
            return "OK", 200

        # ===== URL EXTRACTION =====
        url = None
        text = message.get("text", "")
        entities = message.get("entities", [])

        for entity in entities:
            if entity["type"] == "url":
                url = text[entity["offset"]: entity["offset"] + entity["length"]]
                break
            elif entity["type"] == "text_link":
                url = entity["url"]
                break

        if not url:
            match = re.search(r'https?://[^\s]+', text)
            if match:
                url = match.group(0)

        if not url:
            send_message(chat_id, "📎 Send a YouTube/Instagram link or upload a video file (under 20MB).")
            return "OK", 200

        url = url.strip()

        # ===== YOUTUBE =====
        if any(x in url for x in ["youtube.com", "youtu.be", "m.youtube.com"]):
            send_message(chat_id, "🎬 Fetching audio from YouTube...")
            result = try_yt_dlp(url)
            if not result:
                send_message(chat_id, "❌ YouTube blocked audio extraction on this server.\n\n💡 Workaround: Download the video on your phone and send it here as a file!")
                return "OK", 200
            audio_bytes, mime = result
            handle_audio_bytes(chat_id, audio_bytes, mime)
            return "OK", 200

        # ===== INSTAGRAM =====
        if any(x in url for x in ["instagram.com/reel", "instagram.com/p/", "instagram.com/tv/"]):
            send_message(chat_id, "📸 Fetching audio from Instagram...")
            result = try_yt_dlp(url)
            if not result:
                send_message(chat_id, "❌ Could not extract audio.\n\n💡 Workaround: Download the reel and send it here as a file!")
                return "OK", 200
            audio_bytes, mime = result
            handle_audio_bytes(chat_id, audio_bytes, mime)
            return "OK", 200

        send_message(chat_id, "⚠️ Unsupported link.\n\nSupported:\n• YouTube / Shorts\n• Instagram Reels\n• Upload video file (under 20MB)")

    except Exception as e:
        print(f"Webhook error: {e}")
        send_message(chat_id, f"🛑 Error: {str(e)}")

    return "OK", 200


def try_yt_dlp(url):
    """Try to download audio with yt-dlp, return (bytes, mime_type) or None"""
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_template = os.path.join(tmpdir, "audio.%(ext)s")
            cmd = [
                "yt-dlp",
                "-f", "bestaudio[ext=m4a]/bestaudio/best",
                "--no-playlist",
                "--no-check-certificates",
                "--socket-timeout", "30",
                "-o", output_template,
                url
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
            print(f"yt-dlp code: {result.returncode}")
            print(f"yt-dlp stderr: {result.stderr[-500:]}")

            for fname in os.listdir(tmpdir):
                fpath = os.path.join(tmpdir, fname)
                size = os.path.getsize(fpath)
                print(f"Found file: {fname} size: {size}")
                if size > 0:
                    ext = fname.rsplit('.', 1)[-1].lower()
                    mime_map = {
                        'm4a': 'audio/mp4',
                        'mp4': 'video/mp4',
                        'webm': 'video/webm',
                        'mp3': 'audio/mpeg',
                        'opus': 'audio/opus',
                    }
                    mime = mime_map.get(ext, 'audio/mpeg')
                    with open(fpath, 'rb') as f:
                        return f.read(), mime
    except Exception as e:
        print(f"yt-dlp error: {e}")
    return None


def handle_video_url(chat_id, video_url):
    try:
        send_message(chat_id, "⬆️ Uploading to AI...")
        r = requests.get(video_url, timeout=60)
        print(f"Video download size: {len(r.content)} bytes")
        
        # Try as video first, then audio
        uri = upload_to_gemini(r.content, "video/mp4")
        if not uri:
            send_message(chat_id, "❌ Failed to upload to Gemini. Try a shorter video.")
            return
        generate_subtitles(chat_id, uri, "video/mp4")
    except Exception as e:
        print(f"handle_video_url error: {e}")
        send_message(chat_id, f"🛑 Failed: {str(e)}")


def handle_audio_bytes(chat_id, audio_bytes, mime_type):
    try:
        send_message(chat_id, "✍️ Generating subtitles...")
        print(f"Audio size: {len(audio_bytes)} bytes, mime: {mime_type}")
        uri = upload_to_gemini(audio_bytes, mime_type)
        if not uri:
            send_message(chat_id, "❌ Failed to upload audio to Gemini.")
            return
        generate_subtitles(chat_id, uri, mime_type)
    except Exception as e:
        print(f"handle_audio_bytes error: {e}")
        send_message(chat_id, f"🛑 Failed: {str(e)}")


def upload_to_gemini(file_bytes, mime_type):
    try:
        upload_url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={GEMINI_KEY}"
        r = requests.post(
            upload_url,
            headers={"X-Goog-Upload-Protocol": "multipart"},
            files={
                'metadata': ('metadata', '{"file": {"displayName": "media_file"}}', 'application/json'),
                'file': ('media_file', file_bytes, mime_type)
            },
            timeout=120
        )
        result = r.json()
        print(f"Gemini upload: {result}")
        uri = result.get("file", {}).get("uri")
        if uri:
            print(f"Gemini URI: {uri}")
            return uri
        return None
    except Exception as e:
        print(f"Gemini upload error: {e}")
        return None


def generate_subtitles(chat_id, file_uri, mime_type):
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}"
        payload = {
            "contents": [{
                "parts": [
                    {"text": "Transcribe this audio/video and return accurate subtitles in SRT format. Include timestamps. Return ONLY the raw SRT text without any markdown formatting or code blocks."},
                    {"fileData": {"mimeType": mime_type, "fileUri": file_uri}}
                ]
            }],
            "generationConfig": {"temperature": 0.1}
        }
        r = requests.post(url, json=payload, timeout=120)
        result = r.json()
        print(f"Gemini response: {result}")

        if not result.get("candidates"):
            err = result.get("error", {}).get("message", "Unknown error")
            send_message(chat_id, f"❌ AI failed: {err}")
            return

        srt = result["candidates"][0]["content"]["parts"][0]["text"]
        srt = srt.replace("```srt", "").replace("```", "").strip()

        if len(srt) < 10:
            send_message(chat_id, "❌ Subtitles are empty. Is there speech in the video?")
            return

        send_file(chat_id, srt)
        send_message(chat_id, "✅ Subtitles generated successfully!")

    except Exception as e:
        print(f"generate_subtitles error: {e}")
        send_message(chat_id, f"🛑 Subtitle generation failed: {str(e)}")


def get_telegram_file_url(file_id):
    r = requests.get(f"{TELEGRAM_API}/getFile?file_id={file_id}", timeout=30)
    file_path = r.json()["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"


def send_message(chat_id, text):
    try:
        requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=30)
    except Exception as e:
        print(f"send_message error: {e}")


def send_file(chat_id, srt_text):
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.srt', delete=False, encoding='utf-8') as f:
            f.write(srt_text)
            fname = f.name
        with open(fname, 'rb') as f:
            requests.post(
                f"{TELEGRAM_API}/sendDocument",
                data={"chat_id": chat_id},
                files={"document": ("subtitles.srt", f, "text/plain")},
                timeout=30
            )
        os.unlink(fname)
    except Exception as e:
        print(f"send_file error: {e}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
