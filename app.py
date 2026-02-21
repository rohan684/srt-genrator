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
    return "SRT Bot Running!", 200

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
        if "video" in message:
            file_size_mb = message["video"].get("file_size", 0) / (1024 * 1024)
            if file_size_mb > 20:
                send_message(chat_id, "⚠️ Video too large. Please send under 20MB.")
                return "OK", 200
            send_message(chat_id, "📥 Video received. Preparing subtitles...")
            file_id = message["video"]["file_id"]
            file_url = get_telegram_file_url(file_id)
            handle_video(chat_id, file_url)
            return "OK", 200

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
            send_message(chat_id, "📎 Send a YouTube/Instagram link or upload a video.")
            return "OK", 200

        url = url.strip()

        if any(x in url for x in ["youtube.com", "youtu.be", "m.youtube.com"]):
            send_message(chat_id, "🎬 Fetching audio from YouTube...")
            audio_bytes = download_audio_ytdlp(url)
            if not audio_bytes:
                send_message(chat_id, "❌ Could not extract audio. Try a different link.")
                return "OK", 200
            handle_audio_file(chat_id, audio_bytes)
            return "OK", 200

        if any(x in url for x in ["instagram.com/reel", "instagram.com/p/", "instagram.com/tv/"]):
            send_message(chat_id, "📸 Fetching audio from Instagram...")
            audio_bytes = download_audio_ytdlp(url)
            if not audio_bytes:
                send_message(chat_id, "❌ Could not extract audio. Make sure the reel is public.")
                return "OK", 200
            handle_audio_file(chat_id, audio_bytes)
            return "OK", 200

        send_message(chat_id, "⚠️ Unsupported link.\n\nSupported:\n• YouTube / Shorts\n• Instagram Reels\n• Upload video file (under 20MB)")

    except Exception as e:
        print(f"Webhook error: {e}")
        send_message(chat_id, f"🛑 Error: {str(e)}")

    return "OK", 200


def download_audio_ytdlp(url):
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "audio.mp3")
            cmd = [
                "yt-dlp",
                "--extract-audio",
                "--audio-format", "mp3",
                "--audio-quality", "0",
                "--no-playlist",
                "-o", output_path,
                url
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            print(f"yt-dlp stdout: {result.stdout}")
            print(f"yt-dlp stderr: {result.stderr}")
            if result.returncode == 0 and os.path.exists(output_path):
                with open(output_path, 'rb') as f:
                    return f.read()
            return None
    except Exception as e:
        print(f"yt-dlp error: {e}")
        return None


def handle_video(chat_id, video_url):
    try:
        send_message(chat_id, "🎵 Processing video...")
        r = requests.get(video_url)
        uploaded = upload_to_gemini(r.content, "video/mp4")
        if not uploaded:
            send_message(chat_id, "❌ Failed to upload video to Gemini.")
            return
        generate_subtitles(chat_id, uploaded, "video/mp4")
    except Exception as e:
        send_message(chat_id, f"🛑 Video processing failed: {str(e)}")


def handle_audio_file(chat_id, audio_bytes):
    try:
        send_message(chat_id, "✍️ Generating subtitles...")
        uploaded = upload_to_gemini(audio_bytes, "audio/mpeg")
        if not uploaded:
            send_message(chat_id, "❌ Failed to upload audio to Gemini.")
            return
        generate_subtitles(chat_id, uploaded, "audio/mpeg")
    except Exception as e:
        send_message(chat_id, f"🛑 Audio processing failed: {str(e)}")


def upload_to_gemini(file_bytes, mime_type):
    try:
        upload_url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={GEMINI_KEY}"
        r = requests.post(
            upload_url,
            headers={"X-Goog-Upload-Protocol": "multipart"},
            files={
                'metadata': ('metadata', '{"file": {"displayName": "media_file"}}', 'application/json'),
                'file': ('media_file', file_bytes, mime_type)
            }
        )
        result = r.json()
        print(f"Gemini upload: {result}")
        if result.get("file", {}).get("uri"):
            return result["file"]["uri"]
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
            "generationConfig": {"temperature": 0.1, "topP": 0.8, "topK": 10}
        }
        r = requests.post(url, json=payload)
        result = r.json()
        if not result.get("candidates"):
            send_message(chat_id, "❌ AI failed to process the media.")
            return
        srt = result["candidates"][0]["content"]["parts"][0]["text"]
        srt = srt.replace("```srt", "").replace("```", "").strip()
        if len(srt) < 10:
            send_message(chat_id, "❌ Generated subtitles are empty.")
            return
        send_file(chat_id, srt)
        send_message(chat_id, "✅ Subtitles generated successfully!")
    except Exception as e:
        send_message(chat_id, f"🛑 Subtitle generation failed: {str(e)}")


def get_telegram_file_url(file_id):
    r = requests.get(f"{TELEGRAM_API}/getFile?file_id={file_id}")
    file_path = r.json()["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"


def send_message(chat_id, text):
    requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text})


def send_file(chat_id, srt_text):
    with tempfile.NamedTemporaryFile(mode='w', suffix='.srt', delete=False, encoding='utf-8') as f:
        f.write(srt_text)
        fname = f.name
    with open(fname, 'rb') as f:
        requests.post(
            f"{TELEGRAM_API}/sendDocument",
            data={"chat_id": chat_id},
            files={"document": ("subtitles.srt", f, "text/plain")}
        )
    os.unlink(fname)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
