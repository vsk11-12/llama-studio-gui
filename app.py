"""
Local GGUF Chat — Claude-inspired UI
Features:
  - Claude-style dark UI with sidebar for chat history (rename/delete support)
  - Settings tab: model path, context length, GPU layers, CPU threads, temperature, top-p, repeat penalty
  - Chat history saved to JSON (~/.gguf_chat_history/)
  - Streaming inference with <think> / reasoning_content support & live throttling
  - Toolbar: attach (+), model selector dropdown popup, reasoning effort selector, stop/send button
  - Shift+Enter for newline, Enter to send
"""

import asyncio
import base64
import json
import mimetypes
import os
import re
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
    "model_folder": str(Path.home() / "local-models"),  # folder shown in Settings
    "model_path": "",                                    # full path to the active .gguf file
    "n_ctx": 4096,
    "n_gpu_layers": -1,
    "n_threads": 3,
    "temperature": 0.7,
    "top_p": 0.95,
    "repeat_penalty": 1.1,
    "max_tokens": 2048,
    "system_prompt": "You are a helpful, harmless, and honest assistant.",
    "effort": "Medium",
}


def scan_models(folder: str) -> list[Path]:
    """Return all .gguf files in folder (and one level deep)."""
    p = Path(folder).expanduser()
    found = sorted(p.glob("*.gguf")) + sorted(p.glob("*/*.gguf"))
    return found


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_FILE.read_text())}
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()


def save_settings(s: dict):
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))


def list_conversations() -> list[dict]:
    convs = []
    for f in sorted(HISTORY_DIR.glob("conv_*.json"), reverse=True):
        try:
            data = json.loads(f.read_text())
            convs.append({"id": data["id"], "title": data["title"], "file": str(f)})
        except Exception:
            pass
    return convs


def load_conversation(conv_id: str) -> dict | None:
    p = HISTORY_DIR / f"conv_{conv_id}.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return None


def save_conversation(conv: dict):
    p = HISTORY_DIR / f"conv_{conv['id']}.json"
    p.write_text(json.dumps(conv, indent=2, ensure_ascii=False))


# ── Colour / style tokens ────────────────────────────────────────────────────
BG_PAGE = "#1a1a1a"       # near-black page
BG_SIDEBAR = "#141414"    # slightly darker sidebar
BG_BUBBLE_USER = "#2a2a2a"
BG_BUBBLE_AI = "transparent"
BG_INPUT = "#2a2a2a"
BG_TOOLBAR = "#1a1a1a"
BG_SETTINGS = "#1e1e1e"
BG_CARD = "#242424"

ACCENT = "#cc785c"         # warm terracotta — Claude's send-button hue
ACCENT_DIM = "#a05a40"
TEXT_PRIMARY = "#ececec"
TEXT_SECONDARY = "#888888"
TEXT_HINT = "#555555"
BORDER = "#2e2e2e"
THINKING_COL = "#a0a0a0"
SIDEBAR_W = 240


# ── LLM wrapper (lazy-loaded on first use / settings change) ─────────────────
_llm = None
_llm_lock = threading.Lock()
_llm_settings_snapshot: dict = {}


def get_llm(settings: dict):
    global _llm, _llm_settings_snapshot
    relevant = {k: settings[k] for k in ("model_path", "n_ctx", "n_gpu_layers", "n_threads")}
    if not settings.get("model_path"):
        raise RuntimeError("No model selected. Go to Settings → choose a model folder → select a model.")
    with _llm_lock:
        if _llm is None or relevant != _llm_settings_snapshot:
            try:
                from llama_cpp import Llama

                threads = settings.get("n_threads", 0)
                n_threads_arg = threads if threads > 0 else None

                _llm = Llama(
                    model_path=settings["model_path"],
                    n_ctx=settings["n_ctx"],
                    n_gpu_layers=settings["n_gpu_layers"],
                    n_threads=n_threads_arg,
                    verbose=False,
                )
                _llm_settings_snapshot = relevant
            except Exception as exc:
                raise RuntimeError(f"Failed to load model: {exc}") from exc
    return _llm


