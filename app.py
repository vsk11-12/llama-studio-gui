"""
Local GGUF Chat — Claude-inspired UI
Features:
  - Claude-style dark UI with vertical sidebar for navigation & chat history (pin/rename/delete support)
  - Chat tab: streaming GGUF inference with <think> / reasoning_content support
  - Settings tab: model folder, GGUF model list, generation parameters, system prompt
  - Chat history saved to JSON (~/.gguf_chat_history/)
  - Toolbar: attach (+), model selector, effort selector, token stats, stop/send
  - Shift+Enter for newline, Enter to send
"""

import asyncio
import base64
import csv
import json
import mimetypes
import os
import shutil
import subprocess
import time
import threading
import uuid
from datetime import datetime
from pathlib import Path

import flet as ft

# ── Persistent storage ──────────────────────────────────────────────────────
HISTORY_DIR = Path.home() / ".gguf_chat_history"
HISTORY_DIR.mkdir(exist_ok=True)
SETTINGS_FILE = HISTORY_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "model_folder": str(Path.home() / "local-models"),
    "model_path": "",
    "mmproj_path": "",
    "n_ctx": 4096,
    "n_gpu_layers": -1,
    "n_threads": 3,
    "temperature": 0.7,
    "top_p": 0.95,
    "repeat_penalty": 1.1,
    "max_tokens": 2048,
    "system_prompt": "You are a helpful, harmless, and honest assistant.",
    "effort": "Medium",
    "theme": "dark",
    "font_family": "Sans",
    "font_size": 14,
    "exa_enabled": False,
    "exa_api_key": "",
    "mcp_servers": [],
}


def scan_gguf_models(folder: str) -> list[Path]:
    """Return all .gguf files in folder (and one level deep), sorted by name."""
    p = Path(folder).expanduser()
    return sorted(p.glob("*.gguf")) + sorted(p.glob("*/*.gguf"))



def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {**DEFAULT_SETTINGS, **data}
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    return DEFAULT_SETTINGS.copy()


