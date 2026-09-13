import streamlit as st
import time
import json
import requests
import streamlit.components.v1 as components
from suno_client import SunoClient
from suno_backup import SunoBackupManager, audio_url as track_audio_url, is_affected_period, local_backup_exists, utc_now
import os
import uuid
import io
import re
import zipfile
from urllib.parse import urlparse

st.set_page_config(page_title="Suno Music Studio", page_icon="🎹", layout="wide")

st.markdown("""
<style>
.stApp {
    background-color: #0d1117;
    color: #c9d1d9;
}
/* Style the buttons to be blue */
div.stButton > button {
    background-color: #1f6feb;
    color: white;
    border: none;
    border-radius: 8px;
    padding: 0.5rem 1rem;
    font-weight: 600;
}
div.stButton > button:hover {
    background-color: #388bfd;
    color: white;
    border: none;
}
</style>
""", unsafe_allow_html=True)

st.title("🎹 Suno-Powered Music Studio (V6)")
st.caption("Create, Extend, and Remix your tracks seamlessly.")

# Initialize Session State
if 'suno_client' not in st.session_state:
    st.session_state.suno_client = SunoClient()

if 'active_tasks' not in st.session_state:
    st.session_state.active_tasks = {}
elif isinstance(st.session_state.active_tasks, set):
    # Keep generation tasks started before workspace support working.
    st.session_state.active_tasks = {task_id: None for task_id in st.session_state.active_tasks}

if 'active_stem_tasks' not in st.session_state:
    st.session_state.active_stem_tasks = {}

if "selected_workspace_id" not in st.session_state:
    st.session_state.selected_workspace_id = None

if "pending_workspace_source" in st.session_state:
    pending_source = st.session_state.pop("pending_workspace_source")
    source_type_key = {
        "extend": "ext_source_type",
        "cover": "cov_source_type",
        "overlay": "over_source_type"
    }[pending_source["mode"]]
    source_track_key = {
        "extend": "ext_track_id",
        "cover": "cov_track_id",
        "overlay": "over_track_id"
    }[pending_source["mode"]]
    st.session_state[source_type_key] = "Library Track ID"
    st.session_state[source_track_key] = pending_source["track_id"]

# Current V6 models are listed first; retained models remain selectable for
# existing workflows that depend on Suno API's backward compatibility.
CURRENT_SUNO_MODELS = ["V6", "V6_WILD", "V6_MINI"]
DEPRECATED_SUNO_MODELS = ["V5_5", "V5", "V4_5ALL", "V4_5PLUS", "V4_5", "V4"]
SUNO_MODELS = CURRENT_SUNO_MODELS + DEPRECATED_SUNO_MODELS
DURATION_MODELS = {"V5_5", *CURRENT_SUNO_MODELS}

# Global Model Selection
st.sidebar.title("⚙️ Configuration")
selected_model = st.sidebar.selectbox(
    "Select Suno Model",
    options=SUNO_MODELS,
    index=0,
    help="V6 is the recommended default. Legacy models remain available for backward compatibility."
)
st.session_state.suno_client.model = selected_model

# Helpers
def load_history():
    if not os.path.exists("history.json"):
        return []
    try:
        with open("history.json", "r") as f:
            return json.load(f)
    except:
        return []

def save_history(history):
    temp_path = "history.json.tmp"
    with open(temp_path, "w") as f:
        json.dump(history, f, indent=4)
    os.replace(temp_path, "history.json")

def backup_manager():
    return SunoBackupManager()

def upsert_tracks(tracks):
    """Persist provider metadata and durable-backup state without dropping old fields."""
    history = load_history()
    by_id = {track.get("id"): track for track in history}
    for track in tracks:
        existing = by_id.get(track.get("id"))
        if existing:
            preserved_backup = existing.get("backup")
            existing.update(track)
            if preserved_backup and not track.get("backup"):
                existing["backup"] = preserved_backup
        else:
            history.append(track)
            by_id[track.get("id")] = track
    save_history(history)
    return history

def backup_completed_tracks(tracks, recovery_task_id=None):
    manager = backup_manager()
    results = []
    for track in tracks:
        success, message = manager.backup_track(track, recovery_task_id=recovery_task_id)
        results.append((track.get("id"), success, message))
    upsert_tracks(tracks)
    return results

def recovery_record(track):
    return track.setdefault("recovery", {})

def submit_recovery_for_task(original_task_id, tracks):
    """Create at most one active recovery task for an original generation task."""
    existing = [recovery_record(track) for track in tracks]
    active = next((record for record in existing if record.get("status") in {"submitted", "running"}), None)
    if active:
        return False, f"Recovery already active: {active.get('recoveryTaskId')}", active.get("recoveryTaskId")
    response = st.session_state.suno_client.start_audio_recovery(original_task_id)
    recovery_task_id = (response.get("data") or {}).get("task_id") if isinstance(response, dict) else None
    if not recovery_task_id:
        raise ValueError("Recovery API did not return data.task_id.")
    for track in tracks:
        recovery_record(track).update({
            "status": "submitted", "recoveryTaskId": recovery_task_id,
            "originalTaskId": original_task_id, "submittedAt": utc_now(), "lastError": None,
        })
    upsert_tracks(tracks)
    return True, "Recovery submitted.", recovery_task_id

def poll_recovery_and_backup(recovery_task_id, original_task_id, max_attempts=120):
    """Poll the dedicated recovery endpoint and save every recovered audio file."""
    client = st.session_state.suno_client
    for _ in range(max_attempts):
        response = client.get_audio_recovery_details(recovery_task_id)
        code = response.get("code") if isinstance(response, dict) else None
        if code == 201:
            time.sleep(2)
            continue
        history = load_history()
        task_tracks = [track for track in history if track.get("taskId") == original_task_id]
        if code == 200 and isinstance(response.get("data"), list):
            results = {item.get("id"): item for item in response["data"] if isinstance(item, dict)}
            for track in task_tracks:
                item = results.get(track.get("id"), {})
                record = recovery_record(track)
                if item.get("status") == "success" and item.get("audio_url"):
                    track["audioUrl"] = item["audio_url"]
                    record.update({"status": "recovered", "recoveryTaskId": recovery_task_id,
                                   "completedAt": utc_now(), "lastError": None})
                    backup_manager().backup_track(track, recovery_task_id=recovery_task_id)
                else:
                    record.update({"status": "failed", "recoveryTaskId": recovery_task_id,
                                   "completedAt": utc_now(), "lastError": item.get("error") or "No recovered audio URL returned."})
            save_history(history)
            return True, results
        error = response.get("msg") if isinstance(response, dict) else "Unexpected recovery response."
        for track in task_tracks:
            recovery_record(track).update({"status": "failed", "recoveryTaskId": recovery_task_id,
                                           "completedAt": utc_now(), "lastError": error})
        save_history(history)
        return False, error
    return False, "Recovery is still running; use Poll Recovery again later."

def load_voice_stems():
    if not os.path.exists("voice_stems.json"):
        return []
    try:
        with open("voice_stems.json", "r") as f:
            return json.load(f)
    except:
        return []

def save_voice_stems(stems):
    temp_path = "voice_stems.json.tmp"
    with open(temp_path, "w") as f:
        json.dump(stems, f, indent=4)
    os.replace(temp_path, "voice_stems.json")

def load_workspaces():
    if not os.path.exists("workspaces.json"):
        return []
    try:
        with open("workspaces.json", "r") as f:
            return json.load(f)
    except:
        return []

def save_workspaces(workspaces):
    with open("workspaces.json", "w") as f:
        json.dump(workspaces, f, indent=4)

def get_workspace_name(workspace_id, workspaces=None):
    if not workspace_id:
        return "Unassigned"
    for workspace in workspaces or load_workspaces():
        if workspace.get("id") == workspace_id:
            return workspace.get("name", "Unnamed Workspace")
    return "Unassigned"

def render_workspace_selector(key_prefix):
    workspace_options = {"Unassigned": None}
    for workspace in load_workspaces():
        workspace_options[workspace.get("name", "Unnamed Workspace")] = workspace.get("id")
    selected_name = st.selectbox(
        "Save to Workspace",
        options=list(workspace_options.keys()),
        key=f"{key_prefix}_workspace"
    )
    return workspace_options[selected_name]

def build_workspace_options(workspaces, include_unassigned=True, exclude_workspace_id=None):
    workspace_options = {}
    if include_unassigned and exclude_workspace_id is not None:
        workspace_options["Unassigned"] = None
    for workspace in workspaces:
        workspace_id = workspace.get("id")
        if workspace_id != exclude_workspace_id:
            workspace_options[workspace.get("name", "Unnamed Workspace")] = workspace_id
    return workspace_options

