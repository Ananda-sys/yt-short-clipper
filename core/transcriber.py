import subprocess
import os
import re
import threading
import json
import tempfile
import sys
import time
from pathlib import Path
from openai import OpenAI
from types import SimpleNamespace

# Import debug_log from utils.logger
from utils.logger import debug_log

# Define SUBPROCESS_FLAGS for subprocess calls, mimicking clipper_core.py
SUBPROCESS_FLAGS = 0
if sys.platform == "win32":
    subprocess_has_creationflags = hasattr(subprocess, "CREATE_NO_WINDOW")
    if subprocess_has_creationflags:
        SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW
    else:
        # Fallback for systems where CREATE_NO_WINDOW might not exist (e.g., non-Windows Python)
        debug_log("Warning: subprocess.CREATE_NO_WINDOW not found, using default flags.")

class Transcriber:
    def __init__(
        self,
        ffmpeg_path: str,
        caption_client: OpenAI,
        whisper_model: str,
        ai_providers: dict,
        subtitle_language: str,
        log_callback,
        set_progress_callback,
        report_tokens_callback,
        is_cancelled_callback,
    ):
        self.ffmpeg_path = ffmpeg_path
        self.caption_client = caption_client
        self.whisper_model = whisper_model
        self.ai_providers = ai_providers
        self.subtitle_language = subtitle_language
        self.log = log_callback
        self.set_progress = set_progress_callback
        self.report_tokens = report_tokens_callback
        self.is_cancelled = is_cancelled_callback

    def transcribe_full_video(self, video_path: str) -> str:
        """Transcribe full video audio using Whisper API (Caption Maker).
        
        Extracts audio from the video, compresses to mp3, splits into chunks
        if needed (Whisper API has ~25MB limit), and returns a transcript
        formatted like parse_srt output so find_highlights can consume it directly.
        
        Returns:
            str: Transcript with timestamps in SRT-like format:
                 [HH:MM:SS,mmm - HH:MM:SS,mmm] text
        """
        self.log("[AI Transcription] Transcribing full video with Whisper API...")
        
        # Check Caption Maker is configured
        cm_config = self.ai_providers.get("caption_maker", {})
        if not cm_config.get("api_key"):
            raise Exception(
                "Caption Maker is not configured!\n\n"
                "Please set up Caption Maker in:\n"
                "Settings → AI API Settings → Caption Maker"
            )

        # Gemini route: use native audio endpoint (OpenAI /audio/transcriptions not supported)
        base_url = str(cm_config.get("base_url", "")).lower()
        if "generativelanguage.googleapis.com" in base_url:
            self.log("[AI Transcription] Gemini detected — using native audio API")
            return self._gemini_transcribe_full(video_path, cm_config)
        
        # Extract audio as compressed mp3 to minimize file size
        audio_file = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False).name
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", video_path,
            "-vn",
            "-acodec", "libmp3lame",
            "-ar", "16000",
            "-ac", "1",
            "-b:a", "64k",
            audio_file
        ]
        self.log("  Extracting audio from video...")
        result = subprocess.run(cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
        
        if result.returncode != 0:
            if os.path.exists(audio_file):
                os.unlink(audio_file)
            raise Exception(f"Failed to extract audio from video:\n{result.stderr[:200]}")
        
        file_size_mb = os.path.getsize(audio_file) / (1024 * 1024)
        self.log(f"  Audio file size: {file_size_mb:.1f} MB")
        
        # Get total audio duration
        probe_cmd = [self.ffmpeg_path, "-i", audio_file, "-f", "null", "-"]
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
        duration_match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", probe_result.stderr)
        total_duration = 0
        if duration_match:
            h, m, s = duration_match.groups()
            total_duration = int(h) * 3600 + int(m) * 60 + float(s)
        
        self.log(f"  Audio duration: {total_duration:.0f}s ({total_duration/60:.1f} min)")
        
        # Report Whisper usage
        self.report_tokens(0, 0, total_duration, 0)
        
        # Split into chunks if file is too large (>4MB to avoid proxy timeout)
        MAX_CHUNK_SIZE_MB = 4
        all_segments = []
        
        if file_size_mb <= MAX_CHUNK_SIZE_MB:
            # Single file, transcribe directly
            self.log("  Sending to Whisper API...")
            self.set_progress("Transcribing audio with AI...", 0.3)
            segments = self._whisper_transcribe_file(audio_file, 0)
            all_segments.extend(segments)
        else:
            # Split into chunks by duration
            chunk_count = int(file_size_mb / MAX_CHUNK_SIZE_MB) + 1
            chunk_duration = total_duration / chunk_count
            self.log(f"  File too large, splitting into {chunk_count} chunks (~{chunk_duration:.0f}s each)...")
            
            for i in range(chunk_count):
                if self.is_cancelled():
                    os.unlink(audio_file)
                    return ""
                
                chunk_start = i * chunk_duration
                chunk_file = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False).name
                
                cmd = [
                    self.ffmpeg_path, "-y",
                    "-i", audio_file,
                    "-ss", str(chunk_start),
                    "-t", str(chunk_duration),
                    "-acodec", "libmp3lame",
                    "-ar", "16000",
                    "-ac", "1",
                    "-b:a", "64k",
                    chunk_file
                ]
                subprocess.run(cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
                
                chunk_size = os.path.getsize(chunk_file) / (1024 * 1024)
                self.log(f"  Transcribing chunk {i+1}/{chunk_count} ({chunk_size:.1f}MB, ~{chunk_duration:.0f}s)...")
                self.set_progress(f"Transcribing with AI... chunk {i+1}/{chunk_count}", 
                                  0.3 + (0.2 * (i + 1) / chunk_count))
                
                segments = self._whisper_transcribe_file(chunk_file, chunk_start)
                all_segments.extend(segments)
                
                try:
                    os.unlink(chunk_file)
                except Exception:
                    pass
        
        # Cleanup main audio file
        try:
            os.unlink(audio_file)
        except Exception:
            pass
        
        if not all_segments:
            raise Exception("Whisper API returned empty transcription. The video may have no speech.")
        
        # Format segments into SRT-like transcript (same format as parse_srt output)
        lines = []
        for seg in all_segments:
            start_ts = self._seconds_to_srt_timestamp(seg["start"])
            end_ts = self._seconds_to_srt_timestamp(seg["end"])
            text = seg["text"].strip()
            if text:
                lines.append(f"[{start_ts} - {end_ts}] {text}")
        
        transcript = "\n".join(lines)
        self.log(f"  ✓ Transcription complete: {len(lines)} segments")
        
        return transcript

    def _whisper_transcribe_file(self, audio_path: str, time_offset: float = 0) -> list:
        """Transcribe a single audio file with Whisper API.
        
        Uses raw httpx POST instead of OpenAI SDK for better proxy compatibility.
        
        Args:
            audio_path: Path to audio file
            time_offset: Offset in seconds to add to all timestamps (for chunked files)
        
        Returns:
            list of dicts with 'start', 'end', 'text' keys
        """
        import time as _time
        import requests as _requests
        
        file_size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        base_url = str(self.caption_client.base_url).rstrip("/")
        api_key = self.caption_client.api_key
        
        self.log(f"    Uploading {file_size_mb:.1f}MB to Whisper API ({self.whisper_model})...")
        self.log(f"    Base URL: {base_url}")
        
        # Build multipart form data
        url = f"{base_url}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {api_key}"}
        
        form_data = {
            "model": self.whisper_model,
            "response_format": "verbose_json",
        }
        if self.subtitle_language and self.subtitle_language != "none":
            form_data["language"] = self.subtitle_language
        
        # Run API call in a thread so we can log heartbeat while waiting
        response_data = None
        api_error = None
        
        def _call_api():
            nonlocal response_data, api_error
            try:
                with open(audio_path, "rb") as f:
                    files = {"file": (os.path.basename(audio_path), f, "audio/mpeg")}
                    resp = _requests.post(url, headers=headers, data=form_data, files=files, timeout=600)
                    resp.raise_for_status()
                    response_data = resp.json()
            except Exception as e:
                api_error = e
        
        api_thread = threading.Thread(target=_call_api, daemon=True)
        start_time = _time.time()
        api_thread.start()
        
        # Heartbeat: log every 15s so user knows it's still working
        TIMEOUT_SECONDS = 300  # 5 minutes max per chunk
        while api_thread.is_alive():
            api_thread.join(timeout=15)
            if api_thread.is_alive():
                elapsed = _time.time() - start_time
                
                # Check cancellation
                if self.is_cancelled():
                    self.log(f"    ⚠️ Cancelled by user during Whisper API call")
                    return []
                
                if elapsed > TIMEOUT_SECONDS:
                    self.log(f"    ⏱️ Whisper API timed out after {TIMEOUT_SECONDS}s")
                    raise Exception(
                        f"Whisper API timed out after {TIMEOUT_SECONDS}s.\n\n"
                        "Possible causes:\n"
                        "1. Your AI API provider may not support the Whisper audio endpoint\n"
                        "2. The server may be overloaded or unreachable\n"
                        "3. Network connection issue\n\n"
                        "Try:\n"
                        "- Check if your Caption Maker API supports audio transcription\n"
                        "- Try again later\n"
                        "- Use a different API provider for Caption Maker"
                    )
                self.log(f"    ⏳ Waiting for Whisper API response... ({elapsed:.0f}s elapsed)")
                self.set_progress(f"Transcribing with AI... waiting for response ({elapsed:.0f}s)", 0.35)
        
        elapsed = _time.time() - start_time
        
        if api_error:
            self.log(f"  ❌ Whisper API error after {elapsed:.1f}s: {api_error}")
            raise Exception(f"Whisper transcription failed:\n{str(api_error)}")
        
        if response_data is None:
            self.log(f"  ❌ Whisper API returned no response after {elapsed:.1f}s")
            raise Exception("Whisper API returned no response. The endpoint may not support audio transcription.")
        
        self.log(f"    ✓ Whisper API responded in {elapsed:.1f}s")
        
        segments = []
        if "segments" in response_data and response_data["segments"]:
            for seg in response_data["segments"]:
                segments.append({
                    "start": seg.get("start", 0) + time_offset,
                    "end": seg.get("end", 0) + time_offset,
                    "text": seg.get("text", "")
                })
        
        return segments

    def _whisper_transcribe_words_api(self, audio_path: str):
        """Transcribe an audio file with word-level timestamps using raw HTTP.

        Compresses the audio to MP3 before uploading (the ytclip proxy drops
        connections for large WAV files >~1MB). Uses ``requests`` instead of
        the OpenAI SDK for proxy compatibility. Tries with
        ``timestamp_granularities[]=word`` first; if the proxy rejects it
        (400), retries without that field (still gets segments).

        Returns an object exposing ``.words`` and ``.segments`` (mirroring the
        SDK response shape consumed by ``create_ass_subtitle_capcut``), or
        raises on failure.
        """
        import requests as _requests

        base_url = str(self.caption_client.base_url).rstrip("/")
        api_key = self.caption_client.api_key
        url = f"{base_url}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {api_key}"}

        lang = self.subtitle_language or "id"

        # Compress WAV → MP3 to reduce upload size (proxy rejects large bodies)
        upload_path = audio_path
        mp3_tmp = None
        if audio_path.lower().endswith(".wav"):
            mp3_tmp = audio_path.rsplit(".", 1)[0] + "_upload.mp3"
            cmd = [
                self.ffmpeg_path, "-y",
                "-i", audio_path,
                "-acodec", "libmp3lame",
                "-b:a", "64k",
                "-ar", "16000",
                "-ac", "1",
                mp3_tmp
            ]
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    creationflags=SUBPROCESS_FLAGS)
            if result.returncode == 0 and os.path.exists(mp3_tmp):
                upload_path = mp3_tmp
                self.log(f"  [Caption] Compressed WAV→MP3: "
                         f"{os.path.getsize(audio_path)/1024:.0f}KB → "
                         f"{os.path.getsize(mp3_tmp)/1024:.0f}KB")
            else:
                self.log("  [Caption] MP3 compression failed, uploading WAV as-is")
                mp3_tmp = None

        file_size_mb = os.path.getsize(upload_path) / (1024 * 1024)
        mime = "audio/mpeg" if upload_path.endswith(".mp3") else "audio/wav"
        self.log(f"  [Caption] Uploading {file_size_mb:.2f}MB to Whisper ({self.whisper_model})...")

        # Attempt 1: with word-level granularity
        form_data = [
            ("model", self.whisper_model),
            ("response_format", "verbose_json"),
            ("timestamp_granularities[]", "word"),
            ("timestamp_granularities[]", "segment"),
        ]
        if lang and lang != "none":
            form_data.append(("language", lang))

        resp = None
        for attempt in range(2):
            with open(upload_path, "rb") as f:
                files = {"file": (os.path.basename(upload_path), f, mime)}
                resp = _requests.post(url, headers=headers, data=form_data,
                                      files=files, timeout=600)

            if resp.status_code == 200:
                break

            # Log the actual error body for debugging
            self.log(f"  [Caption] Attempt {attempt+1} failed: HTTP {resp.status_code}")
            try:
                self.log(f"  [Caption] Response: {resp.text[:300]}")
            except Exception:
                pass

            if attempt == 0:
                # Retry without timestamp_granularities (proxy may not support it)
                self.log("  [Caption] Retrying without timestamp_granularities...")
                form_data = [
                    ("model", self.whisper_model),
                    ("response_format", "verbose_json"),
                ]
                if lang and lang != "none":
                    form_data.append(("language", lang))
            else:
                # Both attempts failed — clean up and raise
                if mp3_tmp and os.path.exists(mp3_tmp):
                    os.unlink(mp3_tmp)
                raise Exception(
                    f"Whisper API returned HTTP {resp.status_code}: "
                    f"{resp.text[:300]}"
                )

        # Clean up temp mp3
        if mp3_tmp and os.path.exists(mp3_tmp):
            os.unlink(mp3_tmp)

        data = resp.json()
        self.log(f"  [Caption] Whisper OK, text length: {len(data.get('text', ''))}")

        words = [
            SimpleNamespace(
                word=w.get("word", ""),
                start=w.get("start", 0.0),
                end=w.get("end", 0.0),
            )
            for w in (data.get("words") or [])
        ]
        segments = data.get("segments") or []
        self.log(f"  [Caption] Got {len(words)} words, {len(segments)} segments")
        return SimpleNamespace(words=words, segments=segments,
                               text=data.get("text", ""))

    def _gemini_transcribe_full(self, video_path: str, cm_config: dict) -> str:
        """Transcribe full video using Gemini native audio API (free tier).

        Extracts audio as MP3, sends inline base64 to generateContent, chunks
        for long videos, returns SRT-like transcript (same format as Whisper path).
        """
        import requests as _requests
        import base64 as _b64

        api_key = cm_config.get("api_key", "")
        model = cm_config.get("model", "gemini-flash-latest")
        # Strip OpenAI-compat 'models/' prefix if present (dropdown populates from /models endpoint)
        if model.startswith("models/"):
            model = model[len("models/"):]
        # Base URL may be the OpenAI-compat endpoint; strip to native root
        base_url = str(cm_config.get("base_url", "")).rstrip("/")
        if base_url.endswith("/openai"):
            base_url = base_url[: -len("/openai")]
        native_url = f"{base_url}/models/{model}:generateContent"

        self.log(f"[Gemini] Transcribing with {model}...")

        # Extract audio as compressed mp3 (16kHz mono, 64kbps → ~0.5MB/min)
        audio_file = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False).name
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", video_path,
            "-vn",
            "-acodec", "libmp3lame",
            "-ar", "16000",
            "-ac", "1",
            "-b:a", "64k",
            audio_file
        ]
        self.log("  Extracting audio from video...")
        result = subprocess.run(cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
        if result.returncode != 0:
            if os.path.exists(audio_file):
                os.unlink(audio_file)
            raise Exception(f"Failed to extract audio from video:\n{result.stderr[:200]}")

        size_mb = os.path.getsize(audio_file) / (1024 * 1024)
        self.log(f"  Audio file size: {size_mb:.1f} MB")

        # Probe duration
        probe_cmd = [self.ffmpeg_path, "-i", audio_file, "-f", "null", "-"]
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
        duration_match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", probe_result.stderr)
        total_duration = 0
        if duration_match:
            h, m, s = duration_match.groups()
            total_duration = int(h) * 3600 + int(m) * 60 + float(s)
        self.log(f"  Audio duration: {total_duration:.0f}s ({total_duration/60:.1f} min)")

        # Gemini inline data limit is ~19MB (base64 ~25% overhead). Chunk if needed.
        # 64kbps ≈ 0.48MB/min → ~30min per chunk is safe.
        MAX_CHUNK_MB = 15
        all_segments = []

        def _transcribe_chunk(mp3_path: str, time_offset: float) -> str:
            with open(mp3_path, "rb") as f:
                b64 = _b64.b64encode(f.read()).decode()
            payload = {
                "contents": [{
                    "parts": [
                        {"inline_data": {"mime_type": "audio/mpeg", "data": b64}},
                        {"text": (
                            "Transcribe this audio verbatim. Return ONLY the transcript "
                            "as plain text lines. Do not add commentary."
                        )}
                    ]
                }]
            }
            r = _requests.post(native_url, params={"key": api_key}, json=payload, timeout=600)
            if r.status_code != 200:
                err = r.json().get("error", {}).get("message", r.text[:200])
                raise Exception(f"Gemini transcription failed (HTTP {r.status_code}): {err}")
            body = r.json()
            parts = body.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            text = parts[0].get("text", "") if parts else ""
            self.log(f"  ✓ Gemini chunk OK ({len(text)} chars, "
                     f"{body.get('usageMetadata', {}).get('totalTokenCount', '?')} tokens)")
            return text.strip()

        if size_mb <= MAX_CHUNK_MB:
            self.set_progress("Transcribing with Gemini...", 0.3)
            text = _transcribe_chunk(audio_file, 0)
            if text and text.upper() != "NO_SPEECH":
                all_segments.append((0, total_duration, text))
        else:
            chunk_count = int(size_mb / MAX_CHUNK_MB) + 1
            chunk_duration = total_duration / chunk_count
            self.log(f"  File too large, splitting into {chunk_count} chunks (~{chunk_duration:.0f}s each)...")
            for i in range(chunk_count):
                if self.is_cancelled():
                    os.unlink(audio_file)
                    return ""
                chunk_start = i * chunk_duration
                chunk_file = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False).name
                cmd = [
                    self.ffmpeg_path, "-y",
                    "-i", audio_file,
                    "-ss", str(chunk_start),
                    "-t", str(chunk_duration),
                    "-acodec", "libmp3lame",
                    "-ar", "16000",
                    "-ac", "1",
                    "-b:a", "64k",
                    chunk_file
                ]
                subprocess.run(cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
                self.set_progress(f"Transcribing with Gemini... chunk {i+1}/{chunk_count}",
                                  0.3 + (0.2 * (i + 1) / chunk_count))
                try:
                    text = _transcribe_chunk(chunk_file, chunk_start)
                    if text and text.upper() != "NO_SPEECH":
                        all_segments.append((chunk_start, chunk_start + chunk_duration, text))
                finally:
                    try:
                        os.unlink(chunk_file)
                    except Exception:
                        pass

        try:
            os.unlink(audio_file)
        except Exception:
            pass

        if not all_segments:
            raise Exception("Gemini returned empty transcription. The video may have no speech.")

        # Format into SRT-like transcript
        lines = []
        for start, end, text in all_segments:
            start_ts = self._seconds_to_srt_timestamp(start)
            end_ts = self._seconds_to_srt_timestamp(end)
            lines.append(f"[{start_ts} - {end_ts}] {text}")
        transcript = "\n".join(lines)
        self.log(f"  ✓ Gemini transcription complete: {len(lines)} segments")
        return transcript

    @staticmethod
    def _seconds_to_srt_timestamp(seconds: float) -> str:
        """Convert seconds to SRT timestamp format HH:MM:SS,mmm"""
        if seconds < 0:
            seconds = 0
        
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        remaining_seconds = seconds % 60
        milliseconds = int((remaining_seconds - int(remaining_seconds)) * 1000)
        
        return f"{hours:02}:{minutes:02}:{int(remaining_seconds):02},{milliseconds:03}"
