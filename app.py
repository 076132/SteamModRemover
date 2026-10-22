"""Steam Mod Remover."""

from __future__ import annotations

import argparse
import ctypes
import json
import locale
import queue
import re
import shutil
import struct
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from tkinter import END, BooleanVar, StringVar, Tk, filedialog, messagebox, ttk


DEFAULT_PROTECTED_TERMS = [
    "save", "saves", "savegame", "profile", "profiles", "config", "settings",
    "log", "logs", "screenshot", "screenshots", ".ini", ".cfg", ".json",
]
PROTOBUF_PAYLOAD_MAGIC = 0x71F617D0
DIRECTORY_FLAG = 0x40


def text_file_path() -> Path:
    """Prefer an editable texts.json beside a bundled executable."""
    if getattr(sys, "frozen", False):
        external = Path(sys.executable).with_name("texts.json")
        if external.is_file():
            return external
    bundled = Path(__file__).with_name("texts.json")
    if bundled.is_file():
        return bundled
    raise FileNotFoundError("texts.json was not found beside the application.")


TEXT = json.loads(text_file_path().read_text(encoding="utf-8"))


@dataclass
class Game:
    appid: str
    name: str
    install_dir: str


def parse_acf(path: Path) -> Game | None:
    """Read only the fields required from a Steam appmanifest VDF file."""
    text = path.read_text(encoding="utf-8", errors="replace")
    values = dict(re.findall(r'"(appid|name|installdir)"\s+"([^"]*)"', text, re.I))
    if {"appid", "name", "installdir"} <= values.keys():
        return Game(values["appid"], values["name"], values["installdir"])
    return None


def installed_games(steamapps: Path) -> list[Game]:
    games: list[Game] = []
    for manifest in steamapps.glob("appmanifest_*.acf"):
        game = parse_acf(manifest)
        if game:
            games.append(game)
    return sorted(games, key=lambda item: item.name.lower())


def game_folder(steamapps: Path, game: Game) -> Path:
    return steamapps / "common" / game.install_dir


def _section(text: str, name: str) -> str:
    match = re.search(rf'"{re.escape(name)}"\s*\{{', text, re.I)
    if not match:
        return ""
    start = match.end()
    depth = 1
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index]
    return ""


def installed_depots(manifest_path: Path) -> dict[str, str]:
    """Return {depot_id: manifest_id} from the local appmanifest ACF."""
    text = manifest_path.read_text(encoding="utf-8", errors="replace")
    section = _section(text, "InstalledDepots")
    result: dict[str, str] = {}
    for match in re.finditer(r'"(\d+)"\s*\{([^{}]*)\}', section, re.S):
        manifest = re.search(r'"manifest"\s+"(\d+)"', match.group(2), re.I)
        if manifest:
            result[match.group(1)] = manifest.group(1)
    return result


def depot_cache_for(manifest_path: Path) -> Path:
    text = manifest_path.read_text(encoding="utf-8", errors="replace")
    launcher = re.search(r'"LauncherPath"\s+"([^"]+)"', text, re.I)
    if launcher:
        # VDF strings contain doubled backslashes.
        steam_exe = Path(launcher.group(1).replace("\\\\", "\\"))
        candidate = steam_exe.parent / "depotcache"
        if candidate.is_dir():
            return candidate
    default = Path(r"C:\Program Files (x86)\Steam\depotcache")
    if default.is_dir():
        return default
    raise FileNotFoundError("Steam depotcache 폴더를 찾지 못했습니다.")


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift < 70:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("잘못된 protobuf varint입니다.")


def _protobuf_fields(data: bytes):
    offset = 0
    while offset < len(data):
        key, offset = _varint(data, offset)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, offset = _varint(data, offset)
        elif wire == 1:
            value, offset = data[offset:offset + 8], offset + 8
        elif wire == 2:
            length, offset = _varint(data, offset)
            value, offset = data[offset:offset + length], offset + length
        elif wire == 5:
            value, offset = data[offset:offset + 4], offset + 4
        else:
            raise ValueError(f"지원하지 않는 protobuf wire type: {wire}")
        yield field, wire, value