def move_tracks_to_workspace(history, target_workspace_id, source_workspace_id=None, track_id=None):
    moved_count = 0
    for saved_track in history:
        is_selected_track = track_id is not None and saved_track.get("id") == track_id
        is_source_workspace_track = (
            track_id is None
            and source_workspace_id is not None
            and saved_track.get("workspaceId") == source_workspace_id
        )
        if is_selected_track or is_source_workspace_track:
            saved_track["workspaceId"] = target_workspace_id
            moved_count += 1
    return moved_count

def load_personas():
    if not os.path.exists("personas.json"):
        return []
    try:
        with open("personas.json", "r") as f:
            return json.load(f)
    except:
        return []

def save_personas(personas):
    with open("personas.json", "w") as f:
        json.dump(personas, f, indent=4)

def render_persona_selector(key_prefix):
    personas = load_personas()
    persona_options = {"None": None}
    for p in personas:
        persona_options[f"{p.get('name')} ({p.get('personaId')})"] = p.get('personaId')
    
    selected_persona_key = st.selectbox("Apply Persona (Optional)", options=list(persona_options.keys()), key=f"{key_prefix}_persona")
    return persona_options[selected_persona_key]

def extract_task_ids(res):
    if isinstance(res, dict):
        data = res.get("data")
        if isinstance(data, dict) and "taskId" in data:
            return [data["taskId"]]
        elif "taskId" in res:
            return [res["taskId"]]
    return []

def tracks_from_task_details(details, task_id):
    """Extract generated tracks from a completed generation-status response."""
    if not isinstance(details, dict) or not isinstance(details.get("data"), dict):
        return []

    data = details["data"]
    response_obj = data.get("response")
    if isinstance(response_obj, list):
        tracks = response_obj
    elif isinstance(response_obj, dict):
        tracks = response_obj.get("sunoData", response_obj.get("data", []))
    elif "audio_url" in data or "audioUrl" in data or "id" in data:
        tracks = [data]
    else:
        tracks = []

    if not isinstance(tracks, list):
        return []
    normalized_tracks = []
    for track in tracks:
        if isinstance(track, dict) and track.get("id"):
            normalized_track = dict(track)
            normalized_track["taskId"] = task_id
            normalized_tracks.append(normalized_track)
    return normalized_tracks

def wait_for_generated_tracks(task_id, status_text="Preparing generated track for Persona"):
    """Wait for one generation task and return its generated tracks."""
    progress_bar = st.progress(0, text=f"🛠️ {status_text}...")
    client = st.session_state.suno_client
    for _ in range(120):  # Ten minutes at five-second polling intervals.
        details = client.get_details(task_id)
        data = details.get("data", {}) if isinstance(details, dict) else {}
        status = str(data.get("status", "")).upper()
        if status == "SUCCESS":
            tracks = tracks_from_task_details(details, task_id)
            if not tracks:
                raise ValueError("The generation completed, but no generated audio IDs were returned.")
            history = load_history()
            for track in tracks:
                if not any(saved.get("id") == track.get("id") for saved in history):
                    history.append(track)
            save_history(history)
            progress_bar.progress(100, text="✅ Generated track is ready for Persona creation.")
            return tracks
        if status in {"FAILED", "ERROR"} or status.endswith("_FAILED") or status.endswith("_ERROR") or status.endswith("_EXCEPTION"):
            raise ValueError(data.get("errorMessage") or f"Generation failed with status {status}.")
        progress_bar.progress(50, text=f"🔄 {status_text}... ({status or 'waiting'})")
        time.sleep(5)
    raise TimeoutError("The generated track did not finish within 10 minutes. It may still complete in your Library.")

def get_audio_url(input_str):
    input_str = input_str.strip()
    if input_str.startswith("http"):
        return input_str
    history = load_history()
    for t in history:
        if t.get("id") == input_str:
            return t.get("audioUrl", t.get("audio_url", t.get("url", input_str)))
    
    # Fallback: If it looks like a typical Suno UUID, construct the default CDN URL
    if len(input_str) == 36 and "-" in input_str:
        return f"https://cdn1.suno.ai/{input_str}.mp3"
        
    return input_str

def upload_audio_for_suno(uploaded_file):
    """Return a provider-hosted, publicly readable URL for an uploaded audio file."""
    return st.session_state.suno_client.upload_file(uploaded_file)

def find_library_track(track_id):
    return next((track for track in load_history() if track.get("id") == track_id), None)

def trigger_download(audio_url, filename):
    components.html(
        f"""
        <script>
        fetch("{audio_url}")
            .then(response => response.blob())
            .then(blob => {{
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.style.display = 'none';
                a.href = url;
                a.download = "{filename}.mp3";
                document.body.appendChild(a);
                a.click();
                window.URL.revokeObjectURL(url);
            }})
            .catch(e => console.error('Download error:', e));
        </script>
        """,
        height=0,
    )

def poll_and_display(ids_to_poll):
    pending_ids = list(ids_to_poll)
    completed_audios = []
    client = st.session_state.suno_client
    
    status_msg = "🛠️ Generating..."
    progress_bar = st.progress(0, text=status_msg)
    
    while pending_ids:
        progress_bar.progress(50, text=f"🔄 Polling details for {len(pending_ids)} task(s)...")
        for tid in list(pending_ids):
            try:
                details = client.get_details(tid)
                if isinstance(details, dict) and "data" in details:
                    data = details["data"]
                    status = data.get("status", "").upper()
                    if status == "SUCCESS":
                        tracks = []
                        response_obj = data.get("response")
                        if isinstance(response_obj, list):
                            tracks = response_obj
                        elif isinstance(response_obj, dict):
                            tracks = response_obj.get("sunoData", response_obj.get("data", []))
                        
                        if not tracks:
                            if "audio_url" in data or "id" in data:
                                tracks = [data]
                                
                        if not tracks:
                            st.warning("Task SUCCESS but no tracks parsed! Raw API data:")
                            st.json(data)

                        for track in tracks:
                            track["taskId"] = tid
                            track["workspaceId"] = st.session_state.active_tasks.get(tid)
                            st.success(f"✅ Track `{track.get('id', '')}` generation complete!")
                            completed_audios.append(track)
                        pending_ids.remove(tid)
                    elif status in ["FAILED", "ERROR"] or status.endswith("_FAILED") or status.endswith("_ERROR") or status.endswith("_EXCEPTION"):
                        st.error(f"❌ Task `{tid}` failed with status {status}: {data.get('errorMessage', '')}")
                        pending_ids.remove(tid)
            except Exception as e:
                st.warning(f"⚠️ API polling warning: {e}")
        
        if pending_ids:
            time.sleep(5)
            
    progress_bar.progress(100, text="✅ Generation Complete!")

    history = load_history()
    for track in completed_audios:
        if not any(t.get("id") == track.get("id") for t in history):
            history.append(track)

        with st.container(border=True):
            title = track.get("title", f"Track_{track.get('id')}")
            st.subheader(f"🎵 {title}")
            cols = st.columns([1, 2])
            with cols[0]:
                image_url = track.get("image_url", track.get("imageUrl"))
                if image_url:
                    st.image(image_url, width=200)
            with cols[1]:
                tags = track.get("tags", "")
                st.write(f"**Tags:** {tags}")
                audio_link = track.get("audio_url", track.get("audioUrl", track.get("url")))
                if audio_link:
                    st.audio(audio_link)
                with st.expander("Lyrics"):
                    st.text(track.get("prompt", track.get("lyric", "No lyrics available")))
            
            if audio_link:
                trigger_download(audio_link, title)

    save_history(history)
    backup_results = backup_completed_tracks(completed_audios)
    for track_id, backed_up, message in backup_results:
        if not backed_up:
            st.warning(f"Track `{track_id}` is saved in the Library, but its local backup needs retrying: {message}")
    for tid in ids_to_poll:
        st.session_state.active_tasks.pop(tid, None)

def stem_urls_from_response(response):
    if not isinstance(response, dict):
        return {}
    key_labels = {
        "originUrl": "Original Mix",
        "vocalUrl": "Vocals",
        "instrumentalUrl": "Instrumental",
        "backingVocalsUrl": "Backing Vocals",
        "drumsUrl": "Drums",
        "bassUrl": "Bass",
        "guitarUrl": "Guitar",
        "keyboardUrl": "Keyboard",
        "percussionUrl": "Percussion",
        "stringsUrl": "Strings",
        "synthUrl": "Synth",
        "fxUrl": "FX / Other",
        "brassUrl": "Brass",
        "woodwindsUrl": "Woodwinds",
    }
    urls = {label: response[key] for key, label in key_labels.items() if response.get(key)}
    for stem in response.get("originData", []) or []:
        url = stem.get("audio_url")
        label = stem.get("stem_type_group_name") or "Stem"
        if url:
            urls.setdefault(label, url)
    return urls

def safe_download_name(value):
    return re.sub(r'[^A-Za-z0-9._-]+', '_', value).strip('._') or "voice_stems"

