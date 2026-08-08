"""
Flask-based web server for YT Short Clipper
Serves the web UI and exposes REST API endpoints
"""
import threading
import base64
import requests
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory, send_file
from flask_cors import CORS
from config.config_manager import ConfigManager
from utils.helpers import get_app_dir, get_bundle_dir, get_ffmpeg_path, get_ytdlp_path
from clipper_core import AutoClipperCore


app = Flask(__name__, static_folder='web', static_url_path='')
CORS(app)  # Enable CORS for cross-origin requests


class WebAPI:
    """
    Web API class - same logic as webview_app.py but adapted for Flask
    """
    def __init__(self):
        app_dir = get_app_dir()
        self.config_file = str(app_dir / "config.json")
        self.output_dir = str(app_dir / "output")
        self.status = "idle"
        self.progress = 0.0
        self.thread = None

    def get_progress(self):
        return {"status": self.status, "progress": self.progress}

    def get_icon_data(self):
        try:
            bundle_dir = get_bundle_dir()
            icon_path = Path(bundle_dir) / "assets" / "icon.png"
            if not icon_path.exists():
                return {"data": ""}
            raw = icon_path.read_bytes()
            encoded = base64.b64encode(raw).decode("utf-8")
            return {"data": f"data:image/png;base64,{encoded}"}
        except:
            return {"data": ""}

    def get_ai_settings(self):
        cfg = self._get_cfg()
        return cfg.get("ai_providers", {})

    def get_provider_type(self):
        cfg = self._get_cfg()
        return {"provider_type": cfg.get("provider_type", "ytclip")}

    def validate_api_key(self, base_url, api_key):
        if not base_url:
            return {"status": "error", "message": "Missing base URL"}
        if not api_key:
            return {"status": "error", "message": "Missing API key"}
        url = self._get_models_url(base_url)
        try:
            resp = requests.get(url, headers=self._auth_headers(api_key), timeout=10)
            if resp.status_code == 200:
                return {"status": "ok"}
            return {"status": "error", "message": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def get_models(self, base_url, api_key):
        if not base_url:
            return {"models": []}
        url = self._get_models_url(base_url)
        try:
            resp = requests.get(url, headers=self._auth_headers(api_key), timeout=15)
            if resp.status_code != 200:
                return {"models": []}
            data = resp.json()
            items = data.get("data", [])
            models = []
            for item in items:
                mid = item.get("id")
                if mid:
                    models.append(mid)
            return {"models": models}
        except:
            return {"models": []}

    def save_ai_settings(self, settings):
        if not isinstance(settings, dict):
            return {"status": "error"}
        cfg_mgr = self._get_cfg_manager()
        cfg_mgr.config["ai_providers"] = settings
        provider_type = settings.get("_provider_type")
        if provider_type:
            cfg_mgr.config["provider_type"] = provider_type
        highlight_finder = settings.get("highlight_finder", {})
        cfg_mgr.config["api_key"] = highlight_finder.get("api_key", "")
        cfg_mgr.config["base_url"] = highlight_finder.get("base_url", "https://api.openai.com/v1")
        cfg_mgr.config["model"] = highlight_finder.get("model", "gpt-4.1")
        cfg_mgr.save()
        return {"status": "saved"}

    def start_processing(self, url, num_clips=5, add_captions=True, add_hook=False, subtitle_lang="id"):
        if self.thread and self.thread.is_alive():
            return {"status": "busy"}
        self.thread = threading.Thread(
            target=self._run,
            args=(url, int(num_clips), bool(add_captions), bool(add_hook), subtitle_lang),
            daemon=True,
        )
        self.thread.start()
        return {"status": "started"}

    def _run(self, url, num_clips, add_captions, add_hook, subtitle_lang):
        def log_cb(msg):
            self.status = str(msg)

        def progress_cb(p):
            try:
                self.progress = float(p)
            except:
                self.progress = 0.0

        cfg = self._get_cfg()
        system_prompt = cfg.get("system_prompt", None)
        temperature = cfg.get("temperature", 1.0)
        tts_model = cfg.get("tts_model", "tts-1")
        watermark_settings = cfg.get("watermark", {"enabled": False})
        credit_watermark_settings = cfg.get("credit_watermark", {"enabled": False})
        hook_style_settings = cfg.get("hook_style", {})
        face_tracking_mode = cfg.get("face_tracking_mode", "opencv")
        mediapipe_settings = cfg.get("mediapipe_settings", {
            "lip_activity_threshold": 0.15,
            "switch_threshold": 0.3,
            "min_shot_duration": 90,
            "center_weight": 0.3
        })
        output_dir = cfg.get("output_dir", str(get_app_dir() / "output"))
        model = cfg.get("model", "gpt-4.1")
        ai_providers = cfg.get("ai_providers")

        core = AutoClipperCore(
            client=None,
            ffmpeg_path=get_ffmpeg_path(),
            ytdlp_path=get_ytdlp_path(),
            output_dir=output_dir,
            model=model,
            tts_model=tts_model,
            temperature=temperature,
            system_prompt=system_prompt,
            watermark_settings=watermark_settings,
            credit_watermark_settings=credit_watermark_settings,
            hook_style_settings=hook_style_settings,
            face_tracking_mode=face_tracking_mode,
            mediapipe_settings=mediapipe_settings,
            ai_providers=ai_providers,
            subtitle_language=subtitle_lang,
            log_callback=log_cb,
            progress_callback=lambda s, p=None: progress_cb(p if p is not None else 0.0),
        )
        try:
            self.status = "running"
            self.progress = 0.0
            core.process(url, num_clips=num_clips, add_captions=add_captions, add_hook=add_hook)
            self.status = "complete"
            self.progress = 1.0
        except Exception as e:
            self.status = f"error: {e}"
        finally:
            self.thread = None

    def _get_cfg_manager(self):
        return ConfigManager(Path(self.config_file), Path(self.output_dir))

    def _get_cfg(self):
        cfg_mgr = self._get_cfg_manager()
        return cfg_mgr.get_all() if hasattr(cfg_mgr, "get_all") else cfg_mgr.config

    def _get_models_url(self, base_url):
        url = base_url.rstrip("/")
        # Gemini's OpenAI-compatible endpoint already includes /openai
        if "generativelanguage.googleapis.com" in url:
            return f"{url}/models"
        if url.endswith("/v1"):
            return f"{url}/models"
        return f"{url}/v1/models"

    def _auth_headers(self, api_key):
        return {"Authorization": f"Bearer {api_key}"}


# Create global API instance
api = WebAPI()


# Flask Routes

@app.route('/')
def index():
    """Serve the main HTML page"""
    return send_from_directory('web', 'index.html')


@app.route('/<path:path>')
def serve_static(path):
    """Serve static files from web/ directory"""
    return send_from_directory('web', path)


@app.route('/api/progress', methods=['GET'])
def get_progress():
    """Get current processing progress"""
    return jsonify(api.get_progress())


@app.route('/api/start', methods=['POST'])
def start_processing():
    """Start video processing"""
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No JSON data provided"}), 400
    
    url = data.get('url', '')
    num_clips = data.get('num_clips', 5)
    add_captions = data.get('add_captions', True)
    add_hook = data.get('add_hook', False)
    subtitle_lang = data.get('subtitle_lang', 'id')
    
    result = api.start_processing(url, num_clips, add_captions, add_hook, subtitle_lang)
    return jsonify(result)


@app.route('/api/ai-settings', methods=['GET'])
def get_ai_settings():
    """Get AI provider settings"""
    return jsonify(api.get_ai_settings())


@app.route('/api/ai-settings', methods=['POST'])
def save_ai_settings():
    """Save AI provider settings"""
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No JSON data provided"}), 400
    
    result = api.save_ai_settings(data)
    return jsonify(result)


@app.route('/api/validate-key', methods=['POST'])
def validate_key():
    """Validate API key"""
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No JSON data provided"}), 400
    
    base_url = data.get('base_url', '')
    api_key = data.get('api_key', '')
    
    result = api.validate_api_key(base_url, api_key)
    return jsonify(result)


@app.route('/api/models', methods=['POST'])
def get_models():
    """Get available models from API provider"""
    data = request.get_json()
    if not data:
        return jsonify({"models": []}), 400
    
    base_url = data.get('base_url', '')
    api_key = data.get('api_key', '')
    
    result = api.get_models(base_url, api_key)
    return jsonify(result)


@app.route('/api/provider-type', methods=['GET'])
def get_provider_type():
    """Get current provider type"""
    return jsonify(api.get_provider_type())


@app.route('/api/icon', methods=['GET'])
def get_icon():
    """Get application icon as base64 data URL"""
    return jsonify(api.get_icon_data())


if __name__ == '__main__':
    print("=" * 60)
    print("YT Short Clipper Web Server")
    print("=" * 60)
    print(f"Server starting on http://0.0.0.0:5000")
    print(f"Access from this PC: http://localhost:5000")
    print(f"Access from network: http://<your-ip>:5000")
    print("=" * 60)
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
