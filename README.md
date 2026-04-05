# ⚡ AI Screen Assistant

A lightweight Windows desktop tool that captures any region of your screen,
extracts text with OCR, sends it to **Gemini via OpenRouter**, and automatically
clicks the correct answer on screen.

Designed for multiple-choice and matching questions displayed in any
application — browser-based tests, desktop LMS, certification practice tools, etc.

---

## Features

| Feature | Details |
|---|---|
| **Screen capture** | Drag to select any region (F8) |
| **OCR** | Tesseract — reads text directly from the screenshot |
| **AI solving** | Gemini 2.5 Flash Lite or Flash via OpenRouter |
| **Auto-click** | Clicks the correct answer option on screen |
| **Answer popup** | Floating overlay shows answers with a 9-second auto-dismiss timer |
| **Configurable hotkeys** | Remap F8 / CTRL+SHIFT+S from inside the app |
| **UI scaling** | A− / A+ buttons to scale the interface (90% – 130%) |
| **Persistent settings** | API key, model, hotkeys, and scale saved automatically |
| **Dark / neon UI** | CustomTkinter, fully dark theme |

---

## Models

The application supports two models via OpenRouter:

| Model | Label in app | Best for |
|---|---|---|
| `google/gemini-2.5-flash-lite` | Flash Lite (cheaper) | High-volume, low-cost usage |
| `google/gemini-2.5-flash` | Flash (better quality) | Higher accuracy and reliability |

**Flash Lite** is optimized for cost efficiency.  
**Flash** provides better answer quality at a higher cost.

---

### Approximate pricing *(as of April 2026)*

| Model | Est. cost per request | Est. requests per $1 |
|---|---|---|
| Flash Lite | ~ $0.0002 | ~5,000 |
| Flash | ~ $0.0008 | ~1,250 |

> ⚠️ **Important:** These prices are approximate and intentionally rounded up.
> Real costs may vary depending on usage and can change at any time.
>
> Always check the latest pricing here:  
> 👉 https://openrouter.ai/models
>
> The actual cost per request depends on:
> - **Selected screen region size** (larger area → more image tokens → higher cost)
> - **Amount of OCR text** (more text → more input tokens)
> - **Prompt size** (base cost contribution)
>
> In practice:
> **smaller, more precise screen selections = cheaper requests**

---

## Requirements

- **Python 3.11+**
- **Windows** (tested on Windows 10/11; Linux/macOS untested)
- **Tesseract OCR** — [download the Windows installer](https://github.com/UB-Mannheim/tesseract/wiki)
- An **OpenRouter API key** — [get one here](https://openrouter.ai/keys)

---

## Installation

### 1 — Clone the repo

```bash
git clone https://github.com/delniel/ai-screen-assistant.git
cd ai-screen-assistant
```

### 2 — Create a virtual environment (recommended)

```bash
python -m venv .venv
.venv\Scripts\activate
```

### 3 — Install Python dependencies

```bash
pip install -r requirements.txt
```

### 4 — Install Tesseract OCR

Download and run the Windows installer from:
https://github.com/UB-Mannheim/tesseract/wiki

Default install path: `C:\Program Files\Tesseract-OCR\tesseract.exe`

If you install to a different location, set the environment variable before
running the app:

```powershell
$env:TESSERACT_PATH = "D:\Tools\Tesseract\tesseract.exe"
```

You can also add this permanently via **System Properties → Environment Variables**.

### 5 — Run

```bash
python main.py
```

---

## Usage

### Step 1 — Enter your API key

Paste your OpenRouter API key into the **API KEY** field, or click **⎘ Paste**.
The key is saved locally and loaded automatically next time.

### Step 2 — Choose a model

Select **Flash Lite** for cheaper requests or **Flash** for better accuracy.

### Step 3 — Select a region

Press **F8** and drag a rectangle tightly around the question text and all
answer options. A smaller, focused region gives better OCR results and costs less.

### Step 4 — Solve

Press **CTRL+SHIFT+S**. The app will:
1. Screenshot the selected region
2. Run OCR to extract text
3. Send both the image and OCR text to Gemini
4. Display the answer in a floating popup
5. Automatically click the correct option on screen

---

## Hotkeys

| Hotkey | Action |
|---|---|
| `F8` | Drag-select capture region |
| `CTRL+SHIFT+S` | Capture + solve |

Hotkeys can be remapped via the **⌨ Hotkeys** button.

---

## UI Scaling

Use the **A−** and **A+** buttons to shrink or enlarge the interface.
Range: 90% to 130%, step 5%. The selected scale is saved automatically.

---

## Settings file

Settings are saved to:

```
~/.ai_assistant_settings.json
```

```json
{
  "api_key": "",
  "model_id": "google/gemini-2.5-flash-lite",
  "scale": 1.0,
  "hotkeys": {
    "select_region": "f8",
    "solve": "ctrl+shift+s"
  }
}
```

> **Security:** Your API key is stored in plain text in this local file.
> The file is excluded from version control via `.gitignore`.
> Never share or commit this file.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Tesseract not found` | Install Tesseract and/or set `TESSERACT_PATH` |
| `Invalid API key` | Verify your key at openrouter.ai/keys |
| `No credits` | Top up your OpenRouter balance |
| `Rate limited` | Wait a few seconds and try again |
| Answer not clicked | Make the region larger / zoom in so OCR reads text clearly |
| Hotkeys don't fire | Run as Administrator (keyboard lib may need elevated access) |

---

## Project structure

```
ai-screen-assistant/
├── main.py            # Single-file application
├── requirements.txt   # Python dependencies
├── README.md          # This file
└── .gitignore
```

---

## License

MIT — free to use, no warranty provided.