# ── Main ─────────────────────────────────────────────────────────────────────
def main(page: ft.Page):
    page.title = "Local GGUF Chat"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = BG_PAGE
    page.padding = 0
    page.fonts = {
        "Inter": "https://fonts.gstatic.com/s/inter/v13/UcCO3FwrK3iLTeHuS_fvQtMwCp50KnMw2boKoduKmMEVuLyfAZ9hiJ-Ek-_EeA.woff2"
    }
    page.theme = ft.Theme(font_family="Inter")

    # ── State ────────────────────────────────────────────────────────────────
    settings: dict = load_settings()
    current_conv: dict = {"id": str(uuid.uuid4()), "title": "New chat",
                          "created": datetime.now().isoformat(), "messages": []}
    generating = False
    stop_requested = False

    # ── Attached files state ──────────────────────────────────────────────────
    attached_files: list[dict] = []   # {name, path, mime, text? or b64?}

    TEXT_EXTENSIONS = {
        ".py", ".md", ".txt", ".json", ".csv", ".yaml", ".yml", ".toml",
        ".xml", ".html", ".htm", ".js", ".ts", ".jsx", ".tsx", ".css",
        ".c", ".cpp", ".h", ".hpp", ".java", ".rs", ".go", ".rb", ".sh",
        ".bash", ".zsh", ".fish", ".sql", ".r", ".swift", ".kt", ".scala",
        ".lua", ".php", ".dart", ".tf", ".conf", ".ini", ".env", ".log",
    }

    attachments_row = ft.Row(spacing=6, wrap=True, visible=False)

    def _rebuild_attachment_chips():
        attachments_row.controls.clear()
        for a in attached_files:
            mime = a.get("mime", "")
            is_img = mime.startswith("image/")
            name = a["name"]
            chip = ft.Container(
                content=ft.Row(
                    [
                        ft.Icon(
                            ft.Icons.IMAGE_OUTLINED if is_img else ft.Icons.INSERT_DRIVE_FILE_OUTLINED,
                            size=13, color=TEXT_SECONDARY,
                        ),
                        ft.Text(name, size=12, color=TEXT_PRIMARY,
                                max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        ft.IconButton(
                            icon=ft.Icons.CLOSE, icon_size=12, icon_color=TEXT_SECONDARY,
                            style=ft.ButtonStyle(padding=ft.Padding(left=2, right=2, top=2, bottom=2)),
                            data=name,
                            on_click=lambda e: _remove_attachment(e.control.data),
                        ),
                    ],
                    spacing=4,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                bgcolor="#2e2e2e",
                border_radius=8,
                padding=ft.Padding(left=8, right=4, top=4, bottom=4),
                border=ft.Border(
                    ft.BorderSide(1, BORDER), ft.BorderSide(1, BORDER),
                    ft.BorderSide(1, BORDER), ft.BorderSide(1, BORDER),
                ),
            )
            attachments_row.controls.append(chip)
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
        result = await file_picker.pick_files(allow_multiple=True)
        if not result or not result.files:
            return
        for f in result.files:
            path = Path(f.path)
            mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            entry: dict = {"name": f.name, "path": str(path), "mime": mime}
            try:
                if path.suffix.lower() in TEXT_EXTENSIONS or mime.startswith("text/"):
                    entry["text"] = path.read_text(errors="replace")[:32_000]
                else:
                    entry["b64"] = base64.b64encode(path.read_bytes()).decode()
            except Exception as exc:
                status_text.value = f"Could not read {f.name}: {exc}"
                page.update()
                continue
            if not any(a["name"] == f.name for a in attached_files):
                attached_files.append(entry)
        _rebuild_attachment_chips()
        page.update()

    # ── Sidebar ──────────────────────────────────────────────────────────────
    sidebar_list = ft.ListView(expand=True, spacing=2, padding=ft.Padding(left=8, right=8, top=4, bottom=4))

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
                    border_radius=ft.BorderRadius(10,10,10,10), side=ft.BorderSide(color=BORDER)),
                ft.ControlState.FOCUSED: ft.OutlineInputBorder(
                    border_radius=ft.BorderRadius(10,10,10,10), side=ft.BorderSide(color=ACCENT, width=2)),
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
                    try: topbar_title.value = new_title
                    except: pass
                    save_conversation(current_conv)
                else:
                    data = load_conversation(conv["id"])
                    if data:
                        data["title"] = new_title
                        save_conversation(data)
                refresh_sidebar()
            _close_dialog(dlg)
        dlg = ft.AlertDialog(
            modal=True, bgcolor=BG_CARD, shape=ft.RoundedRectangleBorder(radius=14),
            title=ft.Text("Rename chat", size=15, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
            content=rename_field,
            actions=[
                ft.TextButton("Cancel", on_click=lambda _: _close_dialog(dlg)),
                ft.FilledButton("Save", style=ft.ButtonStyle(bgcolor=ACCENT, color="#ffffff"), on_click=save_rename),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        rename_field.on_submit = save_rename
        _open_dialog(dlg)

    def sidebar_item(conv: dict) -> ft.Control:
        is_active = conv["id"] == current_conv["id"]

        menu_btn = ft.PopupMenuButton(
            icon=ft.Icons.MORE_VERT,
            icon_size=14,
            icon_color=TEXT_SECONDARY,
            bgcolor=BG_CARD,
            elevation=8,
            shape=ft.RoundedRectangleBorder(
                radius=10,
                side=ft.BorderSide(1, BORDER),
            ),
            menu_position=ft.PopupMenuPosition.OVER,
            tooltip="Chat options",
            items=[
                ft.PopupMenuItem(
                    content=ft.Row(
                        [
                            ft.Icon(ft.Icons.EDIT_OUTLINED, size=14, color=TEXT_SECONDARY),
                            ft.Text("Rename", size=12, color=TEXT_PRIMARY),
                        ],
                        spacing=8,
                    ),
                    on_click=lambda _, c=conv: prompt_rename_conversation(c),
                ),
                ft.PopupMenuItem(
                    content=ft.Row(
                        [
                            ft.Icon(ft.Icons.DELETE_OUTLINE, size=14, color="#e5534b"),
                            ft.Text("Delete", size=12, color="#e5534b"),
                        ],
                        spacing=8,
                    ),
                    on_click=lambda _, cid=conv["id"]: delete_conversation(cid),
                ),
            ],
        )

        return ft.Container(
            key=conv["id"],
            content=ft.Row(
                [
                    ft.Text(
                        conv["title"],
                        size=13,
                        color=TEXT_PRIMARY if is_active else TEXT_SECONDARY,
                        overflow=ft.TextOverflow.ELLIPSIS,
                        max_lines=1,
                        expand=True,
                    ),
                    menu_btn,
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding(left=12, right=4, top=4, bottom=4),
            border_radius=8,
            bgcolor="#2a2a2a" if is_active else "transparent",
            on_click=lambda e, c=conv: open_conversation(c["id"]),
        )

    def refresh_sidebar():
        sidebar_list.controls.clear()
        for c in list_conversations():
            sidebar_list.controls.append(sidebar_item(c))
        page.update()

    def new_chat(_=None):
        nonlocal current_conv
        if current_conv["messages"]:
            save_conversation(current_conv)
        current_conv = {
            "id": str(uuid.uuid4()),
            "title": "New chat",
            "created": datetime.now().isoformat(),
            "messages": [],
        }
        chat_list.controls.clear()
        _clear_attachments()
        refresh_sidebar()
        status_text.value = "Idle"
        try: topbar_title.value = current_conv["title"]
        except: pass
        page.update()

    def open_conversation(conv_id: str):
        nonlocal current_conv
        if current_conv["messages"]:
            save_conversation(current_conv)
        data = load_conversation(conv_id)
        if not data:
            return
        current_conv = data
        try: topbar_title.value = current_conv["title"]
        except: pass
        chat_list.controls.clear()
        for i, msg in enumerate(current_conv["messages"]):
            if msg["role"] == "user":
                chat_list.controls.append(
                    build_user_bubble(msg["content"], msg.get("attachments"), msg_idx=i))
            else:
                chat_list.controls.append(
                    build_ai_bubble_static(msg["content"], msg.get("thinking"), msg.get("thinking_elapsed")))
        refresh_sidebar()
        page.update()

    new_chat_btn = ft.IconButton(
        icon=ft.Icons.EDIT_OUTLINED,
        icon_color=TEXT_SECONDARY,
        icon_size=18,
        tooltip="New chat",
        on_click=new_chat,
    )

    sidebar_inner = ft.Container(
        width=SIDEBAR_W,
        bgcolor=BG_SIDEBAR,
        border=ft.Border(right=ft.BorderSide(1, BORDER)),
        content=ft.Column(
            controls=[
                ft.Container(
                    content=ft.Row(
                        [ft.Text("Chats", size=14, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY), new_chat_btn],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    padding=ft.Padding(left=16, right=8, top=16, bottom=8),
                ),
                ft.Divider(height=1, color=BORDER),
                sidebar_list,
            ],
            spacing=0,
            expand=True,
        ),
    )

    # ── Chat area ────────────────────────────────────────────────────────────
    def _start_edit_prompt(msg_idx: int):
        nonlocal stop_requested, generating
        if msg_idx < 0 or msg_idx >= len(current_conv["messages"]):
            return
        if generating:
            stop_requested = True
        original_text = current_conv["messages"][msg_idx]["content"]
        current_conv["messages"] = current_conv["messages"][:msg_idx]
        chat_list.controls = chat_list.controls[:msg_idx]
        input_field.value = original_text
        input_field.disabled = False
        input_field.focus()
        spinner.visible = False
        status_text.value = "Idle"
        generating = False
        stop_requested = False
        send_btn.icon = ft.Icons.ARROW_UPWARD_ROUNDED
        send_btn.icon_color = ACCENT
        send_btn.tooltip = "Send"
        if not current_conv["messages"]:
            welcome.visible = True
        save_conversation(current_conv)
        page.update()

    def build_user_bubble(text: str, attachments: list | None = None,
                          msg_idx: int = -1) -> ft.Control:
        parts: list[ft.Control] = []
        if attachments:
            chips = []
            for a in attachments:
                mime = a.get("mime", "")
                chips.append(
                    ft.Container(
                        content=ft.Row([
                            ft.Icon(
                                ft.Icons.IMAGE_OUTLINED if mime.startswith("image/")
                                else ft.Icons.INSERT_DRIVE_FILE_OUTLINED,
                                size=12, color=TEXT_SECONDARY,
                            ),
                            ft.Text(a["name"], size=11, color=TEXT_SECONDARY,
                                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        ], spacing=4),
                        bgcolor="#333333", border_radius=6,
                        padding=ft.Padding(left=6, right=6, top=3, bottom=3),
                    )
                )
            parts.append(ft.Row(chips, spacing=4, wrap=True))
        parts.append(ft.Text(text, size=14, color=TEXT_PRIMARY, selectable=True))
        edit_btn = ft.IconButton(
            icon=ft.Icons.EDIT_OUTLINED, icon_size=14, icon_color=TEXT_SECONDARY,
            tooltip="Edit & resend", visible=False,
            style=ft.ButtonStyle(padding=ft.Padding(4,4,4,4)),
            data=msg_idx,
            on_click=lambda e: _start_edit_prompt(e.control.data),
        )
        bubble_box = ft.Container(
            content=ft.Column(parts, spacing=6, tight=True),
            bgcolor=BG_BUBBLE_USER,
            padding=ft.Padding(left=16, right=16, top=12, bottom=12),
            border_radius=ft.BorderRadius(18, 18, 4, 18),
            max_width=580,
        )
        inner_row = ft.Row(
            controls=[ft.Container(content=edit_btn, alignment=ft.Alignment(0, 1)), bubble_box],
            alignment=ft.MainAxisAlignment.END,
            vertical_alignment=ft.CrossAxisAlignment.END,
            spacing=4,
        )
        def _on_hover(e):
            edit_btn.visible = (e.data == "true")
            edit_btn.update()
        return ft.Container(content=inner_row, on_hover=_on_hover)

    def build_thinking_box(text: str, done: bool = True,
                           elapsed: float | None = None) -> ft.Control:
        label = f"Thought for {elapsed:.1f}s" if (done and elapsed) else "Thinking…"
        body_txt  = ft.Text(text, size=12, color=THINKING_COL, italic=True,
                            selectable=True, font_family="monospace")
        chevron   = ft.Icon(ft.Icons.EXPAND_MORE, size=14, color=TEXT_SECONDARY)
        body_wrap = ft.Container(
            content=ft.Column([
                ft.Container(content=body_txt,
                             padding=ft.Padding(left=12, right=12, top=8, bottom=8)),
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
            body_wrap.update(); chevron.update()
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
        controls.append(
            ft.Markdown(text, selectable=True,
                        extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
                        on_tap_link=lambda e: page.launch_url(e.data))
        )
        return ft.Container(
            content=ft.Column(controls, spacing=8),
            padding=ft.Padding(left=4, right=4, top=4, bottom=4),
        )

    chat_list = ft.ListView(
        expand=True,
        spacing=20,
        auto_scroll=True,
        padding=ft.Padding(left=48, right=48, top=32, bottom=32),
    )

    welcome = ft.Container(
        content=ft.Column(
            [
                ft.Text("How can I help?", size=28, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
                ft.Text("Running fully local — no data leaves your machine.", size=14, color=TEXT_SECONDARY),
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=8,
        ),
        alignment=ft.Alignment(0, 0),
        expand=True,
        visible=True,
    )

    # ── Toolbar / input area ─────────────────────────────────────────────────
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

    def _active_model_name() -> str:
        mp = settings.get("model_path", "")
        return Path(mp).stem[:28] if mp else "No model selected"

    # ── Model Selector Popup ─────────────────────────────────────────────────
    model_label = ft.Text(
        _active_model_name(),
        size=12,
        color=TEXT_SECONDARY,
        overflow=ft.TextOverflow.ELLIPSIS,
    )

    def _build_model_popup_items() -> list[ft.PopupMenuItem]:
        folder = settings.get("model_folder", "")
        models = scan_models(folder) if folder else []
        
        if not models:
            return [
                ft.PopupMenuItem(
                    content=ft.Container(
                        content=ft.Text(
                            "No .gguf files found in folder.\nSet folder in Settings.",
                            size=12,
                            color=TEXT_SECONDARY,
                            italic=True,
                        ),
                        padding=ft.Padding(left=4, right=8, top=6, bottom=6),
                        width=240,
                    )
                )
            ]

        current = settings.get("model_path", "")
        items = []
        for m in models:
            is_active = str(m) == current
            file_size = f"{m.stat().st_size / 1e9:.1f} GB"
            
            def pick_model(_, mp=str(m)):
                settings["model_path"] = mp
                model_label.value = Path(mp).stem[:28]
                global _llm
                with _llm_lock:
                    _llm = None
                model_popup.items = _build_model_popup_items()
                _refresh_model_list()
                page.update()

            items.append(
                ft.PopupMenuItem(
                    content=ft.Container(
                        content=ft.Row(
                            [
                                ft.Icon(
                                    ft.Icons.CHECK,
                                    size=14,
                                    color=ACCENT if is_active else "transparent",
                                ),
                                ft.Column(
                                    [
                                        ft.Text(
                                            m.stem,
                                            size=13,
                                            color=TEXT_PRIMARY if is_active else TEXT_SECONDARY,
                                            weight=ft.FontWeight.W_500 if is_active else ft.FontWeight.W_400,
                                            overflow=ft.TextOverflow.ELLIPSIS,
                                            max_lines=1,
                                        ),
                                        ft.Text(
                                            f"{file_size} · {m.parent.name}",
                                            size=11,
                                            color=TEXT_HINT,
                                        ),
                                    ],
                                    spacing=1,
                                    expand=True,
                                ),
                            ],
                            spacing=10,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        padding=ft.Padding(left=4, right=8, top=4, bottom=4),
                        width=280,
                    ),
                    on_click=pick_model,
                )
            )
        return items

    model_popup = ft.PopupMenuButton(
        bgcolor=BG_CARD,
        shadow_color="#000000",
        elevation=8,
        shape=ft.RoundedRectangleBorder(
            radius=14,
            side=ft.BorderSide(1, BORDER),
        ),
        menu_position=ft.PopupMenuPosition.OVER,
        tooltip="Click to switch model",
        content=ft.Container(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.MEMORY_OUTLINED, size=12, color=TEXT_SECONDARY),
                    model_label,
                    ft.Icon(ft.Icons.KEYBOARD_ARROW_DOWN, size=12, color=TEXT_SECONDARY),
                ],
                spacing=4,
            ),
            padding=ft.Padding(left=8, right=8, top=4, bottom=4),
            border_radius=12,
            border=ft.Border(
                ft.BorderSide(1, BORDER),
                ft.BorderSide(1, BORDER),
                ft.BorderSide(1, BORDER),
                ft.BorderSide(1, BORDER),
            ),
            ink=True,
        ),
        items=_build_model_popup_items(),
    )

    attach_btn = ft.IconButton(
        icon=ft.Icons.ADD,
        icon_color=TEXT_SECONDARY,
        icon_size=18,
        tooltip="Attach file",
        style=ft.ButtonStyle(padding=ft.Padding(left=6, right=6, top=6, bottom=6)),
        on_click=lambda _: page.run_task(_do_pick_files),
    )
    mic_btn = ft.IconButton(
        icon=ft.Icons.MIC_NONE_OUTLINED,
        icon_color=TEXT_SECONDARY,
        icon_size=18,
        tooltip="Voice input (coming soon)",
        style=ft.ButtonStyle(padding=ft.Padding(left=6, right=6, top=6, bottom=6)),
    )

    # ── Effort Toggle / Selector ─────────────────────────────────────────────
    effort_options = [
        ("Low", "Faster replies, minimal thinking"),
        ("Medium", "Balanced speed and reasoning"),
        ("High", "Deeper analysis, longer context"),
    ]

    effort_label = ft.Text(
        f"Effort: {settings.get('effort', 'Medium')}",
        size=12,
        color=TEXT_SECONDARY,
    )

    def _build_effort_items() -> list[ft.PopupMenuItem]:
        current = settings.get("effort", "Medium")
        items = []
        for level, desc in effort_options:
            is_active = level == current
            items.append(
                ft.PopupMenuItem(
                    content=ft.Container(
                        content=ft.Row(
                            [
                                ft.Icon(
                                    ft.Icons.CHECK,
                                    size=14,
                                    color=ACCENT if is_active else "transparent",
                                ),
                                ft.Column(
                                    [
                                        ft.Text(
                                            level,
                                            size=13,
                                            color=TEXT_PRIMARY if is_active else TEXT_SECONDARY,
                                            weight=ft.FontWeight.W_500 if is_active else ft.FontWeight.W_400,
                                        ),
                                        ft.Text(
                                            desc,
                                            size=11,
                                            color=TEXT_HINT,
                                        ),
                                    ],
                                    spacing=1,
                                ),
                            ],
                            spacing=10,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        padding=ft.Padding(left=4, right=8, top=4, bottom=4),
                        width=210,
                    ),
                    on_click=lambda _, lvl=level: change_effort(lvl),
                )
            )
        return items

    def change_effort(level: str):
        settings["effort"] = level
        effort_label.value = f"Effort: {level}"
        effort_popup.items = _build_effort_items()
        save_settings(settings)
        page.update()

    effort_popup = ft.PopupMenuButton(
        bgcolor=BG_CARD,
        shadow_color="#000000",
        elevation=8,
        shape=ft.RoundedRectangleBorder(
            radius=14,
            side=ft.BorderSide(1, BORDER),
        ),
        menu_position=ft.PopupMenuPosition.OVER,
        tooltip="Select reasoning effort",
        content=ft.Container(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.PSYCHOLOGY_OUTLINED, size=13, color=TEXT_SECONDARY),
                    effort_label,
                    ft.Icon(ft.Icons.KEYBOARD_ARROW_DOWN, size=12, color=TEXT_SECONDARY),
                ],
                spacing=4,
            ),
            padding=ft.Padding(left=8, right=8, top=4, bottom=4),
            border_radius=12,
            border=ft.Border(
                ft.BorderSide(1, BORDER),
                ft.BorderSide(1, BORDER),
                ft.BorderSide(1, BORDER),
                ft.BorderSide(1, BORDER),
            ),
            ink=True,
        ),
        items=_build_effort_items(),
    )
    
    def do_send(e):
        nonlocal generating, current_conv, stop_requested
        if generating:
            return
        prompt = input_field.value.strip()
        if not prompt and not attached_files:
            return

        generating = True
        stop_requested = False
        input_field.value = ""
        input_field.disabled = True

        # Snapshot and clear attachments before any async work
        snapshot = list(attached_files)
        _clear_attachments()

        # Build augmented prompt: inject text file contents inline
        full_prompt = prompt
        for a in snapshot:
            if "text" in a:
                full_prompt += (
                    f"\n\n--- Attached file: {a['name']} ---\n{a['text']}\n--- End of {a['name']} ---"
                )
            elif "b64" in a:
                full_prompt += f"\n\n[Binary file attached: {a['name']} ({a.get('mime','?')})]"

        # Metadata saved to conversation (name + mime only, not content)
        attach_meta = [{"name": a["name"], "mime": a.get("mime", "")} for a in snapshot]

        # Transform send button into stop button
        send_btn.icon = ft.Icons.STOP_ROUNDED
        send_btn.icon_color = ACCENT
        send_btn.tooltip = "Stop generating"

        welcome.visible = False
        pending_msg_idx = len(current_conv["messages"])
        chat_list.controls.append(
            build_user_bubble(prompt, attach_meta if attach_meta else None, msg_idx=pending_msg_idx)
        )

        # ── Live thinking box ─────────────────────────────────────────────
        thinking_content  = [""]
        thinking_elapsed  = [0.0]

        think_label   = ft.Text("Thinking…", size=12, weight=ft.FontWeight.W_500, color="#b0905a")
        think_chevron = ft.Icon(ft.Icons.EXPAND_LESS, size=14, color=TEXT_SECONDARY)
        think_body_txt = ft.Text(
            "", size=12, color=THINKING_COL, italic=True,
            selectable=True, font_family="monospace",
        )
        think_body = ft.Container(
            content=ft.Column([
                ft.Container(content=think_body_txt,
                             padding=ft.Padding(left=12, right=12, top=8, bottom=8)),
            ], spacing=0),
            visible=True,
            animate_size=ft.Animation(200, ft.AnimationCurve.EASE_OUT),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )
        think_expanded = [True]

        def _toggle_think(_=None):
            think_expanded[0] = not think_expanded[0]
            think_body.visible = think_expanded[0]
            think_chevron.name = (ft.Icons.EXPAND_LESS if think_expanded[0]
                                  else ft.Icons.EXPAND_MORE)
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
                    padding=ft.Padding(left=12, right=8, top=8, bottom=8),
                    ink=True,
                ),
                think_body,
            ], spacing=0),
            border=ft.Border(
                left=ft.BorderSide(2, "#b0905a"), top=ft.BorderSide(1, "#333333"),
                right=ft.BorderSide(1, "#333333"), bottom=ft.BorderSide(1, "#333333"),
            ),
            border_radius=8, bgcolor="#1e1a16", visible=False,
        )

        ai_md = ft.Markdown(
            "", selectable=True,
            extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
            on_tap_link=lambda e: page.launch_url(e.data),
        )
        ai_bubble = ft.Container(
            content=ft.Column([think_box, ai_md], spacing=8),
            padding=ft.Padding(left=4, right=4, top=4, bottom=4),
        )
        chat_list.controls.append(ai_bubble)
        page.update()

        # ── Async generate: LLM runs in thread, UI updates on event loop ──
        async def generate_async():
            nonlocal generating, current_conv, stop_requested

            spinner.visible = True
            status_text.value = "Processing…"
            page.update()

            start       = time.time()
            token_count = 0
            in_think    = False
            buf         = ""
            full_response = ""
            # Time-based flush: update UI at most every 50 ms
            last_flush  = [time.time()]

            def flush():
                page.update()
                last_flush[0] = time.time()

            def maybe_flush():
                if time.time() - last_flush[0] >= 0.05:
                    flush()

            try:
                effort_level = settings.get("effort", "Medium")
                dynamic_max_tokens = settings["max_tokens"]
                if effort_level == "Low":
                    dynamic_max_tokens = min(settings["max_tokens"], 1024)
                elif effort_level == "High":
                    dynamic_max_tokens = max(settings["max_tokens"], 4096)

                messages = []
                if settings.get("system_prompt"):
                    messages.append({"role": "system", "content": settings["system_prompt"]})
                for m in current_conv["messages"]:
                    messages.append({"role": m["role"], "content": m["content"]})
                messages.append({"role": "user", "content": full_prompt})

                # Run blocking LLM call in a thread pool so it does not block event loop
                loop = asyncio.get_event_loop()
                llm  = await loop.run_in_executor(None, get_llm, settings)

                stream = await loop.run_in_executor(
                    None,
                    lambda: llm.create_chat_completion(
                        messages=messages, stream=True,
                        temperature=settings["temperature"],
                        top_p=settings["top_p"],
                        repeat_penalty=settings["repeat_penalty"],
                        max_tokens=dynamic_max_tokens,
                    )
                )

                # Iterate stream chunks in executor, yield each to event loop
                def _next_chunk(it):
                    try:
                        return next(it)
                    except StopIteration:
                        return None

                stream_iter = iter(stream)
                while True:
                    if stop_requested:
                        break
                    chunk = await loop.run_in_executor(None, _next_chunk, stream_iter)
                    if chunk is None:
                        break

                    delta_obj = chunk["choices"][0]["delta"]
                    reasoning = delta_obj.get("reasoning_content") or ""
                    content   = delta_obj.get("content") or ""

                    if reasoning:
                        think_box.visible = True
                        thinking_content[0] += reasoning
                        think_body_txt.value = thinking_content[0]
                        token_count += 1
                        elapsed = max(time.time() - start, 0.001)
                        status_text.value = (
                            f"Thinking… {token_count} tok · {token_count/elapsed:.1f} tok/s"
                        )
                        maybe_flush()
                        continue

                    if not content:
                        continue

                    token_count += 1
                    elapsed = max(time.time() - start, 0.001)
                    buf += content

                    if "<think>" in buf:
                        in_think = True
                        think_box.visible = True
                        buf = buf.replace("<think>", "")

                    if "</think>" in buf:
                        before, after = buf.split("</think>", 1)
                        thinking_content[0] += before
                        think_body_txt.value = thinking_content[0]
                        thinking_elapsed[0]  = elapsed
                        think_label.value    = f"Thought for {elapsed:.1f}s"
                        think_body.visible   = False
                        think_expanded[0]    = False
                        think_chevron.name   = ft.Icons.EXPAND_MORE
                        think_box.border = ft.Border(
                            left=ft.BorderSide(2, "#444444"), top=ft.BorderSide(1, "#333333"),
                            right=ft.BorderSide(1, "#333333"), bottom=ft.BorderSide(1, "#333333"),
                        )
                        in_think = False
                        ai_md.value   += after
                        full_response += after
                        buf = ""
                    else:
                        if in_think:
                            thinking_content[0] += buf
                            think_body_txt.value = thinking_content[0]
                            status_text.value = (
                                f"Thinking… {token_count} tok · {token_count/elapsed:.1f} tok/s"
                            )
                        else:
                            ai_md.value   += buf
                            full_response += buf
                            status_text.value = (
                                f"Generating… {token_count} tok · {token_count/elapsed:.1f} tok/s"
                            )
                        buf = ""

                    maybe_flush()

                flush()   # final flush after loop

                total = max(time.time() - start, 0.001)
                status_text.value = (
                    f"Done · {token_count} tok · {total:.1f}s · {token_count/total:.1f} tok/s"
                )

                user_msg: dict = {"role": "user", "content": prompt}
                if attach_meta:
                    user_msg["attachments"] = attach_meta
                current_conv["messages"].append(user_msg)
                ai_msg: dict = {"role": "assistant", "content": full_response}
                if thinking_content[0]:
                    ai_msg["thinking"]         = thinking_content[0]
                    ai_msg["thinking_elapsed"] = thinking_elapsed[0]
                current_conv["messages"].append(ai_msg)

                if len(current_conv["messages"]) == 2:
                    title_base = prompt if prompt else (
                        attach_meta[0]["name"] if attach_meta else "New chat")
                    current_conv["title"] = title_base[:45] + ("…" if len(title_base) > 45 else "")
                    try: topbar_title.value = current_conv["title"]
                    except: pass

                save_conversation(current_conv)
                refresh_sidebar()

            except Exception as exc:
                status_text.value = f"Error: {exc}"
                ai_md.value = f"⚠ {exc}"
            finally:
                spinner.visible       = False
                input_field.disabled  = False
                generating            = False
                stop_requested        = False
                send_btn.icon         = ft.Icons.ARROW_UPWARD_ROUNDED
                send_btn.icon_color   = ACCENT
                send_btn.tooltip      = "Send"
                page.update()

        page.run_task(generate_async)

    def handle_send_or_stop(_):
        nonlocal stop_requested, generating
        if generating:
            stop_requested = True
            status_text.value = "Stopping…"
            page.update()
        else:
            do_send(_)

    input_field.on_submit = do_send

    send_btn = ft.IconButton(
        icon=ft.Icons.ARROW_UPWARD_ROUNDED,
        icon_color=ACCENT,
        icon_size=20,
        tooltip="Send",
        style=ft.ButtonStyle(
            bgcolor={ft.ControlState.DEFAULT: "#2a2a2a", ft.ControlState.HOVERED: "#333333"},
            shape=ft.CircleBorder(),
            padding=ft.Padding(left=8, right=8, top=8, bottom=8),
        ),
        on_click=handle_send_or_stop,
    )

    input_container = ft.Container(
        content=ft.Column(
            [
                attachments_row,
                ft.Row([input_field], vertical_alignment=ft.CrossAxisAlignment.END),
                ft.Row(
                    [
                        attach_btn,
                        model_popup,
                        ft.Row([spinner, status_text], spacing=6),
                        ft.Container(expand=True),
                        mic_btn,
                        effort_popup,
                        send_btn,
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=4,
                ),
            ],
            spacing=4,
        ),
        bgcolor=BG_INPUT,
        border_radius=16,
        padding=ft.Padding(left=12, right=8, top=4, bottom=8),
        border=ft.Border(ft.BorderSide(1, BORDER), ft.BorderSide(1, BORDER), ft.BorderSide(1, BORDER), ft.BorderSide(1, BORDER)),
        margin=ft.Margin(left=0, right=0, top=0, bottom=16),
    )
    chat_view = ft.Column(
        [
            ft.Stack([chat_list, welcome], expand=True, alignment=ft.Alignment(0, 0)),
            ft.Container(
                content=input_container,
                padding=ft.Padding(left=48, right=48, top=0, bottom=0),
            ),
        ],
        expand=True,
        spacing=0,
    )
    # ── Settings tab ─────────────────────────────────────────────────────────
    def _open_native_folder_dialog(initial_directory: str) -> str | None:
        initial = str(Path(initial_directory).expanduser())

        zenity = shutil.which("zenity")
        if zenity:
            result = subprocess.run(
                [zenity, "--file-selection", "--directory",
                 "--title=Select folder containing GGUF models",
                 f"--filename={initial.rstrip('/')}/"],
                capture_output=True, text=True, check=False,
            )
            return result.stdout.strip() if result.returncode == 0 else None

        kdialog = shutil.which("kdialog")
        if kdialog:
            result = subprocess.run(
                [kdialog, "--getexistingdirectory", initial,
                 "Select folder containing GGUF models"],
                capture_output=True, text=True, check=False,
            )
            return result.stdout.strip() if result.returncode == 0 else None

        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected = filedialog.askdirectory(
                title="Select folder containing GGUF models",
                initialdir=initial,
            )
            root.destroy()
            return selected or None
        except Exception as exc:
            raise RuntimeError(
                "No folder dialog is available. Install zenity (sudo apt install zenity) "
                "or kdialog, or install Python Tk support (python3-tk)."
            ) from exc

    async def select_model_folder(_=None):
        try:
            initial_dir = (
                str(Path(settings.get("model_folder", "")).expanduser())
                if settings.get("model_folder") else str(Path.home())
            )
            folder = await asyncio.to_thread(
                _open_native_folder_dialog, initial_dir
            )
            if not folder:
                return

            settings["model_folder"] = folder
            _refresh_model_list()

            candidates = scan_models(folder)
            if not candidates:
                status_text.value = f"No .gguf models found in {folder}"
            else:
                status_text.value = f"Found {len(candidates)} model(s) in folder"
                if len(candidates) == 1 and not settings.get("model_path"):
                    settings["model_path"] = str(candidates[0])
                    model_label.value = candidates[0].stem[:28]
                    _refresh_model_list()

            switch_tab("settings")
            page.update()
        except Exception as exc:
            status_text.value = f"Folder picker error: {exc}"
            page.update()

    model_list_col = ft.Column(spacing=2)
    model_list_container = ft.Container(
        content=model_list_col,
        bgcolor=BG_CARD,
        border_radius=10,
        border=ft.Border(ft.BorderSide(1, BORDER), ft.BorderSide(1, BORDER),
                         ft.BorderSide(1, BORDER), ft.BorderSide(1, BORDER)),
        padding=ft.Padding(left=4, right=4, top=4, bottom=4),
    )

    def _refresh_model_list():
        model_list_col.controls.clear()
        folder = settings.get("model_folder", "")
        models = scan_models(folder) if folder else []
        if not models:
            model_list_col.controls.append(
                ft.Container(
                    content=ft.Text(
                        "No .gguf files found — check the folder path above.",
                        size=12, color=TEXT_SECONDARY, italic=True,
                    ),
                    padding=ft.Padding(left=12, right=12, top=10, bottom=10),
                )
            )
        else:
            current = settings.get("model_path", "")
            for m in models:
                is_active = str(m) == current

                def _pick(e, mp=str(m)):
                    settings["model_path"] = mp
                    model_label.value = Path(mp).stem[:28]
                    global _llm
                    with _llm_lock:
                        _llm = None
                    _refresh_model_list()
                    page.update()

                model_list_col.controls.append(
                    ft.Container(
                        content=ft.Row(
                            [
                                ft.Icon(
                                    ft.Icons.CHECK_CIRCLE if is_active else ft.Icons.RADIO_BUTTON_UNCHECKED,
                                    size=16,
                                    color=ACCENT if is_active else TEXT_HINT,
                                ),
                                ft.Column(
                                    [
                                        ft.Text(
                                            m.stem,
                                            size=13,
                                            color=TEXT_PRIMARY if is_active else TEXT_SECONDARY,
                                            weight=ft.FontWeight.W_500 if is_active else ft.FontWeight.W_400,
                                            overflow=ft.TextOverflow.ELLIPSIS,
                                            max_lines=1,
                                        ),
                                        ft.Text(
                                            f"{m.stat().st_size / 1e9:.2f} GB  ·  {m.name}",
                                            size=11,
                                            color=TEXT_HINT,
                                        ),
                                    ],
                                    spacing=1,
                                    expand=True,
                                ),
                                ft.Text(
                                    "Active" if is_active else "",
                                    size=11,
                                    color=ACCENT,
                                    weight=ft.FontWeight.W_500,
                                ),
                            ],
                            spacing=10,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        padding=ft.Padding(left=12, right=12, top=10, bottom=10),
                        border_radius=8,
                        bgcolor="#2a2a2a" if is_active else "transparent",
                        on_click=_pick,
                        ink=True,
                    )
                )
        try:
            page.update()
        except Exception:
            pass

    _refresh_model_list()

    def make_settings_view() -> ft.Control:
        s = settings

        def num_field(label, key):
            return ft.Column(
                [
                    ft.Text(label, size=12, color=TEXT_SECONDARY, weight=ft.FontWeight.W_500),
                    ft.TextField(
                        value=str(s[key]),
                        keyboard_type=ft.KeyboardType.NUMBER,
                        bgcolor=BG_INPUT,
                        border={
                            ft.ControlState.DEFAULT: ft.OutlineInputBorder(border_radius=ft.BorderRadius(10,10,10,10), side=ft.BorderSide(color=BORDER)),
                            ft.ControlState.FOCUSED: ft.OutlineInputBorder(border_radius=ft.BorderRadius(10,10,10,10), side=ft.BorderSide(color=ACCENT, width=2)),
                        },
                        color=TEXT_PRIMARY,
                        content_padding=ft.Padding(left=12, right=12, top=10, bottom=10),
                        data=key,
                        on_change=lambda e: _update_num(e),
                    ),
                ],
                spacing=4,
            )

        def _update_num(e):
            try:
                val = e.control.value
                orig = settings[e.control.data]
                if isinstance(orig, int):
                    settings[e.control.data] = int(val)
                else:
                    settings[e.control.data] = float(val)
            except ValueError:
                pass

        save_feedback = ft.Text("", size=12, color="#6bcf7f")

        def on_save(_):
            save_settings(settings)
            mp = settings.get("model_path", "")
            model_label.value = Path(mp).stem[:28] if mp else "No model selected"
            save_feedback.value = "Settings saved ✓"
            _refresh_model_list()
            page.update()
            global _llm
            with _llm_lock:
                _llm = None

        return ft.Container(
            content=ft.Column(
                [
                    ft.Text("Settings", size=20, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
                    ft.Divider(color=BORDER),
                    ft.Text("Model", size=13, color=ACCENT, weight=ft.FontWeight.W_600),
                    ft.Text("Models folder", size=12, color=TEXT_SECONDARY, weight=ft.FontWeight.W_500),
                    ft.Row(
                        [
                            ft.TextField(
                                value=str(s.get("model_folder", "")),
                                expand=True,
                                bgcolor=BG_INPUT,
                                border={
                                    ft.ControlState.DEFAULT: ft.OutlineInputBorder(border_radius=ft.BorderRadius(10,10,10,10), side=ft.BorderSide(color=BORDER)),
                                    ft.ControlState.FOCUSED: ft.OutlineInputBorder(border_radius=ft.BorderRadius(10,10,10,10), side=ft.BorderSide(color=ACCENT, width=2)),
                                },
                                color=TEXT_PRIMARY,
                                hint_text="/home/you/local-models",
                                hint_style=ft.TextStyle(color=TEXT_HINT),
                                content_padding=ft.Padding(left=12, right=12, top=10, bottom=10),
                                on_change=lambda e: (
                                    settings.update({"model_folder": e.control.value}),
                                    _refresh_model_list(),
                                ),
                            ),
                            ft.OutlinedButton(
                                "Browse",
                                icon=ft.Icons.FOLDER_OPEN,
                                on_click=select_model_folder,
                            ),
                        ],
                        spacing=8,
                    ),
                    ft.Text(
                        "Point to the folder that holds your .gguf files. "
                        "Models found there will appear in the chat toolbar selector.",
                        size=11,
                        color=TEXT_SECONDARY,
                    ),
                    ft.Text("Models in folder", size=12, color=TEXT_SECONDARY, weight=ft.FontWeight.W_500),
                    model_list_container,
                    ft.Row(
                        [
                            num_field("Context length (n_ctx)", "n_ctx"),
                            num_field("GPU layers (-1 = all)", "n_gpu_layers"),
                        ],
                        spacing=16,
                    ),
                    ft.Row(
                        [
                            num_field("CPU Threads (0 = Auto)", "n_threads"),
                        ],
                        spacing=16,
                    ),
                    ft.Divider(color=BORDER),
                    ft.Text("Generation", size=13, color=ACCENT, weight=ft.FontWeight.W_600),
                    ft.Row(
                        [
                            num_field("Temperature", "temperature"),
                            num_field("Top-p", "top_p"),
                        ],
                        spacing=16,
                    ),
                    ft.Row(
                        [
                            num_field("Repeat penalty", "repeat_penalty"),
                            num_field("Max tokens", "max_tokens"),
                        ],
                        spacing=16,
                    ),
                    ft.Divider(color=BORDER),
                    ft.Text("System prompt", size=13, color=ACCENT, weight=ft.FontWeight.W_600),
                    ft.TextField(
                        value=s["system_prompt"],
                        multiline=True,
                        min_lines=3,
                        max_lines=6,
                        bgcolor=BG_INPUT,
                        border={
                            ft.ControlState.DEFAULT: ft.OutlineInputBorder(border_radius=ft.BorderRadius(10,10,10,10), side=ft.BorderSide(color=BORDER)),
                            ft.ControlState.FOCUSED: ft.OutlineInputBorder(border_radius=ft.BorderRadius(10,10,10,10), side=ft.BorderSide(color=ACCENT, width=2)),
                        },
                        color=TEXT_PRIMARY,
                        content_padding=ft.Padding(left=12, right=12, top=10, bottom=10),
                        on_change=lambda e: settings.update({"system_prompt": e.control.value}),
                    ),
                    ft.Row(
                        [
                            ft.FilledButton(
                                "Save settings",
                                style=ft.ButtonStyle(
                                    bgcolor=ACCENT,
                                    color="#ffffff",
                                    shape=ft.RoundedRectangleBorder(radius=10),
                                ),
                                on_click=on_save,
                            ),
                            save_feedback,
                        ],
                        spacing=16,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=12,
                scroll=ft.ScrollMode.AUTO,
            ),
            padding=48,
            expand=True,
            bgcolor=BG_PAGE,
        )

    # ── Top navigation ───────────────────────────────────────────────────────
    settings_view_ref: list = [None]
    current_tab    = ["chat"]
    sidebar_visible = [True]

    topbar_title = ft.Text(
        current_conv["title"], size=13, weight=ft.FontWeight.W_500, color=TEXT_PRIMARY,
        overflow=ft.TextOverflow.ELLIPSIS, max_lines=1,
    )

    def _open_rename_title_dlg(_=None):
        rename_field = ft.TextField(
            value=current_conv["title"], autofocus=True, bgcolor=BG_INPUT,
            border={
                ft.ControlState.DEFAULT: ft.OutlineInputBorder(
                    border_radius=ft.BorderRadius(10,10,10,10), side=ft.BorderSide(color=BORDER)),
                ft.ControlState.FOCUSED: ft.OutlineInputBorder(
                    border_radius=ft.BorderRadius(10,10,10,10), side=ft.BorderSide(color=ACCENT, width=2)),
            },
            color=TEXT_PRIMARY,
            content_padding=ft.Padding(left=12, right=12, top=10, bottom=10),
        )
        def _save(_=None):
            new_title = rename_field.value.strip()
            if new_title:
                current_conv["title"] = new_title
                topbar_title.value = new_title
                save_conversation(current_conv)
                refresh_sidebar()
                page.update()
            _close_dialog(dlg)
        dlg = ft.AlertDialog(
            modal=True, bgcolor=BG_CARD, shape=ft.RoundedRectangleBorder(radius=14),
            title=ft.Text("Rename chat", size=15, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
            content=rename_field,
            actions=[
                ft.TextButton("Cancel", on_click=lambda _: _close_dialog(dlg)),
                ft.FilledButton("Save",
                    style=ft.ButtonStyle(bgcolor=ACCENT, color="#ffffff"), on_click=_save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        rename_field.on_submit = _save
        _open_dialog(dlg)

    title_pill = ft.Container(
        content=ft.Row(
            [topbar_title, ft.Icon(ft.Icons.KEYBOARD_ARROW_DOWN, size=16, color=TEXT_SECONDARY)],
            spacing=2, vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
        ),
        on_click=_open_rename_title_dlg, tooltip="Click to rename chat",
        border_radius=8, padding=ft.Padding(left=6, right=6, top=4, bottom=4), ink=True,
    )

    def switch_tab(tab_name: str):
        current_tab[0] = tab_name
        main_stack.controls.clear()
        if tab_name == "chat":
            main_stack.controls.append(chat_view)
            title_pill.visible = True
        else:
            settings_view_ref[0] = make_settings_view()
            main_stack.controls.append(settings_view_ref[0])
            title_pill.visible = False
        page.update()

    def _toggle_sidebar(_=None):
        sidebar_visible[0] = not sidebar_visible[0]
        sidebar_inner.visible = sidebar_visible[0]
        page.update()

    topbar = ft.Container(
        content=ft.Row([
            ft.Row([
                ft.IconButton(icon=ft.Icons.MENU, icon_color=TEXT_SECONDARY, icon_size=18,
                              tooltip="Toggle sidebar", on_click=_toggle_sidebar),
                title_pill,
            ], spacing=0, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Container(expand=True),
            ft.Row([
                ft.IconButton(icon=ft.Icons.TUNE_OUTLINED, icon_color=TEXT_SECONDARY, icon_size=18,
                              tooltip="Settings",
                              on_click=lambda _: switch_tab("settings" if current_tab[0]=="chat" else "chat")),
                ft.IconButton(icon=ft.Icons.EDIT_NOTE_OUTLINED, icon_color=TEXT_SECONDARY, icon_size=18,
                              tooltip="New chat", on_click=new_chat),
            ], spacing=0),
        ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
        bgcolor=BG_TOOLBAR,
        border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
        padding=ft.Padding(left=4, right=8, top=4, bottom=4),
        height=48,
    )

    main_stack = ft.Column([chat_view], expand=True, spacing=0)

    body = ft.Row(
        [sidebar_inner, ft.Column([topbar, main_stack], expand=True, spacing=0)],
        expand=True, spacing=0, vertical_alignment=ft.CrossAxisAlignment.START,
    )

    page.add(body)
    refresh_sidebar()


ft.run(main)
