# 🎹 Suno-Powered Music Studio (v5.5)

A powerful Streamlit web application that interfaces with the Suno API to create, extend, and remix music with an automated workflow.

## ✨ Features

- **🎸 Simple Create**: Fast text-to-song generation. Just describe what you want and hit generate.
- **🎛️ Custom Mode**: Full control over your generation. Provide custom lyrics, granular style tags, title, and toggle instrumental mode.
- **🔁 Extend & Remix**: Continue a previously generated session song from a specific timestamp, or upload a completely new audio file to extend.
- **🎤 Cover**: Upload an existing vocal or instrumental track and re-imagine it in an entirely new genre.
- **🗣️ Vocal/Instrumental Overlay**: Upload an instrumental to add AI vocals, or upload an acapella to inject an instrumental backing band (utilizes v5.5 Vocal Consistency logic).
- **🎙️ Voice Stems**: Split a generated, uploaded, or public track into vocals and instrumental accompaniment, a full instrument stem set, or a targeted instrument stem.
- **🎭 Personas from Uploads**: Upload a song, create a generated cover from it, and turn that eligible generated track into a reusable Persona.
- **📚 Local Library & Auto-Backup**: All generated tracks are saved to `history.json` and, after generation, downloaded into a durable local audio-backup folder. Browser download remains available as a convenience.
- **🛟 Recovery & Backup**: Scan the library for missing/broken audio, safely submit selected original Suno tasks for recovery, poll the recovery task, and save recovered audio locally.

## 🚀 Installation & Setup

1. **Navigate to the Project Directory**
   Make sure you are in the project folder.
   ```bash
   cd "d:\Build Songs"
   ```

2. **Activate the Virtual Environment**
   A Python virtual environment `.venv` has already been created for you, and the dependencies are installed. To activate it, run:
   ```bash
   .\.venv\Scripts\Activate.ps1
   ```

3. **Configure the Suno API**
   - Rename the `.env.example` file to `.env` (or create a new `.env` file).
   - Open `.env` and configure your API details:
     ```env
     SUNO_API_URL=http://localhost:3000   # Replace with your unofficial/hosted Suno API base URL
     SUNO_API_KEY=your_api_key_here       # If your endpoint requires a Bearer token
     SUNO_CALLBACK_URL=https://your-public-callback.example/suno  # Required by Suno; polling is used by this app
     SUNO_AUDIO_BACKUP_DIR=audio_backups  # Optional; defaults to ./audio_backups
     ```

## 🎮 How to Use

1. **Start the Application**
   Run the following command to start the Streamlit server:
   ```bash
   streamlit run app.py
   ```

2. **Open the App**
   Your browser should automatically open `http://localhost:8501`. If it doesn't, navigate to that URL manually.

3. **Generate Music**
   - Go to the **Simple Create** or **Custom Mode** tabs.
   - Enter your prompt or lyrics.
   - Click "Generate".
   - You can safely switch between tabs while the app polls for the audio status in the background.
   - Once the status reaches "Complete", the audio player will appear, the track will be saved to your Library, and the MP3 will download automatically!

## 📂 Project Structure

- `app.py`: The main Streamlit GUI and background polling logic workflow.
- `suno_client.py`: The REST API client formatted to securely connect and make requests to the Suno endpoints.
- `history.json`: Your locally persistent database of all generated tracks.
- `audio_backups/`: Durable local audio files. Its location can be changed with `SUNO_AUDIO_BACKUP_DIR`.
- `voice_stems.json`: Your locally persistent database of completed stem-separation results.
- `requirements.txt`: Required Python packages (`streamlit`, `requests`, `python-dotenv`).
- `suno_backup.py`: Local backup, broken-link verification, affected-window detection, and idempotent recovery bookkeeping.

## Recovering old audio

Open **Recovery & Backup** in the app. First use **Check Remote Links** (or the affected-time-window filter), review the grouped original task IDs, explicitly select and confirm only the tasks you want to submit, then use **Poll Recovery and Save Audio**. The API recovers every track from an original task together. The tool records the recovery task ID and will not submit another active recovery for the same original task.
