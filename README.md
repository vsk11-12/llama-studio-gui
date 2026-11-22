# Local GGUF Chat

A lightweight, privacy-focused desktop chat client for running local GGUF Large Language Models (LLMs) completely offline. Built with **Python**, **Flet**, and **llama-cpp-python**, it features a minimalist, dark-mode user interface inspired by Claude, complete with live streaming inference, collapsible reasoning blocks, and persistent local chat history.

---

## Key Features

* **100% Local & Private:** All model inference, settings, and conversation histories remain strictly on your local machine (`~/.gguf_chat_history/`). Zero telemetry or external network requests are made during inference.
* **Claude-Inspired Dark UI:** Clean, distraction-free dark mode (`#1a1a1a`) with warm terracotta accents (`#cc785c`), a collapsible conversation sidebar, and hover-based message actions (edit & resend).
* **Reasoning & `<think>` Tag Support:** Automatically detects and parses `reasoning_content` deltas and `<think>...</think>` blocks into interactive, collapsible thought panels with live elapsed-time tracking.
* **Reasoning Effort Selector:** Quickly toggle between **Low**, **Medium**, and **High** effort modes directly from the input toolbar to dynamically adjust generation token budgets.
* **File Attachments:** Attach multiple text or code files (`.py`, `.md`, `.json`, `.csv`, `.js`, etc.) directly into your prompt context with automatic content extraction.
* **Real-Time Streaming & Metrics:** Smooth, throttled UI updates (50ms intervals) displaying live token counts and generation speed (`tok/s`), with instant generation cancellation.
* **Comprehensive Model & Inference Settings:** Configure your local models directory, auto-scan `.gguf` files, and fine-tune `n_ctx`, GPU offloading (`n_gpu_layers`), CPU threads, temperature, top-p, repeat penalty, max tokens, and system prompts.

---

## Prerequisites

* **Python:** Version 3.10 or higher
* **GGUF Model File(s):** One or more `.gguf` model weights stored locally (e.g., Llama 3, Qwen, DeepSeek-R1, Mistral, Gemma).
* **System Dependencies (Linux only, optional):** `zenity`, `kdialog`, or `python3-tk` for native folder selection dialogs.

---

## Installation

1. **Clone the repository or download `app.py`:**
   ```bash
   git clone https://github.com/yourusername/local-gguf-chat.git
   cd local-gguf-chat
   ```

2. **Create and activate a virtual environment (recommended):**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install required Python packages:**
   ```bash
   pip install flet llama-cpp-python
   ```
   *(Optional) If you have an NVIDIA GPU and want CUDA acceleration, install `llama-cpp-python` with CUDA support according to the [official llama-cpp-python documentation](https://github.com/abetlen/llama-cpp-python).*

---

## Usage

1. **Launch the application:**
   ```bash
   python app.py
   ```
   *(Alternatively, run `flet run app.py`)*

2. **Configure your model folder:**
   * Click the **Settings** icon (sliders icon) in the top-right navigation bar.
   * Enter or browse to the directory containing your `.gguf` files (defaults to `~/local-models`).
   * Select your desired model from the detected list and click **Save settings**.

3. **Start chatting:**
   * Return to the chat view, type your message, and press **Enter** to send (**Shift + Enter** for a new line).
   * Use the bottom toolbar to switch models on the fly, attach code/text files (`+`), or adjust reasoning effort.

---

## Configuration & Storage

All application state and history are stored locally in your home directory under `~/.gguf_chat_history/`:

* `settings.json`: Stores your active model path, model folder, inference parameters, and custom system prompt.
* `conv_<uuid>.json`: Individual JSON files storing conversation titles, timestamps, message history, attachment metadata, and captured reasoning blocks.

### Default Inference Parameters

| Parameter | Default Value | Description |
| :--- | :--- | :--- |
| `n_ctx` | `4096` | Context window size in tokens |
| `n_gpu_layers` | `-1` | Number of layers offloaded to GPU (`-1` = all layers) |
| `n_threads` | `3` | Number of CPU threads used during generation (`0` = auto) |
| `temperature` | `0.7` | Sampling temperature for response creativity |
| `top_p` | `0.95` | Nucleus sampling probability threshold |
| `repeat_penalty` | `1.1` | Penalty applied to repeated tokens |
| `max_tokens` | `2048` | Base maximum tokens generated per response |

---

## Keyboard Shortcuts

* **Enter:** Send message
* **Shift + Enter:** Insert a new line in the message box

---

## License

This project is open-source and available under the [MIT License](LICENSE).