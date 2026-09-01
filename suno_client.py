import os
import requests
import json
from dotenv import load_dotenv

load_dotenv()

class SunoClient:
    def __init__(self, base_url=None, api_key=None):
        self.base_url = base_url or os.getenv("SUNO_API_URL", "https://api.sunoapi.org")
        self.api_key = api_key or os.getenv("SUNO_API_KEY", "")
        self.model = os.getenv("SUNO_API_MODEL", "V5_5") # Default to Suno V5
        self.callback_url = os.getenv("SUNO_CALLBACK_URL", "https://api.sunoapi.org/dummy-callback")

    def _get_headers(self):
        headers = {
            "Content-Type": "application/json"
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def generate(self, prompt, make_instrumental=False, persona_id=None):
        url = f"{self.base_url}/api/v1/generate"
        payload = {
            "prompt": prompt,
            "customMode": False,
            "instrumental": make_instrumental,
            "model": self.model,
            "callBackUrl": self.callback_url
        }
        if persona_id:
            payload["personaId"] = persona_id
            payload["personaModel"] = "style_persona"
        response = requests.post(url, json=payload, headers=self._get_headers())
        response.raise_for_status()
        return response.json()

    def custom_generate(
        self,
        prompt,
        tags,
        title,
        make_instrumental=False,
        persona_id=None,
        model=None,
        duration=None,
        negative_tags=None,
        vocal_gender=None,
        style_weight=None,
        weirdness_constraint=None,
        audio_weight=None,
    ):
        url = f"{self.base_url}/api/v1/generate"
        payload = {
            "prompt": prompt,
            "customMode": True,
            "style": tags,
            "title": title,
            "instrumental": make_instrumental,
            "model": model or self.model,
            "callBackUrl": self.callback_url
        }
        if persona_id:
            payload["personaId"] = persona_id
            payload["personaModel"] = "style_persona"

        # These advanced controls are optional in the Suno API. Omitting an
        # untouched control preserves the provider's defaults.
        optional_parameters = {
            "duration": duration,
            "negativeTags": negative_tags,
            "vocalGender": vocal_gender,
            "styleWeight": style_weight,
            "weirdnessConstraint": weirdness_constraint,
            "audioWeight": audio_weight,
        }
        payload.update({key: value for key, value in optional_parameters.items() if value is not None})
        response = requests.post(url, json=payload, headers=self._get_headers())
        response.raise_for_status()
        return response.json()

    def extend_audio(
        self, upload_url, prompt, persona_id=None, style="", title="", continue_at=None,
        make_instrumental=False, negative_tags=None, vocal_gender=None,
        style_weight=None, weirdness_constraint=None, audio_weight=None,
    ):
        # The studio accepts uploaded files and public URLs, so use the
        # upload-extend endpoint (the /extend endpoint accepts Suno audio IDs).
        url = f"{self.base_url}/api/v1/generate/upload-extend"
        payload = {
            "uploadUrl": upload_url,
            "defaultParamFlag": True,
            "model": self.model,
            "callBackUrl": self.callback_url
        }
        if not make_instrumental:
            payload["prompt"] = prompt
        custom_parameters = {
            "style": style,
            "title": title,
            "continueAt": continue_at,
            "instrumental": make_instrumental,
            "negativeTags": negative_tags,
            "vocalGender": vocal_gender,
            "styleWeight": style_weight,
            "weirdnessConstraint": weirdness_constraint,
            "audioWeight": audio_weight,
        }
        payload.update({key: value for key, value in custom_parameters.items() if value is not None})
        if persona_id:
            payload["personaId"] = persona_id
            payload["personaModel"] = "style_persona"
        response = requests.post(url, json=payload, headers=self._get_headers())
        response.raise_for_status()
        return response.json()

    def get_details(self, task_id):
        """
        Takes a single task_id (string) returned by generation.
        Returns the task status and audio details.
        """
        url = f"{self.base_url}/api/v1/generate/record-info?taskId={task_id}"
        headers = self._get_headers()
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()

    def start_audio_recovery(self, suno_task_id, callback_url=None):
        """Create a recovery task for all tracks from one original Suno task."""
        callback_url = callback_url or self.callback_url
        if not suno_task_id:
            raise ValueError("The original Suno task ID is required for audio recovery.")
        if not callback_url:
            raise ValueError("SUNO_CALLBACK_URL is required by the recovery API.")
        response = requests.post(
            f"{self.base_url}/api/v1/suno/recovery",
            json={"sunoTaskId": suno_task_id, "callBackUrl": callback_url},
            headers=self._get_headers(), timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def get_audio_recovery_details(self, recovery_task_id):
        """Return the dedicated recovery result (not normal generation status)."""
        response = requests.get(
            f"{self.base_url}/api/v1/suno/recovery/record-info",
            params={"task_id": recovery_task_id}, headers=self._get_headers(), timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def upload_file(self, uploaded_file):
        """Upload a local file to Suno's file service and return a public URL."""
        upload_base_url = os.getenv(
            "SUNO_FILE_API_URL", "https://sunoapiorg.redpandaai.co"
        ).rstrip("/")
        files = {
            "file": (
                uploaded_file.name,
                uploaded_file.getvalue(),
                uploaded_file.type or "audio/mpeg",
            )
        }
        response = requests.post(
            f"{upload_base_url}/api/file-stream-upload",
            headers={"Authorization": f"Bearer {self.api_key}"},
            files=files,
            data={"uploadPath": "audio-uploads", "fileName": uploaded_file.name},
        )
        response.raise_for_status()
        data = response.json().get("data", {})
        file_url = data.get("fileUrl") or data.get("downloadUrl")
        if not file_url:
            raise ValueError("The file upload succeeded but did not return a public file URL.")
        return file_url

    def generate_cover(
        self, upload_url, prompt="", style="", title="", make_instrumental=False,
        persona_id=None, duration=None, negative_tags=None, vocal_gender=None,
        style_weight=0.65, weirdness_constraint=0.65, audio_weight=0.65,
    ):
        """
        Uploads audio to Suno via a public URL and generates a cover.
        """
        url = f"{self.base_url}/api/v1/generate/upload-cover"
        payload = {
            "uploadUrl": upload_url,
            "customMode": True,
            "prompt": prompt,
            "style": style,
            "title": title,
            "instrumental": make_instrumental,
            "model": self.model,
            "audioWeight": audio_weight,
            "styleWeight": style_weight,
            "weirdnessConstraint": weirdness_constraint,
            "callBackUrl": self.callback_url
        }
        if persona_id:
            payload["personaId"] = persona_id
            payload["personaModel"] = "style_persona"
        optional_parameters = {
            "duration": duration,
            "negativeTags": negative_tags,
            "vocalGender": vocal_gender,
        }
        payload.update({key: value for key, value in optional_parameters.items() if value is not None})
        response = requests.post(url, json=payload, headers=self._get_headers())
        response.raise_for_status()
        return response.json()

    def add_vocals(
        self, upload_url, prompt, style, title, negative_tags="None", vocal_gender=None,
        style_weight=0.65, weirdness_constraint=0.65, audio_weight=0.65, model=None,
    ):
        """Add generated vocals to an uploaded instrumental track."""
        payload = {
            "uploadUrl": upload_url,
            "prompt": prompt,
            "style": style,
            "title": title,
            "negativeTags": negative_tags,
            "model": model or self.model,
            "audioWeight": audio_weight,
            "styleWeight": style_weight,
            "weirdnessConstraint": weirdness_constraint,
            "callBackUrl": self.callback_url,
        }
        if vocal_gender:
            payload["vocalGender"] = vocal_gender
        response = requests.post(
            f"{self.base_url}/api/v1/generate/add-vocals",
            json=payload,
            headers=self._get_headers(),
        )
        response.raise_for_status()
        return response.json()

    def add_instrumental(
        self, upload_url, tags, title, negative_tags="None", vocal_gender=None,
        style_weight=0.65, weirdness_constraint=0.65, audio_weight=0.65, model=None,
    ):
        """Add instrumental accompaniment to an uploaded vocal track."""
        payload = {
            "uploadUrl": upload_url,
            "tags": tags,
            "title": title,
            "negativeTags": negative_tags,
            "model": model or self.model,
            "audioWeight": audio_weight,
            "styleWeight": style_weight,
            "weirdnessConstraint": weirdness_constraint,
            "callBackUrl": self.callback_url,
        }
        if vocal_gender:
            payload["vocalGender"] = vocal_gender
        response = requests.post(
            f"{self.base_url}/api/v1/generate/add-instrumental",
            json=payload,
            headers=self._get_headers(),
        )
        response.raise_for_status()
        return response.json()

    def separate_stems(self, *, task_id=None, audio_id=None, audio_url=None,
                      separation_type="separate_vocal", stem_name=None):
        """Start a vocal/instrument stem-separation task."""
        if audio_id and audio_url:
            raise ValueError("Provide either audio_id or audio_url, not both.")
        if audio_id and not task_id:
            raise ValueError("task_id is required when separating a generated library track.")
        if not audio_id and not audio_url:
            raise ValueError("Provide a generated audio_id or a public audio_url.")

        payload = {
            "callBackUrl": self.callback_url,
            "type": separation_type,
        }
        if audio_id:
            payload["taskId"] = task_id
            payload["audioId"] = audio_id
        else:
            payload["audioUrl"] = audio_url
        if separation_type == "split_stem_advanced" and stem_name:
            payload["stemName"] = stem_name

        response = requests.post(
            f"{self.base_url}/api/v1/vocal-removal/generate",
            json=payload,
            headers=self._get_headers(),
        )
        response.raise_for_status()
        return response.json()

    def get_stem_details(self, task_id):
        """Return the status and output URLs for a stem-separation task."""
        response = requests.get(
            f"{self.base_url}/api/v1/vocal-removal/record-info",
            params={"taskId": task_id},
            headers=self._get_headers(),
        )
        response.raise_for_status()
        return response.json()

    def generate_persona(self, task_id, audio_id, name, description, vocal_start=0, vocal_end=30, style=""):
        """
        Create a personalized music Persona based on generated music.
        """
        url = f"{self.base_url}/api/v1/generate/generate-persona"
        payload = {
            "taskId": task_id,
            "audioId": audio_id,
            "name": name,
            "description": description,
            "vocalStart": vocal_start,
            "vocalEnd": vocal_end,
            "style": style
        }
        response = requests.post(url, json=payload, headers=self._get_headers())
        response.raise_for_status()
        return response.json()