def read_depot_manifest_entries(path: Path) -> tuple[set[str], set[str]]:
    """Read file and explicit directory paths from a local depotcache manifest."""
    data = path.read_bytes()
    if len(data) < 8:
        raise ValueError(f"Depot manifest가 너무 짧습니다: {path.name}")
    magic, length = struct.unpack_from("<II", data)
    if magic != PROTOBUF_PAYLOAD_MAGIC:
        raise ValueError(f"지원하지 않는 depot manifest 형식입니다: {path.name}")
    payload = data[8:8 + length]
    if len(payload) != length:
        raise ValueError(f"손상된 depot manifest입니다: {path.name}")
    files: set[str] = set()
    directories: set[str] = set()
    for field, wire, mapping in _protobuf_fields(payload):
        if field != 1 or wire != 2:
            continue
        filename: str | None = None
        flags = 0
        for map_field, map_wire, value in _protobuf_fields(mapping):
            if map_field == 1 and map_wire == 2:
                try:
                    filename = value.decode("utf-8").rstrip("\x00 \r\n\t")
                except UnicodeDecodeError as exc:
                    raise ValueError(f"암호화된 파일명은 읽을 수 없습니다: {path.name}") from exc
            elif map_field == 3 and map_wire == 0:
                flags = value
        if filename:
            normalized = filename.replace("\\", "/").rstrip("/")
            if flags & DIRECTORY_FLAG:
                directories.add(normalized)
            else:
                files.add(normalized)
    return files, directories


def read_depot_manifest(path: Path) -> set[str]:
    """Compatibility helper returning only file paths."""
    return read_depot_manifest_entries(path)[0]


def local_depot_entries(steamapps: Path, appid: str, selected: list[str]) -> tuple[set[str], set[str]]:
    appmanifest = steamapps / f"appmanifest_{appid}.acf"
    depots = installed_depots(appmanifest)
    cache = depot_cache_for(appmanifest)
    files: set[str] = set()
    directories: set[str] = set()
    for depot_id in selected:
        manifest_id = depots.get(depot_id)
        if not manifest_id:
            raise ValueError(f"설치 manifest에서 Depot {depot_id}를 찾지 못했습니다.")
        manifest_path = cache / f"{depot_id}_{manifest_id}.manifest"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"로컬 depotcache에 manifest가 없습니다: {manifest_path.name}")
        depot_files, depot_directories = read_depot_manifest_entries(manifest_path)
        files.update(depot_files)
        directories.update(depot_directories)
    return files, directories


def local_depot_files(steamapps: Path, appid: str, selected: list[str]) -> set[str]:
    """Compatibility helper returning only file paths."""
    return local_depot_entries(steamapps, appid, selected)[0]


def current_file_paths(folder: Path) -> set[str]:
    return {item.relative_to(folder).as_posix() for item in folder.rglob("*") if item.is_file()}


def current_directory_paths(folder: Path) -> set[str]:
    return {item.relative_to(folder).as_posix() for item in folder.rglob("*") if item.is_dir()}


def norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("/").casefold()


def scan_files(folder: Path, standard_paths: set[str], protected_terms: list[str], protect: bool) -> tuple[list[str], list[str]]:
    expected = {norm(item) for item in standard_paths}
    extras: list[str] = []
    protected: list[str] = []
    for item in folder.rglob("*"):
        if not item.is_file():
            continue
        relative = item.relative_to(folder).as_posix()
        if norm(relative) in expected:
            continue
        if protect and any(term.casefold() in item.name.casefold() for term in protected_terms if term):
            protected.append(relative)
        else:
            extras.append(relative)
    return sorted(extras), sorted(protected)