def get_stem_source_url(stem_job):
    """Prefer the original library URL when it is available."""
    source_track_id = stem_job.get("sourceTrackId")
    if source_track_id:
        source_track = find_library_track(source_track_id)
        if source_track:
            return (
                source_track.get("sourceAudioUrl")
                or source_track.get("audioUrl")
                or source_track.get("audio_url")
                or stem_job.get("sourceAudioUrl")
            )
    return stem_job.get("sourceAudioUrl")

def build_stem_zip(stem_job):
    """Download the original mix and all returned stems into a ZIP archive."""
    audio_files = []
    source_url = get_stem_source_url(stem_job)
    if source_url:
        audio_files.append(("Original Mix", source_url))
    audio_files.extend(stem_job.get("stems", {}).items())
    if not audio_files:
        raise ValueError("No audio files are available for this stem set.")

    archive_buffer = io.BytesIO()
    title = safe_download_name(stem_job.get("title", "voice_stems"))
    with zipfile.ZipFile(archive_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for label, url in audio_files:
            response = requests.get(url, timeout=60)
            response.raise_for_status()
            extension = os.path.splitext(urlparse(url).path)[1] or ".mp3"
            archive.writestr(f"{title}/{safe_download_name(label)}{extension}", response.content)
    return archive_buffer.getvalue()

def render_stem_result(stem_job, workspaces=None, widget_scope="saved"):
    title = stem_job.get("title", "Voice Stems")
    st.subheader(title)
    st.write(f"**Workspace:** {get_workspace_name(stem_job.get('workspaceId'), workspaces)}")
    st.caption(f"Mode: {stem_job.get('type', 'separate_vocal')} · Separation task: {stem_job.get('id', '')}")
    source_url = get_stem_source_url(stem_job)
    if source_url:
        st.markdown("**Original Mixed Track**")
        st.audio(source_url)
    else:
        st.info("The original mixed-track URL is not available for this stem set.")

    zip_state_key = f"stem_zip_{widget_scope}_{stem_job.get('id')}"
    if st.button("Prepare ZIP Download", key=f"prepare_stem_zip_{widget_scope}_{stem_job.get('id')}"):
        try:
            with st.spinner("Downloading stems and building ZIP file..."):
                st.session_state[zip_state_key] = build_stem_zip(stem_job)
        except Exception as e:
            st.error(f"Could not prepare the ZIP file: {e}")
    if st.session_state.get(zip_state_key):
        st.download_button(
            "Download Complete Stem Set (ZIP)",
            data=st.session_state[zip_state_key],
            file_name=f"{safe_download_name(title)}_stem_set.zip",
            mime="application/zip",
            key=f"download_stem_zip_{widget_scope}_{stem_job.get('id')}",
        )

    st.markdown("**Separated Stems**")
    for label, url in stem_job.get("stems", {}).items():
        st.markdown(f"**{label}**")
        st.audio(url)
        # link_button in Streamlit 1.55 does not accept a key. A normal link also
        # avoids duplicate widget IDs when multiple saved stem sets are displayed.
        st.markdown(f"[Open / Download {label}]({url})")

def poll_and_display_stems(ids_to_poll):
    pending_ids = list(ids_to_poll)
    client = st.session_state.suno_client
    progress_bar = st.progress(0, text="🛠️ Separating stems...")

    while pending_ids:
        progress_bar.progress(50, text=f"🔄 Checking {len(pending_ids)} stem task(s)...")
        for task_id in list(pending_ids):
            try:
                details = client.get_stem_details(task_id)
                data = details.get("data", {}) if isinstance(details, dict) else {}
                status = data.get("successFlag", "").upper()
                if status == "SUCCESS":
                    task_meta = st.session_state.active_stem_tasks.get(task_id, {})
                    stem_job = {
                        "id": task_id,
                        "title": task_meta.get("title", "Voice Stems"),
                        "workspaceId": task_meta.get("workspaceId"),
                        "sourceTrackId": task_meta.get("sourceTrackId"),
                        "sourceAudioUrl": task_meta.get("sourceAudioUrl"),
                        "type": task_meta.get("type", "separate_vocal"),
                        "stemName": task_meta.get("stemName"),
                        "createdAt": data.get("createTime"),
                        "completedAt": data.get("completeTime"),
                        "stems": stem_urls_from_response(data.get("response", {})),
                    }
                    saved_stems = load_voice_stems()
                    saved_stems = [job for job in saved_stems if job.get("id") != task_id]
                    saved_stems.append(stem_job)
                    save_voice_stems(saved_stems)
                    pending_ids.remove(task_id)
                    st.success("✅ Stem separation complete!")
                    render_stem_result(stem_job, load_workspaces(), widget_scope="completed")
                elif status in {"CREATE_TASK_FAILED", "GENERATE_AUDIO_FAILED", "CALLBACK_EXCEPTION", "FAILED", "ERROR"}:
                    st.error(f"❌ Stem separation task `{task_id}` failed: {data.get('errorMessage', '')}")
                    pending_ids.remove(task_id)
            except Exception as e:
                st.warning(f"⚠️ Stem separation polling warning: {e}")
        if pending_ids:
            time.sleep(5)

    progress_bar.progress(100, text="✅ Stem separation complete!")
    for task_id in ids_to_poll:
        st.session_state.active_stem_tasks.pop(task_id, None)


# --- Background Polling Resume ---
if st.session_state.active_tasks:
    st.info(f"Resuming polling for {len(st.session_state.active_tasks)} active task(s)...")
    active_list = list(st.session_state.active_tasks)
    poll_and_display(active_list)

if st.session_state.active_stem_tasks:
    st.info(f"Resuming {len(st.session_state.active_stem_tasks)} active stem-separation task(s)...")
    poll_and_display_stems(list(st.session_state.active_stem_tasks))

# --- UI TABS ---
tabs = st.tabs([
    "🎸 Simple Create", 
    "🎛️ Custom Mode", 
    "🔁 Extend & Remix", 
    "🎤 Cover", 
    "🗣️ Overlay", 
    "🎙️ Voice Stems",
    "📚 Library",
    "🎭 Personas",
    "🗂️ Workspaces",
    "🛟 Recovery & Backup",
])

# 1. Simple Create
with tabs[0]:
    st.header("Simple Text-to-Song")
    simple_prompt = st.text_area("Describe the song you want to hear:", key="simple_prompt", placeholder="A pop song about coding until 3 AM...")
    col1, col2 = st.columns(2)
    with col1:
        is_simple_instrumental = st.toggle("Instrumental Only", value=False, key="simple_inst")
    with col2:
        simple_persona_id = render_persona_selector("simple")
        simple_workspace_id = render_workspace_selector("simple")
    
    if st.button("Generate", key="btn_simple"):
        if simple_prompt:
            try:
                res = st.session_state.suno_client.generate(
                    simple_prompt, 
                    make_instrumental=is_simple_instrumental,
                    persona_id=simple_persona_id
                )
                new_ids = extract_task_ids(res)
                if new_ids:
                    st.session_state.active_tasks.update({task_id: simple_workspace_id for task_id in new_ids})
                    poll_and_display(new_ids)
                else:
                    st.error(f"No valid Task ID returned from API. Response: {res}")
            except Exception as e:
                st.error(f"Error starting generation: {e}")
        else:
            st.warning("Please provide a description.")

# 2. Custom Mode
with tabs[1]:
    st.header("Custom Mode")
    st.caption("Fine-tune the model, exclusions, vocal preference, and creative guidance for this song.")
    col1, col2 = st.columns(2)
    with col1:
        custom_title = st.text_input("Title", key="custom_title", help="Required by Suno in Custom Mode.")
        custom_tags = st.text_area("Style / Tags", key="custom_tags", placeholder="e.g. pop, female vocals, upbeat")
        is_instrumental = st.toggle("Instrumental Only", value=False, key="custom_inst")
    with col2:
        custom_lyrics = st.text_area("Lyrics", height=150, key="custom_lyrics", disabled=is_instrumental)
        custom_persona_id = render_persona_selector("custom")
        custom_workspace_id = render_workspace_selector("custom")

    st.subheader("Generation controls")
    custom_model = st.selectbox(
        "Model",
        options=SUNO_MODELS,
        index=0,
        key="custom_model",
        help="V6 is the recommended default. Exact duration is supported by V6-series models and V5_5.",
    )
    control_col1, control_col2 = st.columns(2)
    with control_col1:
        custom_negative_tags = st.text_input(
            "Avoid (optional)",
            key="custom_negative_tags",
            placeholder="e.g. heavy metal, upbeat drums",
            help="Comma-separated styles or traits to exclude.",
        )
        vocal_options = {"No preference": None, "Male vocal": "m", "Female vocal": "f"}
        selected_vocal = st.selectbox(
            "Vocal preference",
            options=list(vocal_options),
            disabled=is_instrumental,
            key="custom_vocal_gender",
        )
    with control_col2:
        custom_duration = st.slider(
            "Duration (seconds)",
            min_value=10,
            max_value=360,
            value=330,
            step=1,
            disabled=custom_model not in DURATION_MODELS,
            key="custom_duration",
            help="Available for V6-series models and V5_5 Custom Mode. Other models let Suno choose the duration.",
        )

    weight_col1, weight_col2, weight_col3 = st.columns(3)
    with weight_col1:
        custom_style_weight = st.slider(
            "Style strength", 0.0, 1.0, 0.65, 0.01, key="custom_style_weight",
            help="How strongly the output follows the style / tags.",
        )
    with weight_col2:
        custom_weirdness = st.slider(
            "Creative variation", 0.0, 1.0, 0.65, 0.01, key="custom_weirdness",
            help="Higher values allow more novelty and deviation.",
        )
    with weight_col3:
        custom_audio_weight = st.slider(
            "Audio influence", 0.0, 1.0, 0.65, 0.01, key="custom_audio_weight",
            help="Controls audio-reference influence where the selected generation flow supports it.",
        )

    if st.button("Generate Custom", key="btn_custom"):
        if not is_instrumental and not custom_lyrics:
            st.warning("Please provide lyrics or check 'Instrumental Only'.")
        elif not custom_tags:
             st.warning("Please provide style/tags.")
        elif not custom_title:
             st.warning("Please provide a title for Custom Mode.")
        else:
            try:
                res = st.session_state.suno_client.custom_generate(
                    prompt=custom_lyrics if not is_instrumental else "",
                    tags=custom_tags,
                    title=custom_title,
                    make_instrumental=is_instrumental,
                    persona_id=custom_persona_id,
                    model=custom_model,
                    duration=custom_duration if custom_model in DURATION_MODELS else None,
                    negative_tags=custom_negative_tags or None,
                    vocal_gender=None if is_instrumental else vocal_options[selected_vocal],
                    style_weight=custom_style_weight,
                    weirdness_constraint=custom_weirdness,
                    audio_weight=custom_audio_weight,
                )
                new_ids = extract_task_ids(res)
                if new_ids:
                    st.session_state.active_tasks.update({task_id: custom_workspace_id for task_id in new_ids})
                    poll_and_display(new_ids)
                else:
                    st.error(f"No valid Task ID returned from API. Response: {res}")
            except Exception as e:
                st.error(f"Error starting custom generation: {e}")

# 3. Extend & Remix
with tabs[2]:
    st.header("Extend Audio")
    st.write("Extend an uploaded file, a previous session song, or a public Audio URL.")
    
    ext_source_type = st.radio("Source Type", ["Upload File", "Library Track ID", "Public URL"], horizontal=True, key="ext_source_type")
    
    ext_uploaded_file = None
    ext_track_id = ""
    ext_public_url = ""
    
    if ext_source_type == "Upload File":
        ext_uploaded_file = st.file_uploader("Upload Audio File (up to 1 min for V4_5ALL, 8 mins for others)", type=['mp3', 'wav', 'm4a', 'aac'], key="ext_upload")
    elif ext_source_type == "Library Track ID":
        ext_track_id = st.text_input("Library Track ID", key="ext_track_id")
    else:
        ext_public_url = st.text_input("Public Audio URL", key="ext_public_url")
        
    ext_title = st.text_input("Extension Title", value="Extended Output", key="ext_title")
    ext_style = st.text_input("Extension Style / Tags", key="ext_style", placeholder="e.g. cinematic orchestral, evolving")
    ext_instrumental = st.toggle("Instrumental Extension", value=False, key="ext_inst")
    ext_prompt = st.text_area("New Lyrics or Prompt for the extension", key="ext_prompt", disabled=ext_instrumental)
    ext_continue_at = st.number_input(
        "Continue from (seconds)", min_value=0.1, value=60.0, step=0.1, key="ext_continue_at",
        help="Choose a point before the source track ends.",
    )
    ext_negative_tags = st.text_input("Avoid (optional)", key="ext_negative_tags", placeholder="e.g. abrupt ending, heavy drums")
    ext_vocal_options = {"No preference": None, "Male vocal": "m", "Female vocal": "f"}
    ext_vocal_label = st.selectbox("Vocal preference", list(ext_vocal_options), disabled=ext_instrumental, key="ext_vocal_gender")
    ext_weight_1, ext_weight_2, ext_weight_3 = st.columns(3)
    with ext_weight_1:
        ext_style_weight = st.slider("Style strength", 0.0, 1.0, 0.65, 0.01, key="ext_style_weight")
    with ext_weight_2:
        ext_weirdness = st.slider("Creative variation", 0.0, 1.0, 0.65, 0.01, key="ext_weirdness")
    with ext_weight_3:
        ext_audio_weight = st.slider("Source audio influence", 0.0, 1.0, 0.65, 0.01, key="ext_audio_weight")
    ext_persona_id = render_persona_selector("ext")
    ext_workspace_id = render_workspace_selector("ext")
    
    if st.button("Extend Audio", key="btn_extend"):
        valid_source = False
        if ext_source_type == "Upload File" and ext_uploaded_file:
            valid_source = True
        elif ext_source_type == "Library Track ID" and ext_track_id:
            valid_source = True
        elif ext_source_type == "Public URL" and ext_public_url:
            valid_source = True
            
        if valid_source and ext_title.strip() and ext_style.strip() and (ext_instrumental or ext_prompt.strip()):
            try:
                st.info("Preparing source audio...")
                resolved_url = None
                if ext_source_type == "Upload File":
                    with st.spinner("Uploading audio for Suno..."):
                        resolved_url = upload_audio_for_suno(ext_uploaded_file)
                elif ext_source_type == "Library Track ID":
                    resolved_url = get_audio_url(ext_track_id)
                else:
                    resolved_url = ext_public_url
                    
                st.info("Sending extend request to Suno...")
                res = st.session_state.suno_client.extend_audio(
                    resolved_url, ext_prompt, persona_id=ext_persona_id, style=ext_style,
                    title=ext_title, continue_at=ext_continue_at,
                    make_instrumental=ext_instrumental, negative_tags=ext_negative_tags or None,
                    vocal_gender=None if ext_instrumental else ext_vocal_options[ext_vocal_label],
                    style_weight=ext_style_weight, weirdness_constraint=ext_weirdness,
                    audio_weight=ext_audio_weight,
                )
                new_ids = extract_task_ids(res)
                if new_ids:
                    st.session_state.active_tasks.update({task_id: ext_workspace_id for task_id in new_ids})
                    poll_and_display(new_ids)
                else:
                    st.error(f"No valid Task ID returned from API. Response: {res}")
            except Exception as e:
                st.error(f"Error extending: {e}")
        else:
            st.warning("Please provide a valid source, title, style, and a prompt (unless instrumental).")

# 4. Cover
with tabs[3]:
    st.header("Cover Mode")
    st.write("Provide an uploaded file, or a Track ID from your Library, or a public Audio URL to re-imagine it in a new genre.")
    
    cover_source_type = st.radio("Source Type", ["Upload File", "Library Track ID", "Public URL"], horizontal=True, key="cov_source_type")
    
    cov_uploaded_file = None
    cov_track_id = ""
    cov_public_url = ""
    cov_library_track = None
    
    if cover_source_type == "Upload File":
        cov_uploaded_file = st.file_uploader("Upload Audio File (up to 1 min for V4_5ALL, 8 mins for others)", type=['mp3', 'wav', 'm4a', 'aac'])
    elif cover_source_type == "Library Track ID":
        cov_track_id = st.text_input("Library Track ID", key="cov_track_id", help="Paste the Track ID shown in your Library.")
        if cov_track_id.strip():
            cov_library_track = find_library_track(cov_track_id.strip())
            if cov_library_track:
                st.caption(f"Selected: {cov_library_track.get('title', 'Untitled track')}")
            else:
                st.caption("No saved Library track matches this ID.")
    else:
        cov_public_url = st.text_input("Public Audio URL", key="cov_public_url")
        
    cov_title = st.text_input("Title", value="Cover Output", key="cov_title")
    cov_tags = st.text_area("New Genre / Tags", key="cov_tags")
    cov_instrumental = st.toggle("Instrumental Only", value=False, key="cov_inst")
    cov_lyrics = st.text_area("Lyrics (Leave empty if instrumental, required for vocal cover)", height=150, key="cov_lyrics", disabled=cov_instrumental)
    cov_negative_tags = st.text_input("Avoid (optional)", key="cov_negative_tags", placeholder="e.g. distorted guitar, fast drums")
    cov_vocal_options = {"No preference": None, "Male vocal": "m", "Female vocal": "f"}
    cov_vocal_label = st.selectbox("Vocal preference", list(cov_vocal_options), disabled=cov_instrumental, key="cov_vocal_gender")
    cov_duration = st.slider(
        "Duration (seconds)", 10, 360, 330, 1, disabled=selected_model not in DURATION_MODELS, key="cov_duration",
        help="Available when the sidebar model is V6-series or V5_5.",
    )
    cov_weight_1, cov_weight_2, cov_weight_3 = st.columns(3)
    with cov_weight_1:
        cov_style_weight = st.slider("Style strength", 0.0, 1.0, 0.65, 0.01, key="cov_style_weight")
    with cov_weight_2:
        cov_weirdness = st.slider("Creative variation", 0.0, 1.0, 0.65, 0.01, key="cov_weirdness")
    with cov_weight_3:
        cov_audio_weight = st.slider("Source audio influence", 0.0, 1.0, 0.65, 0.01, key="cov_audio_weight")
    cov_persona_id = render_persona_selector("cov")
    cov_workspace_id = render_workspace_selector("cov")
    
    if st.button("Generate Cover", key="btn_cover"):
        valid_source = False
        if cover_source_type == "Upload File" and cov_uploaded_file:
            valid_source = True
        elif cover_source_type == "Library Track ID" and cov_library_track:
            valid_source = True
        elif cover_source_type == "Public URL" and cov_public_url.strip():
            valid_source = True
            
        if not valid_source:
            if cover_source_type == "Library Track ID":
                st.warning("That Library Track ID was not found. Paste the ID exactly as shown in the Library.")
            else:
                st.warning("Please provide an audio file or a public audio URL.")
        elif not cov_title.strip():
            st.warning("Please provide a title for the generated cover.")
        elif not cov_tags.strip():
            st.warning("Please provide Cover Style / Tags (for example: 'acoustic folk, warm male vocal').")
        else:
            if not cov_instrumental and not cov_lyrics.strip():
                st.warning("Please provide lyrics in the prompt for a vocal cover, or check 'Instrumental Only'.")
            else:
                try:
                    st.info("Preparing source audio...")
                    resolved_url = None
                    if cover_source_type == "Upload File":
                        with st.spinner("Uploading audio for Suno..."):
                            resolved_url = upload_audio_for_suno(cov_uploaded_file)
                    elif cover_source_type == "Library Track ID":
                        resolved_url = (
                            cov_library_track.get("audioUrl")
                            or cov_library_track.get("audio_url")
                            or cov_library_track.get("sourceAudioUrl")
                        )
                        if not resolved_url:
                            raise ValueError("This Library track does not have an audio URL. Use the Public URL source instead.")
                    else:
                        resolved_url = cov_public_url.strip()
                    
                    st.info("Sending cover request to Suno...")
                    prompt_to_send = cov_lyrics if not cov_instrumental else ""
                    res = st.session_state.suno_client.generate_cover(
                        resolved_url, 
                        prompt=prompt_to_send, 
                        style=cov_tags, 
                        title=cov_title,
                        make_instrumental=cov_instrumental,
                        persona_id=cov_persona_id,
                        duration=cov_duration if selected_model in DURATION_MODELS else None,
                        negative_tags=cov_negative_tags or None,
                        vocal_gender=None if cov_instrumental else cov_vocal_options[cov_vocal_label],
                        style_weight=cov_style_weight,
                        weirdness_constraint=cov_weirdness,
                        audio_weight=cov_audio_weight,
                    )
                    new_ids = extract_task_ids(res)
                    if new_ids:
                        st.session_state.active_tasks.update({task_id: cov_workspace_id for task_id in new_ids})
                        poll_and_display(new_ids)
                    else:
                        st.error(f"No valid Task ID returned from API. Response: {res}")
                except Exception as e:
                    st.error(f"Error creating cover: {e}")
# 5. Vocal/Instrumental Overlay
with tabs[4]:
    st.header("Vocal / Instrumental Overlay")
    overlay_type = st.radio("Overlay Type", ["Add Vocals (to Instrumental)", "Add Instrumental (to Vocal)"], key="overlay_type")
    
    st.write("Provide an uploaded file, or a Track ID from your Library, or a public Audio URL to overlay.")
    over_source_type = st.radio("Source Type", ["Upload File", "Library Track ID", "Public URL"], horizontal=True, key="over_source_type")
    
    over_uploaded_file = None
    over_track_id = ""
    over_public_url = ""
    
    if over_source_type == "Upload File":
        over_uploaded_file = st.file_uploader("Upload Audio File (up to 1 min for V4_5ALL, 8 mins for others)", type=['mp3', 'wav', 'm4a', 'aac'], key="over_upload")
    elif over_source_type == "Library Track ID":
        over_track_id = st.text_input("Library Track ID to Overlay", key="over_track_id")
    else:
        over_public_url = st.text_input("Public Audio URL to Overlay", key="over_public_url")
        
    over_title = st.text_input("Output Title", value="Overlay Output", key="over_title")
    if overlay_type == "Add Vocals (to Instrumental)":
        over_lyrics = st.text_area("Lyrics for the new vocals", key="over_lyrics")
        over_style = st.text_input("Vocal Style", placeholder="e.g. warm Bengali folk vocals, acoustic", key="over_style")
        over_negative_tags = st.text_input("Avoid (optional)", value="None", key="over_negative_vocals")
        over_tags = ""
    else:
        over_tags = st.text_input("Instrumental Style", placeholder="e.g. acoustic guitar, strings, gentle percussion", key="over_tags")
        over_lyrics = st.text_area(
            "Lyrics / Vocal Guide (recommended)",
            placeholder="Paste the vocal lyrics here to help preserve phrasing, sections, and timing.",
            key="over_instrumental_lyrics",
        )
        over_negative_tags = st.text_input("Avoid (optional)", value="None", key="over_negative_instrumental")
        over_style = ""
    over_model = st.selectbox(
        "Generation model",
        SUNO_MODELS,
        key="over_model",
        help="V6-series models are recommended. Legacy models remain available for backward compatibility.",
    )
    over_vocal_options = {"No preference": None, "Male vocal": "m", "Female vocal": "f"}
    over_vocal_label = st.selectbox("Vocal preference", list(over_vocal_options), key="over_vocal_gender")
    over_weight_1, over_weight_2, over_weight_3 = st.columns(3)
    with over_weight_1:
        over_style_weight = st.slider("Style strength", 0.0, 1.0, 0.65, 0.01, key="over_style_weight")
    with over_weight_2:
        over_weirdness = st.slider("Creative variation", 0.0, 1.0, 0.65, 0.01, key="over_weirdness")
    with over_weight_3:
        over_audio_weight = st.slider("Source audio influence", 0.0, 1.0, 0.65, 0.01, key="over_audio_weight")
    over_workspace_id = render_workspace_selector("over")
    
    if st.button("Generate Overlay", key="btn_overlay"):
        valid_source = False
        if over_source_type == "Upload File" and over_uploaded_file:
            valid_source = True
        elif over_source_type == "Library Track ID" and over_track_id:
            valid_source = True
        elif over_source_type == "Public URL" and over_public_url:
            valid_source = True
            
        adding_vocals = overlay_type == "Add Vocals (to Instrumental)"
        has_generation_details = (
            bool(over_title.strip())
            and bool(over_style.strip() if adding_vocals else over_tags.strip())
            and (not adding_vocals or bool(over_lyrics.strip()))
        )
        if valid_source and has_generation_details:
            try:
                st.info("Preparing source audio...")
                resolved_url = None
                if over_source_type == "Upload File":
                    with st.spinner("Uploading audio for Suno..."):
                        resolved_url = upload_audio_for_suno(over_uploaded_file)
                elif over_source_type == "Library Track ID":
                    resolved_url = get_audio_url(over_track_id)
                else:
                    resolved_url = over_public_url
                    
                st.info("Sending overlay request to Suno...")
                if adding_vocals:
                    res = st.session_state.suno_client.add_vocals(
                        resolved_url,
                        prompt=over_lyrics,
                        style=over_style,
                        title=over_title,
                        negative_tags=over_negative_tags,
                        vocal_gender=over_vocal_options[over_vocal_label],
                        style_weight=over_style_weight,
                        weirdness_constraint=over_weirdness,
                        audio_weight=over_audio_weight,
                        model=over_model,
                    )
                else:
                    instrumental_context = over_tags
                    if over_lyrics.strip():
                        instrumental_context += f"\n\nVocal phrasing and section guide:\n{over_lyrics.strip()}"
                    res = st.session_state.suno_client.add_instrumental(
                        resolved_url,
                        tags=instrumental_context,
                        title=over_title,
                        negative_tags=over_negative_tags,
                        vocal_gender=over_vocal_options[over_vocal_label],
                        style_weight=over_style_weight,
                        weirdness_constraint=over_weirdness,
                        audio_weight=over_audio_weight,
                        model=over_model,
                    )
                new_ids = extract_task_ids(res)
                if new_ids:
                    st.session_state.active_tasks.update({task_id: over_workspace_id for task_id in new_ids})
                    poll_and_display(new_ids)
                else:
                    st.error(f"No valid Task ID returned from API. Response: {res}")
            except Exception as e:
                st.error(f"Error with overlay: {e}")
        else:
            st.warning("Please provide a valid source, title, and style. Lyrics are required when adding vocals.")

# 6. Voice Stems
with tabs[5]:
    st.header("Voice Stems")
    st.write("Split a track into clean vocals and accompaniment, or extract a fuller set of stems.")
    st.caption("Each separation request consumes provider credits. Returned audio links are temporary, so download stems you want to keep.")

    stem_source_type = st.radio(
        "Source Type",
        ["Library Track", "Upload File", "Public URL"],
        horizontal=True,
        key="stem_source_type",
    )
    stem_uploaded_file = None
    stem_track_id = ""
    stem_public_url = ""
    if stem_source_type == "Library Track":
        stem_track_id = st.text_input("Library Track ID", key="stem_track_id")
        st.caption("Library tracks require both their saved Track ID and generation Task ID.")
    elif stem_source_type == "Upload File":
        stem_uploaded_file = st.file_uploader(
            "Upload audio (maximum 20 MB)",
            type=["mp3", "wav", "m4a", "aac"],
            key="stem_upload",
        )
    else:
        stem_public_url = st.text_input("Public Audio URL", key="stem_public_url")

    separation_options = {
        "Vocals + Instrumental (10 credits)": "separate_vocal",
        "Full multi-stem split (50 credits)": "split_stem",
        "One specific stem (20 credits)": "split_stem_advanced",
    }
    stem_mode_label = st.selectbox("Separation Mode", list(separation_options), key="stem_mode")
    stem_mode = separation_options[stem_mode_label]
    stem_name = None
    if stem_mode == "split_stem_advanced":
        stem_name = st.selectbox(
            "Stem to extract",
            ["Lead Vocal", "Backing Vocals", "Drum Kit", "Bass", "Piano", "Guitar", "Synth", "String Section", "Percussion"],
            key="stem_name",
        )
    stem_title = st.text_input("Stem Set Title", value="Voice Stems", key="stem_title")
    stem_workspace_id = render_workspace_selector("stem")

    if st.button("Separate Stems", key="btn_stems"):
        valid_source = (
            (stem_source_type == "Library Track" and bool(stem_track_id.strip()))
            or (stem_source_type == "Upload File" and stem_uploaded_file is not None)
            or (stem_source_type == "Public URL" and bool(stem_public_url.strip()))
        )
        if not valid_source or not stem_title.strip():
            st.warning("Please provide a source and a stem set title.")
        elif stem_uploaded_file is not None and stem_uploaded_file.size > 20 * 1024 * 1024:
            st.warning("The separation API accepts uploaded files up to 20 MB.")
        else:
            try:
                source_track = None
                source_url = None
                if stem_source_type == "Library Track":
                    source_track = find_library_track(stem_track_id.strip())
                    if not source_track:
                        raise ValueError("That Library Track ID was not found.")
                    if not source_track.get("taskId"):
                        raise ValueError("This library track has no generation Task ID, so it cannot be submitted as an existing Suno track. Use its public audio URL instead.")
                    res = st.session_state.suno_client.separate_stems(
                        task_id=source_track["taskId"],
                        audio_id=source_track["id"],
                        separation_type=stem_mode,
                        stem_name=stem_name,
                    )
                    source_url = (
                        source_track.get("sourceAudioUrl")
                        or source_track.get("audioUrl")
                        or get_audio_url(stem_track_id)
                    )
                else:
                    if stem_source_type == "Upload File":
                        with st.spinner("Uploading audio for stem separation..."):
                            source_url = upload_audio_for_suno(stem_uploaded_file)
                    else:
                        source_url = stem_public_url.strip()
                    res = st.session_state.suno_client.separate_stems(
                        audio_url=source_url,
                        separation_type=stem_mode,
                        stem_name=stem_name,
                    )

                stem_task_ids = extract_task_ids(res)
                if stem_task_ids:
                    task_meta = {
                        "title": stem_title.strip(),
                        "workspaceId": stem_workspace_id,
                        "sourceTrackId": source_track.get("id") if source_track else None,
                        "sourceAudioUrl": source_url,
                        "type": stem_mode,
                        "stemName": stem_name,
                    }
                    st.session_state.active_stem_tasks.update({task_id: task_meta for task_id in stem_task_ids})
                    poll_and_display_stems(stem_task_ids)
                else:
                    st.error(f"No valid separation Task ID returned from API. Response: {res}")
            except Exception as e:
                st.error(f"Error starting stem separation: {e}")

    st.divider()
    st.subheader("Saved Stem Sets")
    saved_stems = load_voice_stems()
    workspaces = load_workspaces()
    stem_workspace_options = {"All Workspaces": "all", "Unassigned": None}
    for workspace in workspaces:
        stem_workspace_options[workspace.get("name", "Unnamed Workspace")] = workspace.get("id")
    selected_stem_workspace = st.selectbox("Filter by Workspace", list(stem_workspace_options), key="stem_workspace_filter")
    selected_stem_workspace_id = stem_workspace_options[selected_stem_workspace]
    filtered_stems = saved_stems if selected_stem_workspace_id == "all" else [
        stem for stem in saved_stems if stem.get("workspaceId") == selected_stem_workspace_id
    ]
    if not filtered_stems:
        st.info("No saved stem sets in this workspace yet.")
    else:
        for index, stem_job in enumerate(reversed(filtered_stems)):
            with st.container(border=True):
                render_stem_result(stem_job, workspaces, widget_scope=f"saved_{index}")
                if st.button("Delete Stem Set", key=f"delete_stem_set_{stem_job.get('id')}_{index}"):
                    save_voice_stems([job for job in saved_stems if job.get("id") != stem_job.get("id")])
                    st.rerun()

# 7. Library
with tabs[6]:
    st.header("Local Library")
    history = load_history()
    workspaces = load_workspaces()
    workspace_filter_options = {"All Workspaces": "all", "Unassigned": None}
    for workspace in workspaces:
        workspace_filter_options[workspace.get("name", "Unnamed Workspace")] = workspace.get("id")
    selected_workspace_filter = st.selectbox(
        "Filter by Workspace",
        options=list(workspace_filter_options.keys()),
        key="library_workspace_filter"
    )
    selected_workspace_id = workspace_filter_options[selected_workspace_filter]
    filtered_history = history if selected_workspace_id == "all" else [
        track for track in history if track.get("workspaceId") == selected_workspace_id
    ]

    if not filtered_history:
        st.write("No tracks in this workspace yet.")
    else:
        for idx, track in enumerate(reversed(filtered_history)):
            with st.container(border=True):
                title = track.get("title", f"Track_{track.get('id')}")
                st.subheader(f"{title} (ID: {track.get('id')})")
                cols = st.columns([1, 2])
                with cols[0]:
                    image_url = track.get("image_url", track.get("imageUrl"))
                    if image_url:
                        st.image(image_url, width=150)
                with cols[1]:
                    st.write(f"**Workspace:** {get_workspace_name(track.get('workspaceId'), workspaces)}")
                    assignment_options = {"Unassigned": None}
                    for workspace in workspaces:
                        assignment_options[workspace.get("name", "Unnamed Workspace")] = workspace.get("id")
                    current_workspace_id = track.get("workspaceId")
                    assignment_labels = list(assignment_options.keys())
                    assignment_values = list(assignment_options.values())
                    current_index = assignment_values.index(current_workspace_id) if current_workspace_id in assignment_values else 0
                    new_workspace_name = st.selectbox(
                        "Move to Workspace",
                        options=assignment_labels,
                        index=current_index,
                        key=f"library_move_target_{track.get('id')}"
                    )
                    new_workspace_id = assignment_options[new_workspace_name]
                    if st.button("Move Track", key=f"library_move_track_{track.get('id')}"):
                        if new_workspace_id == current_workspace_id:
                            st.info("This track is already in that workspace.")
                        else:
                            latest_history = load_history()
                            moved_count = move_tracks_to_workspace(
                                latest_history,
                                new_workspace_id,
                                track_id=track.get("id")
                            )
                            if moved_count:
                                save_history(latest_history)
                                st.rerun()
                            else:
                                st.warning("That track was not found in the saved library.")
                    st.write(f"**Tags:** {track.get('tags', '')}")
                    audio_url = track.get("audio_url", track.get("audioUrl", track.get("url")))
                    if audio_url:
                        st.audio(audio_url)
                    else:
                        st.write("Audio URL not available.")
                    
                    if st.button("🗑️ Delete Track", key=f"del_{track.get('id')}_{idx}"):
                        history.remove(track)
                        save_history(history)
                        st.rerun()

# 8. Personas
with tabs[7]:
    st.header("🎭 Persona Management")
    st.write("Extract the musical characteristics from eligible generated tracks to reuse them across new creations!")
    
    personas_state = load_personas()
    history_state = load_history()
    
    eligible_tracks = [t for t in history_state if "taskId" in t and "id" in t]
    
    with st.expander("➕ Generate New Persona", expanded=True):
        st.write("Choose a generated library track, or upload a song and create an eligible cover from it first.")
        st.caption("The provider does not create Personas directly from raw uploads. Uploaded songs are first turned into a generated cover, then the Persona is created from that result.")
        
        persona_source = st.radio("Base Music Source", ["From Library", "Upload a Song", "Manual IDs"], horizontal=True)
        
        p_task_id = ""
        p_audio_id = ""
        p_uploaded_file = None
        p_upload_title = ""
        p_upload_style = ""
        p_upload_instrumental = False
        p_upload_lyrics = ""
        p_upload_duration = 330
        
        if persona_source == "From Library":
            if not eligible_tracks:
                st.warning("No eligible tracks found in history. Please generate a new track first (Simple or Custom mode).")
            else:
                track_options = {f"{t.get('title', 'Unknown')} ({t['id']})": t for t in eligible_tracks}
                selected_track_key = st.selectbox("Select Track", options=list(track_options.keys()))
                if selected_track_key:
                    t = track_options[selected_track_key]
                    p_task_id = t["taskId"]
                    p_audio_id = t["id"]
        elif persona_source == "Upload a Song":
            p_uploaded_file = st.file_uploader(
                "Upload Song",
                type=["mp3", "wav", "m4a", "aac"],
                key="persona_upload",
                help="Only upload audio you own or are authorized to use.",
            )
            p_upload_title = st.text_input("Generated Cover Title", value="Persona Reference", key="persona_upload_title")
            p_upload_style = st.text_input(
                "Cover Style / Tags",
                key="persona_upload_style",
                placeholder="e.g. warm acoustic folk, intimate male vocal",
            )
            p_upload_instrumental = st.toggle("Create an instrumental cover", value=False, key="persona_upload_instrumental")
            p_upload_duration = st.slider(
                "Generated Cover Duration (seconds)",
                min_value=10,
                max_value=360,
                value=330,
                step=1,
                disabled=selected_model not in DURATION_MODELS,
                key="persona_upload_duration",
                help="Available when the sidebar model is V6-series or V5_5.",
            )
            p_upload_lyrics = st.text_area(
                "Lyrics for the generated cover",
                key="persona_upload_lyrics",
                disabled=p_upload_instrumental,
                help="Required for a vocal cover. These lyrics are used to make the generated track eligible for Persona creation.",
            )
        else:
            col1, col2 = st.columns(2)
            with col1:
                p_task_id = st.text_input("Task ID")
            with col2:
                p_audio_id = st.text_input("Audio ID")
                
        st.divider()
        st.write("Persona Details")
        
        p_name = st.text_input("Persona Name (e.g. Electronic Pop Singer)", help="Required")
        p_desc = st.text_area("Description", help="Required. E.g. A modern electronic music style pop singer...", height=100)
        p_style = st.text_input("Style / Tags (Optional)")
        
        col3, col4 = st.columns(2)
        with col3:
            p_vocal_start = st.number_input("Vocal Start (seconds)", min_value=0.0, value=0.0, step=1.0)
        with col4:
            p_vocal_end = st.number_input("Vocal End (seconds)", min_value=0.0, value=30.0, step=1.0)
            
        if st.button("Generate Persona"):
            analysis_length = p_vocal_end - p_vocal_start
            if not p_name or not p_desc:
                st.warning("Name and Description are required.")
            elif not 10 <= analysis_length <= 30:
                st.warning("The analysis segment must be between 10 and 30 seconds long.")
            else:
                with st.spinner("Extracting persona from track..."):
                    try:
                        if persona_source == "Upload a Song":
                            if not p_uploaded_file or not p_upload_title.strip() or not p_upload_style.strip():
                                raise ValueError("Upload a song and provide a cover title and style/tags.")
                            if not p_upload_instrumental and not p_upload_lyrics.strip():
                                raise ValueError("Provide lyrics for a vocal cover, or select an instrumental cover.")
                            with st.spinner("Uploading song and creating an eligible cover..."):
                                upload_url = upload_audio_for_suno(p_uploaded_file)
                                cover_result = st.session_state.suno_client.generate_cover(
                                    upload_url,
                                    prompt="" if p_upload_instrumental else p_upload_lyrics,
                                    style=p_upload_style,
                                    title=p_upload_title,
                                    make_instrumental=p_upload_instrumental,
                                    duration=p_upload_duration if selected_model in DURATION_MODELS else None,
                                )
                                task_ids = extract_task_ids(cover_result)
                                if not task_ids:
                                    raise ValueError("The cover request did not return a Task ID.")
                                generated_tracks = wait_for_generated_tracks(task_ids[0])
                                p_task_id = task_ids[0]
                                p_audio_id = generated_tracks[0]["id"]

                        if not p_task_id or not p_audio_id:
                            raise ValueError("Task ID and Audio ID are required.")
                        res = st.session_state.suno_client.generate_persona(
                            task_id=p_task_id, 
                            audio_id=p_audio_id, 
                            name=p_name, 
                            description=p_desc, 
                            vocal_start=p_vocal_start, 
                            vocal_end=p_vocal_end, 
                            style=p_style
                        )
                        if isinstance(res, dict) and res.get("code") == 200:
                            data = res.get("data", {})
                            if "personaId" in data:
                                new_persona = {
                                    "personaId": data["personaId"],
                                    "name": data.get("name", p_name),
                                    "description": data.get("description", p_desc),
                                    "style": p_style
                                }
                                personas_state.append(new_persona)
                                save_personas(personas_state)
                                st.success(f"Successfully generated persona: {new_persona['name']}")
                            else:
                                st.error("Response did not contain a personaId.")
                        else:
                            st.error(f"Failed to generate persona. Error: {res.get('msg')}")
                    except Exception as e:
                        st.error(f"Error generating persona: {e}")

    st.subheader("Your Personas")
    if not personas_state:
        st.info("No personas generated yet.")
    else:
        for idx, p in enumerate(personas_state):
            with st.container(border=True):
                st.write(f"**Name:** {p.get('name')}")
                st.write(f"**ID:** `{p.get('personaId')}`")
                st.write(f"**Description:** {p.get('description')}")
                if p.get('style'):
                    st.write(f"**Style:** {p.get('style')}")
                if st.button("🗑️ Delete", key=f"del_persona_{idx}"):
                    personas_state.remove(p)
                    save_personas(personas_state)
                    st.rerun()

# 9. Workspaces
with tabs[8]:
    st.header("Workspace Management")
    st.write("Organize songs by project, release, mood, or any other creative goal.")

    workspaces_state = load_workspaces()
    history_state = load_history()

    if "workspace_move_message" in st.session_state:
        st.success(st.session_state.pop("workspace_move_message"))

    selected_workspace = next(
        (workspace for workspace in workspaces_state if workspace.get("id") == st.session_state.selected_workspace_id),
        None
    )
    if selected_workspace:
        selected_workspace_id = selected_workspace.get("id")
        selected_tracks = [
            track for track in history_state if track.get("workspaceId") == selected_workspace_id
        ]
        back_col, title_col = st.columns([1, 4])
        with back_col:
            if st.button("Back", key="close_workspace"):
                st.session_state.selected_workspace_id = None
                st.rerun()
        with title_col:
            st.subheader(selected_workspace.get("name", "Unnamed Workspace"))
            if selected_workspace.get("description"):
                st.write(selected_workspace["description"])

        if not selected_tracks:
            st.info("This workspace does not have any songs yet.")
        else:
            move_options = build_workspace_options(
                workspaces_state,
                include_unassigned=True,
                exclude_workspace_id=selected_workspace_id
            )
            if move_options:
                move_select_col, move_button_col = st.columns([3, 1])
                with move_select_col:
                    target_workspace_name = st.selectbox(
                        "Move all songs to",
                        options=list(move_options.keys()),
                        key=f"move_all_target_{selected_workspace_id}"
                    )
                with move_button_col:
                    st.write("")
                    st.write("")
                    if st.button("Move Songs", key=f"move_all_songs_{selected_workspace_id}"):
                        target_workspace_id = move_options[target_workspace_name]
                        latest_history = load_history()
                        moved_count = move_tracks_to_workspace(
                            latest_history,
                            target_workspace_id,
                            source_workspace_id=selected_workspace_id
                        )
                        if moved_count:
                            save_history(latest_history)
                            saved_history = load_history()
                            saved_count = sum(
                                1 for track in saved_history
                                if track.get("workspaceId") == target_workspace_id
                            )
                            st.session_state.selected_workspace_id = None
                            st.session_state.workspace_move_message = (
                                f"Moved {moved_count} song(s) to {target_workspace_name}. "
                                f"{target_workspace_name} now has {saved_count} song(s)."
                            )
                            st.rerun()

                        else:
                            st.warning("No songs were moved.")
            else:
                st.info("Create another workspace before moving these songs.")

            for index, track in enumerate(reversed(selected_tracks)):
                track_id = track.get("id", "")
                with st.container(border=True):
                    st.write(f"**{track.get('title', f'Track_{track_id}')}**")
                    st.caption(f"Track ID: {track_id}")
                    st.write(f"**Tags:** {track.get('tags', '')}")
                    audio_url = track.get("audio_url", track.get("audioUrl", track.get("url")))
                    if audio_url:
                        st.audio(audio_url)
                    track_move_options = build_workspace_options(
                        workspaces_state,
                        include_unassigned=True,
                        exclude_workspace_id=selected_workspace_id
                    )
                    if track_move_options:
                        track_move_select_col, track_move_button_col = st.columns([3, 1])
                        with track_move_select_col:
                            track_target_workspace_name = st.selectbox(
                                "Move this track to",
                                options=list(track_move_options.keys()),
                                key=f"move_track_target_{track_id}_{index}"
                            )
                        with track_move_button_col:
                            st.write("")
                            st.write("")
                            if st.button("Move Track", key=f"move_track_{track_id}_{index}"):
                                target_workspace_id = track_move_options[track_target_workspace_name]
                                latest_history = load_history()
                                moved_count = move_tracks_to_workspace(
                                    latest_history,
                                    target_workspace_id,
                                    track_id=track_id
                                )
                                if moved_count:
                                    save_history(latest_history)
                                    saved_history = load_history()
                                    saved_count = sum(
                                        1 for saved_track in saved_history
                                        if saved_track.get("workspaceId") == target_workspace_id
                                    )
                                    st.session_state.selected_workspace_id = None
                                    st.session_state.workspace_move_message = (
                                        f"Moved track to {track_target_workspace_name}. "
                                        f"{track_target_workspace_name} now has {saved_count} song(s)."
                                    )
                                    st.rerun()
                                else:
                                    st.warning("That track was not found in the saved library.")
                    _, use_extend, use_cover, use_overlay = st.columns([2, 1, 1, 1])
                    for mode, column, label in [
                        ("extend", use_extend, "Use in Extend"),
                        ("cover", use_cover, "Use in Cover"),
                        ("overlay", use_overlay, "Use in Overlay")
                    ]:
                        with column:
                            if st.button(label, key=f"workspace_{mode}_{track_id}_{index}"):
                                st.session_state.pending_workspace_source = {
                                    "mode": mode,
                                    "track_id": track_id
                                }
                                st.rerun()
        st.divider()
        st.stop()

    with st.expander("Create Workspace", expanded=True):
        workspace_name = st.text_input("Workspace Name", key="new_workspace_name")
        workspace_description = st.text_area(
            "Description (Optional)",
            key="new_workspace_description",
            placeholder="For example: Acoustic EP, wedding songs, or summer release ideas."
        )
        _, create_col = st.columns([4, 1])
        with create_col:
            if st.button("Create Workspace", key="create_workspace"):
                clean_name = workspace_name.strip()
                if not clean_name:
                    st.warning("Please enter a workspace name.")
                elif any(w.get("name", "").lower() == clean_name.lower() for w in workspaces_state):
                    st.warning("A workspace with this name already exists.")
                else:
                    workspaces_state.append({
                        "id": str(uuid.uuid4()),
                        "name": clean_name,
                        "description": workspace_description.strip()
                    })
                    save_workspaces(workspaces_state)
                    st.rerun()

    st.subheader("Your Workspaces")
    if not workspaces_state:
        st.info("No workspaces yet. Create one above to start organizing your songs.")
    else:
        for workspace in workspaces_state:
            workspace_id = workspace.get("id")
            song_count = sum(1 for track in history_state if track.get("workspaceId") == workspace_id)
            with st.container(border=True):
                st.write(f"**{workspace.get('name', 'Unnamed Workspace')}** - {song_count} song(s)")
                if workspace.get("description"):
                    st.write(workspace["description"])

                updated_name = st.text_input(
                    "Name",
                    value=workspace.get("name", ""),
                    key=f"workspace_name_{workspace_id}"
                )
                updated_description = st.text_area(
                    "Description",
                    value=workspace.get("description", ""),
                    key=f"workspace_description_{workspace_id}"
                )
                _, open_col, update_col, delete_col = st.columns([2, 1, 1, 1])
                with open_col:
                    if st.button("Open Workspace", key=f"open_workspace_{workspace_id}"):
                        st.session_state.selected_workspace_id = workspace_id
                        st.rerun()
                with update_col:
                    if st.button("Save Changes", key=f"save_workspace_{workspace_id}"):
                        clean_name = updated_name.strip()
                        if not clean_name:
                            st.warning("Workspace name cannot be empty.")
                        elif any(w.get("id") != workspace_id and w.get("name", "").lower() == clean_name.lower() for w in workspaces_state):
                            st.warning("A workspace with this name already exists.")
                        else:
                            workspace["name"] = clean_name
                            workspace["description"] = updated_description.strip()
                            save_workspaces(workspaces_state)
                            st.rerun()
                with delete_col:
                    if st.button("Delete Workspace", key=f"delete_workspace_{workspace_id}"):
                        for track in history_state:
                            if track.get("workspaceId") == workspace_id:
                                track["workspaceId"] = None
                        save_history(history_state)
                        workspaces_state.remove(workspace)
                        save_workspaces(workspaces_state)
                        st.rerun()

# 10. Recovery & Backup
with tabs[9]:
    st.header("Suno Recovery & Permanent Backup")
    st.write("Keep a local copy of completed tracks and recover missing or broken provider audio links by original generation task.")
    st.caption("This Streamlit app has no public callback receiver. Recovery uses the configured callback value to satisfy the API, then polls the documented recovery endpoint.")
    history, manager = load_history(), backup_manager()
    missing_backup = [t for t in history if not local_backup_exists(t, manager.backup_dir)]
    affected = [t for t in missing_backup if is_affected_period(t)]
    missing_url = [t for t in missing_backup if not track_audio_url(t)]
    st.write(f"Library: {len(history)} tracks · backups missing: {len(missing_backup)} · affected-window without backup: {len(affected)} · no stored audio URL: {len(missing_url)}")

    if st.button("Back Up Existing Remote Tracks", key="backup_existing"):
        results = backup_completed_tracks(missing_backup)
        st.success(f"Backed up {sum(ok for _, ok, _ in results)} track(s).")
        for track_id, ok, message in results:
            if not ok: st.warning(f"{track_id}: {message}")

    scope = st.radio("Recovery candidates", ["Missing audio URL", "Affected time window", "Check remote links"], horizontal=True, key="recovery_scope")
    if scope == "Affected time window":
        candidates = affected
    elif scope == "Check remote links":
        if st.button("Check Remote Links", key="check_recovery_links"):
            checked = []
            for track in missing_backup:
                playable, reason = manager.remote_is_playable(track_audio_url(track))
                track.setdefault("backup", {}).update({"remoteCheck": "playable" if playable else "broken", "remoteCheckError": reason, "checkedAt": utc_now()})
                if not playable: checked.append(track.get("id"))
            save_history(history)
            st.session_state["checked_recovery_candidates"] = checked
        checked = st.session_state.get("checked_recovery_candidates", [])
        candidates = [t for t in load_history() if t.get("id") in checked]
    else:
        candidates = missing_url

    candidate_tasks = {}
    for track in candidates:
        if track.get("taskId"): candidate_tasks.setdefault(track["taskId"], []).append(track)
    if any(not t.get("taskId") for t in candidates):
        st.warning("Some candidates have no original task ID and cannot be submitted to Suno recovery.")
    if candidate_tasks:
        st.dataframe([{"Original task ID": task_id, "Tracks": len(tracks), "Titles": ", ".join(str(t.get("title", t.get("id"))) for t in tracks)} for task_id, tracks in candidate_tasks.items()], use_container_width=True)
    else:
        st.info("No recovery candidates in this selection.")
    selected = st.multiselect("Original generation tasks to recover", list(candidate_tasks), key="recovery_tasks")
    confirmed = st.checkbox("I confirm that the selected tasks should be submitted to Suno recovery.", key="recovery_confirm")
    if st.button("Submit Selected Recovery Tasks", key="submit_recovery", disabled=not (selected and confirmed)):
        for task_id in selected:
            try:
                submitted, message, recovery_id = submit_recovery_for_task(task_id, candidate_tasks[task_id])
                (st.success if submitted else st.info)(f"{task_id}: {message} {recovery_id or ''}")
            except Exception as exc:
                st.error(f"{task_id}: recovery was not submitted: {exc}")

    active = {}
    for track in load_history():
        recovery = track.get("recovery") or {}
        if recovery.get("recoveryTaskId") and recovery.get("status") in {"submitted", "running"}:
            active[recovery["recoveryTaskId"]] = recovery.get("originalTaskId") or track.get("taskId")
    if active:
        recovery_id = st.selectbox("Active recovery task", list(active), key="poll_recovery_task")
        if st.button("Poll Recovery and Save Audio", key="poll_recovery"):
            with st.spinner("Waiting for recovery result and saving recovered audio locally..."):
                completed, result = poll_recovery_and_backup(recovery_id, active[recovery_id])
            (st.success if completed else st.warning)("Recovery finished and was saved." if completed else str(result))