def _atomic_json_write(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def save_settings(s: dict):
    _atomic_json_write(SETTINGS_FILE, s)


def list_conversations() -> list[dict]:
    convs = []
    for f in HISTORY_DIR.glob("conv_*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("messages", []), list):
                continue
            conv_id = data.get("id")
            title = data.get("title")
            if not isinstance(conv_id, str) or not isinstance(title, str):
                continue
            convs.append({
                "id": conv_id,
                "title": title,
                "pinned": bool(data.get("pinned", False)),
                "created": data.get("created", "") if isinstance(data.get("created", ""), str) else "",
                "file": str(f),
            })
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
    # Keep pinned conversations first and newest conversations first within each group.
    convs.sort(key=lambda c: c["created"], reverse=True)
    convs.sort(key=lambda c: not c["pinned"])
    return convs


def load_conversation(conv_id: str) -> dict | None:
    if not isinstance(conv_id, str) or not conv_id or Path(conv_id).name != conv_id:
        return None
    path = HISTORY_DIR / f"conv_{conv_id}.json"
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            return None
        if data.get("id") != conv_id:
            return None
        return data
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def save_conversation(conv: dict):
    conv_id = conv.get("id") if isinstance(conv, dict) else None
    if not isinstance(conv_id, str) or not conv_id or Path(conv_id).name != conv_id:
        raise ValueError("Invalid conversation ID.")
    if not isinstance(conv.get("messages"), list):
        raise ValueError("Conversation messages must be a list.")
    p = HISTORY_DIR / f"conv_{conv_id}.json"
    _atomic_json_write(p, conv)


# ── Colour / style tokens ────────────────────────────────────────────────────
BG_PAGE = "#1a1a1a"
BG_SIDEBAR = "#141414"
BG_BUBBLE_USER = "#2a2a2a"
BG_BUBBLE_AI = "transparent"
BG_INPUT = "#2a2a2a"
BG_TOOLBAR = "#1a1a1a"
BG_SETTINGS = "#1e1e1e"
BG_CARD = "#242424"

ACCENT = "#cc785c"
ACCENT_DIM = "#a05a40"
TEXT_PRIMARY = "#ececec"
TEXT_SECONDARY = "#888888"
TEXT_HINT = "#555555"
BORDER = "#2e2e2e"
THINKING_COL = "#a0a0a0"
SIDEBAR_W = 240

# All application palettes use local/system fonts; no external font assets are loaded.
THEME_PALETTES = {
    "dark": {
        "BG_PAGE": "#1a1a1a", "BG_SIDEBAR": "#141414",
        "BG_BUBBLE_USER": "#2a2a2a", "BG_BUBBLE_AI": "transparent",
        "BG_INPUT": "#2a2a2a", "BG_TOOLBAR": "#1a1a1a",
        "BG_SETTINGS": "#1e1e1e", "BG_CARD": "#242424",
        "ACCENT": "#cc785c", "ACCENT_DIM": "#a05a40",
        "TEXT_PRIMARY": "#ececec", "TEXT_SECONDARY": "#888888",
        "TEXT_HINT": "#555555", "BORDER": "#2e2e2e",
        "THINKING_COL": "#a0a0a0",
    },
    "light": {
        "BG_PAGE": "#f5f6f8", "BG_SIDEBAR": "#e9edf2",
        "BG_BUBBLE_USER": "#e5eaf1", "BG_BUBBLE_AI": "transparent",
        "BG_INPUT": "#e5eaf1", "BG_TOOLBAR": "#f5f6f8",
        "BG_SETTINGS": "#ffffff", "BG_CARD": "#ffffff",
        "ACCENT": "#a45132", "ACCENT_DIM": "#874126",
        "TEXT_PRIMARY": "#202630", "TEXT_SECONDARY": "#586273",
        "TEXT_HINT": "#87909d", "BORDER": "#d8dee7",
        "THINKING_COL": "#687386",
    },
    "monokai": {
        "BG_PAGE": "#272822", "BG_SIDEBAR": "#1e1f1c",
        "BG_BUBBLE_USER": "#3e3d32", "BG_BUBBLE_AI": "transparent",
        "BG_INPUT": "#3e3d32", "BG_TOOLBAR": "#272822",
        "BG_SETTINGS": "#2d2e27", "BG_CARD": "#34352d",
        "ACCENT": "#a6e22e", "ACCENT_DIM": "#86b51f",
        "TEXT_PRIMARY": "#f8f8f2", "TEXT_SECONDARY": "#c2c2b5",
        "TEXT_HINT": "#858577", "BORDER": "#49483e",
        "THINKING_COL": "#aeae9d",
    },
    "midnight_teal": {
        "BG_PAGE": "#0b1720", "BG_SIDEBAR": "#08121a",
        "BG_BUBBLE_USER": "#18313b", "BG_BUBBLE_AI": "transparent",
        "BG_INPUT": "#18313b", "BG_TOOLBAR": "#0b1720",
        "BG_SETTINGS": "#10212b", "BG_CARD": "#142a34",
        "ACCENT": "#48c9b0", "ACCENT_DIM": "#2da18d",
        "TEXT_PRIMARY": "#e4f2f1", "TEXT_SECONDARY": "#9bb9ba",
        "TEXT_HINT": "#648184", "BORDER": "#25434a",
        "THINKING_COL": "#9bb9ba",
    },
    "midnight_sage": {
        "BG_PAGE": "#111b18", "BG_SIDEBAR": "#0c1512",
        "BG_BUBBLE_USER": "#24352d", "BG_BUBBLE_AI": "transparent",
        "BG_INPUT": "#24352d", "BG_TOOLBAR": "#111b18",
        "BG_SETTINGS": "#17231d", "BG_CARD": "#1d2c24",
        "ACCENT": "#a6c69a", "ACCENT_DIM": "#7f9f75",
        "TEXT_PRIMARY": "#e7eee6", "TEXT_SECONDARY": "#a8b9a6",
        "TEXT_HINT": "#718273", "BORDER": "#304438",
        "THINKING_COL": "#a8b9a6",
    },
}

def apply_theme_tokens(theme_name: str) -> dict:
    """Apply a palette to module-level UI tokens and return the previous palette."""
    global BG_PAGE, BG_SIDEBAR, BG_BUBBLE_USER, BG_BUBBLE_AI, BG_INPUT
    global BG_TOOLBAR, BG_SETTINGS, BG_CARD, ACCENT, ACCENT_DIM
    global TEXT_PRIMARY, TEXT_SECONDARY, TEXT_HINT, BORDER, THINKING_COL
    palette = THEME_PALETTES.get(theme_name, THEME_PALETTES["dark"])
    previous = {name: globals()[name] for name in palette}
    for name, value in palette.items():
        globals()[name] = value
    return previous


# ── LLM wrapper ─────────────────────────────────────────────────────────────
_llm = None
_llm_lock = threading.RLock()
_inference_lock = threading.Lock()
_llm_settings_snapshot: dict = {}


def get_llm(settings: dict):
    """Return a cached Llama instance, reloading when relevant settings change."""
    global _llm, _llm_settings_snapshot
    relevant = {k: settings.get(k) for k in ("model_path", "mmproj_path", "n_ctx", "n_gpu_layers", "n_threads")}
    if not settings.get("model_path"):
        raise RuntimeError("No model selected. Go to Settings → LLM → choose a model folder → select a model.")
    with _llm_lock:
        if _llm is None or relevant != _llm_settings_snapshot:
            try:
                from llama_cpp import Llama
                threads = settings.get("n_threads", 0)
                n_threads_arg = threads if threads > 0 else None
                new_llm = Llama(
                    model_path=settings["model_path"],
                    n_ctx=int(settings["n_ctx"]),
                    n_gpu_layers=int(settings["n_gpu_layers"]),
                    n_threads=n_threads_arg,
                    verbose=False,
                )
                old_llm = _llm
                _llm = new_llm
                _llm_settings_snapshot = relevant
                if old_llm is not None:
                    close = getattr(old_llm, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:
                            pass
            except Exception as exc:
                raise RuntimeError(f"Failed to load model: {exc}") from exc
    return _llm


# ── Attachment ingestion ─────────────────────────────────────────────────────
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 100_000
MAX_PDF_PAGES = 40


def _extract_attachment(path: Path, mime: str) -> dict:
    """Extract safe, bounded text or image payloads from common local files."""
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError(f"File is too large ({size / 1024 / 1024:.1f} MB). Limit: 20 MB.")
    ext = path.suffix.lower()
    result = {"name": path.name, "path": str(path), "mime": mime, "size": size}
    if mime.startswith("image/") or ext in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
        if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"}:
            raise ValueError(f"Unsupported image format: {mime}")
        result["b64"] = base64.b64encode(path.read_bytes()).decode("ascii")
        result["kind"] = "image"
        result["lines_count"] = 1
        return result
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        chunks = []
        for page_no, page in enumerate(reader.pages[:MAX_PDF_PAGES], 1):
            text = page.extract_text() or ""
            if text.strip():
                chunks.append(f"[Page {page_no}]\n{text}")
        result["text"] = "\n\n".join(chunks)[:MAX_TEXT_CHARS]
        result["kind"] = "document"
        result["lines_count"] = max(1, result["text"].count("\n") + 1)
        if not result["text"].strip():
            result["note"] = "No selectable text found; scanned PDFs need OCR before their contents can be read."
        return result
    if ext == ".docx":
        from docx import Document
        doc = Document(str(path))
        chunks = [para.text for para in doc.paragraphs if para.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                chunks.append(" | ".join(cell.text for cell in row.cells))
        result["text"] = "\n".join(chunks)[:MAX_TEXT_CHARS]
        result["kind"] = "document"
        result["lines_count"] = max(1, result["text"].count("\n") + 1)
        return result
    if ext == ".xlsx":
        from openpyxl import load_workbook
        wb = load_workbook(str(path), read_only=True, data_only=True)
        chunks = []
        total_len = 0
        for ws in wb.worksheets:
            header = f"[Sheet: {ws.title}]"
            chunks.append(header)
            total_len += len(header)
            if total_len >= MAX_TEXT_CHARS:
                break
            for row in ws.iter_rows(values_only=True):
                vals = ["" if v is None else str(v) for v in row]
                if any(vals):
                    line = "\t".join(vals)
                    chunks.append(line)
                    total_len += len(line) + 1
                if total_len >= MAX_TEXT_CHARS:
                    break
            if total_len >= MAX_TEXT_CHARS:
                break
        wb.close()
        result["text"] = "\n".join(chunks)[:MAX_TEXT_CHARS]
        result["kind"] = "document"
        result["lines_count"] = max(1, result["text"].count("\n") + 1)
        return result
    if ext == ".pptx":
        from pptx import Presentation
        prs = Presentation(str(path))
        chunks = []
        for idx, slide in enumerate(prs.slides, 1):
            texts = [shape.text for shape in slide.shapes if getattr(shape, "has_text_frame", False) and shape.text.strip()]
            chunks.append(f"[Slide {idx}]\n" + "\n".join(texts))
        result["text"] = "\n\n".join(chunks)[:MAX_TEXT_CHARS]
        result["kind"] = "document"
        result["lines_count"] = max(1, result["text"].count("\n") + 1)
        return result
    if ext in {".csv", ".tsv"}:
        delimiter = "\t" if ext == ".tsv" else ","
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
            rows = list(csv.reader(f, delimiter=delimiter))
        result["text"] = "\n".join("\t".join(row) for row in rows)[:MAX_TEXT_CHARS]
        result["kind"] = "document"
        result["lines_count"] = max(1, result["text"].count("\n") + 1)
        return result
    if ext in {".txt", ".md", ".py", ".json", ".yaml", ".yml", ".toml", ".xml", ".html", ".htm",
               ".js", ".ts", ".jsx", ".tsx", ".css", ".c", ".cpp", ".h", ".hpp", ".java", ".rs",
               ".go", ".rb", ".sh", ".bash", ".zsh", ".fish", ".sql", ".r", ".swift", ".kt",
               ".scala", ".lua", ".php", ".dart", ".tf", ".conf", ".ini", ".env", ".log"} or mime.startswith("text/"):
        text = path.read_text(encoding="utf-8", errors="replace")
        result["text"] = text[:MAX_TEXT_CHARS]
        result["kind"] = "text"
        result["lines_count"] = max(1, text.count("\n") + 1)
        return result
    raise ValueError(f"Unsupported file type: {ext or mime}. Supported: text, PDF, DOCX, XLSX, PPTX, CSV, and images.")


# ── Main ─────────────────────────────────────────────────────────────────────
def main(page: ft.Page):
    page.title = "Local GGUF Chat"
    page.padding = 0
    clipboard = ft.Clipboard()

    settings: dict = load_settings()
    active_theme = settings.get("theme", "dark")
    if active_theme not in THEME_PALETTES:
        active_theme = "dark"
        settings["theme"] = active_theme
    apply_theme_tokens(active_theme)
    selected_font = str(settings.get("font_family", "Sans") or "Sans")
    try:
        selected_font_size = max(10, min(24, int(settings.get("font_size", 14))))
    except (TypeError, ValueError):
        selected_font_size = 14
        settings["font_size"] = selected_font_size
    page.theme_mode = ft.ThemeMode.LIGHT if active_theme == "light" else ft.ThemeMode.DARK
    page.bgcolor = BG_PAGE
    # Use installed system fonts only. No remote font URLs or downloads.
    page.theme = ft.Theme(font_family=selected_font)
    current_conv: dict = {
        "id": str(uuid.uuid4()),
        "title": "New chat",
        "created": datetime.now().isoformat(),
        "messages": [],
    }
    generating = False
    stop_requested = False

    attached_files: list[dict] = []

    attachments_row = ft.Row(spacing=8, wrap=True, visible=False)

    def _rebuild_attachment_chips():
        attachments_row.controls.clear()
        for a in attached_files:
            mime = a.get("mime", "")
            is_img = mime.startswith("image/")
            name = a["name"]
            if is_img:
                img_src = f"data:{mime};base64,{a['b64']}" if "b64" in a else a["path"]
                content = ft.Stack([
                    ft.Image(src=img_src, width=70, height=70, fit=ft.ImageFit.COVER, border_radius=8),
                    ft.Container(
                        content=ft.IconButton(
                            icon=ft.Icons.CLOSE, icon_size=12, icon_color="#ffffff",
                            bgcolor="#80000000", border_radius=10,
                            style=ft.ButtonStyle(padding=ft.Padding(2, 2, 2, 2)),
                            data=name, on_click=lambda e: _remove_attachment(e.control.data),
                        ),
                        alignment=ft.Alignment(1, -1),
                        padding=2,
                    )
                ], width=70, height=70)
            else:
                lines_count = a.get("lines_count", 1)
                lines_str = f"{lines_count} line" if lines_count == 1 else f"{lines_count} lines"
                ext_badge = Path(name).suffix.lstrip(".").upper() or "TXT"
                content = ft.Container(
                    content=ft.Column([
                        ft.Row([
                            ft.Text(name, size=12, weight=ft.FontWeight.W_500, color=TEXT_PRIMARY,
                                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, expand=True),
                            ft.IconButton(
                                icon=ft.Icons.CLOSE, icon_size=12, icon_color=TEXT_SECONDARY,
                                style=ft.ButtonStyle(padding=ft.Padding(0, 0, 0, 0)),
                                data=name, on_click=lambda e: _remove_attachment(e.control.data),
                            ),
                        ], spacing=2, vertical_alignment=ft.CrossAxisAlignment.START),
                        ft.Text(lines_str, size=11, color=TEXT_SECONDARY),
                        ft.Container(expand=True),
                        ft.Row([
                            ft.Container(
                                content=ft.Text(ext_badge, size=9, weight=ft.FontWeight.W_600, color=TEXT_SECONDARY),
                                bgcolor="#333333", border_radius=4, padding=ft.Padding(6, 2, 6, 2),
                            )
                        ], alignment=ft.MainAxisAlignment.START),
                    ], spacing=2),
                    bgcolor="#222222", border_radius=10, border=ft.Border.all(1, BORDER),
                    padding=ft.Padding(10, 8, 8, 8), width=120, height=80,
                )
            attachments_row.controls.append(content)
        attachments_row.visible = bool(attached_files)

    def _remove_attachment(name: str):
        for i, a in enumerate(attached_files):
            if a["name"] == name:
                attached_files.pop(i)
                break
        _rebuild_attachment_chips()
        page.update()

    def _clear_attachments():
        attached_files.clear()
        attachments_row.controls.clear()
        attachments_row.visible = False

    file_picker = ft.FilePicker()
    page.services.append(file_picker)

    async def _do_pick_files():
        try:
            picked_list = await file_picker.pick_files(allow_multiple=True, with_data=True)
        except TypeError:
            picked_list = await file_picker.pick_files(allow_multiple=True)
        if not picked_list:
            return
        files = picked_list if isinstance(picked_list, list) else getattr(picked_list, "files", [])
        for f in files:
            f_path = getattr(f, "path", None)
            f_name = getattr(f, "name", str(f_path or "unnamed"))
            if f_path:
                path = Path(f_path)
            else:
                file_bytes = getattr(f, "bytes", None)
                if not file_bytes:
                    status_text.value = f"Cannot access {f_name}: picker returned neither a local path nor file bytes."
                    continue
                if len(file_bytes) > MAX_FILE_BYTES:
                    status_text.value = f"{f_name} is larger than the 20 MB attachment limit."
                    continue
                storage_dir = HISTORY_DIR / "attachments"
                storage_dir.mkdir(parents=True, exist_ok=True)
                path = storage_dir / f"{uuid.uuid4().hex}_{Path(f_name).name}"
                path.write_bytes(file_bytes)
            mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            try:
                entry = _extract_attachment(path, mime)
                entry["source_path"] = str(path.resolve())
                if entry.get("kind") == "image" and f_path:
                    storage_dir = HISTORY_DIR / "attachments"
                    storage_dir.mkdir(parents=True, exist_ok=True)
                    stored_path = storage_dir / f"{uuid.uuid4().hex}{path.suffix.lower()}"
                    shutil.copy2(path, stored_path)
                    entry["path"] = str(stored_path)
            except ImportError as exc:
                status_text.value = f"Missing attachment dependency for {f_name}: {exc}."
                continue
            except Exception as exc:
                status_text.value = f"Could not attach {f_name}: {exc}"
                continue
            if not any(a.get("source_path", a.get("path")) == entry.get("source_path", entry.get("path")) for a in attached_files):
                attached_files.append(entry)
        _rebuild_attachment_chips()
        page.update()

    def delete_conversation(conv_id: str):
        nonlocal current_conv
        p = HISTORY_DIR / f"conv_{conv_id}.json"
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass
        if current_conv["id"] == conv_id:
            new_chat()
        else:
            refresh_sidebar()

    def _open_dialog(dlg):
        if dlg not in page.overlay:
            page.overlay.append(dlg)
        dlg.open = True
        page.update()

    def _close_dialog(dlg):
        dlg.open = False
        page.update()

    def prompt_rename_conversation(conv: dict):
        rename_field = ft.TextField(
            value=conv["title"], autofocus=True, bgcolor=BG_INPUT,
            border={
                ft.ControlState.DEFAULT: ft.OutlineInputBorder(
                    border_radius=ft.BorderRadius(10, 10, 10, 10),
                    side=ft.BorderSide(color=BORDER)),
                ft.ControlState.FOCUSED: ft.OutlineInputBorder(
                    border_radius=ft.BorderRadius(10, 10, 10, 10),
                    side=ft.BorderSide(color=ACCENT, width=2)),
            },
            color=TEXT_PRIMARY,
            content_padding=ft.Padding(left=12, right=12, top=10, bottom=10),
        )

        def save_rename(_=None):
            new_title = rename_field.value.strip()
            if new_title:
                conv["title"] = new_title
                if conv["id"] == current_conv["id"]:
                    current_conv["title"] = new_title
                    try:
                        topbar_title.value = new_title
                    except Exception:
                        pass
                    save_conversation(current_conv)
                else:
                    data = load_conversation(conv["id"])
                    if data:
                        data["title"] = new_title
                        save_conversation(data)
                refresh_sidebar()
            _close_dialog(dlg)

        dlg = ft.AlertDialog(
            modal=True, bgcolor=BG_CARD,
            shape=ft.RoundedRectangleBorder(radius=14),
            title=ft.Text("Rename chat", size=15, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
            content=rename_field,
            actions=[
                ft.TextButton("Cancel", on_click=lambda _: _close_dialog(dlg)),
                ft.FilledButton("Save",
                    style=ft.ButtonStyle(bgcolor=ACCENT, color="#ffffff"),
                    on_click=save_rename),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        rename_field.on_submit = save_rename
        _open_dialog(dlg)

    # ── Search bar ───────────────────────────────────────────────────────────
    search_query = [""]
    search_field = ft.TextField(
        hint_text="Search chats…",
        hint_style=ft.TextStyle(color=TEXT_HINT, size=12),
        text_style=ft.TextStyle(color=TEXT_PRIMARY, size=12),
        border=ft.NoInputBorder(),
        filled=False,
        dense=True,
        text_vertical_align=ft.VerticalAlignment.CENTER,
        content_padding=ft.Padding(left=0, right=0, top=6, bottom=6),
        expand=True,
        cursor_color=ACCENT,
    )
    search_clear = ft.IconButton(
        icon=ft.Icons.CLOSE, icon_size=14, icon_color=TEXT_SECONDARY, visible=False,
        style=ft.ButtonStyle(padding=ft.Padding(2, 2, 2, 2)),
    )
    search_box = ft.Container(
        height=34,
        content=ft.Row(
            [ft.Icon(ft.Icons.SEARCH, size=15, color=TEXT_HINT), search_field, search_clear],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        bgcolor="#222222",
        border_radius=8,
        border=ft.Border.all(1, BORDER),
        padding=ft.Padding(left=10, right=4, top=0, bottom=0),
        margin=ft.Margin(left=8, right=8, top=8, bottom=4),
    )

    def _on_search(e):
        search_query[0] = search_field.value or ""
        search_clear.visible = bool(search_query[0])
        refresh_sidebar()

    def _on_search_clear(_):
        search_field.value = ""
        search_query[0] = ""
        search_clear.visible = False
        refresh_sidebar()

    search_field.on_change = _on_search
    search_clear.on_click = _on_search_clear

    show_search = [False]
    recent_list_col = ft.Column(spacing=2, expand=True, scroll=ft.ScrollMode.AUTO)

    def _styled_border():
        return {
            ft.ControlState.DEFAULT: ft.OutlineInputBorder(
                border_radius=ft.BorderRadius(10, 10, 10, 10),
                side=ft.BorderSide(color=BORDER)),
            ft.ControlState.FOCUSED: ft.OutlineInputBorder(
                border_radius=ft.BorderRadius(10, 10, 10, 10),
                side=ft.BorderSide(color=ACCENT, width=2)),
        }

    def _sidebar_nav_item(icon, label, on_click_fn, active=False):
        return ft.Container(
            content=ft.Row(
                [
                    ft.Icon(icon, size=16, color=TEXT_PRIMARY if active else TEXT_SECONDARY),
                    ft.Text(
                        label,
                        size=13,
                        color=TEXT_PRIMARY if active else TEXT_SECONDARY,
                        weight=ft.FontWeight.W_500 if active else ft.FontWeight.W_400,
                    ),
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding(left=12, right=12, top=8, bottom=8),
            margin=ft.Margin(left=8, right=8, top=1, bottom=1),
            border_radius=8,
            bgcolor="#262626" if active else "transparent",
            ink=True,
            on_click=on_click_fn,
        )

    def _toggle_search(_=None):
        show_search[0] = not show_search[0]
        search_box.visible = show_search[0]
        if show_search[0]:
            search_field.focus()
        else:
            search_field.value = ""
            search_query[0] = ""
            refresh_sidebar()
        page.update()

    search_box.visible = False

    POPUP_MENU_SHAPE = ft.RoundedRectangleBorder(radius=14, side=ft.BorderSide(1, BORDER))
    POPUP_MENU_ELEVATION = 8

    def _menu_item_row(icon, label, shortcut: str = "", color=TEXT_PRIMARY, is_submenu=False):
        trailing = []
        if is_submenu:
            trailing.append(ft.Icon(ft.Icons.CHEVRON_RIGHT, size=14, color=TEXT_SECONDARY))
        elif shortcut:
            trailing.append(ft.Text(shortcut, size=11, color=TEXT_HINT))
        return ft.Container(
            content=ft.Row(
                [
                    ft.Icon(icon, size=13, color=color),
                    ft.Text(label, size=12, color=color, expand=True,
                            weight=ft.FontWeight.W_400, max_lines=1),
                    *trailing,
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding(left=7, right=7, top=3, bottom=3),
            width=148,
            height=26,
            border_radius=6,
        )

    def _toggle_pin_current_item(conv):
        conv["pinned"] = not conv.get("pinned", False)
        save_conversation(conv)
        refresh_sidebar()
        page.update()

    async def _share_conv(conv):
        data = load_conversation(conv["id"])
        msgs = data.get("messages", []) if data else []
        md_lines = [f"# {conv['title']}\n"]
        for m in msgs:
            role = "User" if m["role"] == "user" else "Assistant"
            md_lines.append(f"**{role}:**\n{m['content']}\n")
        try:
            await clipboard.set("\n".join(md_lines))
            status_text.value = "Copied chat to clipboard ✓"
        except Exception as exc:
            status_text.value = f"Could not copy chat: {exc}"
        page.update()

    def _build_chats(query=""):
        recent_list_col.controls.clear()
        convs = list_conversations()
        q = query.strip().lower()
        for conv in convs:
            if q and q not in conv["title"].lower():
                continue
            is_active = conv["id"] == current_conv["id"]
            cid = conv["id"]

            item_menu = ft.PopupMenuButton(
                icon=ft.Icons.MORE_VERT,
                icon_size=14,
                icon_color=TEXT_SECONDARY,
                bgcolor=BG_CARD,
                shadow_color="#000000",
                elevation=POPUP_MENU_ELEVATION,
                shape=POPUP_MENU_SHAPE,
                menu_padding=ft.Padding(4, 4, 4, 4),
                menu_position=ft.PopupMenuPosition.OVER,
                items=[
                    ft.PopupMenuItem(height=30, padding=ft.Padding(0, 0, 0, 0),
                        content=_menu_item_row(ft.Icons.PUSH_PIN_OUTLINED, "Pin", "P"),
                        on_click=lambda _, c=conv: _toggle_pin_current_item(c)),
                    ft.PopupMenuItem(height=30, padding=ft.Padding(0, 0, 0, 0),
                        content=_menu_item_row(ft.Icons.EDIT_OUTLINED, "Rename", "R"),
                        on_click=lambda _, c=conv: prompt_rename_conversation(c)),
                    ft.PopupMenuItem(height=30, padding=ft.Padding(0, 0, 0, 0),
                        content=_menu_item_row(ft.Icons.IOS_SHARE_OUTLINED, "Share chat"),
                        on_click=lambda _, c=conv: _share_conv(c)),
                    ft.PopupMenuItem(height=8, padding=ft.Padding(0, 0, 0, 0),
                        content=ft.Divider(height=1, color=BORDER), disabled=True),
                    ft.PopupMenuItem(height=30, padding=ft.Padding(0, 0, 0, 0),
                        content=_menu_item_row(ft.Icons.DELETE_OUTLINE, "Delete", "D", color="#e5534b"),
                        on_click=lambda _, id_val=cid: delete_conversation(id_val)),
                ],
            )

            recent_list_col.controls.append(
                ft.Container(
                    content=ft.Row(
                        [
                            ft.Icon(
                                ft.Icons.PUSH_PIN if conv.get("pinned") else ft.Icons.DONUT_LARGE,
                                size=13,
                                color=ACCENT if conv.get("pinned") else TEXT_HINT,
                            ),
                            ft.Text(conv["title"], size=13,
                                    color=TEXT_PRIMARY if is_active else TEXT_SECONDARY,
                                    overflow=ft.TextOverflow.ELLIPSIS, max_lines=1, expand=True),
                            item_menu,
                        ],
                        spacing=6,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=ft.Padding(left=12, right=4, top=4, bottom=4),
                    margin=ft.Margin(left=8, right=8, top=1, bottom=1),
                    border_radius=10,
                    bgcolor="#242424" if is_active else "transparent",
                    ink=True,
                    on_click=lambda _, cid=cid: (switch_tab("chat"), open_conversation(cid)),
                )
            )

    def refresh_sidebar():
        _build_chats(search_query[0])
        page.update()

    welcome_heading = ft.Text("How can I help?", size=28, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY)
    welcome_subtitle = ft.Text("Running fully local — no data leaves your machine.", size=14, color=TEXT_SECONDARY)
    welcome = ft.Container(
        content=ft.Column(
            [
                welcome_heading,
                welcome_subtitle,
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=8,
        ),
        alignment=ft.Alignment(0, 0),
        expand=True,
        visible=True,
    )

    temporary_mode = [False]

    def new_chat(_=None):
        nonlocal current_conv
        if current_conv["messages"] and not current_conv.get("temporary"):
            save_conversation(current_conv)
        current_conv = {
            "id": str(uuid.uuid4()),
            "title": "New chat",
            "created": datetime.now().isoformat(),
            "messages": [],
        }
        chat_list.controls.clear()
        _clear_attachments()
        welcome.visible = True
        refresh_sidebar()
        status_text.value = "Idle"
        try:
            topbar_title.value = current_conv["title"]
        except Exception:
            pass
        page.update()

    def enter_temporary_chat(_=None):
        nonlocal current_conv
        if generating:
            status_text.value = "Stop generation before opening a temporary chat."
            page.update()
            return
        if current_conv.get("messages") and not current_conv.get("temporary"):
            save_conversation(current_conv)
        temporary_mode[0] = True
        current_conv = {
            "id": str(uuid.uuid4()),
            "title": "Incognito chat",
            "created": datetime.now().isoformat(),
            "messages": [],
            "temporary": True,
        }
        chat_list.controls.clear()
        _clear_attachments()
        welcome_heading.value = "You’re incognito"
        welcome_subtitle.value = "Temporary chats stay out of your saved history. They are cleared when you leave."
        welcome.visible = True
        topbar_title.value = "Incognito chat"
        status_text.value = "Temporary chat · not saved"
        temp_banner.visible = True
        sidebar_inner.visible = False
        page.update()

    def exit_temporary_chat(_=None):
        if generating:
            status_text.value = "Stop generation before leaving the temporary chat."
            page.update()
            return
        temporary_mode[0] = False
        temp_banner.visible = False
        new_chat()
        welcome_heading.value = "How can I help?"
        welcome_subtitle.value = "Running fully local — no data leaves your machine."
        sidebar_inner.visible = (current_tab[0] != "settings" and sidebar_vis[0])
        page.update()

    def clear_current_chat(_=None):
        """Clear the active conversation without creating a new chat."""
        if generating:
            status_text.value = "Stop generation before clearing this chat."
            page.update()
            return

        def _confirm_clear(_=None):
            current_conv["messages"] = []
            history_file = HISTORY_DIR / f"conv_{current_conv['id']}.json"
            try:
                history_file.unlink(missing_ok=True)
            except OSError as exc:
                status_text.value = f"Could not clear saved chat: {exc}"
                _close_dialog(dlg)
                return
            chat_list.controls.clear()
            _clear_attachments()
            welcome.visible = True
            status_text.value = "Chat cleared"
            refresh_sidebar()
            _close_dialog(dlg)

        dlg = ft.AlertDialog(
            modal=True, bgcolor=BG_CARD,
            shape=ft.RoundedRectangleBorder(radius=14),
            title=ft.Text("Clear this chat?", size=15, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
            content=ft.Text(
                "This will delete all messages and files in this chat. This cannot be undone.",
                size=13, color=TEXT_SECONDARY,
            ),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _: _close_dialog(dlg)),
                ft.FilledButton("Clear chat",
                    style=ft.ButtonStyle(bgcolor="#b94b45", color="#ffffff"),
                    on_click=_confirm_clear),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        _open_dialog(dlg)

    def open_conversation(conv_id: str):
        nonlocal current_conv
        if current_conv["messages"] and not current_conv.get("temporary"):
            save_conversation(current_conv)
        temporary_mode[0] = False
        temp_banner.visible = False
        welcome_heading.value = "How can I help?"
        welcome_subtitle.value = "Running fully local — no data leaves your machine."
        sidebar_inner.visible = (current_tab[0] != "settings" and sidebar_vis[0])
        data = load_conversation(conv_id)
        if not data:
            return
        current_conv = data
        try:
            topbar_title.value = current_conv["title"]
        except Exception:
            pass
        chat_list.controls.clear()
        for i, msg in enumerate(current_conv["messages"]):
            if msg["role"] == "user":
                chat_list.controls.append(
                    build_user_bubble(msg["content"], msg.get("attachments"), msg_idx=i))
            else:
                chat_list.controls.append(
                    build_ai_bubble_static(msg["content"], msg.get("thinking"),
                                          msg.get("thinking_elapsed")))
        welcome.visible = not bool(current_conv["messages"])
        refresh_sidebar()
        page.update()

    # ── Sidebar nav ──────────────────────────────────────────────────────────
    sidebar_nav_col = ft.Column(
        [
            _sidebar_nav_item(ft.Icons.EDIT_NOTE_OUTLINED, "New chat",
                              lambda _: (switch_tab("chat"), new_chat())),
            _sidebar_nav_item(ft.Icons.SEARCH, "Search", _toggle_search),
            search_box,
            _sidebar_nav_item(ft.Icons.ELECTRICAL_SERVICES_OUTLINED, "MCP",
                              lambda _: switch_tab("mcp")),
            _sidebar_nav_item(ft.Icons.SETTINGS_OUTLINED, "Settings",
                              lambda _: switch_tab("settings")),
        ],
        spacing=2,
    )

    mcp_col = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO)

    sidebar_inner = ft.Container(
        width=SIDEBAR_W,
        bgcolor=BG_SIDEBAR,
        border=ft.Border(right=ft.BorderSide(1, BORDER)),
        padding=ft.Padding(top=10, bottom=10, left=0, right=0),
        content=ft.Column(
            [
                sidebar_nav_col,
                ft.Container(
                    content=ft.Text("Recent conversations", size=11,
                                    color=TEXT_SECONDARY, weight=ft.FontWeight.W_500),
                    padding=ft.Padding(left=18, right=12, top=16, bottom=6),
                ),
                recent_list_col,
            ],
            spacing=0,
            expand=True,
        ),
    )

    _build_chats()

    # ── Bubble builders ──────────────────────────────────────────────────────
    def _start_edit_prompt(msg_idx: int):
        nonlocal stop_requested, generating
        if msg_idx < 0 or msg_idx >= len(current_conv["messages"]):
            return
        if generating:
            stop_requested = True
            discard_active_generation[0] = True
            if active_stop[0] is not None:
                active_stop[0].set()
        original_text = current_conv["messages"][msg_idx]["content"]
        current_conv["messages"] = current_conv["messages"][:msg_idx]
        chat_list.controls = chat_list.controls[:msg_idx]
        input_field.value = original_text
        input_field.disabled = False
        input_field.focus()
        spinner.visible = False
        status_text.value = "Stopping previous generation…" if generating else "Idle"
        if not generating:
            stop_requested = False
            send_btn.icon = ft.Icons.ARROW_UPWARD_ROUNDED
            send_btn.icon_color = ACCENT
            send_btn.tooltip = "Send"
        if not current_conv["messages"]:
            welcome.visible = True
        if not current_conv.get("temporary"):
            save_conversation(current_conv)
        page.update()

    def build_user_bubble(text: str, attachments: list | None = None, msg_idx: int = -1) -> ft.Control:
        parts: list[ft.Control] = []
        if attachments:
            chips = []
            for a in attachments:
                mime = a.get("mime", "")
                chips.append(ft.Container(
                    content=ft.Row([
                        ft.Icon(ft.Icons.IMAGE_OUTLINED if mime.startswith("image/")
                                else ft.Icons.INSERT_DRIVE_FILE_OUTLINED,
                                size=12, color=TEXT_SECONDARY),
                        ft.Text(a["name"], size=11, color=TEXT_SECONDARY,
                                max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    ], spacing=4),
                    bgcolor="#333333", border_radius=6,
                    padding=ft.Padding(left=6, right=6, top=3, bottom=3),
                ))
            parts.append(ft.Row(chips, spacing=4, wrap=True))
        parts.append(ft.Text(text, size=14, color=TEXT_PRIMARY, selectable=True))

        async def _copy_prompt(e):
            prompt_text = e.control.data or ""
            try:
                await clipboard.set(prompt_text)
                status_text.value = "Prompt copied to clipboard ✓"
            except Exception as exc:
                status_text.value = f"Could not copy prompt: {exc}"
            page.update()

        # Compact actions are placed inside the bubble, at its lower-right corner.
        copy_btn = ft.IconButton(
            icon=ft.Icons.CONTENT_COPY,
            icon_size=11,
            icon_color=TEXT_SECONDARY,
            tooltip="Copy prompt",
            visible=False,
            data=text,
            width=22,
            height=22,
            style=ft.ButtonStyle(padding=0),
            on_click=_copy_prompt,
        )
        edit_btn = ft.IconButton(
            icon=ft.Icons.EDIT_OUTLINED,
            icon_size=11,
            icon_color=TEXT_SECONDARY,
            tooltip="Edit prompt",
            visible=False,
            width=22,
            height=22,
            style=ft.ButtonStyle(padding=0),
            data=msg_idx,
            on_click=lambda e: _start_edit_prompt(e.control.data),
        )
        action_row = ft.Row(
            controls=[copy_btn, edit_btn],
            alignment=ft.MainAxisAlignment.END,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=2,
            tight=True,
        )
        bubble_content = ft.Column(
            controls=[*parts, action_row],
            spacing=5,
            tight=True,
            # Keep the bubble content intrinsic-width: short prompts stay compact,
            # while long prompts grow naturally up to the bubble's max_width.
            # END aligns the compact action row to the bubble's lower-right edge;
            # Text itself retains its normal left-to-right text alignment.
            horizontal_alignment=ft.CrossAxisAlignment.END,
        )
        bubble_box = ft.Container(
            content=bubble_content,
            bgcolor=BG_BUBBLE_USER,
            padding=ft.Padding(left=16, right=10, top=12, bottom=7),
            border_radius=ft.BorderRadius(18, 18, 4, 18),
            max_width=580,
        )

        def _on_hover(e):
            # Flet's MouseEvent.data is a string containing the hover state.
            is_hovered = str(e.data).strip().lower() in {"true", "1"}
            changed = copy_btn.visible != is_hovered or edit_btn.visible != is_hovered
            if changed:
                copy_btn.visible = is_hovered
                edit_btn.visible = is_hovered
                bubble_box.update()

        bubble_box.on_hover = _on_hover
        return ft.Row(
            controls=[bubble_box],
            alignment=ft.MainAxisAlignment.END,
            vertical_alignment=ft.CrossAxisAlignment.END,
        )

    def build_thinking_box(text: str, done: bool = True, elapsed: float | None = None) -> ft.Control:
        label = f"Thought for {elapsed:.1f}s" if (done and elapsed) else "Thinking…"
        body_txt = ft.Text(text, size=12, color=THINKING_COL, italic=True,
                           selectable=True, font_family="monospace")
        chevron = ft.Icon(ft.Icons.EXPAND_MORE, size=14, color=TEXT_SECONDARY)
        body_wrap = ft.Container(
            content=ft.Column([
                ft.Container(content=body_txt, padding=ft.Padding(left=12, right=12, top=8, bottom=8)),
            ], spacing=0),
            visible=False,
            animate_size=ft.Animation(200, ft.AnimationCurve.EASE_OUT),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )
        expanded = [False]
        def _toggle(_=None):
            expanded[0] = not expanded[0]
            body_wrap.visible = expanded[0]
            chevron.name = ft.Icons.EXPAND_LESS if expanded[0] else ft.Icons.EXPAND_MORE
            body_wrap.update()
            chevron.update()
        return ft.Container(
            content=ft.Column([
                ft.Container(
                    content=ft.Row([
                        ft.Icon(ft.Icons.AUTO_AWESOME_OUTLINED, size=14, color="#b0905a"),
                        ft.Text(label, size=12, weight=ft.FontWeight.W_500, color="#b0905a"),
                        ft.Container(expand=True), chevron,
                    ], spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    on_click=_toggle,
                    padding=ft.Padding(left=12, right=8, top=8, bottom=8), ink=True,
                ),
                body_wrap,
            ], spacing=0),
            border=ft.Border(
                left=ft.BorderSide(2, "#444444"), top=ft.BorderSide(1, "#333333"),
                right=ft.BorderSide(1, "#333333"), bottom=ft.BorderSide(1, "#333333"),
            ),
            border_radius=8, bgcolor="#1e1a16",
        )

    def build_ai_bubble_static(text: str, thinking: str | None = None,
                                thinking_elapsed: float | None = None) -> ft.Control:
        controls = []
        if thinking:
            controls.append(build_thinking_box(thinking, done=True, elapsed=thinking_elapsed))
        controls.append(ft.Markdown(text, selectable=True,
                                    extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
                                    on_tap_link=lambda e: page.launch_url(e.data)))
        return ft.Container(
            content=ft.Column(controls, spacing=8),
            padding=ft.Padding(left=4, right=4, top=4, bottom=4),
        )

    # ── Chat UI ──────────────────────────────────────────────────────────────
    chat_list = ft.ListView(
        expand=True, spacing=20, auto_scroll=True,
        padding=ft.Padding(left=48, right=48, top=32, bottom=32),
    )

    spinner = ft.ProgressRing(width=14, height=14, stroke_width=2, color=ACCENT, visible=False)
    status_text = ft.Text("Idle", size=11, color=TEXT_SECONDARY)

    input_field = ft.TextField(
        hint_text="Message your local model…",
        hint_style=ft.TextStyle(color=TEXT_HINT, size=14),
        text_style=ft.TextStyle(color=TEXT_PRIMARY, size=14),
        border=ft.NoInputBorder(),
        filled=False,
        expand=True,
        multiline=True,
        shift_enter=True,
        min_lines=1,
        max_lines=6,
        content_padding=ft.Padding(left=0, right=0, top=12, bottom=12),
        cursor_color=ACCENT,
    )

    # ── Model selector popup ─────────────────────────────────────────────────
    def _active_model_name() -> str:
        mp = settings.get("model_path", "")
        return Path(mp).stem[:28] if mp else "No model"

    model_label = ft.Text(_active_model_name(), size=12, color=TEXT_SECONDARY,
                          overflow=ft.TextOverflow.ELLIPSIS)

    def _build_model_popup_items() -> list[ft.PopupMenuItem]:
        folder = settings.get("model_folder", "")
        models = scan_gguf_models(folder) if folder else []
        if not models:
            return [ft.Container(
                content=ft.Text("No .gguf files found.\nSet folder in Settings.",
                                size=12, color=TEXT_SECONDARY, italic=True),
                padding=ft.Padding(left=8, right=8, top=8, bottom=8), width=280,
            )]
        current = settings.get("model_path", "")
        rows = []
        for m in models:
            is_active = str(m) == current
            file_size = f"{m.stat().st_size / 1e9:.1f} GB"
            def pick(_, mp=str(m)):
                settings["model_path"] = mp
                save_settings(settings)
                model_label.value = Path(mp).stem[:28]
                _rebuild_model_popup()
                _refresh_llm_model_list()
                page.update()
            rows.append(ft.PopupMenuItem(
                content=ft.Container(
                    content=ft.Row([
                        ft.Icon(ft.Icons.CHECK, size=14,
                                color=ACCENT if is_active else "transparent"),
                        ft.Column([
                            ft.Text(m.stem, size=13,
                                    color=TEXT_PRIMARY if is_active else TEXT_SECONDARY,
                                    weight=ft.FontWeight.W_500 if is_active else ft.FontWeight.W_400,
                                    overflow=ft.TextOverflow.ELLIPSIS, max_lines=1),
                            ft.Text(f"{file_size} · {m.parent.name}", size=11, color=TEXT_HINT),
                        ], spacing=1, expand=True),
                    ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    padding=ft.Padding(left=4, right=8, top=4, bottom=4), width=280,
                ),
                on_click=pick,
            ))
        return rows

    def _rebuild_model_popup():
        model_popup.items = _build_model_popup_items()
        model_label.value = _active_model_name()

    model_popup = ft.PopupMenuButton(
        bgcolor=BG_CARD,
        shadow_color="#000000",
        elevation=8,
        shape=ft.RoundedRectangleBorder(radius=14, side=ft.BorderSide(1, BORDER)),
        menu_position=ft.PopupMenuPosition.OVER,
        tooltip="Click to switch model",
        content=ft.Container(
            content=ft.Row([
                ft.Icon(ft.Icons.MEMORY_OUTLINED, size=12, color=TEXT_SECONDARY),
                model_label,
                ft.Icon(ft.Icons.KEYBOARD_ARROW_DOWN, size=12, color=TEXT_SECONDARY),
            ], spacing=4),
            padding=ft.Padding(left=8, right=8, top=4, bottom=4),
            border_radius=12,
            border=ft.Border.all(1, BORDER),
            ink=True,
        ),
        items=_build_model_popup_items(),
    )

    attach_btn = ft.IconButton(
        icon=ft.Icons.ADD, icon_color=TEXT_SECONDARY, icon_size=18, tooltip="Attach file",
        style=ft.ButtonStyle(padding=ft.Padding(left=6, right=6, top=6, bottom=6)),
        on_click=lambda _: page.run_task(_do_pick_files),
    )

    # ── Token stats / context ring ───────────────────────────────────────────
    live_context = {"conversation_id": None, "prompt_tokens": 0, "generated_tokens": 0}
    context_ring = ft.ProgressRing(value=0, width=19, height=19, stroke_width=2.2,
                                   color=ACCENT, bgcolor="#3a3a3a")

    def _get_token_stats():
        ctx_max = max(1, int(settings.get("n_ctx", 4096)))
        if live_context["conversation_id"] == current_conv.get("id"):
            prompt_toks = live_context["prompt_tokens"]
            gen_toks = live_context["generated_tokens"]
            avg_speed = live_context.get("avg_speed", 0.0)
        else:
            prompt_toks = 0
            gen_toks = 0
            try:
                if not settings.get("model_path"):
                    raise RuntimeError("No model loaded")
                llm_instance = get_llm(settings)
                for m in current_conv.get("messages", []):
                    content = m.get("content", "")
                    toks = len(llm_instance.tokenize(content.encode("utf-8"), add_bos=False, special=True))
                    if m.get("role") == "user":
                        prompt_toks += toks
                    else:
                        gen_toks += toks
            except Exception:
                total_chars = sum(len(m.get("content", "")) for m in current_conv.get("messages", []))
                prompt_toks = max(1, total_chars // 4) if current_conv.get("messages") else 0
                output_chars = sum(len(m.get("content", "")) for m in current_conv.get("messages", [])
                                   if m.get("role") == "assistant")
                gen_toks = max(0, output_chars // 4)
            avg_speed = 0.0

        used_toks = prompt_toks + gen_toks
        pct = min(100, int((used_toks / ctx_max) * 100))
        rem_toks = max(0, ctx_max - used_toks)

        def fmt_k(n):
            return f"{n/1000:.2f}K" if n >= 1000 else str(n)

        return {
            "ctx_used": fmt_k(used_toks),
            "ctx_max": fmt_k(ctx_max),
            "pct": pct,
            "rem": fmt_k(rem_toks),
            "prompt_toks": f"{prompt_toks:,}",
            "gen_toks": f"{gen_toks:,}",
            "kv_total": f"{used_toks:,}",
            "avg_speed": f"{avg_speed:.1f}t/s" if avg_speed > 0 else "Not available for saved turns",
        }

    def _build_token_popup_items():
        stats = _get_token_stats()
        return [
            ft.PopupMenuItem(
                disabled=True,
                content=ft.Container(
                    content=ft.Column([
                        ft.Row([
                            ft.Text("Context", size=12, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
                            ft.Container(expand=True),
                            ft.Text(f"{stats['ctx_used']} / {stats['ctx_max']}", size=12, color=TEXT_SECONDARY),
                        ]),
                        ft.Container(
                            content=ft.Container(bgcolor=ACCENT, border_radius=2, height=4,
                                                 width=max(4, int(180 * stats["pct"] / 100))),
                            bgcolor="#333333", border_radius=2, height=4, width=180,
                            margin=ft.Margin(top=4, bottom=4),
                        ),
                        ft.Row([
                            ft.Text(f"{stats['pct']}% used", size=11, color=TEXT_SECONDARY),
                            ft.Container(expand=True),
                            ft.Text(f"{stats['rem']} remaining", size=11, color=TEXT_SECONDARY),
                        ]),
                        ft.Divider(height=1, color=BORDER),
                        ft.Text("Token usage details", size=12, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
                        ft.Text("ACROSS ALL TURNS", size=9, color=TEXT_HINT, weight=ft.FontWeight.W_600),
                        ft.Row([ft.Text("Prompt tokens evaluated", size=11, color=TEXT_SECONDARY),
                                ft.Container(expand=True),
                                ft.Text(stats["prompt_toks"] + " tok", size=11, color=TEXT_PRIMARY)], spacing=4),
                        ft.Row([ft.Text("Tokens generated", size=11, color=TEXT_SECONDARY),
                                ft.Container(expand=True),
                                ft.Text(stats["gen_toks"] + " tok", size=11, color=TEXT_PRIMARY)], spacing=4),
                        ft.Divider(height=1, color=BORDER),
                        ft.Text("THIS TURN · KV CACHE", size=9, color=TEXT_HINT, weight=ft.FontWeight.W_600),
                        ft.Row([ft.Text("Prompt", size=11, color=TEXT_SECONDARY),
                                ft.Container(expand=True),
                                ft.Text(stats["prompt_toks"] + " tok", size=11, color=TEXT_PRIMARY)], spacing=4),
                        ft.Row([ft.Text("Generated", size=11, color=TEXT_SECONDARY),
                                ft.Container(expand=True),
                                ft.Text(stats["gen_toks"] + " tok", size=11, color=TEXT_PRIMARY)], spacing=4),
                        ft.Row([ft.Text("KV cache total", size=11, color=TEXT_SECONDARY),
                                ft.Container(expand=True),
                                ft.Text(stats["kv_total"] + " tok", size=11, color=TEXT_PRIMARY)], spacing=4),
                        ft.Divider(height=1, color=BORDER),
                        ft.Row([ft.Text("Avg speed", size=11, color=TEXT_SECONDARY),
                                ft.Container(expand=True),
                                ft.Text(stats["avg_speed"], size=11, color=TEXT_PRIMARY)], spacing=4),
                    ], spacing=4, tight=True),
                    padding=ft.Padding(8, 8, 8, 8), width=240,
                )
            )
        ]

    token_stats_popup = ft.PopupMenuButton(
        bgcolor=BG_CARD, shadow_color="#000000", elevation=8,
        shape=ft.RoundedRectangleBorder(radius=14, side=ft.BorderSide(1, BORDER)),
        menu_position=ft.PopupMenuPosition.OVER,
        tooltip="Context window usage",
        content=ft.Container(
            content=ft.Row([context_ring], spacing=4),
            width=30, height=30,
            alignment=ft.Alignment(0, 0),
            padding=ft.Padding(4, 4, 4, 4),
            border_radius=8, ink=True,
        ),
        items=_build_token_popup_items(),
    )

    def _update_context_ring():
        stats = _get_token_stats()
        context_ring.value = stats["pct"] / 100.0
        token_stats_popup.tooltip = f"Context window: {stats['pct']}% used"

    # ── Effort popup ─────────────────────────────────────────────────────────
    effort_options = [
        ("Low",    "Faster replies, minimal thinking"),
        ("Medium", "Balanced speed and reasoning"),
        ("High",   "Deeper analysis, longer context"),
    ]

    effort_label = ft.Text(f"Effort: {settings.get('effort', 'Medium')}", size=12, color=TEXT_SECONDARY)

    def _build_effort_items() -> list[ft.PopupMenuItem]:
        current = settings.get("effort", "Medium")
        items = []
        for level, desc in effort_options:
            is_active = level == current
            items.append(ft.PopupMenuItem(
                content=ft.Container(
                    content=ft.Row([
                        ft.Icon(ft.Icons.CHECK, size=13,
                                color=ACCENT if is_active else "transparent"),
                        ft.Column([
                            ft.Text(level, size=12,
                                    color=TEXT_PRIMARY if is_active else TEXT_SECONDARY,
                                    weight=ft.FontWeight.W_500 if is_active else ft.FontWeight.W_400),
                            ft.Text(desc, size=10, color=TEXT_HINT),
                        ], spacing=0, expand=True),
                    ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    padding=ft.Padding(left=6, right=8, top=4, bottom=4), width=210,
                ),
                on_click=lambda _, lvl=level: change_effort(lvl),
            ))
        return items

    def change_effort(level: str):
        settings["effort"] = level
        effort_label.value = f"Effort: {level}"
        effort_popup.items = _build_effort_items()
        save_settings(settings)
        page.update()

    effort_popup = ft.PopupMenuButton(
        bgcolor=BG_CARD, shadow_color="#000000", elevation=8,
        shape=ft.RoundedRectangleBorder(radius=14, side=ft.BorderSide(1, BORDER)),
        menu_position=ft.PopupMenuPosition.OVER,
        tooltip="Select reasoning effort",
        content=ft.Container(
            content=ft.Row([
                ft.Icon(ft.Icons.PSYCHOLOGY_OUTLINED, size=13, color=TEXT_SECONDARY),
                effort_label,
                ft.Icon(ft.Icons.KEYBOARD_ARROW_DOWN, size=12, color=TEXT_SECONDARY),
            ], spacing=4),
            padding=ft.Padding(left=8, right=8, top=4, bottom=4),
            border_radius=12,
            border=ft.Border.all(1, BORDER),
            ink=True,
        ),
        items=_build_effort_items(),
    )

    # ── Send / stop ──────────────────────────────────────────────────────────
    active_stop = [None]
    discard_active_generation = [False]

    def do_send(e):
        nonlocal generating, current_conv, stop_requested
        if generating:
            return
        prompt = input_field.value.strip()
        if not prompt and not attached_files:
            return

        conv = current_conv
        turn_settings = dict(settings)
        stop_event = threading.Event()
        active_stop[0] = stop_event
        discard_active_generation[0] = False
        generating = True
        stop_requested = False
        input_field.value = ""
        input_field.disabled = True
        snapshot = list(attached_files)
        _clear_attachments()

        text_parts = [prompt] if prompt else []
        image_parts = []
        attach_meta = []
        for a in snapshot:
            attach_meta.append({
                "name": a["name"], "mime": a.get("mime", ""),
                "size": a.get("size", 0), "kind": a.get("kind", "file"),
                "path": a.get("path", ""),
            })
            if a.get("text"):
                text_parts.append(f"\n\n--- Attached file: {a['name']} ---\n{a['text']}\n--- End of {a['name']} ---")
            if a.get("kind") == "image":
                image_parts.append({"type": "image_url", "image_url": {"url": f"data:{a['mime']};base64,{a['b64']}"}})
            for frame in a.get("frames", []):
                text_parts.append(f"\n[Video frame at {frame['timestamp']} seconds]")
                image_parts.append({"type": "image_url", "image_url": {"url": f"data:{frame['mime']};base64,{frame['b64']}"}})
            if a.get("note"):
                text_parts.append(f"\n[Note about {a['name']}: {a['note']}]")

        full_prompt = "".join(text_parts).strip()
        user_content = ([{"type": "text", "text": full_prompt}] if full_prompt else []) + image_parts
        if not image_parts:
            user_content = full_prompt

        user_msg = {"role": "user", "content": prompt, "model_context": full_prompt}
        if attach_meta:
            user_msg["attachments"] = attach_meta
        conv["messages"].append(user_msg)
        if len(conv["messages"]) == 1:
            title_source = prompt or (attach_meta[0]["name"] if attach_meta else "New chat")
            conv["title"] = title_source[:45] + ("…" if len(title_source) > 45 else "")
            if current_conv.get("id") == conv.get("id"):
                topbar_title.value = conv["title"]
        if not conv.get("temporary"):
            try:
                save_conversation(conv)
            except Exception as exc:
                status_text.value = f"Could not save conversation: {exc}"

        send_btn.icon = ft.Icons.STOP_ROUNDED
        send_btn.icon_color = ACCENT
        send_btn.tooltip = "Stop generating"
        welcome.visible = False
        pending_msg_idx = len(conv["messages"]) - 1
        chat_list.controls.append(build_user_bubble(prompt, attach_meta if attach_meta else None, msg_idx=pending_msg_idx))

        thinking_content = [""]
        thinking_elapsed = [0.0]
        think_label = ft.Text("Thinking…", size=12, weight=ft.FontWeight.W_500, color="#b0905a")
        think_chevron = ft.Icon(ft.Icons.EXPAND_LESS, size=14, color=TEXT_SECONDARY)
        think_body_txt = ft.Text("", size=12, color=THINKING_COL, italic=True,
                                 selectable=True, font_family="monospace")
        think_body = ft.Container(
            content=ft.Column([ft.Container(content=think_body_txt,
                                            padding=ft.Padding(left=12, right=12, top=8, bottom=8))], spacing=0),
            visible=True,
            animate_size=ft.Animation(200, ft.AnimationCurve.EASE_OUT),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )
        think_expanded = [True]
        def _toggle_think(_=None):
            think_expanded[0] = not think_expanded[0]
            think_body.visible = think_expanded[0]
            think_chevron.name = ft.Icons.EXPAND_LESS if think_expanded[0] else ft.Icons.EXPAND_MORE
            think_body.update()
            think_chevron.update()
        think_box = ft.Container(
            content=ft.Column([
                ft.Container(
                    content=ft.Row([
                        ft.Icon(ft.Icons.AUTO_AWESOME_OUTLINED, size=14, color="#b0905a"),
                        think_label, ft.Container(expand=True), think_chevron,
                    ], spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    on_click=_toggle_think,
                    padding=ft.Padding(left=12, right=8, top=8, bottom=8), ink=True,
                ),
                think_body,
            ], spacing=0),
            border=ft.Border(
                left=ft.BorderSide(2, "#b0905a"), top=ft.BorderSide(1, "#333333"),
                right=ft.BorderSide(1, "#333333"), bottom=ft.BorderSide(1, "#333333"),
            ),
            border_radius=8, bgcolor="#1e1a16", visible=False,
        )
        ai_md = ft.Markdown("", selectable=True,
                            extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
                            on_tap_link=lambda ev: page.launch_url(ev.data))
        ai_bubble = ft.Container(
            content=ft.Column([think_box, ai_md], spacing=8),
            padding=ft.Padding(left=4, right=4, top=4, bottom=4),
        )
        chat_list.controls.append(ai_bubble)
        page.update()

        async def generate_async():
            nonlocal generating, stop_requested
            spinner.visible = True
            status_text.value = "Processing attachments…" if snapshot else "Processing…"
            page.update()
            start = time.time()
            token_count = 0
            in_think = False
            full_response = ""
            pending = ""
            last_flush = [time.time()]
            loop = asyncio.get_running_loop()
            acquired = False

            def flush():
                try:
                    if live_context["conversation_id"] == conv.get("id"):
                        try:
                            live_context["generated_tokens"] = len(
                                llm.tokenize(full_response.encode("utf-8"), add_bos=False, special=True))
                        except Exception:
                            live_context["generated_tokens"] = max(0, len(full_response) // 4)
                        _update_context_ring()
                        token_stats_popup.items = _build_token_popup_items()
                    page.update()
                except Exception:
                    pass
                last_flush[0] = time.time()

            def maybe_flush():
                if time.time() - last_flush[0] >= 0.05:
                    flush()

            def next_chunk(iterator):
                try:
                    return next(iterator)
                except StopIteration:
                    return None

            try:
                if image_parts and not turn_settings.get("mmproj_path"):
                    raise RuntimeError("Images/video require a vision-capable GGUF setup. Set a compatible LLaVA mmproj file in Settings → LLM.")
                effort_level = turn_settings.get("effort", "Medium")
                dynamic_max_tokens = int(turn_settings.get("max_tokens", 2048))
                if effort_level == "Low":
                    dynamic_max_tokens = min(dynamic_max_tokens, 1024)
                elif effort_level == "High":
                    dynamic_max_tokens = max(dynamic_max_tokens, 4096)
                model_context_size = int(turn_settings.get("n_ctx", 4096))
                dynamic_max_tokens = min(dynamic_max_tokens, max(64, model_context_size - 256))
                messages = []
                if turn_settings.get("system_prompt"):
                    messages.append({"role": "system", "content": turn_settings["system_prompt"]})
                for m in conv["messages"][:-1]:
                    prior_content = m.get("model_context", m.get("content", ""))
                    if m.get("role") == "user" and m.get("attachments"):
                        prior_parts = [{"type": "text", "text": prior_content}] if prior_content else []
                        for old_att in m["attachments"]:
                            old_path = old_att.get("path")
                            if old_att.get("kind") == "image" and old_path and Path(old_path).is_file():
                                old_bytes = base64.b64encode(Path(old_path).read_bytes()).decode("ascii")
                                prior_parts.append({"type": "image_url", "image_url": {
                                    "url": f"data:{old_att.get('mime','image/jpeg')};base64,{old_bytes}"}})
                        if len(prior_parts) > 1:
                            prior_content = prior_parts
                    messages.append({"role": m["role"], "content": prior_content})
                messages.append({"role": "user", "content": user_content})

                await loop.run_in_executor(None, _inference_lock.acquire)
                acquired = True
                llm = await loop.run_in_executor(None, get_llm, turn_settings)
                context_limit = max(128, model_context_size - dynamic_max_tokens - 128)

                def _message_cost(message):
                    content = message.get("content", "")
                    if isinstance(content, list):
                        text_content = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
                        image_count = sum(1 for p in content if p.get("type") == "image_url")
                    else:
                        text_content = str(content)
                        image_count = 0
                    try:
                        text_tokens = len(llm.tokenize(text_content.encode("utf-8"), add_bos=False, special=True)) if text_content else 0
                    except Exception:
                        text_tokens = max(1, len(text_content) // 4) if text_content else 0
                    return text_tokens + image_count * 1024

                has_system_message = bool(messages and messages[0].get("role") == "system")
                history_start = 1 if has_system_message else 0
                current_user_index = len(messages) - 1

                while sum(_message_cost(m) for m in messages) > context_limit:
                    # Preserve the system prompt (when present) and the current request.
                    if current_user_index <= history_start:
                        break
                    first_user_index = next(
                        (i for i in range(history_start, current_user_index)
                         if messages[i].get("role") == "user"),
                        None,
                    )
                    if first_user_index is None:
                        del messages[history_start:current_user_index]
                        current_user_index = history_start
                        break
                    next_user_index = next(
                        (i for i in range(first_user_index + 1, current_user_index)
                         if messages[i].get("role") == "user"),
                        current_user_index,
                    )
                    del messages[first_user_index:next_user_index]
                    current_user_index -= next_user_index - first_user_index
                prompt_token_count = sum(_message_cost(m) for m in messages)
                if prompt_token_count > context_limit:
                    raise RuntimeError("The current prompt or attachments exceed the available context window.")
                live_context["conversation_id"] = conv.get("id")
                live_context["prompt_tokens"] = prompt_token_count
                live_context["generated_tokens"] = 0
                live_context["avg_speed"] = 0.0
                _update_context_ring()
                token_stats_popup.items = _build_token_popup_items()
                page.update()
                stream = await loop.run_in_executor(None, lambda: llm.create_chat_completion(
                    messages=messages, stream=True,
                    temperature=float(turn_settings.get("temperature", 0.7)),
                    top_p=float(turn_settings.get("top_p", 0.95)),
                    repeat_penalty=float(turn_settings.get("repeat_penalty", 1.1)),
                    max_tokens=dynamic_max_tokens,
                ))
                iterator = iter(stream)
                while not stop_event.is_set():
                    chunk = await loop.run_in_executor(None, next_chunk, iterator)
                    if chunk is None:
                        break
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    reasoning = delta.get("reasoning_content") or ""
                    content = delta.get("content") or ""
                    if reasoning:
                        think_box.visible = True
                        thinking_content[0] += reasoning
                        think_body_txt.value = thinking_content[0]
                        token_count += 1
                        elapsed = max(time.time() - start, 0.001)
                        live_context["avg_speed"] = token_count / elapsed
                        status_text.value = f"Thinking… {token_count} tok · {token_count / elapsed:.1f} tok/s"
                        maybe_flush()
                        continue
                    if not content:
                        continue
                    token_count += 1
                    pending += content
                    while pending:
                        if in_think:
                            close_at = pending.find("</think>")
                            if close_at >= 0:
                                thinking_content[0] += pending[:close_at]
                                pending = pending[close_at + len("</think>"):]
                                elapsed = max(time.time() - start, 0.001)
                                thinking_elapsed[0] = elapsed
                                think_label.value = f"Thought for {elapsed:.1f}s"
                                think_body_txt.value = thinking_content[0]
                                think_body.visible = False
                                think_expanded[0] = False
                                think_chevron.name = ft.Icons.EXPAND_MORE
                                in_think = False
                            else:
                                keep = min(len("</think>") - 1, len(pending))
                                if len(pending) > keep:
                                    thinking_content[0] += pending[:-keep]
                                    pending = pending[-keep:]
                                break
                        else:
                            open_at = pending.find("<think>")
                            if open_at >= 0:
                                visible = pending[:open_at]
                                if visible:
                                    ai_md.value += visible
                                    full_response += visible
                                pending = pending[open_at + len("<think>"):]
                                in_think = True
                                think_box.visible = True
                            else:
                                keep = min(len("<think>") - 1, len(pending))
                                if len(pending) > keep:
                                    visible = pending[:-keep]
                                    ai_md.value += visible
                                    full_response += visible
                                    pending = pending[-keep:]
                                break
                    think_body_txt.value = thinking_content[0]
                    elapsed = max(time.time() - start, 0.001)
                    live_context["avg_speed"] = token_count / elapsed
                    status_text.value = f"Generating… {token_count} tok · {token_count / elapsed:.1f} tok/s"
                    maybe_flush()

                if pending:
                    if in_think:
                        thinking_content[0] += pending
                    else:
                        ai_md.value += pending
                        full_response += pending
                flush()
                total = max(time.time() - start, 0.001)
                stopped = stop_event.is_set()
                live_context["avg_speed"] = token_count / total
                status_text.value = ("Stopped" if stopped else "Done") + f" · {token_count} tok · {total:.1f}s · {token_count / total:.1f} t/s"
                if not discard_active_generation[0]:
                    ai_msg = {"role": "assistant", "content": full_response, "stopped": stopped}
                    if thinking_content[0]:
                        ai_msg["thinking"] = thinking_content[0]
                        ai_msg["thinking_elapsed"] = thinking_elapsed[0]
                    conv["messages"].append(ai_msg)
                    if not conv.get("temporary"):
                        save_conversation(conv)
                        refresh_sidebar()
            except Exception as exc:
                if not discard_active_generation[0]:
                    status_text.value = f"Error: {exc}"
                    ai_md.value = f"⚠ {exc}"
                    conv["messages"].append({"role": "assistant", "content": f"Generation failed: {exc}", "error": True})
                    if not conv.get("temporary"):
                        try:
                            save_conversation(conv)
                        except Exception:
                            pass
            finally:
                if acquired:
                    _inference_lock.release()
                if active_stop[0] is stop_event:
                    spinner.visible = False
                    input_field.disabled = False
                    generating = False
                    stop_requested = False
                    active_stop[0] = None
                    discard_active_generation[0] = False
                    send_btn.icon = ft.Icons.ARROW_UPWARD_ROUNDED
                    send_btn.icon_color = ACCENT
                    send_btn.tooltip = "Send"
                    if live_context["conversation_id"] == conv.get("id"):
                        _update_context_ring()
                    token_stats_popup.items = _build_token_popup_items()
                    page.update()

        page.run_task(generate_async)

    def handle_send_or_stop(_):
        nonlocal stop_requested, generating
        if generating:
            stop_requested = True
            if active_stop[0] is not None:
                active_stop[0].set()
            status_text.value = "Stopping after the current inference chunk…"
            page.update()
        else:
            do_send(_)

    input_field.on_submit = do_send

    send_btn = ft.IconButton(
        icon=ft.Icons.ARROW_UPWARD_ROUNDED, icon_color=ACCENT, icon_size=20, tooltip="Send",
        style=ft.ButtonStyle(
            bgcolor={ft.ControlState.DEFAULT: "#2a2a2a", ft.ControlState.HOVERED: "#333333"},
            shape=ft.CircleBorder(), padding=ft.Padding(8, 8, 8, 8),
        ),
        on_click=handle_send_or_stop,
    )

    input_container = ft.Container(
        content=ft.Column([
            attachments_row,
            ft.Row([input_field], vertical_alignment=ft.CrossAxisAlignment.END),
            ft.Row(
                [
                    attach_btn, model_popup,
                    ft.Row([spinner, status_text], spacing=6),
                    ft.Container(expand=True),
                    token_stats_popup, effort_popup, send_btn,
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=4,
            ),
        ], spacing=4),
        bgcolor=BG_INPUT,
        border_radius=16,
        padding=ft.Padding(left=12, right=8, top=4, bottom=8),
        border=ft.Border.all(1, BORDER),
        margin=ft.Margin(left=0, right=0, top=0, bottom=16),
    )

    temp_banner = ft.Container(
        content=ft.Row([
            ft.Icon(ft.Icons.VISIBILITY_OFF_OUTLINED, size=15, color=TEXT_SECONDARY),
            ft.Text("Incognito chat", size=12, color=TEXT_PRIMARY, weight=ft.FontWeight.W_500),
            ft.Container(expand=True),
            ft.IconButton(icon=ft.Icons.CLOSE, icon_size=16, icon_color=TEXT_SECONDARY,
                          tooltip="Leave temporary chat", on_click=exit_temporary_chat),
        ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
        bgcolor=BG_TOOLBAR, padding=ft.Padding(left=14, right=8, top=2, bottom=2),
        border=ft.Border(bottom=ft.BorderSide(1, BORDER)), height=40, visible=False,
    )

    chat_view = ft.Column(
        [
            temp_banner,
            ft.Stack([chat_list, welcome], expand=True, alignment=ft.Alignment(0, 0)),
            ft.Container(content=input_container, padding=ft.Padding(left=48, right=48, top=0, bottom=0)),
        ],
        expand=True, spacing=0, visible=True,
    )

    # ── Settings (Native folder dialog) ─────────────────────────────────────
    def _open_native_folder_dialog(initial_directory: str) -> str | None:
        initial = str(Path(initial_directory).expanduser())
        zenity = shutil.which("zenity")
        if zenity:
            result = subprocess.run(
                [zenity, "--file-selection", "--directory",
                 "--title=Select model folder", f"--filename={initial.rstrip('/')}/"],
                capture_output=True, text=True, check=False)
            return result.stdout.strip() if result.returncode == 0 else None
        kdialog = shutil.which("kdialog")
        if kdialog:
            result = subprocess.run(
                [kdialog, "--getexistingdirectory", initial, "Select model folder"],
                capture_output=True, text=True, check=False)
            return result.stdout.strip() if result.returncode == 0 else None
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected = filedialog.askdirectory(title="Select model folder", initialdir=initial)
            root.destroy()
            return selected or None
        except Exception as exc:
            raise RuntimeError("No folder dialog is available.") from exc

    async def select_model_folder(_=None):
        try:
            initial_dir = str(Path(settings.get("model_folder", "")).expanduser()) if settings.get("model_folder") else str(Path.home())
            folder = await asyncio.to_thread(_open_native_folder_dialog, initial_dir)
            if not folder:
                return
            settings["model_folder"] = folder
            _refresh_llm_model_list()
            candidates = scan_gguf_models(folder)
            if candidates:
                if len(candidates) == 1 and not settings.get("model_path"):
                    settings["model_path"] = str(candidates[0])
                    model_label.value = candidates[0].stem[:28]
                status_text.value = f"Found {len(candidates)} GGUF model(s)"
            else:
                status_text.value = f"No .gguf models found in {folder}"
            save_settings(settings)
            switch_tab("settings")
            page.update()
        except Exception as exc:
            status_text.value = f"Folder picker error: {exc}"
            page.update()

    # ── Settings LLM tab ─────────────────────────────────────────────────────
    llm_model_list_col = ft.Column(spacing=2)
    llm_model_list_container = ft.Container(
        content=llm_model_list_col, bgcolor=BG_CARD, border_radius=10,
        border=ft.Border.all(1, BORDER), padding=ft.Padding(4, 4, 4, 4),
    )

    def _refresh_llm_model_list():
        llm_model_list_col.controls.clear()
        folder = settings.get("model_folder", "")
        models = scan_gguf_models(folder) if folder else []
        if not models:
            llm_model_list_col.controls.append(ft.Container(
                content=ft.Text("No .gguf files found — check the folder path above.",
                                size=12, color=TEXT_SECONDARY, italic=True),
                padding=ft.Padding(12, 12, 10, 10),
            ))
        else:
            current = settings.get("model_path", "")
            for m in models:
                is_active = str(m) == current
                def _pick(e, mp=str(m)):
                    settings["model_path"] = mp
                    save_settings(settings)
                    model_label.value = Path(mp).stem[:28]
                    _refresh_llm_model_list()
                    _rebuild_model_popup()
                    page.update()
                llm_model_list_col.controls.append(ft.Container(
                    content=ft.Row([
                        ft.Icon(ft.Icons.CHECK_CIRCLE if is_active else ft.Icons.RADIO_BUTTON_UNCHECKED,
                                size=16, color=ACCENT if is_active else TEXT_HINT),
                        ft.Column([
                            ft.Text(m.stem, size=13,
                                    color=TEXT_PRIMARY if is_active else TEXT_SECONDARY,
                                    weight=ft.FontWeight.W_500 if is_active else ft.FontWeight.W_400,
                                    overflow=ft.TextOverflow.ELLIPSIS, max_lines=1),
                            ft.Text(f"{m.stat().st_size / 1e9:.2f} GB  ·  {m.name}",
                                    size=11, color=TEXT_HINT),
                        ], spacing=1, expand=True),
                        ft.Text("Active" if is_active else "", size=11, color=ACCENT,
                                weight=ft.FontWeight.W_500),
                    ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    padding=ft.Padding(12, 12, 10, 10), border_radius=8,
                    bgcolor="#2a2a2a" if is_active else "transparent",
                    on_click=_pick, ink=True,
                ))
        try:
            page.update()
        except Exception:
            pass

    _refresh_llm_model_list()

    def make_settings_llm_content() -> ft.Control:
        s = settings

        def num_field(label, key):
            return ft.Column([
                ft.Text(label, size=12, color=TEXT_SECONDARY, weight=ft.FontWeight.W_500),
                ft.TextField(
                    value=str(s[key]), keyboard_type=ft.KeyboardType.NUMBER,
                    bgcolor=BG_INPUT, border=_styled_border(),
                    color=TEXT_PRIMARY, content_padding=ft.Padding(12, 12, 10, 10),
                    data=key, on_change=lambda e: _update_num(e),
                ),
            ], spacing=4)

        def _update_num(e):
            key = e.control.data
            limits = {
                "n_ctx": (512, 131072), "n_gpu_layers": (-1, 256),
                "n_threads": (0, 256), "temperature": (0.0, 2.0),
                "top_p": (0.0, 1.0), "repeat_penalty": (0.5, 2.0),
                "max_tokens": (1, 32768),
            }
            try:
                orig = settings[key]
                val = int(e.control.value) if isinstance(orig, int) else float(e.control.value)
                low, high = limits[key]
                if not low <= val <= high:
                    e.control.error_text = f"Enter a value from {low} to {high}."
                    e.control.update()
                    return
                e.control.error_text = None
                settings[key] = val
                e.control.update()
            except (ValueError, TypeError):
                e.control.error_text = "Enter a valid number."
                e.control.update()

        save_feedback = ft.Text("", size=12, color="#6bcf7f")

        def on_save(_):
            save_settings(settings)
            mp = settings.get("model_path", "")
            model_label.value = Path(mp).stem[:28] if mp else "No model"
            save_feedback.value = "Settings saved ✓"
            _refresh_llm_model_list()
            page.update()

        return ft.Container(
            content=ft.Column([
                ft.Text("Settings", size=20, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
                ft.Divider(color=BORDER),
                ft.Text("Model", size=13, color=ACCENT, weight=ft.FontWeight.W_600),
                ft.Text("Models folder", size=12, color=TEXT_SECONDARY, weight=ft.FontWeight.W_500),
                ft.Row([
                    ft.TextField(
                        value=str(s.get("model_folder", "")), expand=True,
                        bgcolor=BG_INPUT, border=_styled_border(),
                        color=TEXT_PRIMARY, content_padding=ft.Padding(12, 12, 10, 10),
                        on_change=lambda e: (settings.update({"model_folder": e.control.value}),
                                             _refresh_llm_model_list()),
                    ),
                    ft.OutlinedButton("Browse", icon=ft.Icons.FOLDER_OPEN,
                                      on_click=select_model_folder),
                ], spacing=8),
                ft.Text("GGUF models in folder", size=12, color=TEXT_SECONDARY, weight=ft.FontWeight.W_500),
                llm_model_list_container,
                ft.Row([num_field("Context length (n_ctx)", "n_ctx"),
                        num_field("GPU layers (-1 = all)", "n_gpu_layers")], spacing=16),
                ft.Row([num_field("CPU Threads (0 = Auto)", "n_threads")], spacing=16),
                ft.Divider(color=BORDER),
                ft.Text("Generation", size=13, color=ACCENT, weight=ft.FontWeight.W_600),
                ft.Row([num_field("Temperature", "temperature"),
                        num_field("Top-p", "top_p")], spacing=16),
                ft.Row([num_field("Repeat penalty", "repeat_penalty"),
                        num_field("Max tokens", "max_tokens")], spacing=16),
                ft.Divider(color=BORDER),
                ft.Text("System prompt", size=13, color=ACCENT, weight=ft.FontWeight.W_600),
                ft.TextField(
                    value=s["system_prompt"], multiline=True, min_lines=3, max_lines=6,
                    bgcolor=BG_INPUT, border=_styled_border(),
                    color=TEXT_PRIMARY, content_padding=ft.Padding(12, 12, 10, 10),
                    on_change=lambda e: settings.update({"system_prompt": e.control.value}),
                ),
                ft.Row([
                    ft.FilledButton("Save settings",
                        style=ft.ButtonStyle(bgcolor=ACCENT, color="#ffffff",
                                             shape=ft.RoundedRectangleBorder(radius=10)),
                        on_click=on_save),
                    save_feedback,
                ], spacing=16, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ], spacing=12, scroll=ft.ScrollMode.AUTO),
            padding=48, expand=True, bgcolor=BG_PAGE,
        )

    # ── Settings sections ────────────────────────────────────────────────────
    def _installed_system_fonts() -> list[str]:
        """Return locally installed font families; never fetch fonts from the network."""
        fonts = set()
        fc_list = shutil.which("fc-list")
        if fc_list:
            try:
                result = subprocess.run(
                    [fc_list, ":", "family"], capture_output=True, text=True,
                    check=False, timeout=3,
                )
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        # fc-list may return aliases separated by commas.
                        for family in line.split(","):
                            family = family.strip()
                            if family:
                                fonts.add(family)
            except (OSError, subprocess.SubprocessError):
                pass
        # Common local families provide sensible choices on macOS/Windows too.
        fonts.update({"Sans", "Serif", "Monospace", "Arial", "Helvetica",
                      "Times New Roman", "Georgia", "Verdana", "DejaVu Sans",
                      "Liberation Sans", "Noto Sans"})
        current = str(settings.get("font_family", "Sans"))
        fonts.add(current)
        ordered = sorted(fonts, key=str.casefold)
        return ([current] + [font for font in ordered if font != current])[:120]

    def _walk_controls(control):
        """Walk the live Flet control tree without following parent/page cycles."""
        seen = set()
        stack = [control]
        while stack:
            item = stack.pop()
            if item is None or id(item) in seen:
                continue
            seen.add(id(item))
            yield item
            children = getattr(item, "controls", None)
            if isinstance(children, (list, tuple)):
                stack.extend(children)
            content = getattr(item, "content", None)
            if content is not None:
                stack.append(content)
            # Flet styles/borders carry color properties outside the control tree.
            for attr in ("border", "style", "icon", "leading", "trailing"):
                nested = getattr(item, attr, None)
                if nested is not None and not isinstance(nested, (str, int, float, bool)):
                    stack.append(nested)

    def _recolor_value(value, color_map, seen=None):
        """Replace old palette values in Flet controls and style objects."""
        if seen is None:
            seen = set()
        if value is None or id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, (list, tuple)):
            for child in value:
                _recolor_value(child, color_map, seen)
            return
        if isinstance(value, dict):
            for child in value.values():
                _recolor_value(child, color_map, seen)
            return
        module = getattr(value.__class__, "__module__", "")
        if module.startswith("flet") or value.__class__.__name__ in {"Border", "BorderSide", "ButtonStyle"}:
            try:
                attrs = vars(value)
            except TypeError:
                return
            for attr, child in list(attrs.items()):
                if isinstance(child, str) and child in color_map:
                    try:
                        setattr(value, attr, color_map[child])
                    except Exception:
                        pass
                elif not isinstance(child, (str, int, float, bool, bytes)):
                    _recolor_value(child, color_map, seen)

    def _apply_live_theme(previous_palette: dict, next_theme: str):
        new_palette = THEME_PALETTES[next_theme]
        # A few original tokens share values (page/toolbar); map each unique value once.
        color_map = {}
        for token, old_value in previous_palette.items():
            color_map.setdefault(old_value, new_palette[token])
        for root in page.controls:
            for control in _walk_controls(root):
                _recolor_value(control, color_map)
        page.theme_mode = ft.ThemeMode.LIGHT if next_theme == "light" else ft.ThemeMode.DARK
        page.bgcolor = BG_PAGE
        page.theme = ft.Theme(font_family=str(settings.get("font_family", "Sans")))

    def _apply_live_font_size(new_size: int, old_size: int):
        factor = new_size / max(1, old_size)
        for root in page.controls:
            for control in _walk_controls(root):
                control_type = control.__class__.__name__
                if control_type == "Text":
                    size = getattr(control, "size", None)
                    if isinstance(size, (int, float)):
                        try:
                            control.size = max(9, min(32, round(size * factor, 1)))
                        except Exception:
                            pass
                elif control_type in {"TextField", "Dropdown"}:
                    size = getattr(control, "text_size", None)
                    if isinstance(size, (int, float)):
                        try:
                            control.text_size = max(9, min(24, round(size * factor, 1)))
                        except Exception:
                            pass

    def make_settings_ui_view() -> ft.Control:
        theme_names = {
            "dark": "Dark", "light": "Light", "monokai": "Monokai",
            "midnight_teal": "Midnight Teal", "midnight_sage": "Midnight Sage",
        }
        font_options = [ft.dropdown.Option(font) for font in _installed_system_fonts()]
        font_dropdown = ft.Dropdown(
            label="System font", value=selected_font, options=font_options,
            expand=True, bgcolor=BG_INPUT, border_color=BORDER,
        )
        theme_dropdown = ft.Dropdown(
            label="Color theme", value=active_theme, expand=True,
            bgcolor=BG_INPUT, border_color=BORDER,
            options=[ft.dropdown.Option(key, text=label)
                     for key, label in theme_names.items()],
        )
        size_dropdown = ft.Dropdown(
            label="Font size", value=str(selected_font_size), width=180,
            bgcolor=BG_INPUT, border_color=BORDER,
            options=[ft.dropdown.Option(str(size), text=f"{size} pt")
                     for size in (11, 12, 13, 14, 15, 16, 17, 18, 20, 22)],
        )
        feedback = ft.Text("Changes are saved automatically.", size=11, color=TEXT_SECONDARY)

        def change_font(e):
            nonlocal selected_font
            selected_font = str(e.control.value or "Sans")
            settings["font_family"] = selected_font
            save_settings(settings)
            page.theme = ft.Theme(font_family=selected_font)
            feedback.value = f"Font set to {selected_font}."
            page.update()

        def change_size(e):
            nonlocal selected_font_size
            try:
                next_size = max(10, min(24, int(e.control.value)))
            except (TypeError, ValueError):
                return
            old_size = selected_font_size
            selected_font_size = next_size
            settings["font_size"] = next_size
            save_settings(settings)
            _apply_live_font_size(next_size, old_size)
            feedback.value = f"Text size set to {next_size} pt."
            page.update()

        def change_theme(e):
            nonlocal active_theme
            next_theme = str(e.control.value or "dark")
            if next_theme not in THEME_PALETTES:
                return
            previous = apply_theme_tokens(next_theme)
            active_theme = next_theme
            settings["theme"] = next_theme
            save_settings(settings)
            _apply_live_theme(previous, next_theme)
            feedback.value = f"{theme_names[next_theme]} theme applied."
            page.update()

        # Flet 0.80+ uses on_select for Dropdown selection events.
        font_dropdown.on_select = change_font
        size_dropdown.on_select = change_size
        theme_dropdown.on_select = change_theme

        return ft.Container(
            padding=32, expand=True, bgcolor=BG_PAGE,
            content=ft.Column([
                ft.Text("Interface", size=18, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
                ft.Text("Choose a locally installed system font and customize the app appearance.",
                        size=13, color=TEXT_SECONDARY),
                ft.Container(
                    content=ft.Column([
                        ft.Text("Typography", size=14, color=TEXT_PRIMARY, weight=ft.FontWeight.W_600),
                        ft.Row([font_dropdown, size_dropdown], spacing=12,
                               vertical_alignment=ft.CrossAxisAlignment.END),
                        ft.Text("Only fonts already installed on your computer are used. No font downloads are required.",
                                size=11, color=TEXT_HINT),
                    ], spacing=12),
                    bgcolor=BG_CARD, border=ft.Border.all(1, BORDER),
                    border_radius=10, padding=18,
                ),
                ft.Container(
                    content=ft.Column([
                        ft.Text("Appearance", size=14, color=TEXT_PRIMARY, weight=ft.FontWeight.W_600),
                        theme_dropdown,
                        ft.Text("Dark, Light, Monokai, Midnight Teal, and Midnight Sage.",
                                size=11, color=TEXT_HINT),
                    ], spacing=12),
                    bgcolor=BG_CARD, border=ft.Border.all(1, BORDER),
                    border_radius=10, padding=18,
                ),
                feedback,
            ], spacing=16, scroll=ft.ScrollMode.AUTO),
        )

    def make_settings_future_view() -> ft.Control:
        feature_items = [
            (ft.Icons.TRAVEL_EXPLORE_OUTLINED, "Web search", "Optional search provider integration"),
            (ft.Icons.HUB_OUTLINED, "MCP servers", "Connect external tools and context providers"),
            (ft.Icons.IMAGE_OUTLINED, "Multimodal models", "More complete image and vision model configuration"),
            (ft.Icons.TUNE_OUTLINED, "Advanced controls", "Additional model-specific and application preferences"),
        ]
        rows = []
        for icon, title, description in feature_items:
            rows.append(ft.Container(
                content=ft.Row([
                    ft.Icon(icon, size=21, color=TEXT_SECONDARY),
                    ft.Column([
                        ft.Text(title, size=13, color=TEXT_PRIMARY,
                                weight=ft.FontWeight.W_500),
                        ft.Text(description, size=12, color=TEXT_SECONDARY),
                    ], spacing=3, expand=True),
                    ft.Text("Planned", size=11, color=TEXT_HINT),
                ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                bgcolor=BG_CARD, border=ft.Border.all(1, BORDER),
                border_radius=10, padding=16,
            ))
        return ft.Container(
            padding=32, expand=True, bgcolor=BG_PAGE,
            content=ft.Column([
                ft.Text("Coming next", size=18, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
                ft.Text(
                    "A separate home for integrations and capabilities that can be added later. "
                    "These entries are informational and do not enable unfinished features.",
                    size=13, color=TEXT_SECONDARY,
                ),
                *rows,
            ], spacing=12, scroll=ft.ScrollMode.AUTO),
        )

    settings_llm_view = make_settings_llm_content()
    settings_ui_view = make_settings_ui_view()
    settings_future_view = make_settings_future_view()
    settings_section_views = {
        "settings_ui": settings_ui_view,
        "settings_llm": settings_llm_view,
        "settings_future": settings_future_view,
    }
    settings_section_buttons = {}
    current_settings_section = ["settings_llm"]

    def switch_settings_section(section: str):
        if section not in settings_section_views:
            section = "settings_llm"
        current_settings_section[0] = section
        for key, view in settings_section_views.items():
            view.visible = (key == section)
        for key, button in settings_section_buttons.items():
            active = key == section
            button.bgcolor = BG_CARD if active else "transparent"
            button.border = ft.Border.all(1, ACCENT if active else BORDER)
            button.content.color = ACCENT if active else TEXT_SECONDARY
        page.update()

    def make_settings_section_button(label: str, section: str) -> ft.Control:
        button = ft.Container(
            content=ft.Text(label, size=13, color=TEXT_SECONDARY,
                            weight=ft.FontWeight.W_500),
            padding=ft.Padding(left=16, right=16, top=9, bottom=9),
            border=ft.Border.all(1, BORDER), border_radius=8,
            bgcolor="transparent", ink=True,
            on_click=lambda _, key=section: switch_settings_section(key),
        )
        settings_section_buttons[section] = button
        return button

    settings_view = ft.Container(
        expand=True, bgcolor=BG_PAGE,
        content=ft.Column([
            ft.Container(
                padding=ft.Padding(left=32, right=32, top=26, bottom=12),
                content=ft.Row([
                    ft.Column([
                        ft.Text("Settings", size=21, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
                        ft.Text("Configure the interface, local models, and upcoming capabilities.",
                                size=12, color=TEXT_SECONDARY),
                    ], spacing=5, expand=True),
                    ft.OutlinedButton(
                        "Back to chat", icon=ft.Icons.ARROW_BACK,
                        on_click=lambda _: switch_tab("chat"),
                    ),
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ),
            ft.Container(
                padding=ft.Padding(left=32, right=32, top=4, bottom=12),
                content=ft.Row([
                    make_settings_section_button("UI", "settings_ui"),
                    make_settings_section_button("LLM", "settings_llm"),
                    make_settings_section_button("Future Features", "settings_future"),
                ], spacing=8, wrap=True),
            ),
            ft.Divider(color=BORDER, height=1),
            ft.Stack(list(settings_section_views.values()), expand=True),
        ], spacing=0, expand=True),
    )
    settings_ui_view.visible = False
    settings_llm_view.visible = True
    settings_future_view.visible = False
    for section, button in settings_section_buttons.items():
        active = section == "settings_llm"
        button.bgcolor = BG_CARD if active else "transparent"
        button.border = ft.Border.all(1, ACCENT if active else BORDER)
        button.content.color = ACCENT if active else TEXT_SECONDARY

    # ── MCP placeholder ──────────────────────────────────────────────────────
    mcp_col = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO)
    mcp_view = ft.Container(
        content=mcp_col, padding=32, expand=True, bgcolor=BG_PAGE, visible=False,
    )

    # ── Topbar ───────────────────────────────────────────────────────────────
    current_tab = ["chat"]
    sidebar_vis = [True]

    topbar_title = ft.Text(
        current_conv["title"], size=13, weight=ft.FontWeight.W_500,
        color=TEXT_PRIMARY, overflow=ft.TextOverflow.ELLIPSIS, max_lines=1,
    )

    def _open_rename_title_dlg(_=None):
        rf = ft.TextField(
            value=current_conv["title"], autofocus=True,
            bgcolor=BG_INPUT, border=_styled_border(),
            color=TEXT_PRIMARY, content_padding=ft.Padding(12, 12, 10, 10),
        )

        def _save(_=None):
            t = rf.value.strip()
            if t:
                current_conv["title"] = t
                topbar_title.value = t
                save_conversation(current_conv)
                refresh_sidebar()
                page.update()
            _close_dialog(dlg)

        dlg = ft.AlertDialog(
            modal=True, bgcolor=BG_CARD,
            shape=ft.RoundedRectangleBorder(radius=14),
            title=ft.Text("Rename chat", size=15, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
            content=rf,
            actions=[
                ft.TextButton("Cancel", on_click=lambda _: _close_dialog(dlg)),
                ft.FilledButton("Save", style=ft.ButtonStyle(bgcolor=ACCENT, color="#ffffff"), on_click=_save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        rf.on_submit = _save
        _open_dialog(dlg)

    def _toggle_pin_current(_=None):
        current_conv["pinned"] = not current_conv.get("pinned", False)
        save_conversation(current_conv)
        refresh_sidebar()
        page.update()

    async def _share_current_chat(_=None):
        md_lines = [f"# {current_conv['title']}\n"]
        for m in current_conv.get("messages", []):
            role = "User" if m["role"] == "user" else "Assistant"
            md_lines.append(f"**{role}:**\n{m['content']}\n")
        try:
            await clipboard.set("\n".join(md_lines))
            status_text.value = "Copied chat to clipboard ✓"
        except Exception as exc:
            status_text.value = f"Could not copy chat: {exc}"
        page.update()

    title_pill = ft.PopupMenuButton(
        bgcolor=BG_CARD, shadow_color="#000000",
        elevation=POPUP_MENU_ELEVATION, shape=POPUP_MENU_SHAPE,
        menu_padding=ft.Padding(4, 4, 4, 4),
        menu_position=ft.PopupMenuPosition.UNDER,
        tooltip="Chat options",
        content=ft.Container(
            content=ft.Row([
                topbar_title,
                ft.Container(
                    content=ft.Icon(ft.Icons.KEYBOARD_ARROW_DOWN, size=14, color=TEXT_SECONDARY),
                    bgcolor="#262626", border_radius=4, padding=ft.Padding(2, 2, 2, 2),
                ),
            ], spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True),
            border_radius=8, padding=ft.Padding(left=8, right=6, top=3, bottom=3), ink=True,
        ),
        items=[
            ft.PopupMenuItem(height=30, padding=ft.Padding(0, 0, 0, 0),
                content=_menu_item_row(ft.Icons.PUSH_PIN_OUTLINED, "Pin", "P"),
                on_click=_toggle_pin_current),
            ft.PopupMenuItem(height=30, padding=ft.Padding(0, 0, 0, 0),
                content=_menu_item_row(ft.Icons.EDIT_OUTLINED, "Rename", "R"),
                on_click=_open_rename_title_dlg),
            ft.PopupMenuItem(height=30, padding=ft.Padding(0, 0, 0, 0),
                content=_menu_item_row(ft.Icons.IOS_SHARE_OUTLINED, "Share chat"),
                on_click=_share_current_chat),
            ft.PopupMenuItem(height=8, padding=ft.Padding(0, 0, 0, 0),
                content=ft.Divider(height=1, color=BORDER), disabled=True),
            ft.PopupMenuItem(height=30, padding=ft.Padding(0, 0, 0, 0),
                content=_menu_item_row(ft.Icons.DELETE_OUTLINE, "Delete", "D", color="#e5534b"),
                on_click=lambda _: delete_conversation(current_conv["id"])),
        ],
    )

    # Build all views once so settings fields retain their state while navigating.
    settings_view.visible = False

    main_stack = ft.Stack(
        [chat_view, mcp_view, settings_view],
        expand=True,
    )

    # ── Tab switching ─────────────────────────────────────────────────────────
    def switch_tab(tab_name: str):
        current_tab[0] = tab_name
        chat_view.visible = (tab_name == "chat")
        mcp_view.visible = (tab_name == "mcp")
        settings_view.visible = (tab_name == "settings")
        # Give Settings a distraction-free workspace without chat navigation.
        sidebar_inner.visible = (tab_name != "settings" and sidebar_vis[0] and not temporary_mode[0])

        if tab_name == "mcp":
            mcp_col.controls.clear()
            mcp_col.controls.append(ft.Container(
                content=ft.Column([
                    ft.Icon(ft.Icons.ELECTRICAL_SERVICES_OUTLINED, size=40, color=TEXT_SECONDARY),
                    ft.Text("MCP Servers", size=20, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
                    ft.Text(
                        "Model Context Protocol server support is coming soon.\n"
                        "Configure server entries in Settings → LLM.",
                        size=14, color=TEXT_SECONDARY, text_align=ft.TextAlign.CENTER,
                    ),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                   alignment=ft.MainAxisAlignment.CENTER, spacing=12),
                alignment=ft.Alignment(0, 0), expand=True,
            ))

        # Settings is a focused workspace: hide the regular chat topbar entirely.
        # This removes chat title/actions (New chat, Settings, Clear) while settings are open.
        topbar.visible = (tab_name != "settings")
        clear_btn.visible = (tab_name == "chat")
        page.update()

    def _toggle_sidebar(_=None):
        sidebar_vis[0] = not sidebar_vis[0]
        sidebar_inner.visible = sidebar_vis[0]
        page.update()

    # Settings icon toggles between chat and settings
    def _settings_toggle(_):
        if current_tab[0] == "settings":
            switch_tab("chat")
        else:
            switch_tab("settings")

    clear_btn = ft.IconButton(
        icon=ft.Icons.CLEANING_SERVICES_OUTLINED, icon_color=TEXT_SECONDARY,
        icon_size=18, tooltip="Clear chat", on_click=clear_current_chat,
        visible=True,
    )

    topbar = ft.Container(
        content=ft.Row([
            ft.Row([
                ft.IconButton(icon=ft.Icons.MENU, icon_color=TEXT_SECONDARY, icon_size=18,
                              tooltip="Toggle sidebar", on_click=_toggle_sidebar),
                title_pill,
            ], spacing=0, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Container(expand=True),
            ft.Row([
                ft.IconButton(icon=ft.Icons.VISIBILITY_OFF_OUTLINED, icon_color=TEXT_SECONDARY,
                              icon_size=18, tooltip="Temporary chat", on_click=enter_temporary_chat),
                ft.IconButton(icon=ft.Icons.TUNE_OUTLINED, icon_color=TEXT_SECONDARY,
                              icon_size=18, tooltip="Settings", on_click=_settings_toggle),
                clear_btn,
            ], spacing=0),
        ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
        bgcolor=BG_TOOLBAR,
        border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
        padding=ft.Padding(left=4, right=8, top=4, bottom=4),
        height=48,
    )

    body = ft.Row(
        [sidebar_inner, ft.Column([topbar, main_stack], expand=True, spacing=0)],
        expand=True, spacing=0, vertical_alignment=ft.CrossAxisAlignment.START,
    )

    page.add(body)
    # Apply the saved typography scale to the initial control tree.
    if selected_font_size != 14:
        _apply_live_font_size(selected_font_size, 14)
    refresh_sidebar()


ft.run(main)