def save_snapshot(path: Path, game: Game, standard_paths: set[str], standard_dirs: set[str], source: str) -> None:
    payload = {
        "format": "steam-mod-remover-file-list",
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "game": asdict(game),
        "paths": sorted(standard_paths),
        "directories": sorted(standard_dirs),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_snapshot(path: Path) -> tuple[Game | None, set[str], set[str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("format") != "steam-mod-remover-file-list":
        raise ValueError("이 프로그램이 만든 기준 목록 파일이 아닙니다.")
    raw_game = payload.get("game")
    game = Game(**raw_game) if isinstance(raw_game, dict) else None
    paths = payload.get("paths")
    if not isinstance(paths, list) or not all(isinstance(value, str) for value in paths):
        raise ValueError("기준 목록의 paths 형식이 올바르지 않습니다.")
    directories = payload.get("directories", [])
    if not isinstance(directories, list) or not all(isinstance(value, str) for value in directories):
        raise ValueError("기준 목록의 directories 형식이 올바르지 않습니다.")
    return game, set(paths), set(directories)


class App(ttk.Frame):
    def __init__(self, root: Tk, args: argparse.Namespace) -> None:
        super().__init__(root, padding=12)
        self.root, self.args = root, args
        system_language = (locale.getlocale()[0] or locale.getdefaultlocale()[0] or "").casefold()
        self.language = next((code for code in TEXT if system_language.startswith(code.casefold())), "en" if "en" in TEXT else next(iter(TEXT)))
        self.language_choice = StringVar(value=TEXT[self.language]["language_name"])
        self.steamapps = StringVar(value=args.steamapps or "")
        self.game_choice = StringVar()
        self.protect = BooleanVar(value=True)
        self.destination = StringVar(value="recycle")
        self.backup_folder = StringVar()
        self.status = StringVar(value=self.t("initial_status"))
        self.games: dict[str, Game] = {}
        self.depots: dict[str, str] = {}
        self.standard_paths: set[str] = set()
        self.standard_dirs: set[str] = set()
        self.standard_source = ""
        self.standard_kind = ""
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.ui: dict[str, object] = {}
        self.row_items: dict[str, tuple[str, str]] = {}
        self.checked_paths: set[str] = set()
        self._build()
        if self.steamapps.get():
            self.load_games()
        if args.snapshot:
            self.import_snapshot_path(Path(args.snapshot), show_error=False)
        self.root.after(100, self.poll)

    def t(self, key: str, **kwargs) -> str:
        fallback = TEXT.get("en", next(iter(TEXT.values())))
        return TEXT[self.language].get(key, fallback[key]).format(**kwargs)

    def language_changed(self, _event=None) -> None:
        self.language = next(code for code, values in TEXT.items() if values["language_name"] == self.language_choice.get())
        if self.standard_kind == "depot":
            self.standard_source = self.t("source_depot")
        elif self.standard_kind == "current":
            self.standard_source = self.t("source_current")
        self.refresh_language()
        self.refresh_comparison()

    def refresh_language(self) -> None:
        self.root.title(self.t("title"))
        for key, text_key in {
            "title": "title", "steamapps_label": "steamapps", "browse": "browse",
            "load_games": "load_games", "game_label": "game", "query_depots": "query_depots", "load_depot": "load_depot",
            "load_file": "load_file", "load_current": "load_current", "save_current": "save_current", "protect": "protect",
            "destination_label": "destination", "backup_browse": "choose_backup", "select_candidates": "select_candidates",
            "select_protected": "select_protected", "remove_checked": "remove_checked",
        }.items():
            widget = self.ui.get(key)
            if widget:
                widget.configure(text=self.t(text_key))
        self.depot_frame.configure(text=self.t("depot_frame"))
        self.delete_radio.configure(text=self.t("delete"))
        self.backup_radio.configure(text=self.t("backup"))
        self.tree.heading("checked", text=self.t("select"))
        self.tree.heading("kind", text=self.t("kind"))
        self.tree.heading("path", text=self.t("path"))
        self.update_destination_ui()

    def _build(self) -> None:
        self.grid(sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.ui["title"] = ttk.Label(self, font=("Segoe UI", 14, "bold")); self.ui["title"].grid(row=0, column=0, columnspan=2, sticky="w")
        self.language_box = ttk.Combobox(self, textvariable=self.language_choice, values=[values["language_name"] for values in TEXT.values()], state="readonly", width=10)
        self.language_box.grid(row=0, column=2, sticky="e")
        self.language_box.bind("<<ComboboxSelected>>", self.language_changed)
        self.ui["steamapps_label"] = ttk.Label(self); self.ui["steamapps_label"].grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(self, textvariable=self.steamapps).grid(row=1, column=1, sticky="ew", padx=6, pady=(8, 0))
        self.ui["browse"] = ttk.Button(self, command=self.pick_steamapps); self.ui["browse"].grid(row=1, column=2, pady=(8, 0))
        self.ui["load_games"] = ttk.Button(self, command=self.load_games); self.ui["load_games"].grid(row=2, column=1, sticky="w", pady=6)
        self.ui["game_label"] = ttk.Label(self); self.ui["game_label"].grid(row=3, column=0, sticky="w")
        self.game_box = ttk.Combobox(self, textvariable=self.game_choice, state="readonly")
        self.game_box.grid(row=3, column=1, sticky="ew", padx=6)
        self.game_box.bind("<<ComboboxSelected>>", self.game_selected)
        self.ui["query_depots"] = ttk.Button(self, command=self.fetch_depots); self.ui["query_depots"].grid(row=3, column=2)
        self.depot_frame = ttk.LabelFrame(self, padding=6)
        self.depot_frame.grid(row=4, columnspan=3, sticky="nsew", pady=8)
        self.depot_vars: dict[str, BooleanVar] = {}
        self.ui["load_depot"] = ttk.Button(self, command=self.fetch_files); self.ui["load_depot"].grid(row=5, column=0, sticky="w")
        self.ui["load_file"] = ttk.Button(self, command=self.import_snapshot); self.ui["load_file"].grid(row=5, column=1, sticky="w")
        self.ui["load_current"] = ttk.Button(self, command=self.load_current_files); self.ui["load_current"].grid(row=6, column=0, sticky="w", pady=(6, 0))
        self.ui["save_current"] = ttk.Button(self, command=self.export_snapshot); self.ui["save_current"].grid(row=6, column=1, sticky="w", pady=(6, 0))
        self.terms = ttk.Entry(self)
        self.terms.insert(0, ", ".join(DEFAULT_PROTECTED_TERMS))
        self.ui["protect"] = ttk.Checkbutton(self, variable=self.protect, command=self.refresh_comparison); self.ui["protect"].grid(row=7, column=0, sticky="w", pady=(12, 0))
        self.terms.grid(row=7, column=1, columnspan=2, sticky="ew", padx=6, pady=(12, 0))
        self.terms.bind("<FocusOut>", lambda _event: self.refresh_comparison())
        self.ui["destination_label"] = ttk.Label(self); self.ui["destination_label"].grid(row=8, column=0, sticky="w", pady=(8, 0))
        self.delete_radio = ttk.Radiobutton(self, variable=self.destination, value="recycle", command=self.update_destination_ui)
        self.delete_radio.grid(row=8, column=1, sticky="w", padx=6, pady=(8, 0))
        self.backup_radio = ttk.Radiobutton(self, variable=self.destination, value="backup", command=self.update_destination_ui)
        self.backup_radio.grid(row=8, column=2, sticky="w", pady=(8, 0))
        self.ui["backup_browse"] = ttk.Button(self, command=self.pick_backup_folder)
        self.ui["backup_browse"].grid(row=9, column=2, sticky="e", pady=(4, 0))
        self.ui["select_candidates"] = ttk.Button(self, command=lambda: self.select_category("candidate")); self.ui["select_candidates"].grid(row=10, column=0, sticky="w", pady=10)
        self.ui["select_protected"] = ttk.Button(self, command=lambda: self.select_category("protected")); self.ui["select_protected"].grid(row=10, column=1, sticky="w", pady=10)
        self.ui["remove_checked"] = ttk.Button(self, command=self.remove_files); self.ui["remove_checked"].grid(row=10, column=2, sticky="e", pady=10)
        self.tree = ttk.Treeview(self, columns=("checked", "kind", "path"), show="headings", height=14)
        self.tree.heading("checked", text="")
        self.tree.heading("kind", text="")
        self.tree.heading("path", text="")
        self.tree.column("checked", width=56, stretch=False, anchor="center")
        self.tree.column("kind", width=145, stretch=False)
        self.tree.column("path", width=650)
        self.tree.bind("<Button-1>", self.tree_clicked)
        self.tree.grid(row=11, columnspan=3, sticky="nsew")
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=11, column=3, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.rowconfigure(11, weight=1)
        ttk.Label(self, textvariable=self.status).grid(row=12, columnspan=4, sticky="w", pady=(8, 0))
        self.refresh_language()

    def selected_game(self) -> Game | None:
        return self.games.get(self.game_choice.get())

    def update_destination_ui(self) -> None:
        if self.destination.get() == "backup":
            self.ui["backup_browse"].grid()
        else:
            self.ui["backup_browse"].grid_remove()

    def pick_backup_folder(self) -> None:
        selected = filedialog.askdirectory(title=self.t("choose_backup"))
        if selected:
            self.backup_folder.set(selected)
            self.destination.set("backup")
            self.update_destination_ui()
            self.status.set(self.t("backup_selected", path=selected))

    def tree_clicked(self, event) -> None:
        if self.tree.identify_region(event.x, event.y) != "cell" or self.tree.identify_column(event.x) != "#1":
            return
        item_id = self.tree.identify_row(event.y)
        if not item_id or item_id not in self.row_items:
            return
        path, category = self.row_items[item_id]
        if category == "matching":
            return
        if path in self.checked_paths:
            self.checked_paths.remove(path)
            self.tree.set(item_id, "checked", "☐")
        else:
            self.checked_paths.add(path)
            self.tree.set(item_id, "checked", "☑")

    def select_category(self, category: str) -> None:
        for item_id, (path, item_category) in self.row_items.items():
            if item_category == category:
                self.checked_paths.add(path)
                self.tree.set(item_id, "checked", "☑")

    def comparison_sets(self) -> tuple[list[str], list[str], list[str]] | None:
        game = self.selected_game()
        if not game or not self.standard_paths:
            return None
        folder = game_folder(Path(self.steamapps.get()), game)
        if not folder.is_dir():
            return None
        actual = current_file_paths(folder)
        expected = {norm(item) for item in self.standard_paths}
        terms = [part.strip() for part in self.terms.get().split(",") if part.strip()]
        candidates: list[str] = []
        protected: list[str] = []
        present_expected: list[str] = []
        for item in actual:
            if norm(item) in expected:
                present_expected.append(item)
            elif self.protect.get() and any(term.casefold() in Path(item).name.casefold() for term in terms):
                protected.append(item)
            else:
                candidates.append(item)
        return sorted(candidates), sorted(protected), sorted(present_expected)

    def empty_candidate_directories(self, folder: Path) -> list[str]:
        """Find empty non-baseline folders, excluding folders needed by baseline files."""
        required = {norm(item) for item in self.standard_dirs}
        for file_path in self.standard_paths:
            parent = Path(file_path).parent
            while parent != Path("."):
                required.add(norm(parent.as_posix()))
                parent = parent.parent
        candidates: list[Path] = []
        for directory in folder.rglob("*"):
            if not directory.is_dir() or norm(directory.relative_to(folder).as_posix()) in required:
                continue
            try:
                next(directory.iterdir())
            except StopIteration:
                candidates.append(directory)
        # A parent is sufficient; never separately move a descendant folder.
        candidates.sort(key=lambda item: len(item.parts))
        top_level: list[Path] = []
        for directory in candidates:
            if not any(parent in directory.parents for parent in top_level):
                top_level.append(directory)
        return [directory.relative_to(folder).as_posix() for directory in top_level]

    def remove_files(self) -> None:
        comparison = self.comparison_sets()
        game = self.selected_game()
        if not comparison or not game:
            messagebox.showwarning(self.t("error"), self.t("no_baseline"), parent=self.root)
            return
        candidates, protected, _ = comparison
        removable = set(candidates) | set(protected)
        selected = sorted(path for path in self.checked_paths if path in removable)
        if not selected:
            messagebox.showinfo(self.t("info"), self.t("nothing"), parent=self.root)
            return
        folder = game_folder(Path(self.steamapps.get()), game)
        destination = self.destination.get()
        destination_name = self.t("delete") if destination == "recycle" else self.t("backup")
        if destination == "backup":
            if not self.backup_folder.get():
                messagebox.showwarning(self.t("error"), self.t("backup_missing"), parent=self.root)
                return
            backup_root = Path(self.backup_folder.get()).resolve()
            try:
                backup_root.relative_to(folder.resolve())
                messagebox.showerror(self.t("error"), self.t("backup_inside_game"), parent=self.root)
                return
            except ValueError:
                pass
        if not messagebox.askyesno(
            self.t("warning"), self.t("warn_text", count=len(selected), destination=destination_name), parent=self.root,
        ):
            return
        try:
            if destination == "recycle":
                self.move_to_recycle_bin(folder, selected)
            else:
                self.move_to_backup(folder, selected, Path(self.backup_folder.get()))
        except Exception as exc:
            messagebox.showerror(self.t("error"), str(exc), parent=self.root)
            return
        empty_directories = self.empty_candidate_directories(folder)
        try:
            if destination == "recycle":
                self.move_to_recycle_bin(folder, empty_directories)
            else:
                self.move_to_backup(folder, empty_directories, Path(self.backup_folder.get()))
        except Exception as exc:
            messagebox.showerror(self.t("error"), str(exc), parent=self.root)
            return
        self.show_comparison()
        messagebox.showinfo(self.t("info"), self.t("removed", count=len(selected), directories=len(empty_directories), destination=destination_name), parent=self.root)

    @staticmethod
    def move_to_recycle_bin(folder: Path, relative_paths: list[str]) -> None:
        if not hasattr(ctypes, "windll"):
            raise OSError("Recycle Bin is supported only on Windows.")

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", ctypes.c_void_p), ("wFunc", ctypes.c_uint), ("pFrom", ctypes.c_wchar_p),
                       ("pTo", ctypes.c_wchar_p), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", ctypes.c_int),
                       ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p)]

        # FO_DELETE plus FOF_ALLOWUNDO moves files to the Windows Recycle Bin.
        for relative in relative_paths:
            source = (folder / relative).resolve()
            try:
                source.relative_to(folder.resolve())
            except ValueError as exc:
                raise ValueError(f"Invalid removal path: {relative}") from exc
            operation = SHFILEOPSTRUCTW(None, 3, str(source) + "\0\0", None, 0x40 | 0x10 | 0x04 | 0x0400, False, None, None)
            result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
            if result or operation.fAnyOperationsAborted:
                raise OSError(f"Failed to move to Recycle Bin: {source.name} (code {result})")

    @staticmethod
    def move_to_backup(folder: Path, relative_paths: list[str], backup_root: Path) -> None:
        session = backup_root / f"SteamModRemover_{datetime.now():%Y%m%d_%H%M%S_%f}"
        for relative in relative_paths:
            source = (folder / relative).resolve()
            try:
                relative_path = source.relative_to(folder.resolve())
            except ValueError as exc:
                raise ValueError(f"Invalid removal path: {relative}") from exc
            target = session / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))

    def pick_steamapps(self) -> None:
        selected = filedialog.askdirectory(title="steamapps 폴더 선택")
        if selected:
            self.steamapps.set(selected)
            self.load_games()

    def reset_game_data(self) -> None:
        self.depots.clear()
        self.depot_vars.clear()
        self.standard_paths.clear()
        self.standard_dirs.clear()
        self.standard_source = ""
        self.standard_kind = ""
        self.checked_paths.clear()
        self.row_items.clear()
        for child in self.depot_frame.winfo_children():
            child.destroy()
        self.tree.delete(*self.tree.get_children())

    def game_selected(self, _event=None) -> None:
        self.reset_game_data()
        self.fetch_depots()

    def load_games(self) -> None:
        directory = Path(self.steamapps.get())
        if not directory.is_dir():
            messagebox.showerror(self.t("error"), self.t("path_error"), parent=self.root)
            return
        self.reset_game_data()
        found = installed_games(directory)
        self.games = {f"{game.name}  (App ID: {game.appid})": game for game in found}
        self.game_box["values"] = list(self.games)
        if self.games:
            self.game_choice.set(next(iter(self.games)))
            if self.args.appid:
                for label, game in self.games.items():
                    if game.appid == self.args.appid:
                        self.game_choice.set(label)
                        break
        if self.games:
            self.fetch_depots()
        else:
            self.status.set(self.t("no_games"))

    def background(self, operation: str, func) -> None:
        self.status.set(self.t("reading"))
        def run() -> None:
            try:
                self.events.put((operation, func()))
            except Exception as exc:  # show useful network/parser errors in UI
                self.events.put(("error", exc))
        threading.Thread(target=run, daemon=True).start()

    def poll(self) -> None:
        try:
            while True:
                operation, value = self.events.get_nowait()
                if operation == "error":
                    self.status.set(self.t("failed"))
                    messagebox.showerror(self.t("error"), str(value), parent=self.root)
                elif operation == "depots":
                    self.show_depots(value)  # type: ignore[arg-type]
                elif operation == "files":
                    appid, paths, directories = value  # type: ignore[misc]
                    if not self.selected_game() or self.selected_game().appid != appid:
                        continue
                    self.standard_paths = paths
                    self.standard_dirs = directories
                    self.standard_source = self.t("source_depot")
                    self.standard_kind = "depot"
                    self.show_comparison()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def fetch_depots(self) -> None:
        game = self.selected_game()
        if not game:
            messagebox.showwarning(self.t("error"), self.t("select_game"), parent=self.root)
            return
        manifest_path = Path(self.steamapps.get()) / f"appmanifest_{game.appid}.acf"
        try:
            depots = installed_depots(manifest_path)
            self.show_depots({depot_id: f"Manifest {manifest_id}" for depot_id, manifest_id in depots.items()})
        except Exception as exc:
            messagebox.showerror(self.t("error"), str(exc), parent=self.root)

    def show_depots(self, depots: dict[str, str]) -> None:
        self.depots = depots
        for child in self.depot_frame.winfo_children():
            child.destroy()
        self.depot_vars.clear()
        for index, (depot_id, name) in enumerate(depots.items()):
            variable = BooleanVar(value=True)
            self.depot_vars[depot_id] = variable
            ttk.Checkbutton(self.depot_frame, text=f"{depot_id} — {name}", variable=variable).grid(row=index, column=0, sticky="w")
        self.status.set(self.t("depots_found", count=len(depots)))

    def fetch_files(self) -> None:
        selected = [depot_id for depot_id, variable in self.depot_vars.items() if variable.get()]
        if not selected:
            messagebox.showwarning(self.t("error"), self.t("select_depot"), parent=self.root)
            return
        game = self.selected_game()
        if not game:
            return
        self.background("files", lambda: (game.appid, *local_depot_entries(Path(self.steamapps.get()), game.appid, selected)))

    def load_current_files(self) -> None:
        game = self.selected_game()
        if not game:
            messagebox.showwarning(self.t("error"), self.t("select_game"), parent=self.root)
            return
        folder = game_folder(Path(self.steamapps.get()), game)
        if not folder.is_dir():
            messagebox.showerror(self.t("error"), self.t("folder_missing", path=folder), parent=self.root)
            return
        self.standard_paths = current_file_paths(folder)
        self.standard_dirs = current_directory_paths(folder)
        self.standard_source = self.t("source_current")
        self.standard_kind = "current"
        self.checked_paths.clear()
        self.row_items.clear()
        self.tree.delete(*self.tree.get_children())
        for item in sorted(self.standard_paths):
            self.tree.insert("", END, values=("", self.t("current_baseline"), item))
        self.status.set(self.t("current_loaded", count=len(self.standard_paths)))

    def import_snapshot(self) -> None:
        chosen = filedialog.askopenfilename(title=self.t("open_baseline"), filetypes=[("JSON", "*.json")])
        if not chosen:
            return
        self.import_snapshot_path(Path(chosen), show_error=True)

    def import_snapshot_path(self, path: Path, show_error: bool) -> None:
        try:
            stored_game, paths, directories = load_snapshot(path)
            self.standard_paths = paths
            self.standard_dirs = directories
            self.standard_source = self.t("source_saved", name=path.name)
            self.standard_kind = "snapshot"
            if stored_game:
                matching_label = next((label for label, game in self.games.items() if game.appid == stored_game.appid), None)
                label = matching_label or f"{stored_game.name}  (App ID: {stored_game.appid})"
                if not matching_label:
                    self.games[label] = stored_game
                    self.game_box["values"] = list(self.games)
                self.game_choice.set(label)
            self.show_comparison()
        except Exception as exc:
            if show_error:
                messagebox.showerror(self.t("error"), str(exc), parent=self.root)
            else:
                self.status.set(f"명령행 기준 목록을 불러오지 못했습니다: {exc}")

    def export_snapshot(self) -> None:
        game = self.selected_game()
        if not game or not self.standard_paths:
            messagebox.showwarning(self.t("error"), self.t("no_baseline"), parent=self.root)
            return
        chosen = filedialog.asksaveasfilename(title=self.t("save_baseline"), defaultextension=".json", filetypes=[("JSON", "*.json")])
        if chosen:
            save_snapshot(Path(chosen), game, self.standard_paths, self.standard_dirs, self.standard_source or "불러온 기준 목록")
            self.status.set(self.t("saved"))

    def show_comparison(self) -> None:
        """Show all actual files: removal candidates first, then protected and matching files."""
        comparison = self.comparison_sets()
        if not comparison:
            return
        candidates, protected, present_expected = comparison
        self.tree.delete(*self.tree.get_children())
        self.row_items.clear()
        self.checked_paths = set(candidates)
        for item in sorted(candidates):
            item_id = self.tree.insert("", END, values=("☑", self.t("candidate"), item))
            self.row_items[item_id] = (item, "candidate")
        for item in sorted(protected):
            item_id = self.tree.insert("", END, values=("☐", self.t("protected"), item))
            self.row_items[item_id] = (item, "protected")
        for item in sorted(present_expected):
            item_id = self.tree.insert("", END, values=("", self.t("matching"), item))
            self.row_items[item_id] = (item, "matching")
        self.status.set(self.t("comparison", source=self.standard_source, candidates=len(candidates), protected=len(protected), matching=len(present_expected)))

    def refresh_comparison(self) -> None:
        if self.standard_kind in {"depot", "snapshot"}:
            self.show_comparison()

