# THON Announcer

Records a THON org meeting, writes a draft announcement from it, and posts it
to the org's GroupMe after the president approves it.

Everything except the final post runs on this laptop. Whisper transcribes the
audio locally, and a local Ollama model writes the draft. The only thing that
leaves the machine is the announcement text, and only when you press **Post**.

## How it works

1. **Record.** Press the big Record button, or drop in a recording you already
   have. Tell the room "This meeting is being recorded" first.
2. **Write.** When you press Stop, it transcribes the meeting and writes a draft.
3. **Review.** Edit the draft. Anything the model wasn't sure about, like a
   name, date, or place, is marked `[?]` and highlighted in yellow. Not happy
   with it? Pick Shorter, More hype, or More formal and press **Rewrite it**.
   This only redoes the writing step, so it doesn't transcribe again.
4. **Post.** **Post to GroupMe** shows exactly what will be sent, then posts it
   once you confirm. **Copy** is there if you'd rather paste it yourself.

Past meetings, their transcripts, and search are under **History**.

## One-time setup (Windows)

### 1. Python

Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/).
During setup, tick **Add python.exe to PATH**.

### 2. Ollama, the local writing model

1. Download and install Ollama from [ollama.com/download](https://ollama.com/download).
2. Open a terminal and run:

   ```
   ollama pull llama3.2:3b
   ```

   This downloads the model once (about 2 GB). Ollama then runs in the
   background, with an icon in the system tray. If drafts ever fail with
   "Could not reach Ollama", open the Ollama app again.

### 3. A GroupMe bot for your group

1. Go to [dev.groupme.com/bots](https://dev.groupme.com/bots) and log in with
   your GroupMe account.
2. Click **Create Bot**, choose your org's group, and give it a name like
   "THON Announcements". You can leave the callback URL blank.
3. Copy the **Bot ID** from the list of bots.
4. In the app, click **Settings** (top right) and paste it into
   **GroupMe bot ID**, then press Save.

The bot ID is saved in `groupme_bot_id.txt`, which is kept out of git. Anyone
with the ID can post to the group, so don't share it. Setting a
`GROUPME_BOT_ID` environment variable works too, and it overrides the file.

### 4. Make it sound like you (optional)

In **Settings**, paste two or three announcements you've sent before into
**Past announcements**. New drafts will copy their tone and format. They're
saved in `announcement_examples.txt`.

## Starting the app

Double-click **THON Announcer.bat**. The first run installs everything it needs,
which takes a few minutes. After that it opens straight away.

To install by hand instead:

```
python -m pip install -r requirements.txt
python -m pip install -r requirements-desktop.txt
python run.py ui
```

`python run.py doctor` checks that the microphone library, Whisper, Ollama,
and the GroupMe bot ID are all set up.

## Recording sources

**Microphone** is the default and records the laptop's default recording
device, for in-person meetings. **Computer audio** records whatever is playing
through the speakers, for Zoom or Teams meetings. Recording is Windows only.

## Speed

Transcription picks its model automatically: `large-v3` on an NVIDIA GPU, and
the faster `small.en` on a CPU. On a typical laptop CPU, an hour-long meeting
takes roughly 15 to 30 minutes to transcribe. Writing the draft takes another
minute or two. Transcripts are cached, so **Rewrite it** and **Try again**
skip straight to the writing step.

Override the writing model with the `OLLAMA_MODEL` environment variable, or
point at a different Ollama server with `OLLAMA_HOST`.

## CLI

```
python run.py ui                 # open the app
python run.py record             # record the mic until Ctrl+C (--source speakers for Zoom)
python run.py process recordings/meeting_2026-10-11_1900.wav
python run.py devices            # list speaker-capture devices
python run.py doctor             # what's installed and what's missing
```

## Layout

```
lecture_agent/
  capture.py      microphone and WASAPI loopback recording
  transcribe.py   faster-whisper, cached to <audio>.transcript.json
  summarize.py    Ollama map-reduce: extract notes per chunk, then write the announcement
  groupme.py      Bot API posting, split into 1000-character messages
  config.py       finds the GroupMe bot ID
  pipeline.py     transcribe then summarize
  library.py      what exists on disk, and search
app/
  server.py       FastAPI backend, binds to 127.0.0.1 only
  jobs.py         background processing queue
  desktop.py      opens the app window
  static/         the frontend
recordings/       audio and cached transcripts
notes/            drafts (.md) and extracted notes (.notes.json)
```

## GPU (optional)

Transcription works on any CPU. With an NVIDIA GPU it's faster, but it needs
cuBLAS and cuDNN 9 for CUDA 12, which pip doesn't install on Windows. Put the
DLLs from [Purfview's archive](https://github.com/Purfview/whisper-standalone-win/releases/tag/libs)
in a `cuda_libs/` folder here and they're picked up automatically.
`python run.py doctor` says whether the GPU is in use.
