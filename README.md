# Local GGUF Chat

A privacy-first, Claude-inspired desktop chat application for running local GGUF language models. Built with **Python** and **Flet**, it brings a sleek, modern conversational interface directly to your machine with zero cloud dependencies—ensuring your prompts, files, and conversations remain 100% local.

---

## ✨ Features

* **Modern, Claude-Inspired UI**: Clean, distraction-free interface featuring a collapsible sidebar, conversation search, pinned chats, rename/delete controls, and a responsive layout.
* **Local GGUF Inference**: Powered by `llama-cpp-python` for fast, offline execution of quantized models with configurable GPU offloading (`n_gpu_layers`), CPU threads, and context limits (`n_ctx`).
* **Reasoning & Thinking Trace Support**: Automatically parses and displays model reasoning traces (`<think>` tags and `reasoning_content`) in collapsible, timed thinking blocks.
* **Multi-Format File Attachments**:
  * **Documents**: PDF (`.pdf`), Word (`.docx`), Excel (`.xlsx`), PowerPoint (`.pptx`), and CSV/TSV files.
  * **Code & Text**: Markdown, Python, JavaScript/TypeScript, JSON, YAML, HTML/CSS, and plain text.
  * **Images**: PNG, JPEG, WEBP, GIF, and BMP support for vision-capable GGUF setups (`mmproj`).
* **Context & Token Telemetry**: Real-time context window usage ring, token counts (prompt vs. generated), KV cache estimates, and live generation speed (`tokens/sec`).
* **Incognito / Temporary Mode**: Run off-the-record chats that stay out of your persistent local history and clear automatically upon exit.
* **Live Theme & Typography Customization**: Switch seamlessly between **Dark**, **Light**, **Monokai**, **Midnight Teal**, and **Midnight Sage** palettes, and customize font sizes using locally installed system fonts.
* **Persistent Local History**: Automatically saves conversations atomically to `~/.gguf_chat_history/` as clean JSON files, with one-click Markdown export to your clipboard.

---

## 🛠️ Prerequisites

* **Python**: 3.10 or higher
* **Hardware**: A CPU or GPU capable of running GGUF models via `llama.cpp`
* **OS**: Linux, macOS, or Windows *(Linux users benefit from `zenity`, `kdialog`, or `tk` for native folder selection)*

---

## 📦 Installation

1. **Clone the repository** (or place `app.py` in your project directory):
   ```bash
   git clone https://github.com/yourusername/local-gguf-chat.git
   cd local-gguf-chat
   ```

2. **Create and activate a virtual environment**:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install the required dependencies**:
   ```bash
   pip install flet llama-cpp-python pypdf python-docx openpyxl python-pptx
   ```

   > **GPU Acceleration Note**: For CUDA, Metal, or Vulkan support, install `llama-cpp-python` with the appropriate hardware acceleration flags according to the [official llama-cpp-python documentation](https://github.com/abetlen/llama-cpp-python).

---

## 🚀 Usage

1. **Launch the application**:
   ```bash
   python app.py
   ```
   *(Alternatively, run `flet run app.py`)*

2. **Configure Your Model Folder**:
   * Open **Settings** (via the sidebar or top-right tune icon) and go to the **LLM** tab.
   * Click **Browse** (or enter a path directly) to select the directory containing your `.gguf` model files (defaults to `~/local-models`).
   * Select your desired model from the list or use the quick-switcher pill in the chat toolbar.

3. **Adjust Generation Parameters**:
   * Customize **Context Length (`n_ctx`)**, **GPU Layers (`n_gpu_layers`)**, **Temperature**, **Top-p**, **Repeat Penalty**, **Max Tokens**, and your **System Prompt**, then click **Save settings**.

---

## ⌨️ Controls & Shortcuts

| Action | Control / Shortcut |
| :--- | :--- |
| **Send Message** | `Enter` or Click **Send** (`↑`) |
| **Insert New Line** | `Shift + Enter` |
| **Stop Generation** | Click **Stop** (`■`) during streaming |
| **Edit & Re-run Prompt** | Hover over any sent message and click the **Edit** (`✎`) icon |
| **Copy Prompt / Share Chat** | Hover over a user bubble to copy, or use the chat header dropdown to export full Markdown |
| **Toggle Incognito Mode** | Click the **Visibility Off** icon in the top-right toolbar |

---

## 📁 Data & Privacy

All configuration and conversation data are stored locally on your machine using atomic file writes to prevent data corruption:

```text
~/.gguf_chat_history/
├── settings.json          # User preferences, model paths, and generation parameters
├── conv_<uuid>.json       # Individual saved chat histories
└── attachments/           # Cached copies of attached images for multimodal history
```

No telemetry, remote font downloads, or external API calls are performed by the application.

---

## 📄 License

This project is open-source and available under the [MIT License](LICENSE).