def main() -> int:
    parser = argparse.ArgumentParser(description="Steam Mod Remover")
    parser.add_argument("--steamapps", help="steamapps 폴더. GUI를 미리 채웁니다.")
    parser.add_argument("--appid", help="자동 선택하거나 headless 검사에 사용할 App ID.")
    parser.add_argument("--snapshot", help="기준 목록 JSON을 자동으로 불러옵니다.")
    parser.add_argument("--headless", action="store_true", help="GUI 없이 JSON 검사 결과만 출력합니다. --steamapps, --appid, --snapshot이 필요합니다.")
    args = parser.parse_args()
    if args.headless:
        if not (args.steamapps and args.appid and args.snapshot):
            parser.error("--headless에는 --steamapps, --appid, --snapshot이 모두 필요합니다.")
        matches = [game for game in installed_games(Path(args.steamapps)) if game.appid == args.appid]
        if not matches:
            parser.error("지정한 App ID의 설치 게임을 steamapps 폴더에서 찾지 못했습니다.")
        _, standard_paths, _ = load_snapshot(Path(args.snapshot))
        extras, protected = scan_files(game_folder(Path(args.steamapps), matches[0]), standard_paths, DEFAULT_PROTECTED_TERMS, True)
        print(json.dumps({"app_id": args.appid, "candidates": extras, "protected": protected, "read_only": True}, ensure_ascii=False, indent=2))
        return 0
    root = Tk()
    root.title("Steam Mod Remover")
    root.minsize(820, 600)
    App(root, args)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
