"""
PROJECTOR OPU Control — приложение для управления двумя устройствами по TCP.
Устройство 1: ОПУ TL.0250 (Pan-Tilt) — ASCII-протокол, 192.168.1.115:9762
Устройство 2: RelayX3 (реле) — бинарный протокол, 192.168.1.114:9761
"""

import socket
import threading
import time
import json
import os
import sys
import asyncio
import functools
import qrcode
import websockets.asyncio.server
from http.server import HTTPServer, SimpleHTTPRequestHandler
from PIL import Image, ImageTk
import customtkinter as ctk

# ============================================================
# Настройки подключения
# ============================================================
PAN_TILT_HOST = "192.168.11.30"
PAN_TILT_PORT = 9760
RELAY_HOST = "192.168.1.114"
RELAY_PORT = 9761
SOCKET_TIMEOUT = 2  # секунды
POLL_INTERVAL_MS = 500  # интервал опроса позиций в мс

# PyInstaller bundle support
if getattr(sys, 'frozen', False):
    _BASE_DIR = os.path.dirname(sys.executable)
    _BUNDLE_DIR = sys._MEIPASS
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    _BUNDLE_DIR = _BASE_DIR

SETTINGS_FILE = os.path.join(_BASE_DIR, "settings.json")
WEB_DIR = os.path.join(_BUNDLE_DIR, "web")
WS_PORT = 8765
HTTP_PORT = 8080

# ============================================================
# Настройки внешнего вида
# ============================================================
WINDOW_WIDTH = 450
WINDOW_HEIGHT = 650
COLOR_CONNECTED = "#2CC985"
COLOR_DISCONNECTED = "#E74C3C"
COLOR_RELAY_ON = "#2CC985"
COLOR_RELAY_OFF = "#555555"


# ============================================================
# Класс для управления Pan-Tilt (ОПУ TL.0250)
# ============================================================
class PanTiltDevice:
    """Управление поворотным устройством ОПУ TL.0250 по ASCII-протоколу."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sock: socket.socket | None = None
        self.connected = False
        self._lock = threading.Lock()

    def connect(self) -> bool:
        """Подключиться к устройству. Возвращает True при успехе."""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(SOCKET_TIMEOUT)
            self.sock.connect((self.host, self.port))
            self.connected = True
            return True
        except Exception:
            self.connected = False
            self.sock = None
            return False

    def disconnect(self):
        """Закрыть соединение."""
        with self._lock:
            self.connected = False
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None

    def send_command(self, command: str) -> bool:
        """Отправить ASCII-команду вида $command#"""
        with self._lock:
            if not self.connected or not self.sock:
                return False
            try:
                self.sock.sendall(command.encode("utf-8"))
                return True
            except Exception:
                self.connected = False
                return False

    def send_and_read(self, command: str) -> str | None:
        """Отправить команду и прочитать ответ до символа '#'."""
        with self._lock:
            if not self.connected or not self.sock:
                return None
            try:
                # Flush any stale data in buffer
                self.sock.setblocking(False)
                try:
                    while self.sock.recv(256):
                        pass
                except (BlockingIOError, OSError):
                    pass
                self.sock.setblocking(True)
                self.sock.settimeout(SOCKET_TIMEOUT)
                
                self.sock.sendall(command.encode("utf-8"))
                # Read until '#' terminator
                buf = b""
                while True:
                    chunk = self.sock.recv(1)
                    if not chunk:
                        return None
                    buf += chunk
                    if chunk == b"#":
                        break
                    if len(buf) > 512:
                        break
                return buf.decode("utf-8", errors="ignore").strip()
            except Exception:
                self.connected = False
                return None

    # --- Команды движения ---
    def pan_left(self, speed: int):
        self.send_command(f"$i,-{speed}#")

    def pan_right(self, speed: int):
        self.send_command(f"$i,{speed}#")

    def tilt_up(self, speed: int):
        self.send_command(f"$v,{speed}#")

    def tilt_down(self, speed: int):
        self.send_command(f"$v,-{speed}#")

    def stop_pan(self):
        self.send_command("$g#")

    def stop_tilt(self):
        self.send_command("$t#")

    def stop_all(self):
        """Остановить обе оси."""
        self.send_command("$g#")
        self.send_command("$t#")

    # --- Запросы позиции ---
    def get_pan_position(self) -> float | None:
        resp = self.send_and_read("$c#")
        if resp:
            try:
                val = resp.strip("$#").split(",")[1]
                return float(val)
            except (IndexError, ValueError):
                return None
        return None

    def get_tilt_position(self) -> float | None:
        resp = self.send_and_read("$p#")
        if resp:
            try:
                val = resp.strip("$#").split(",")[1]
                return float(val)
            except (IndexError, ValueError):
                return None
        return None

    def get_state(self) -> str | None:
        resp = self.send_and_read("$a#")
        if resp:
            try:
                val = resp.strip("$#").split(",")[1]
                return val
            except IndexError:
                return None
        return None

    # --- Позиционирование ---
    def go_to_pan(self, pos: float, speed: float):
        self.send_command(f"$j,{pos},{speed}#")

    def go_to_tilt(self, pos: float, speed: float):
        self.send_command(f"$w,{pos},{speed}#")

    # --- Качание ---
    def swing_pan(self, pos1: float, pos2: float, speed: float):
        self.send_command(f"$k,{pos1},{pos2},{speed}#")

    def swing_tilt(self, pos1: float, pos2: float, speed: float):
        self.send_command(f"$x,{pos1},{pos2},{speed}#")

    # --- Ограничения скорости ---
    def get_speed_limits_pan(self) -> tuple | None:
        resp = self.send_and_read("$l#")
        if resp:
            try:
                parts = resp.strip("$#").split(",")
                if len(parts) >= 3:
                    return (float(parts[0]), float(parts[1]), float(parts[2]))
            except (ValueError, IndexError):
                pass
        return None

    def get_speed_limits_tilt(self) -> tuple | None:
        resp = self.send_and_read("$y#")
        if resp:
            try:
                parts = resp.strip("$#").split(",")
                if len(parts) >= 3:
                    return (float(parts[0]), float(parts[1]), float(parts[2]))
            except (ValueError, IndexError):
                pass
        return None

    def set_speed_limits_pan(self, min_s: float, max_s: float, acc: float):
        self.send_command(f"$l,{min_s},{max_s},{acc}#")

    def set_speed_limits_tilt(self, min_s: float, max_s: float, acc: float):
        self.send_command(f"$y,{min_s},{max_s},{acc}#")

    # --- Ограничения углов ---
    def get_angle_limits_pan(self) -> tuple | None:
        resp = self.send_and_read("$m#")
        if resp:
            try:
                parts = resp.strip("$#").split(",")
                if len(parts) >= 3:
                    return (int(parts[0]), float(parts[1]), float(parts[2]))
            except (ValueError, IndexError):
                pass
        return None

    def get_angle_limits_tilt(self) -> tuple | None:
        resp = self.send_and_read("$z#")
        if resp:
            try:
                parts = resp.strip("$#").split(",")
                if len(parts) >= 3:
                    return (int(parts[0]), float(parts[1]), float(parts[2]))
            except (ValueError, IndexError):
                pass
        return None

    def set_angle_limits_pan(self, enable: int, left: float, right: float):
        self.send_command(f"$m,{enable},{left},{right}#")

    def set_angle_limits_tilt(self, enable: int, left: float, right: float):
        self.send_command(f"$z,{enable},{left},{right}#")

    # --- Статус устройства ---
    def get_temperature(self) -> str | None:
        return self.send_and_read("$0#")

    def get_voltage(self) -> str | None:
        return self.send_and_read("$1#")

    def get_pan_state(self) -> str | None:
        return self.send_and_read("$a#")

    def get_tilt_state(self) -> str | None:
        return self.send_and_read("$n#")

    def start_selfdiag_pan(self):
        self.send_command("$a,1#")

    def start_selfdiag_tilt(self):
        self.send_command("$n,1#")

    # --- Пресеты Pelco-D ---
    def save_preset(self, preset_id: int):
        self.send_command(f"$6,{preset_id}#")

    def go_to_preset(self, preset_id: int, pan_speed: float = 50, tilt_speed: float = 50):
        self.send_command(f"$7,{preset_id},{pan_speed},{tilt_speed}#")

    def delete_preset(self, preset_id: int):
        self.send_command(f"$8,{preset_id}#")

    # --- Информация об устройстве ---
    def get_firmware_type(self): return self.send_and_read("$I#")
    def get_firmware_version(self): return self.send_and_read("$V#")
    def get_power_info(self): return self.send_and_read("$D#")

    # --- Режим управления (v≥1.18) ---
    def get_control_mode_pan(self): return self.send_and_read("$E#")
    def set_control_mode_pan(self, mode: int, precision: int):
        self.send_command(f"$E,{mode},{precision}#")
    def get_control_mode_tilt(self): return self.send_and_read("$G#")
    def set_control_mode_tilt(self, mode: int, precision: int):
        self.send_command(f"$G,{mode},{precision}#")

    # --- Самодиагностика (v≥1.18) ---
    def get_selfdiag_settings_pan(self): return self.send_and_read("$F#")
    def set_selfdiag_settings_pan(self, auto: int, speed: float):
        self.send_command(f"$F,{auto},{speed}#")
    def get_selfdiag_settings_tilt(self): return self.send_and_read("$H#")
    def set_selfdiag_settings_tilt(self, auto: int, speed: float):
        self.send_command(f"$H,{auto},{speed}#")

    # --- Настройки Pelco-D ---
    def get_pelcod_settings(self): return self.send_and_read("$9#")
    def set_pelcod_settings(self, port: int, addr: int, tilt_inverse: int):
        self.send_command(f"$9,{port},{addr},{tilt_inverse}#")

    # --- Настройки RS-485 (v≥1.18) ---
    def get_rs485_settings(self): return self.send_and_read("$A#")
    def set_rs485_settings(self, port: int, baudrate: int, mode: int):
        self.send_command(f"$A,{port},{baudrate},{mode}#")

    # --- Ошибки ---
    def get_pan_errors(self): return self.send_and_read("$b#")
    def get_tilt_errors(self): return self.send_and_read("$o#")

    # --- Занятость ---
    def get_pan_busy(self): return self.send_and_read("$e#")
    def get_tilt_busy(self): return self.send_and_read("$r#")

    # --- Сброс модуля (EEPROM!) ---
    def reset_module(self, module_id: int):
        self.send_command(f"$2,{module_id}#")

    # --- Перезагрузка устройства ---
    def reboot_device(self):
        self.send_command("$3#")


# ============================================================
# Класс для управления RelayX3
# ============================================================
class RelayDevice:
    """Управление платой реле RelayX3 по бинарному протоколу RS-485 через TCP."""

    CHANNELS = {
        1: {"on": (0x88, 0x00), "off": (0x08, 0x00), "name": "Канал 1"},
        2: {"on": (0x02, 0x00), "off": (0x04, 0x00), "name": "Канал 2"},
        3: {"on": (0x00, 0x20), "off": (0x00, 0x40), "name": "Канал 3"},
    }

    def __init__(self, host: str, port: int, address: int = 1):
        self.host = host
        self.port = port
        self.address = address
        self.sock: socket.socket | None = None
        self.connected = False
        self.channel_states = {1: False, 2: False, 3: False}
        self._lock = threading.Lock()

    def _make_packet(self, cmd1: int, cmd2: int, data1: int = 0, data2: int = 0) -> bytes:
        checksum = (self.address + cmd1 + cmd2 + data1 + data2) & 0xFF
        return bytes([0xFF, self.address, cmd1, cmd2, data1, data2, checksum])

    def connect(self) -> bool:
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(SOCKET_TIMEOUT)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock.connect((self.host, self.port))
            self.connected = True
            print(f"[RELAY] Подключено к {self.host}:{self.port}")
            return True
        except Exception as e:
            self.connected = False
            self.sock = None
            print(f"[RELAY] Ошибка подключения: {e}")
            return False

    def disconnect(self):
        with self._lock:
            self.connected = False
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None

    def send_command(self, cmd1: int, cmd2: int, data1: int = 0, data2: int = 0) -> bool:
        packet = self._make_packet(cmd1, cmd2, data1, data2)
        print(f"[RELAY] TX: {packet.hex(' ')}")
        with self._lock:
            if not self.connected or not self.sock:
                print("[RELAY] Не подключено, отправка невозможна")
                return False
            try:
                self.sock.sendall(packet)
                print("[RELAY] Отправлено успешно")
                return True
            except Exception as e:
                self.connected = False
                print(f"[RELAY] Ошибка отправки: {e}")
                return False

    def query_command(self, cmd1: int, cmd2: int, data1: int = 0, data2: int = 0) -> bytes | None:
        packet = self._make_packet(cmd1, cmd2, data1, data2)
        with self._lock:
            if not self.connected or not self.sock:
                return None
            try:
                self.sock.sendall(packet)
                self.sock.settimeout(1)
                resp = self.sock.recv(7)
                self.sock.settimeout(SOCKET_TIMEOUT)
                if len(resp) == 7:
                    return resp
                return None
            except Exception:
                self.sock.settimeout(SOCKET_TIMEOUT)
                return None

    def read_status(self) -> dict[int, bool] | None:
        resp = self.query_command(0x00, 0x77)
        if resp is None:
            return None
        status_word = (resp[4] << 8) | resp[5]
        return {
            1: bool(status_word & 0x0010),
            2: bool(status_word & 0x0080),
            3: bool(status_word & 0x0400),
        }

    def toggle_channel(self, ch: int) -> bool:
        if ch not in self.CHANNELS:
            return False
        if self.channel_states[ch]:
            cmd1, cmd2 = self.CHANNELS[ch]["off"]
        else:
            cmd1, cmd2 = self.CHANNELS[ch]["on"]
        success = self.send_command(cmd1, cmd2)
        if success:
            self.channel_states[ch] = not self.channel_states[ch]
        print(f"[RELAY] Канал {ch} -> {'ВКЛ' if self.channel_states[ch] else 'ВЫКЛ'}")
        return success

    def set_channel(self, ch: int, on: bool) -> bool:
        if ch not in self.CHANNELS:
            return False
        if on:
            cmd1, cmd2 = self.CHANNELS[ch]["on"]
        else:
            cmd1, cmd2 = self.CHANNELS[ch]["off"]
        success = self.send_command(cmd1, cmd2)
        if success:
            self.channel_states[ch] = on
        return success


# ============================================================
# Главное приложение (GUI)
# ============================================================
class ControlApp(ctk.CTk):
    """Главное окно приложения PROJECTOR OPU Control."""

    def __init__(self):
        super().__init__()

        # --- Настройка окна ---
        self.title("Контроллер Управления Прожектором")
        self.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.minsize(WINDOW_WIDTH, 500)
        self.resizable(False, True)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # --- Загрузка сохранённых настроек ---
        self._load_settings()

        # --- Таймерные структуры ---
        self.timer_threads: dict[int, threading.Thread] = {}
        self.timer_stop_flags: dict[int, threading.Event] = {}
        self.timer_widgets: dict[int, dict] = {}  # виджеты таймера для каждого канала
        self.sequential_thread: threading.Thread | None = None
        self.sequential_stop_flag: threading.Event = threading.Event()
        self.timer_mode = ctk.StringVar(value=self._settings.get("timer_mode", "parallel"))

        # --- Создание объектов устройств ---
        pt_host = self._settings.get("pt_host", PAN_TILT_HOST)
        pt_port = self._settings.get("pt_port", PAN_TILT_PORT)
        rl_host = self._settings.get("rl_host", RELAY_HOST)
        rl_port = self._settings.get("rl_port", RELAY_PORT)
        self.pan_tilt = PanTiltDevice(pt_host, pt_port)
        self.relay = RelayDevice(rl_host, rl_port)

        # --- Построение интерфейса ---
        self._build_connection_panel()
        self._build_pantilt_panel()
        self._build_relay_panel()

        # --- Фоновые потоки подключения ---
        self._start_connection_threads()

        # --- Запуск опроса позиций и статуса реле ---
        self._poll_positions()
        self._poll_relay_status()

        # --- WebSocket + HTTP серверы ---
        self._ws_clients: set = set()
        self._ws_stop_event = threading.Event()
        self._ws_thread = threading.Thread(target=self._run_ws_server, daemon=True)
        self._ws_thread.start()
        self._http_server = None
        self._http_thread = threading.Thread(target=self._run_http_server, daemon=True)
        self._http_thread.start()

        # --- Обработка закрытия окна ---
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # --------------------------------------------------------
    # Панель статусов подключения
    # --------------------------------------------------------
    def _build_connection_panel(self):
        self.conn_frame = ctk.CTkFrame(self, corner_radius=10)
        self.conn_frame.pack(padx=15, pady=(15, 5), fill="x")

        # Header with title + gear
        hdr = ctk.CTkFrame(self.conn_frame, fg_color="transparent")
        hdr.pack(fill="x", padx=10, pady=(8, 0))

        ctk.CTkLabel(hdr, text="Статус подключения",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")

        self.conn_gear_btn = ctk.CTkButton(
            hdr, text="⚙", width=28, height=28,
            font=ctk.CTkFont(size=14),
            fg_color="transparent", hover_color="#3A3A3A",
            command=self._toggle_conn_settings
        )
        self.conn_gear_btn.pack(side="right")

        self.qr_btn = ctk.CTkButton(
            hdr, text="📱 QR", width=36, height=28,
            font=ctk.CTkFont(size=12),
            fg_color="transparent", hover_color="#3A3A3A",
            command=self._show_qr_popup
        )
        self.qr_btn.pack(side="right", padx=(0, 4))

        # Status indicators (compact inline)
        indicators = ctk.CTkFrame(self.conn_frame, fg_color="transparent")
        indicators.pack(padx=10, pady=(5, 8), fill="x")

        self.pt_status_label = ctk.CTkLabel(
            indicators, text="● ОПУ",
            font=ctk.CTkFont(size=12),
            text_color=COLOR_DISCONNECTED
        )
        self.pt_status_label.pack(side="left", padx=(5, 15))

        self.rl_status_label = ctk.CTkLabel(
            indicators, text="● RelayX3",
            font=ctk.CTkFont(size=12),
            text_color=COLOR_DISCONNECTED
        )
        self.rl_status_label.pack(side="left", padx=5)

        # Settings section (hidden)
        self.conn_settings_visible = False
        self.conn_settings_frame = ctk.CTkFrame(self.conn_frame, fg_color="#1F1F1F", corner_radius=8)

        self._build_conn_settings()

    def _build_conn_settings(self):
        """Build IP/port entries in the connection settings frame."""
        f = self.conn_settings_frame
        for w in f.winfo_children():
            w.destroy()

        lbl_font = ctk.CTkFont(size=11)
        entry_style = {"height": 28, "font": ctk.CTkFont(size=11), "corner_radius": 6,
                       "fg_color": "#1A1A1A", "border_color": "#3A3A3A"}

        # OPU row
        r1 = ctk.CTkFrame(f, fg_color="transparent")
        r1.pack(fill="x", padx=10, pady=(10, 4))
        ctk.CTkLabel(r1, text="ОПУ IP:", font=lbl_font, text_color="#9CA3AF").pack(side="left", padx=(0, 4))
        self.conn_pt_ip = ctk.CTkEntry(r1, width=130, **entry_style)
        self.conn_pt_ip.insert(0, self.pan_tilt.host)
        self.conn_pt_ip.pack(side="left", padx=(0, 8))
        ctk.CTkLabel(r1, text="Порт:", font=lbl_font, text_color="#9CA3AF").pack(side="left", padx=(0, 4))
        self.conn_pt_port = ctk.CTkEntry(r1, width=60, **entry_style)
        self.conn_pt_port.insert(0, str(self.pan_tilt.port))
        self.conn_pt_port.pack(side="left")

        # Relay row
        r2 = ctk.CTkFrame(f, fg_color="transparent")
        r2.pack(fill="x", padx=10, pady=(4, 4))
        ctk.CTkLabel(r2, text="Реле IP:", font=lbl_font, text_color="#9CA3AF").pack(side="left", padx=(0, 4))
        self.conn_rl_ip = ctk.CTkEntry(r2, width=130, **entry_style)
        self.conn_rl_ip.insert(0, self.relay.host)
        self.conn_rl_ip.pack(side="left", padx=(0, 8))
        ctk.CTkLabel(r2, text="Порт:", font=lbl_font, text_color="#9CA3AF").pack(side="left", padx=(0, 4))
        self.conn_rl_port = ctk.CTkEntry(r2, width=60, **entry_style)
        self.conn_rl_port.insert(0, str(self.relay.port))
        self.conn_rl_port.pack(side="left")

        # Apply button
        btn_row = ctk.CTkFrame(f, fg_color="transparent")
        btn_row.pack(fill="x", padx=10, pady=(6, 10))
        ctk.CTkButton(
            btn_row, text="Применить", width=100, height=28,
            font=ctk.CTkFont(size=11), corner_radius=6,
            fg_color="#3B82F6", hover_color="#2563EB",
            command=self._apply_conn_settings
        ).pack(side="left")

    def _toggle_conn_settings(self):
        if self.conn_settings_visible:
            self.conn_settings_frame.pack_forget()
            self.conn_settings_visible = False
        else:
            self.conn_settings_frame.pack(fill="x", padx=10, pady=(0, 8))
            self.conn_settings_visible = True
        self.after(50, self._fit_window_height)

    def _apply_conn_settings(self):
        """Apply new IP/port, reconnect devices."""
        new_pt_ip = self.conn_pt_ip.get().strip()
        new_pt_port = int(self.conn_pt_port.get().strip() or 9760)
        new_rl_ip = self.conn_rl_ip.get().strip()
        new_rl_port = int(self.conn_rl_port.get().strip() or 9761)

        # Disconnect and update
        self.pan_tilt.disconnect()
        self.pan_tilt.host = new_pt_ip
        self.pan_tilt.port = new_pt_port

        self.relay.disconnect()
        self.relay.host = new_rl_ip
        self.relay.port = new_rl_port

        # Update status
        self.after(0, self._update_pt_status)
        self.after(0, self._update_rl_status)

    # --------------------------------------------------------
    # Панель управления Pan-Tilt
    # --------------------------------------------------------
    def _build_pantilt_panel(self):
        self.pt_frame = ctk.CTkFrame(self, corner_radius=12, fg_color="#2B2B2B")
        self.pt_frame.pack(padx=15, pady=5, fill="both", expand=True)

        # --- Header: title + gear button ---
        header = ctk.CTkFrame(self.pt_frame, fg_color="transparent")
        header.pack(fill="x", padx=15, pady=(12, 0))

        title_row = ctk.CTkFrame(header, fg_color="transparent")
        title_row.pack(fill="x")

        ctk.CTkLabel(title_row, text="Управление ОПУ",
                     font=ctk.CTkFont(size=14, weight="bold"),
                     text_color="#E5E7EB").pack(side="left")

        self.pt_settings_btn = ctk.CTkButton(
            title_row, text="⚙", width=32, height=32,
            font=ctk.CTkFont(size=16),
            fg_color="transparent", hover_color="#3A3A3A",
            command=self._toggle_pantilt_settings
        )
        self.pt_settings_btn.pack(side="right")

        # Accent line
        accent = ctk.CTkFrame(self.pt_frame, height=2, fg_color="#3B82F6",
                              corner_radius=1)
        accent.pack(fill="x", padx=15, pady=(6, 8))

        self.pt_settings_visible = False
        self.pt_settings_frame = ctk.CTkScrollableFrame(
            self.pt_frame, fg_color="#1F1F1F", corner_radius=10,
            height=350, scrollbar_button_color="#1F1F1F",
            scrollbar_button_hover_color="#3A3A3A"
        )

        self.pt_dpad_frame = ctk.CTkFrame(self.pt_frame, fg_color="transparent")
        self.pt_dpad_frame.pack(pady=5)
        dpad = self.pt_dpad_frame

        # Общие стили кнопок
        _btn = {"width": 70, "height": 70, "corner_radius": 12,
                "fg_color": "#2B2B2B", "hover_color": "#3B82F6",
                "border_width": 1, "border_color": "#4A4A4A"}
        _gap = {"padx": 3, "pady": 3}

        # Диагональные кнопки
        btn_ul = ctk.CTkButton(dpad, text="◤", font=ctk.CTkFont(size=22),
                               command=lambda: None, **_btn)
        btn_ul.grid(row=0, column=0, **_gap)

        btn_up = ctk.CTkButton(dpad, text="▲", font=ctk.CTkFont(size=26),
                               command=lambda: None, **_btn)
        btn_up.grid(row=0, column=1, **_gap)

        btn_ur = ctk.CTkButton(dpad, text="◥", font=ctk.CTkFont(size=22),
                               command=lambda: None, **_btn)
        btn_ur.grid(row=0, column=2, **_gap)

        btn_left = ctk.CTkButton(dpad, text="◀", font=ctk.CTkFont(size=26),
                                 command=lambda: None, **_btn)
        btn_left.grid(row=1, column=0, **_gap)

        btn_stop = ctk.CTkButton(dpad, text="STOP", width=70, height=70,
                                 font=ctk.CTkFont(size=13, weight="bold"),
                                 corner_radius=12,
                                 fg_color="#C0392B", hover_color="#E74C3C",
                                 border_width=1, border_color="#E74C3C",
                                 command=self._stop_all)
        btn_stop.grid(row=1, column=1, **_gap)

        btn_right = ctk.CTkButton(dpad, text="▶", font=ctk.CTkFont(size=26),
                                  command=lambda: None, **_btn)
        btn_right.grid(row=1, column=2, **_gap)

        btn_dl = ctk.CTkButton(dpad, text="◣", font=ctk.CTkFont(size=22),
                               command=lambda: None, **_btn)
        btn_dl.grid(row=2, column=0, **_gap)

        btn_down = ctk.CTkButton(dpad, text="▼", font=ctk.CTkFont(size=26),
                                 command=lambda: None, **_btn)
        btn_down.grid(row=2, column=1, **_gap)

        btn_dr = ctk.CTkButton(dpad, text="◢", font=ctk.CTkFont(size=22),
                               command=lambda: None, **_btn)
        btn_dr.grid(row=2, column=2, **_gap)

        # Привязки: кардинальные направления
        btn_up.bind("<ButtonPress-1>", lambda e: self._tilt_up_start())
        btn_up.bind("<ButtonRelease-1>", lambda e: self._tilt_stop())
        btn_down.bind("<ButtonPress-1>", lambda e: self._tilt_down_start())
        btn_down.bind("<ButtonRelease-1>", lambda e: self._tilt_stop())
        btn_left.bind("<ButtonPress-1>", lambda e: self._pan_left_start())
        btn_left.bind("<ButtonRelease-1>", lambda e: self._pan_stop())
        btn_right.bind("<ButtonPress-1>", lambda e: self._pan_right_start())
        btn_right.bind("<ButtonRelease-1>", lambda e: self._pan_stop())

        # Привязки: диагональные направления
        btn_ul.bind("<ButtonPress-1>", lambda e: self._diag_up_left_start())
        btn_ul.bind("<ButtonRelease-1>", lambda e: self._stop_all())
        btn_ur.bind("<ButtonPress-1>", lambda e: self._diag_up_right_start())
        btn_ur.bind("<ButtonRelease-1>", lambda e: self._stop_all())
        btn_dl.bind("<ButtonPress-1>", lambda e: self._diag_down_left_start())
        btn_dl.bind("<ButtonRelease-1>", lambda e: self._stop_all())
        btn_dr.bind("<ButtonPress-1>", lambda e: self._diag_down_right_start())
        btn_dr.bind("<ButtonRelease-1>", lambda e: self._stop_all())

        speed_frame = ctk.CTkFrame(self.pt_frame, fg_color="transparent")
        speed_frame.pack(pady=(10, 0), fill="x", padx=20)

        # --- Pan speed ---
        saved_pan = self._settings.get("pan_speed",
                                       self._settings.get("speed", 30))
        self.pan_speed_label = ctk.CTkLabel(
            speed_frame, text=f"Pan: {saved_pan} °/сек",
            font=ctk.CTkFont(size=12))
        self.pan_speed_label.pack()

        self.pan_speed_slider = ctk.CTkSlider(
            speed_frame, from_=5, to=300, number_of_steps=295,
            command=self._on_pan_speed_change
        )
        self.pan_speed_slider.set(saved_pan)
        self.pan_speed_slider.pack(fill="x", pady=(2, 6))

        # --- Tilt speed ---
        saved_tilt = self._settings.get("tilt_speed",
                                        self._settings.get("speed", 30))
        self.tilt_speed_label = ctk.CTkLabel(
            speed_frame, text=f"Tilt: {saved_tilt} °/сек",
            font=ctk.CTkFont(size=12))
        self.tilt_speed_label.pack()

        self.tilt_speed_slider = ctk.CTkSlider(
            speed_frame, from_=5, to=180, number_of_steps=175,
            command=self._on_tilt_speed_change
        )
        self.tilt_speed_slider.set(saved_tilt)
        self.tilt_speed_slider.pack(fill="x", pady=(2, 4))

        # --- Tilt inversion ---
        self.tilt_invert_var = ctk.BooleanVar(
            value=self._settings.get("tilt_invert", False))
        self.tilt_invert_cb = ctk.CTkCheckBox(
            speed_frame, text="Инверсия Tilt",
            variable=self.tilt_invert_var,
            font=ctk.CTkFont(size=12),
            fg_color="#3B82F6", hover_color="#2563EB"
        )
        self.tilt_invert_cb.pack(pady=(0, 8))

        pos_frame = ctk.CTkFrame(self.pt_frame, fg_color="transparent")
        pos_frame.pack(pady=(0, 10), fill="x", padx=20)

        self.pan_pos_label = ctk.CTkLabel(pos_frame, text="Pan: --.-°",
                                          font=ctk.CTkFont(size=13))
        self.pan_pos_label.grid(row=0, column=0, padx=10, pady=3, sticky="w")

        self.tilt_pos_label = ctk.CTkLabel(pos_frame, text="Tilt: --.-°",
                                           font=ctk.CTkFont(size=13))
        self.tilt_pos_label.grid(row=0, column=1, padx=10, pady=3, sticky="e")

        pos_frame.columnconfigure((0, 1), weight=1)

    # --------------------------------------------------------
    # Панель управления реле (современный UI)
    # --------------------------------------------------------
    def _build_relay_panel(self):
        self.relay_frame = ctk.CTkFrame(self, corner_radius=12, fg_color="#2B2B2B")
        self.relay_frame.pack(padx=15, pady=(5, 15), fill="x")

        # --- Header: "РЕЛЕ" + accent line + gear icon ---
        header = ctk.CTkFrame(self.relay_frame, fg_color="transparent")
        header.pack(fill="x", padx=15, pady=(12, 0))

        title_row = ctk.CTkFrame(header, fg_color="transparent")
        title_row.pack(fill="x")

        ctk.CTkLabel(title_row, text="РЕЛЕ",
                     font=ctk.CTkFont(size=14, weight="bold"),
                     text_color="#E5E7EB").pack(side="left")

        self.settings_btn = ctk.CTkButton(
            title_row, text="⚙", width=32, height=32,
            font=ctk.CTkFont(size=16),
            fg_color="transparent", hover_color="#3A3A3A",
            command=self._toggle_channel_settings
        )
        self.settings_btn.pack(side="right")

        # Accent line under header
        accent = ctk.CTkFrame(self.relay_frame, height=2, fg_color="#2CC985",
                              corner_radius=1)
        accent.pack(fill="x", padx=15, pady=(6, 8))

        # --- Initialize channel data ---
        self.channel_checkboxes = {}
        self.channel_name_entries = {}
        self.channel_settings_visible = False
        saved_names = self._settings.get("channel_names", {})
        saved_visible = self._settings.get("channels_visible", {})
        self.channel_custom_names = {
            1: saved_names.get("1", "Проектор"),
            2: saved_names.get("2", "Канал 2"),
            3: saved_names.get("3", "Канал 3"),
        }
        # Pre-create BooleanVars so buttons are visible at startup
        for ch_num in RelayDevice.CHANNELS:
            self.channel_checkboxes[ch_num] = ctk.BooleanVar(
                value=saved_visible.get(str(ch_num), ch_num == 1)
            )

        # --- Settings panel (hidden by default) ---
        self.channel_settings_frame = ctk.CTkScrollableFrame(
            self.relay_frame, fg_color="#1F1F1F", corner_radius=10,
            height=300, scrollbar_button_color="#1F1F1F",
            scrollbar_button_hover_color="#3A3A3A"
        )

        # --- Mode selector frame (shown only in settings, created once) ---
        self.mode_selector_frame = ctk.CTkFrame(self.relay_frame, fg_color="#1F1F1F",
                                                 corner_radius=10)
        self._build_mode_selector()

        # --- Main relay buttons container ---
        self.relay_buttons_frame = ctk.CTkFrame(self.relay_frame, fg_color="transparent")
        self.relay_buttons_frame.pack(padx=15, pady=(0, 15), fill="x")

        self.relay_buttons: dict[int, ctk.CTkButton] = {}
        self.relay_countdown_labels: dict[int, ctk.CTkLabel] = {}
        self._rebuild_relay_buttons()

    def _build_mode_selector(self):
        """Build the mode selector + action buttons inside mode_selector_frame."""
        for w in self.mode_selector_frame.winfo_children():
            w.destroy()

        inner = ctk.CTkFrame(self.mode_selector_frame, fg_color="transparent")
        inner.pack(fill="x", padx=12, pady=10)

        ctk.CTkLabel(inner, text="Режим:",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color="#9CA3AF").pack(side="left", padx=(0, 8))

        self.radio_parallel = ctk.CTkRadioButton(
            inner, text="Параллельно", variable=self.timer_mode,
            value="parallel", font=ctk.CTkFont(size=12),
            fg_color="#2CC985", hover_color="#2CC985"
        )
        self.radio_parallel.pack(side="left", padx=(0, 12))

        self.radio_sequential = ctk.CTkRadioButton(
            inner, text="Последовательно", variable=self.timer_mode,
            value="sequential", font=ctk.CTkFont(size=12),
            fg_color="#2CC985", hover_color="#2CC985"
        )
        self.radio_sequential.pack(side="left", padx=(0, 15))

        btn_row = ctk.CTkFrame(self.mode_selector_frame, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(0, 10))

        self.start_all_btn = ctk.CTkButton(
            btn_row, text="▶  Запуск всех", width=160, height=34,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#3B82F6", hover_color="#2563EB",
            corner_radius=8, command=self._start_all_timers
        )
        self.start_all_btn.pack(side="left", padx=(0, 8), expand=True, fill="x")

        self.stop_all_btn = ctk.CTkButton(
            btn_row, text="⏹  Стоп всех", width=160, height=34,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#EF4444", hover_color="#DC2626",
            corner_radius=8, command=self._stop_all_timers
        )
        self.stop_all_btn.pack(side="left", expand=True, fill="x")

    def _build_settings_panel(self):
        """Rebuild settings panel content dynamically."""
        for w in self.channel_settings_frame.winfo_children():
            w.destroy()

        visible = self._get_visible_channels()
        saved_timers = self._settings.get("timer_settings", {})

        # --- Section 1: Channels ---
        ch_section = ctk.CTkFrame(self.channel_settings_frame, fg_color="transparent")
        ch_section.pack(fill="x", padx=12, pady=(12, 6))

        ctk.CTkLabel(ch_section, text="Каналы",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color="#9CA3AF").pack(anchor="w")

        sep1 = ctk.CTkFrame(ch_section, height=1, fg_color="#3A3A3A", corner_radius=0)
        sep1.pack(fill="x", pady=(4, 8))

        for ch_num in RelayDevice.CHANNELS:
            row = ctk.CTkFrame(ch_section, fg_color="#262626", corner_radius=8)
            row.pack(fill="x", pady=2)

            var = self.channel_checkboxes[ch_num]
            cb = ctk.CTkCheckBox(
                row, text=f"CH{ch_num}", variable=var,
                command=self._on_channel_visibility_changed,
                font=ctk.CTkFont(size=12), width=75,
                fg_color="#2CC985", hover_color="#27AE60"
            )
            cb.pack(side="left", padx=(10, 5), pady=6)

            entry = ctk.CTkEntry(
                row, width=180, height=30,
                font=ctk.CTkFont(size=12), corner_radius=6,
                fg_color="#1A1A1A", border_color="#3A3A3A",
                placeholder_text="Название..."
            )
            entry.insert(0, self.channel_custom_names.get(ch_num, f"Канал {ch_num}"))
            entry.bind("<KeyRelease>", lambda e, c=ch_num: self._on_name_change(c))
            entry.pack(side="left", padx=(5, 10), pady=6, fill="x", expand=True)
            self.channel_name_entries[ch_num] = entry

        # --- Section 2: Timers (only for visible channels) ---
        if visible:
            tm_section = ctk.CTkFrame(self.channel_settings_frame, fg_color="transparent")
            tm_section.pack(fill="x", padx=12, pady=(8, 6))

            ctk.CTkLabel(tm_section, text="Таймеры",
                         font=ctk.CTkFont(size=12, weight="bold"),
                         text_color="#9CA3AF").pack(anchor="w")

            sep2 = ctk.CTkFrame(tm_section, height=1, fg_color="#3A3A3A", corner_radius=0)
            sep2.pack(fill="x", pady=(4, 8))

            for ch_num in visible:
                ch_key = str(ch_num)
                ts = saved_timers.get(ch_key, {})
                self._build_timer_row(ch_num, ts, tm_section)

        # --- Section 3: Mode (only if 2+ visible channels) ---
        # Mode selector packing is handled by _toggle_channel_settings / _on_channel_visibility_changed

    def _build_timer_row(self, ch_num: int, defaults: dict, parent):
        """Build timer config row inside the settings panel."""
        name = self.channel_custom_names.get(ch_num, f"Канал {ch_num}")
        card = ctk.CTkFrame(parent, fg_color="#262626", corner_radius=8)
        card.pack(fill="x", pady=3)

        ctk.CTkLabel(card, text=name, font=ctk.CTkFont(size=12, weight="bold"),
                     text_color="#E5E7EB").pack(anchor="w", padx=10, pady=(8, 4))

        controls = ctk.CTkFrame(card, fg_color="transparent")
        controls.pack(fill="x", padx=10, pady=(0, 8))

        lbl_style = ctk.CTkFont(size=11)
        ctk.CTkLabel(controls, text="Ч:", font=lbl_style, text_color="#9CA3AF").pack(side="left")
        spin_h = ctk.CTkEntry(controls, width=42, height=28, font=ctk.CTkFont(size=11),
                               justify="center", corner_radius=6,
                               fg_color="#1A1A1A", border_color="#3A3A3A")
        spin_h.insert(0, str(defaults.get("hours", 0)))
        spin_h.pack(side="left", padx=(2, 8))

        ctk.CTkLabel(controls, text="М:", font=lbl_style, text_color="#9CA3AF").pack(side="left")
        spin_m = ctk.CTkEntry(controls, width=42, height=28, font=ctk.CTkFont(size=11),
                               justify="center", corner_radius=6,
                               fg_color="#1A1A1A", border_color="#3A3A3A")
        spin_m.insert(0, str(defaults.get("minutes", 0)))
        spin_m.pack(side="left", padx=(2, 8))

        ctk.CTkLabel(controls, text="С:", font=lbl_style, text_color="#9CA3AF").pack(side="left")
        spin_s = ctk.CTkEntry(controls, width=42, height=28, font=ctk.CTkFont(size=11),
                               justify="center", corner_radius=6,
                               fg_color="#1A1A1A", border_color="#3A3A3A")
        spin_s.insert(0, str(defaults.get("seconds", 30)))
        spin_s.pack(side="left", padx=(2, 8))

        ctk.CTkLabel(controls, text="Циклы:", font=lbl_style, text_color="#9CA3AF").pack(side="left")
        spin_c = ctk.CTkEntry(controls, width=50, height=28, font=ctk.CTkFont(size=11),
                               justify="center", corner_radius=6,
                               fg_color="#1A1A1A", border_color="#3A3A3A")
        spin_c.insert(0, str(defaults.get("cycles", 1)))
        spin_c.pack(side="left", padx=(2, 8))

        btn_start = ctk.CTkButton(
            controls, text="▶", width=32, height=28,
            font=ctk.CTkFont(size=12), corner_radius=6,
            fg_color="#3B82F6", hover_color="#2563EB",
            command=lambda c=ch_num: self._start_channel_timer(c)
        )
        btn_start.pack(side="left", padx=(4, 2))

        btn_stop = ctk.CTkButton(
            controls, text="⏹", width=32, height=28,
            font=ctk.CTkFont(size=12), corner_radius=6,
            fg_color="#EF4444", hover_color="#DC2626",
            command=lambda c=ch_num: self._stop_channel_timer(c)
        )
        btn_stop.pack(side="left", padx=(0, 2))

        # Compact countdown label (shown inline under the button in main view)
        countdown_label = ctk.CTkLabel(
            card, text="", font=ctk.CTkFont(size=11),
            text_color="#9CA3AF"
        )

        self.timer_widgets[ch_num] = {
            "frame": card,
            "spin_h": spin_h,
            "spin_m": spin_m,
            "spin_s": spin_s,
            "spin_c": spin_c,
            "countdown": countdown_label,
        }

    def _toggle_channel_settings(self):
        """Toggle settings panel visibility."""
        if self.channel_settings_visible:
            self.channel_settings_frame.pack_forget()
            self.mode_selector_frame.pack_forget()
            self.channel_settings_visible = False
        else:
            self._build_settings_panel()
            self.channel_settings_frame.pack(fill="x", padx=12, pady=(4, 4),
                                             before=self.relay_buttons_frame)
            self.channel_settings_visible = True
            visible = self._get_visible_channels()
            if len(visible) >= 2:
                self.mode_selector_frame.pack(fill="x", padx=12, pady=(4, 4),
                                              before=self.relay_buttons_frame)
        self.after(50, self._fit_window_height)

    def _on_name_change(self, ch_num: int):
        entry = self.channel_name_entries[ch_num]
        self.channel_custom_names[ch_num] = entry.get() or f"Канал {ch_num}"
        self._update_relay_button(ch_num)

    def _get_visible_channels(self) -> list[int]:
        """Return list of channels visible on desktop (channels_visible checkboxes)."""
        return [ch for ch, var in self.channel_checkboxes.items() if var.get()]

    def _on_channel_visibility_changed(self):
        """Called when a channel checkbox is toggled in settings.
        Controls both desktop button visibility and mobile relay list."""
        # Stop all active timers for safety
        for ch in list(self.timer_stop_flags.keys()):
            self.timer_stop_flags[ch].set()
        self.timer_stop_flags.clear()
        self.timer_threads.clear()
        self.timer_widgets.clear()

        # Rebuild settings panel if visible
        if self.channel_settings_visible:
            self._build_settings_panel()
            visible = self._get_visible_channels()
            if len(visible) >= 2:
                self.mode_selector_frame.pack(fill="x", padx=12, pady=(4, 4),
                                              before=self.relay_buttons_frame)
            else:
                self.mode_selector_frame.pack_forget()

        # Rebuild main buttons
        self._rebuild_relay_buttons()

    def _rebuild_relay_buttons(self):
        """Rebuild main view toggle buttons for visible channels."""
        for widget in self.relay_buttons_frame.winfo_children():
            widget.destroy()
        self.relay_buttons.clear()
        self.relay_countdown_labels.clear()

        visible = self._get_visible_channels()

        for ch_num in visible:
            state = self.relay.channel_states[ch_num]
            name = self.channel_custom_names.get(ch_num, f"Канал {ch_num}")
            indicator = "● ВКЛ" if state else "○ ВЫКЛ"
            color = COLOR_RELAY_ON if state else COLOR_RELAY_OFF
            hover = "#27AE60" if state else "#6B6B6B"

            btn = ctk.CTkButton(
                self.relay_buttons_frame,
                text=f"  {name}    {indicator}",
                height=48,
                font=ctk.CTkFont(size=14, weight="bold"),
                fg_color=color,
                hover_color=hover,
                corner_radius=10,
                anchor="w",
                command=lambda c=ch_num: self._toggle_relay_channel(c)
            )
            btn.pack(fill="x", pady=(6, 0))
            self.relay_buttons[ch_num] = btn

            # Compact inline countdown label (hidden when no timer active)
            countdown = ctk.CTkLabel(
                self.relay_buttons_frame, text="",
                font=ctk.CTkFont(size=11),
                text_color="#9CA3AF"
            )
            countdown.pack(pady=(0, 2))
            self.relay_countdown_labels[ch_num] = countdown

        self.after(50, self._fit_window_height)

    def _fit_window_height(self):
        """Fit window height to content."""
        self.update_idletasks()
        needed = self.winfo_reqheight()
        current_w = self.winfo_width()
        self.geometry(f"{current_w}x{needed}")

    # --------------------------------------------------------
    # Pan-Tilt настройки: toggle / build
    # --------------------------------------------------------
    def _toggle_pantilt_settings(self):
        """Toggle pan-tilt settings panel visibility."""
        if self.pt_settings_visible:
            self.pt_settings_frame.pack_forget()
            self.pt_settings_visible = False
        else:
            self._build_pantilt_settings()
            self.pt_settings_frame.pack(fill="x", padx=12, pady=(4, 4),
                                         before=self.pt_dpad_frame)
            self.pt_settings_visible = True
        self.after(50, self._fit_window_height)

    def _pt_section_label(self, parent, text):
        lbl = ctk.CTkLabel(parent, text=text,
                           font=ctk.CTkFont(size=12, weight="bold"),
                           text_color="#9CA3AF")
        lbl.pack(anchor="w")
        sep = ctk.CTkFrame(parent, height=1, fg_color="#3A3A3A", corner_radius=0)
        sep.pack(fill="x", pady=(4, 8))

    def _pt_entry(self, parent, width=65, placeholder=""):
        e = ctk.CTkEntry(parent, width=width, height=28,
                          font=ctk.CTkFont(size=11), justify="center",
                          corner_radius=6, fg_color="#1A1A1A",
                          border_color="#3A3A3A",
                          placeholder_text=placeholder)
        return e

    def _pt_action_btn(self, parent, text, cmd, width=80, color="#3B82F6", hover="#2563EB"):
        return ctk.CTkButton(parent, text=text, width=width, height=28,
                              font=ctk.CTkFont(size=11, weight="bold"),
                              corner_radius=6, fg_color=color, hover_color=hover,
                              command=cmd)

    def _build_pantilt_settings(self):
        """Rebuild pan-tilt settings panel content."""
        for w in self.pt_settings_frame.winfo_children():
            w.destroy()

        lbl_style = ctk.CTkFont(size=11)

        # ===== Section: Positioning =====
        pos_sec = ctk.CTkFrame(self.pt_settings_frame, fg_color="transparent")
        pos_sec.pack(fill="x", padx=12, pady=(12, 6))
        self._pt_section_label(pos_sec, "Позиционирование")

        # Pan go-to
        pan_row = ctk.CTkFrame(pos_sec, fg_color="#262626", corner_radius=8)
        pan_row.pack(fill="x", pady=2)
        ctk.CTkLabel(pan_row, text="Pan °:", font=lbl_style, text_color="#9CA3AF").pack(side="left", padx=(10, 4), pady=6)
        self.pt_goto_pan_entry = self._pt_entry(pan_row, width=70, placeholder="0.00")
        self.pt_goto_pan_entry.pack(side="left", padx=(0, 6), pady=6)
        self._pt_action_btn(pan_row, "Перейти", self._pt_goto_pan).pack(side="left", padx=(0, 10), pady=6)

        # Tilt go-to
        tilt_row = ctk.CTkFrame(pos_sec, fg_color="#262626", corner_radius=8)
        tilt_row.pack(fill="x", pady=2)
        ctk.CTkLabel(tilt_row, text="Tilt °:", font=lbl_style, text_color="#9CA3AF").pack(side="left", padx=(10, 4), pady=6)
        self.pt_goto_tilt_entry = self._pt_entry(tilt_row, width=70, placeholder="0.00")
        self.pt_goto_tilt_entry.pack(side="left", padx=(0, 6), pady=6)
        self._pt_action_btn(tilt_row, "Перейти", self._pt_goto_tilt).pack(side="left", padx=(0, 10), pady=6)

        # ===== Section: Swing =====
        sw_sec = ctk.CTkFrame(self.pt_settings_frame, fg_color="transparent")
        sw_sec.pack(fill="x", padx=12, pady=(8, 6))
        self._pt_section_label(sw_sec, "Качание")

        # Pan swing
        psw_row = ctk.CTkFrame(sw_sec, fg_color="#262626", corner_radius=8)
        psw_row.pack(fill="x", pady=2)
        ctk.CTkLabel(psw_row, text="Pan:", font=lbl_style, text_color="#9CA3AF").pack(side="left", padx=(10, 4), pady=6)
        self.pt_swing_pan_p1 = self._pt_entry(psw_row, width=60, placeholder="Поз1")
        self.pt_swing_pan_p1.pack(side="left", padx=(0, 4), pady=6)
        self.pt_swing_pan_p2 = self._pt_entry(psw_row, width=60, placeholder="Поз2")
        self.pt_swing_pan_p2.pack(side="left", padx=(0, 4), pady=6)
        self.pt_swing_pan_spd = self._pt_entry(psw_row, width=50, placeholder="Скор")
        self.pt_swing_pan_spd.pack(side="left", padx=(0, 4), pady=6)
        self._pt_action_btn(psw_row, "Качать", self._pt_swing_pan, width=65).pack(side="left", padx=(0, 8), pady=6)

        # Tilt swing
        tsw_row = ctk.CTkFrame(sw_sec, fg_color="#262626", corner_radius=8)
        tsw_row.pack(fill="x", pady=2)
        ctk.CTkLabel(tsw_row, text="Tilt:", font=lbl_style, text_color="#9CA3AF").pack(side="left", padx=(10, 4), pady=6)
        self.pt_swing_tilt_p1 = self._pt_entry(tsw_row, width=60, placeholder="Поз1")
        self.pt_swing_tilt_p1.pack(side="left", padx=(0, 4), pady=6)
        self.pt_swing_tilt_p2 = self._pt_entry(tsw_row, width=60, placeholder="Поз2")
        self.pt_swing_tilt_p2.pack(side="left", padx=(0, 4), pady=6)
        self.pt_swing_tilt_spd = self._pt_entry(tsw_row, width=50, placeholder="Скор")
        self.pt_swing_tilt_spd.pack(side="left", padx=(0, 4), pady=6)
        self._pt_action_btn(tsw_row, "Качать", self._pt_swing_tilt, width=65).pack(side="left", padx=(0, 8), pady=6)

        # ===== Section: Speed limits =====
        spd_sec = ctk.CTkFrame(self.pt_settings_frame, fg_color="transparent")
        spd_sec.pack(fill="x", padx=12, pady=(8, 6))
        self._pt_section_label(spd_sec, "Ограничения скорости  ⚠ EEPROM")

        # Pan speed limits
        pspd_row = ctk.CTkFrame(spd_sec, fg_color="#262626", corner_radius=8)
        pspd_row.pack(fill="x", pady=2)
        ctk.CTkLabel(pspd_row, text="Pan:", font=lbl_style, text_color="#9CA3AF").pack(side="left", padx=(10, 4), pady=6)
        self.pt_spd_pan_min = self._pt_entry(pspd_row, width=50, placeholder="Min")
        self.pt_spd_pan_min.pack(side="left", padx=(0, 3), pady=6)
        self.pt_spd_pan_max = self._pt_entry(pspd_row, width=50, placeholder="Max")
        self.pt_spd_pan_max.pack(side="left", padx=(0, 3), pady=6)
        self.pt_spd_pan_acc = self._pt_entry(pspd_row, width=50, placeholder="Acc")
        self.pt_spd_pan_acc.pack(side="left", padx=(0, 3), pady=6)
        self._pt_action_btn(pspd_row, "◀", self._pt_read_speed_pan, width=32, color="#555", hover="#666").pack(side="left", padx=(0, 2), pady=6)
        self._pt_action_btn(pspd_row, "✓", self._pt_set_speed_pan, width=32).pack(side="left", padx=(0, 8), pady=6)

        # Tilt speed limits
        tspd_row = ctk.CTkFrame(spd_sec, fg_color="#262626", corner_radius=8)
        tspd_row.pack(fill="x", pady=2)
        ctk.CTkLabel(tspd_row, text="Tilt:", font=lbl_style, text_color="#9CA3AF").pack(side="left", padx=(10, 4), pady=6)
        self.pt_spd_tilt_min = self._pt_entry(tspd_row, width=50, placeholder="Min")
        self.pt_spd_tilt_min.pack(side="left", padx=(0, 3), pady=6)
        self.pt_spd_tilt_max = self._pt_entry(tspd_row, width=50, placeholder="Max")
        self.pt_spd_tilt_max.pack(side="left", padx=(0, 3), pady=6)
        self.pt_spd_tilt_acc = self._pt_entry(tspd_row, width=50, placeholder="Acc")
        self.pt_spd_tilt_acc.pack(side="left", padx=(0, 3), pady=6)
        self._pt_action_btn(tspd_row, "◀", self._pt_read_speed_tilt, width=32, color="#555", hover="#666").pack(side="left", padx=(0, 2), pady=6)
        self._pt_action_btn(tspd_row, "✓", self._pt_set_speed_tilt, width=32).pack(side="left", padx=(0, 8), pady=6)

        # ===== Section: Angle limits =====
        ang_sec = ctk.CTkFrame(self.pt_settings_frame, fg_color="transparent")
        ang_sec.pack(fill="x", padx=12, pady=(8, 6))
        self._pt_section_label(ang_sec, "Ограничения углов  ⚠ EEPROM")

        # Pan angle limits
        pang_row = ctk.CTkFrame(ang_sec, fg_color="#262626", corner_radius=8)
        pang_row.pack(fill="x", pady=2)
        ctk.CTkLabel(pang_row, text="Pan:", font=lbl_style, text_color="#9CA3AF").pack(side="left", padx=(10, 4), pady=6)
        self.pt_ang_pan_en = ctk.CTkCheckBox(pang_row, text="", width=30,
                                               font=lbl_style, fg_color="#3B82F6",
                                               hover_color="#2563EB")
        self.pt_ang_pan_en.pack(side="left", padx=(0, 4), pady=6)
        ctk.CTkLabel(pang_row, text="L:", font=lbl_style, text_color="#9CA3AF").pack(side="left", padx=(0, 2), pady=6)
        self.pt_ang_pan_left = self._pt_entry(pang_row, width=60, placeholder="180.50")
        self.pt_ang_pan_left.pack(side="left", padx=(0, 3), pady=6)
        ctk.CTkLabel(pang_row, text="R:", font=lbl_style, text_color="#9CA3AF").pack(side="left", padx=(0, 2), pady=6)
        self.pt_ang_pan_right = self._pt_entry(pang_row, width=60, placeholder="90.00")
        self.pt_ang_pan_right.pack(side="left", padx=(0, 3), pady=6)
        self._pt_action_btn(pang_row, "◀", self._pt_read_angle_pan, width=32, color="#555", hover="#666").pack(side="left", padx=(0, 2), pady=6)
        self._pt_action_btn(pang_row, "✓", self._pt_set_angle_pan, width=32).pack(side="left", padx=(0, 8), pady=6)

        # Tilt angle limits
        tang_row = ctk.CTkFrame(ang_sec, fg_color="#262626", corner_radius=8)
        tang_row.pack(fill="x", pady=2)
        ctk.CTkLabel(tang_row, text="Tilt:", font=lbl_style, text_color="#9CA3AF").pack(side="left", padx=(10, 4), pady=6)
        ctk.CTkLabel(tang_row, text="L:", font=lbl_style, text_color="#9CA3AF").pack(side="left", padx=(0, 2), pady=6)
        self.pt_ang_tilt_left = self._pt_entry(tang_row, width=60, placeholder="270.00")
        self.pt_ang_tilt_left.pack(side="left", padx=(0, 3), pady=6)
        ctk.CTkLabel(tang_row, text="R:", font=lbl_style, text_color="#9CA3AF").pack(side="left", padx=(0, 2), pady=6)
        self.pt_ang_tilt_right = self._pt_entry(tang_row, width=60, placeholder="90.00")
        self.pt_ang_tilt_right.pack(side="left", padx=(0, 3), pady=6)
        self._pt_action_btn(tang_row, "◀", self._pt_read_angle_tilt, width=32, color="#555", hover="#666").pack(side="left", padx=(0, 2), pady=6)
        self._pt_action_btn(tang_row, "✓", self._pt_set_angle_tilt, width=32).pack(side="left", padx=(0, 8), pady=6)

        # ===== Section: Device status =====
        stat_sec = ctk.CTkFrame(self.pt_settings_frame, fg_color="transparent")
        stat_sec.pack(fill="x", padx=12, pady=(8, 6))
        self._pt_section_label(stat_sec, "Состояние устройства")

        stat_card = ctk.CTkFrame(stat_sec, fg_color="#262626", corner_radius=8)
        stat_card.pack(fill="x", pady=2)

        self.pt_temp_label = ctk.CTkLabel(stat_card, text="Температура: --",
                                           font=lbl_style, text_color="#E5E7EB")
        self.pt_temp_label.pack(anchor="w", padx=10, pady=(8, 2))

        self.pt_volt_label = ctk.CTkLabel(stat_card, text="Напряжение: --",
                                           font=lbl_style, text_color="#E5E7EB")
        self.pt_volt_label.pack(anchor="w", padx=10, pady=(0, 2))

        self.pt_pstate_label = ctk.CTkLabel(stat_card, text="Pan: --",
                                             font=lbl_style, text_color="#E5E7EB")
        self.pt_pstate_label.pack(anchor="w", padx=10, pady=(0, 2))

        self.pt_tstate_label = ctk.CTkLabel(stat_card, text="Tilt: --",
                                             font=lbl_style, text_color="#E5E7EB")
        self.pt_tstate_label.pack(anchor="w", padx=10, pady=(0, 8))

        stat_btns = ctk.CTkFrame(stat_sec, fg_color="transparent")
        stat_btns.pack(fill="x", pady=(4, 0))
        self._pt_action_btn(stat_btns, "Прочитать всё", self._pt_read_status, width=120).pack(side="left", padx=(0, 6))
        self._pt_action_btn(stat_btns, "Самодиаг. Pan", self._pt_selfdiag_pan, width=110, color="#E67E22", hover="#D35400").pack(side="left", padx=(0, 6))
        self._pt_action_btn(stat_btns, "Самодиаг. Tilt", self._pt_selfdiag_tilt, width=110, color="#E67E22", hover="#D35400").pack(side="left")

        # ===== Section: Presets =====
        pre_sec = ctk.CTkFrame(self.pt_settings_frame, fg_color="transparent")
        pre_sec.pack(fill="x", padx=12, pady=(8, 12))
        self._pt_section_label(pre_sec, "Пресеты Pelco-D")

        pre_card = ctk.CTkFrame(pre_sec, fg_color="#262626", corner_radius=8)
        pre_card.pack(fill="x", pady=2)

        pre_inner = ctk.CTkFrame(pre_card, fg_color="transparent")
        pre_inner.pack(fill="x", padx=10, pady=8)

        ctk.CTkLabel(pre_inner, text="ID:", font=lbl_style, text_color="#9CA3AF").pack(side="left", padx=(0, 4))
        self.pt_preset_id = self._pt_entry(pre_inner, width=45, placeholder="1-64")
        self.pt_preset_id.pack(side="left", padx=(0, 8))

        self._pt_action_btn(pre_inner, "Сохранить", self._pt_save_preset, width=80).pack(side="left", padx=(0, 4))
        self._pt_action_btn(pre_inner, "Перейти", self._pt_goto_preset, width=70).pack(side="left", padx=(0, 4))
        self._pt_action_btn(pre_inner, "Удалить", self._pt_delete_preset, width=65, color="#EF4444", hover="#DC2626").pack(side="left")

        # ===== Section 7: Информация об устройстве =====
        info_sec = ctk.CTkFrame(self.pt_settings_frame, fg_color="transparent")
        info_sec.pack(fill="x", padx=12, pady=(8, 6))
        self._pt_section_label(info_sec, "Информация об устройстве")

        info_card = ctk.CTkFrame(info_sec, fg_color="#262626", corner_radius=8)
        info_card.pack(fill="x", pady=2)

        self.pt_firmware_label = ctk.CTkLabel(info_card, text="Прошивка: --",
                                               font=lbl_style, text_color="#E5E7EB")
        self.pt_firmware_label.pack(anchor="w", padx=10, pady=(8, 2))

        self.pt_fw_version_label = ctk.CTkLabel(info_card, text="Версия: --",
                                                 font=lbl_style, text_color="#E5E7EB")
        self.pt_fw_version_label.pack(anchor="w", padx=10, pady=(0, 2))

        self.pt_power_label = ctk.CTkLabel(info_card, text="Ток/Мощность: --",
                                           font=lbl_style, text_color="#E5E7EB")
        self.pt_power_label.pack(anchor="w", padx=10, pady=(0, 8))

        info_btns = ctk.CTkFrame(info_sec, fg_color="transparent")
        info_btns.pack(fill="x", pady=(4, 0))
        self._pt_action_btn(info_btns, "Прочитать всё", self._pt_read_device_info, width=120).pack(side="left")

        # ===== Section 8: Режим управления (EEPROM!) =====
        ctrl_sec = ctk.CTkFrame(self.pt_settings_frame, fg_color="transparent")
        ctrl_sec.pack(fill="x", padx=12, pady=(8, 6))
        self._pt_section_label(ctrl_sec, "Режим управления  ⚠ EEPROM")

        # Pan control mode
        pan_ctrl_row = ctk.CTkFrame(ctrl_sec, fg_color="#262626", corner_radius=8)
        pan_ctrl_row.pack(fill="x", pady=2)
        ctk.CTkLabel(pan_ctrl_row, text="Pan:", font=lbl_style, text_color="#9CA3AF").pack(side="left", padx=(10, 6), pady=6)
        self.pt_ctrl_pan_mode = ctk.CTkSegmentedButton(
            pan_ctrl_row, values=["Стандарт", "Синхрон"],
            font=lbl_style, height=28, corner_radius=6,
            fg_color="#1A1A1A", selected_color="#3B82F6",
            selected_hover_color="#2563EB")
        self.pt_ctrl_pan_mode.set("Стандарт")
        self.pt_ctrl_pan_mode.pack(side="left", padx=(0, 6), pady=6)
        self.pt_ctrl_pan_prec = ctk.CTkCheckBox(
            pan_ctrl_row, text="Повыш. дискр.", width=100,
            font=lbl_style, fg_color="#3B82F6", hover_color="#2563EB")
        self.pt_ctrl_pan_prec.pack(side="left", padx=(0, 4), pady=6)
        self._pt_action_btn(pan_ctrl_row, "◀", self._pt_read_ctrl_pan, width=32, color="#555", hover="#666").pack(side="left", padx=(0, 2), pady=6)
        self._pt_action_btn(pan_ctrl_row, "✓", self._pt_set_ctrl_pan, width=32).pack(side="left", padx=(0, 8), pady=6)

        # Tilt control mode
        tilt_ctrl_row = ctk.CTkFrame(ctrl_sec, fg_color="#262626", corner_radius=8)
        tilt_ctrl_row.pack(fill="x", pady=2)
        ctk.CTkLabel(tilt_ctrl_row, text="Tilt:", font=lbl_style, text_color="#9CA3AF").pack(side="left", padx=(10, 6), pady=6)
        self.pt_ctrl_tilt_mode = ctk.CTkSegmentedButton(
            tilt_ctrl_row, values=["Стандарт", "Синхрон"],
            font=lbl_style, height=28, corner_radius=6,
            fg_color="#1A1A1A", selected_color="#3B82F6",
            selected_hover_color="#2563EB")
        self.pt_ctrl_tilt_mode.set("Стандарт")
        self.pt_ctrl_tilt_mode.pack(side="left", padx=(0, 6), pady=6)
        self.pt_ctrl_tilt_prec = ctk.CTkCheckBox(
            tilt_ctrl_row, text="Повыш. дискр.", width=100,
            font=lbl_style, fg_color="#3B82F6", hover_color="#2563EB")
        self.pt_ctrl_tilt_prec.pack(side="left", padx=(0, 4), pady=6)
        self._pt_action_btn(tilt_ctrl_row, "◀", self._pt_read_ctrl_tilt, width=32, color="#555", hover="#666").pack(side="left", padx=(0, 2), pady=6)
        self._pt_action_btn(tilt_ctrl_row, "✓", self._pt_set_ctrl_tilt, width=32).pack(side="left", padx=(0, 8), pady=6)

        ctk.CTkLabel(ctrl_sec, text="⚠ Запись в EEPROM!",
                     font=ctk.CTkFont(size=10), text_color="#EF4444").pack(anchor="w", pady=(4, 0))

        # ===== Section 9: Настройки самодиагностики (EEPROM!) =====
        sdiag_sec = ctk.CTkFrame(self.pt_settings_frame, fg_color="transparent")
        sdiag_sec.pack(fill="x", padx=12, pady=(8, 6))
        self._pt_section_label(sdiag_sec, "Настройки самодиагностики  ⚠ EEPROM")

        # Pan selfdiag
        psd_row = ctk.CTkFrame(sdiag_sec, fg_color="#262626", corner_radius=8)
        psd_row.pack(fill="x", pady=2)
        ctk.CTkLabel(psd_row, text="Pan:", font=lbl_style, text_color="#9CA3AF").pack(side="left", padx=(10, 4), pady=6)
        self.pt_sdiag_pan_auto = ctk.CTkCheckBox(
            psd_row, text="Авто", width=60,
            font=lbl_style, fg_color="#3B82F6", hover_color="#2563EB")
        self.pt_sdiag_pan_auto.pack(side="left", padx=(0, 4), pady=6)
        ctk.CTkLabel(psd_row, text="Скор:", font=lbl_style, text_color="#9CA3AF").pack(side="left", padx=(0, 2), pady=6)
        self.pt_sdiag_pan_speed = self._pt_entry(psd_row, width=55, placeholder="1.0")
        self.pt_sdiag_pan_speed.pack(side="left", padx=(0, 4), pady=6)
        self._pt_action_btn(psd_row, "◀", self._pt_read_sdiag_pan, width=32, color="#555", hover="#666").pack(side="left", padx=(0, 2), pady=6)
        self._pt_action_btn(psd_row, "✓", self._pt_set_sdiag_pan, width=32).pack(side="left", padx=(0, 8), pady=6)

        # Tilt selfdiag
        tsd_row = ctk.CTkFrame(sdiag_sec, fg_color="#262626", corner_radius=8)
        tsd_row.pack(fill="x", pady=2)
        ctk.CTkLabel(tsd_row, text="Tilt:", font=lbl_style, text_color="#9CA3AF").pack(side="left", padx=(10, 4), pady=6)
        self.pt_sdiag_tilt_auto = ctk.CTkCheckBox(
            tsd_row, text="Авто", width=60,
            font=lbl_style, fg_color="#3B82F6", hover_color="#2563EB")
        self.pt_sdiag_tilt_auto.pack(side="left", padx=(0, 4), pady=6)
        ctk.CTkLabel(tsd_row, text="Скор:", font=lbl_style, text_color="#9CA3AF").pack(side="left", padx=(0, 2), pady=6)
        self.pt_sdiag_tilt_speed = self._pt_entry(tsd_row, width=55, placeholder="1.0")
        self.pt_sdiag_tilt_speed.pack(side="left", padx=(0, 4), pady=6)
        self._pt_action_btn(tsd_row, "◀", self._pt_read_sdiag_tilt, width=32, color="#555", hover="#666").pack(side="left", padx=(0, 2), pady=6)
        self._pt_action_btn(tsd_row, "✓", self._pt_set_sdiag_tilt, width=32).pack(side="left", padx=(0, 8), pady=6)

        ctk.CTkLabel(sdiag_sec, text="⚠ Запись в EEPROM!",
                     font=ctk.CTkFont(size=10), text_color="#EF4444").pack(anchor="w", pady=(4, 0))

        # ===== Section 10: Настройки Pelco-D (EEPROM!) =====
        pd_sec = ctk.CTkFrame(self.pt_settings_frame, fg_color="transparent")
        pd_sec.pack(fill="x", padx=12, pady=(8, 6))
        self._pt_section_label(pd_sec, "Настройки Pelco-D  ⚠ EEPROM")

        pd_card = ctk.CTkFrame(pd_sec, fg_color="#262626", corner_radius=8)
        pd_card.pack(fill="x", pady=2)

        pd_inner = ctk.CTkFrame(pd_card, fg_color="transparent")
        pd_inner.pack(fill="x", padx=10, pady=8)

        ctk.CTkLabel(pd_inner, text="Порт:", font=lbl_style, text_color="#9CA3AF").pack(side="left", padx=(0, 3))
        self.pt_pd_port = self._pt_entry(pd_inner, width=60, placeholder="9761")
        self.pt_pd_port.pack(side="left", padx=(0, 6))
        ctk.CTkLabel(pd_inner, text="Адр:", font=lbl_style, text_color="#9CA3AF").pack(side="left", padx=(0, 3))
        self.pt_pd_addr = self._pt_entry(pd_inner, width=45, placeholder="1")
        self.pt_pd_addr.pack(side="left", padx=(0, 6))
        self.pt_pd_tilt_inv = ctk.CTkCheckBox(
            pd_inner, text="Инв. Tilt", width=80,
            font=lbl_style, fg_color="#3B82F6", hover_color="#2563EB")
        self.pt_pd_tilt_inv.pack(side="left", padx=(0, 4))

        pd_btns = ctk.CTkFrame(pd_sec, fg_color="transparent")
        pd_btns.pack(fill="x", pady=(4, 0))
        self._pt_action_btn(pd_btns, "◀ Прочитать", self._pt_read_pelcod, width=100, color="#555", hover="#666").pack(side="left", padx=(0, 4))
        self._pt_action_btn(pd_btns, "✓ Применить", self._pt_set_pelcod, width=100).pack(side="left")

        ctk.CTkLabel(pd_sec, text="⚠ Запись в EEPROM!",
                     font=ctk.CTkFont(size=10), text_color="#EF4444").pack(anchor="w", pady=(4, 0))

        # ===== Section 11: Диагностика =====
        diag_sec = ctk.CTkFrame(self.pt_settings_frame, fg_color="transparent")
        diag_sec.pack(fill="x", padx=12, pady=(8, 6))
        self._pt_section_label(diag_sec, "Диагностика")

        diag_card = ctk.CTkFrame(diag_sec, fg_color="#262626", corner_radius=8)
        diag_card.pack(fill="x", pady=2)

        self.pt_pan_busy_label = ctk.CTkLabel(diag_card, text="Pan занятость: --",
                                               font=lbl_style, text_color="#E5E7EB")
        self.pt_pan_busy_label.pack(anchor="w", padx=10, pady=(8, 2))

        self.pt_tilt_busy_label = ctk.CTkLabel(diag_card, text="Tilt занятость: --",
                                                font=lbl_style, text_color="#E5E7EB")
        self.pt_tilt_busy_label.pack(anchor="w", padx=10, pady=(0, 2))

        self.pt_pan_err_label = ctk.CTkLabel(diag_card, text="Pan ошибки: --",
                                              font=lbl_style, text_color="#E5E7EB")
        self.pt_pan_err_label.pack(anchor="w", padx=10, pady=(0, 2))

        self.pt_tilt_err_label = ctk.CTkLabel(diag_card, text="Tilt ошибки: --",
                                               font=lbl_style, text_color="#E5E7EB")
        self.pt_tilt_err_label.pack(anchor="w", padx=10, pady=(0, 8))

        diag_btns = ctk.CTkFrame(diag_sec, fg_color="transparent")
        diag_btns.pack(fill="x", pady=(4, 0))
        self._pt_action_btn(diag_btns, "Прочитать всё", self._pt_read_diagnostics, width=120).pack(side="left")

        # ===== Section 12: Управление устройством =====
        mgmt_sec = ctk.CTkFrame(self.pt_settings_frame, fg_color="transparent")
        mgmt_sec.pack(fill="x", padx=12, pady=(8, 12))
        self._pt_section_label(mgmt_sec, "Управление устройством")

        reset_row = ctk.CTkFrame(mgmt_sec, fg_color="#262626", corner_radius=8)
        reset_row.pack(fill="x", pady=2)

        reset_inner = ctk.CTkFrame(reset_row, fg_color="transparent")
        reset_inner.pack(fill="x", padx=10, pady=8)

        self._pt_action_btn(reset_inner, "Сброс Pan", self._pt_reset_pan, width=80, color="#E67E22", hover="#D35400").pack(side="left", padx=(0, 4))
        self._pt_action_btn(reset_inner, "Сброс Tilt", self._pt_reset_tilt, width=80, color="#E67E22", hover="#D35400").pack(side="left", padx=(0, 4))
        self._pt_action_btn(reset_inner, "Сброс Pelco-D", self._pt_reset_pelcod, width=90, color="#E67E22", hover="#D35400").pack(side="left", padx=(0, 4))
        self._pt_action_btn(reset_inner, "Сброс всех", self._pt_reset_all, width=80, color="#EF4444", hover="#DC2626").pack(side="left")

        reboot_card = ctk.CTkFrame(mgmt_sec, fg_color="#262626", corner_radius=8)
        reboot_card.pack(fill="x", pady=(6, 2))
        self._pt_action_btn(reboot_card, "⏻  Перезагрузка устройства", self._pt_reboot,
                            width=200, color="#DC2626", hover="#B91C1C").pack(padx=10, pady=10)

    # --------------------------------------------------------
    # Pan-Tilt настройки: обработчики новых секций
    # --------------------------------------------------------
    def _pt_read_device_info(self):
        def _run():
            fw = self.pan_tilt.get_firmware_type()
            ver = self.pan_tilt.get_firmware_version()
            pwr = self.pan_tilt.get_power_info()
            def _update():
                if fw:
                    # $IT# → type = "T"
                    raw = fw.strip("$#")
                    if raw.startswith("I"):
                        raw = raw[1:]  # remove command letter
                    self.pt_firmware_label.configure(text=f"Прошивка: {raw}")
                else:
                    self.pt_firmware_label.configure(text="Прошивка: нет ответа")
                if ver:
                    # $V0074# → version hex after 'V'
                    raw = ver.strip("$#")
                    if raw.startswith("V"):
                        raw = raw[1:]
                    try:
                        v = int(raw, 16) / 100.0
                        self.pt_fw_version_label.configure(text=f"Версия: {v:.2f}")
                    except ValueError:
                        self.pt_fw_version_label.configure(text=f"Версия: {raw}")
                else:
                    self.pt_fw_version_label.configure(text="Версия: нет ответа")
                if pwr:
                    # $D,current,power#
                    parts = pwr.strip("$#").split(",")
                    if len(parts) >= 3:
                        self.pt_power_label.configure(text=f"Ток: {parts[1]}A  Мощность: {parts[2]}W")
                    else:
                        self.pt_power_label.configure(text=f"Питание: {pwr}")
                else:
                    self.pt_power_label.configure(text="Ток/Мощность: нет ответа")
            self.after(0, _update)
        threading.Thread(target=_run, daemon=True).start()

    def _pt_read_ctrl_pan(self):
        def _run():
            r = self.pan_tilt.get_control_mode_pan()
            if r:
                parts = r.strip("$#").split(",")
                if len(parts) >= 3:
                    mode = int(parts[1])
                    prec = int(parts[2])
                    def _update():
                        self.pt_ctrl_pan_mode.set("Синхрон" if mode == 1 else "Стандарт")
                        if prec:
                            self.pt_ctrl_pan_prec.select()
                        else:
                            self.pt_ctrl_pan_prec.deselect()
                    self.after(0, _update)
        threading.Thread(target=_run, daemon=True).start()

    def _pt_set_ctrl_pan(self):
        def _run():
            mode = 1 if self.pt_ctrl_pan_mode.get() == "Синхрон" else 0
            prec = 1 if self.pt_ctrl_pan_prec.get() else 0
            self.pan_tilt.set_control_mode_pan(mode, prec)
        threading.Thread(target=_run, daemon=True).start()

    def _pt_read_ctrl_tilt(self):
        def _run():
            r = self.pan_tilt.get_control_mode_tilt()
            if r:
                parts = r.strip("$#").split(",")
                if len(parts) >= 3:
                    mode = int(parts[1])
                    prec = int(parts[2])
                    def _update():
                        self.pt_ctrl_tilt_mode.set("Синхрон" if mode == 1 else "Стандарт")
                        if prec:
                            self.pt_ctrl_tilt_prec.select()
                        else:
                            self.pt_ctrl_tilt_prec.deselect()
                    self.after(0, _update)
        threading.Thread(target=_run, daemon=True).start()

    def _pt_set_ctrl_tilt(self):
        def _run():
            mode = 1 if self.pt_ctrl_tilt_mode.get() == "Синхрон" else 0
            prec = 1 if self.pt_ctrl_tilt_prec.get() else 0
            self.pan_tilt.set_control_mode_tilt(mode, prec)
        threading.Thread(target=_run, daemon=True).start()

    def _pt_read_sdiag_pan(self):
        def _run():
            r = self.pan_tilt.get_selfdiag_settings_pan()
            if r:
                parts = r.strip("$#").split(",")
                if len(parts) >= 3:
                    auto = int(parts[1])
                    speed = parts[2]
                    def _update():
                        if auto:
                            self.pt_sdiag_pan_auto.select()
                        else:
                            self.pt_sdiag_pan_auto.deselect()
                        self.pt_sdiag_pan_speed.delete(0, "end")
                        self.pt_sdiag_pan_speed.insert(0, speed)
                    self.after(0, _update)
        threading.Thread(target=_run, daemon=True).start()

    def _pt_set_sdiag_pan(self):
        def _run():
            try:
                auto = 1 if self.pt_sdiag_pan_auto.get() else 0
                speed = float(self.pt_sdiag_pan_speed.get())
                self.pan_tilt.set_selfdiag_settings_pan(auto, speed)
            except ValueError:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _pt_read_sdiag_tilt(self):
        def _run():
            r = self.pan_tilt.get_selfdiag_settings_tilt()
            if r:
                parts = r.strip("$#").split(",")
                if len(parts) >= 3:
                    auto = int(parts[1])
                    speed = parts[2]
                    def _update():
                        if auto:
                            self.pt_sdiag_tilt_auto.select()
                        else:
                            self.pt_sdiag_tilt_auto.deselect()
                        self.pt_sdiag_tilt_speed.delete(0, "end")
                        self.pt_sdiag_tilt_speed.insert(0, speed)
                    self.after(0, _update)
        threading.Thread(target=_run, daemon=True).start()

    def _pt_set_sdiag_tilt(self):
        def _run():
            try:
                auto = 1 if self.pt_sdiag_tilt_auto.get() else 0
                speed = float(self.pt_sdiag_tilt_speed.get())
                self.pan_tilt.set_selfdiag_settings_tilt(auto, speed)
            except ValueError:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _pt_read_pelcod(self):
        def _run():
            r = self.pan_tilt.get_pelcod_settings()
            if r:
                parts = r.strip("$#").split(",")
                if len(parts) >= 4:
                    port = parts[1]
                    addr = parts[2]
                    tilt_inv = int(parts[3])
                    def _update():
                        self.pt_pd_port.delete(0, "end")
                        self.pt_pd_port.insert(0, port)
                        self.pt_pd_addr.delete(0, "end")
                        self.pt_pd_addr.insert(0, addr)
                        if tilt_inv:
                            self.pt_pd_tilt_inv.select()
                        else:
                            self.pt_pd_tilt_inv.deselect()
                    self.after(0, _update)
        threading.Thread(target=_run, daemon=True).start()

    def _pt_set_pelcod(self):
        def _run():
            try:
                port = int(self.pt_pd_port.get())
                addr = int(self.pt_pd_addr.get())
                tilt_inv = 1 if self.pt_pd_tilt_inv.get() else 0
                self.pan_tilt.set_pelcod_settings(port, addr, tilt_inv)
            except ValueError:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _pt_read_diagnostics(self):
        busy_map = {"0": "Бездействие", "1": "Удержание", "2": "Остановка",
                    "3": "Разгон", "4": "Торможение", "5": "Движение"}
        def _run():
            pb = self.pan_tilt.get_pan_busy()
            tb = self.pan_tilt.get_tilt_busy()
            pe = self.pan_tilt.get_pan_errors()
            te = self.pan_tilt.get_tilt_errors()
            def _update():
                if pb:
                    val = pb.strip("$#").split(",")
                    code = val[1] if len(val) >= 2 else "?"
                    self.pt_pan_busy_label.configure(
                        text=f"Pan: {busy_map.get(code, code)}")
                else:
                    self.pt_pan_busy_label.configure(text="Pan занятость: нет ответа")
                if tb:
                    val = tb.strip("$#").split(",")
                    code = val[1] if len(val) >= 2 else "?"
                    self.pt_tilt_busy_label.configure(
                        text=f"Tilt: {busy_map.get(code, code)}")
                else:
                    self.pt_tilt_busy_label.configure(text="Tilt занятость: нет ответа")
                if pe:
                    val = pe.strip("$#").split(",")
                    err = val[1] if len(val) >= 2 else pe
                    self.pt_pan_err_label.configure(text=f"Pan ошибки: {err}")
                else:
                    self.pt_pan_err_label.configure(text="Pan ошибки: нет ответа")
                if te:
                    val = te.strip("$#").split(",")
                    err = val[1] if len(val) >= 2 else te
                    self.pt_tilt_err_label.configure(text=f"Tilt ошибки: {err}")
                else:
                    self.pt_tilt_err_label.configure(text="Tilt ошибки: нет ответа")
            self.after(0, _update)
        threading.Thread(target=_run, daemon=True).start()

    def _pt_reset_pan(self):
        from tkinter import messagebox
        if messagebox.askyesno("Сброс Pan", "Выполнить сброс модуля Pan?"):
            threading.Thread(target=self.pan_tilt.reset_module, args=(1,), daemon=True).start()

    def _pt_reset_tilt(self):
        from tkinter import messagebox
        if messagebox.askyesno("Сброс Tilt", "Выполнить сброс модуля Tilt?"):
            threading.Thread(target=self.pan_tilt.reset_module, args=(2,), daemon=True).start()

    def _pt_reset_pelcod(self):
        from tkinter import messagebox
        if messagebox.askyesno("Сброс Pelco-D", "Выполнить сброс модуля Pelco-D?"):
            threading.Thread(target=self.pan_tilt.reset_module, args=(3,), daemon=True).start()

    def _pt_reset_all(self):
        from tkinter import messagebox
        if messagebox.askyesno("Сброс всех", "Выполнить сброс всех модулей?"):
            def _run():
                self.pan_tilt.reset_module(1)
                self.pan_tilt.reset_module(2)
                self.pan_tilt.reset_module(3)
            threading.Thread(target=_run, daemon=True).start()

    def _pt_reboot(self):
        from tkinter import messagebox
        if messagebox.askyesno("Перезагрузка", "Выполнить перезагрузку устройства?"):
            threading.Thread(target=self.pan_tilt.reboot_device, daemon=True).start()

    # --------------------------------------------------------
    # Pan-Tilt settings handlers
    # --------------------------------------------------------
    def _pt_goto_pan(self):
        def _run():
            try:
                pos = float(self.pt_goto_pan_entry.get())
                spd = self._get_pan_speed()
                self.pan_tilt.go_to_pan(pos, spd)
            except ValueError:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _pt_goto_tilt(self):
        def _run():
            try:
                pos = float(self.pt_goto_tilt_entry.get())
                spd = self._get_tilt_speed()
                self.pan_tilt.go_to_tilt(pos, spd)
            except ValueError:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _pt_swing_pan(self):
        def _run():
            try:
                p1 = float(self.pt_swing_pan_p1.get())
                p2 = float(self.pt_swing_pan_p2.get())
                spd = float(self.pt_swing_pan_spd.get())
                self.pan_tilt.swing_pan(p1, p2, spd)
            except ValueError:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _pt_swing_tilt(self):
        def _run():
            try:
                p1 = float(self.pt_swing_tilt_p1.get())
                p2 = float(self.pt_swing_tilt_p2.get())
                spd = float(self.pt_swing_tilt_spd.get())
                self.pan_tilt.swing_tilt(p1, p2, spd)
            except ValueError:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _pt_read_speed_pan(self):
        def _run():
            r = self.pan_tilt.get_speed_limits_pan()
            if r:
                self.after(0, lambda: (
                    self.pt_spd_pan_min.delete(0, "end"),
                    self.pt_spd_pan_min.insert(0, str(r[0])),
                    self.pt_spd_pan_max.delete(0, "end"),
                    self.pt_spd_pan_max.insert(0, str(r[1])),
                    self.pt_spd_pan_acc.delete(0, "end"),
                    self.pt_spd_pan_acc.insert(0, str(r[2]))
                ))
        threading.Thread(target=_run, daemon=True).start()

    def _pt_read_speed_tilt(self):
        def _run():
            r = self.pan_tilt.get_speed_limits_tilt()
            if r:
                self.after(0, lambda: (
                    self.pt_spd_tilt_min.delete(0, "end"),
                    self.pt_spd_tilt_min.insert(0, str(r[0])),
                    self.pt_spd_tilt_max.delete(0, "end"),
                    self.pt_spd_tilt_max.insert(0, str(r[1])),
                    self.pt_spd_tilt_acc.delete(0, "end"),
                    self.pt_spd_tilt_acc.insert(0, str(r[2]))
                ))
        threading.Thread(target=_run, daemon=True).start()

    def _pt_set_speed_pan(self):
        def _run():
            try:
                mn = float(self.pt_spd_pan_min.get())
                mx = float(self.pt_spd_pan_max.get())
                ac = float(self.pt_spd_pan_acc.get())
                self.pan_tilt.set_speed_limits_pan(mn, mx, ac)
            except ValueError:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _pt_set_speed_tilt(self):
        def _run():
            try:
                mn = float(self.pt_spd_tilt_min.get())
                mx = float(self.pt_spd_tilt_max.get())
                ac = float(self.pt_spd_tilt_acc.get())
                self.pan_tilt.set_speed_limits_tilt(mn, mx, ac)
            except ValueError:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _pt_read_angle_pan(self):
        def _run():
            r = self.pan_tilt.get_angle_limits_pan()
            if r:
                self.after(0, lambda: (
                    self.pt_ang_pan_en.select() if r[0] else self.pt_ang_pan_en.deselect(),
                    self.pt_ang_pan_left.delete(0, "end"),
                    self.pt_ang_pan_left.insert(0, str(r[1])),
                    self.pt_ang_pan_right.delete(0, "end"),
                    self.pt_ang_pan_right.insert(0, str(r[2]))
                ))
        threading.Thread(target=_run, daemon=True).start()

    def _pt_read_angle_tilt(self):
        def _run():
            r = self.pan_tilt.get_angle_limits_tilt()
            if r:
                self.after(0, lambda: (
                    self.pt_ang_tilt_left.delete(0, "end"),
                    self.pt_ang_tilt_left.insert(0, str(r[1])),
                    self.pt_ang_tilt_right.delete(0, "end"),
                    self.pt_ang_tilt_right.insert(0, str(r[2]))
                ))
        threading.Thread(target=_run, daemon=True).start()

    def _pt_set_angle_pan(self):
        def _run():
            try:
                en = 1 if self.pt_ang_pan_en.get() else 0
                left = float(self.pt_ang_pan_left.get())
                right = float(self.pt_ang_pan_right.get())
                self.pan_tilt.set_angle_limits_pan(en, left, right)
            except ValueError:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _pt_set_angle_tilt(self):
        def _run():
            try:
                left = float(self.pt_ang_tilt_left.get())
                right = float(self.pt_ang_tilt_right.get())
                self.pan_tilt.set_angle_limits_tilt(1, left, right)
            except ValueError:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _pt_read_status(self):
        def _run():
            state_map = {"0": "Не готов", "1": "Самодиагностика", "2": "Готов", "3": "Обогрев"}
            temp = self.pan_tilt.get_temperature()
            volt = self.pan_tilt.get_voltage()
            ps = self.pan_tilt.get_pan_state()
            ts = self.pan_tilt.get_tilt_state()

            def _update():
                if temp:
                    # $0,temp1,,temp2# or $0,temp1,temp2,temp3,temp4#
                    parts = temp.strip("$#").split(",")
                    if len(parts) >= 4:
                        self.pt_temp_label.configure(
                            text=f"Температура: {parts[1]}°C / {parts[3]}°C")
                    elif len(parts) >= 2:
                        self.pt_temp_label.configure(
                            text=f"Температура: {parts[1]}°C")
                else:
                    self.pt_temp_label.configure(text="Температура: нет ответа")
                if volt:
                    parts = volt.strip("$#").split(",")
                    if len(parts) >= 2:
                        self.pt_volt_label.configure(
                            text=f"Напряжение: {parts[1]} В")
                else:
                    self.pt_volt_label.configure(text="Напряжение: нет ответа")
                if ps:
                    val = ps.strip("$#").split(",")
                    st = val[1] if len(val) >= 2 else "?"
                    self.pt_pstate_label.configure(
                        text=f"Pan: {state_map.get(st, st)}")
                else:
                    self.pt_pstate_label.configure(text="Pan: нет ответа")
                if ts:
                    val = ts.strip("$#").split(",")
                    st = val[1] if len(val) >= 2 else "?"
                    self.pt_tstate_label.configure(
                        text=f"Tilt: {state_map.get(st, st)}")
                else:
                    self.pt_tstate_label.configure(text="Tilt: нет ответа")
            self.after(0, _update)
        threading.Thread(target=_run, daemon=True).start()

    def _pt_selfdiag_pan(self):
        threading.Thread(target=self.pan_tilt.start_selfdiag_pan, daemon=True).start()

    def _pt_selfdiag_tilt(self):
        threading.Thread(target=self.pan_tilt.start_selfdiag_tilt, daemon=True).start()

    def _pt_save_preset(self):
        def _run():
            try:
                pid = int(self.pt_preset_id.get())
                self.pan_tilt.save_preset(pid)
            except ValueError:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _pt_goto_preset(self):
        def _run():
            try:
                pid = int(self.pt_preset_id.get())
                ps = self._get_pan_speed()
                ts = self._get_tilt_speed()
                self.pan_tilt.go_to_preset(pid, ps, ts)
            except ValueError:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _pt_delete_preset(self):
        def _run():
            try:
                pid = int(self.pt_preset_id.get())
                self.pan_tilt.delete_preset(pid)
            except ValueError:
                pass
        threading.Thread(target=_run, daemon=True).start()

    # --------------------------------------------------------
    # Таймер: запуск / остановка / логика
    # --------------------------------------------------------
    def _get_channel_duration(self, ch: int) -> int:
        """Получить длительность в секундах из виджетов канала."""
        w = self.timer_widgets.get(ch)
        if not w:
            return 0
        h = int(w["spin_h"].get())
        m = int(w["spin_m"].get())
        s = int(w["spin_s"].get())
        return h * 3600 + m * 60 + s

    def _get_channel_cycles(self, ch: int) -> int:
        w = self.timer_widgets.get(ch)
        if not w:
            return 1
        return int(w["spin_c"].get())

    def _start_channel_timer(self, ch: int):
        """Запустить таймер для одного канала (параллельный режим)."""
        self._stop_channel_timer(ch)
        duration = self._get_channel_duration(ch)
        cycles = self._get_channel_cycles(ch)
        if duration <= 0:
            return

        stop_event = threading.Event()
        self.timer_stop_flags[ch] = stop_event
        t = threading.Thread(
            target=self._timer_loop,
            args=(ch, duration, cycles, stop_event),
            daemon=True
        )
        self.timer_threads[ch] = t
        t.start()

    def _stop_channel_timer(self, ch: int):
        """Остановить таймер канала и выключить канал."""
        flag = self.timer_stop_flags.get(ch)
        if flag:
            flag.set()
        self.timer_stop_flags.pop(ch, None)
        self.timer_threads.pop(ch, None)
        # Выключить канал
        threading.Thread(target=self.relay.set_channel, args=(ch, False), daemon=True).start()
        self.after(0, lambda: self._update_relay_button(ch))
        # Сбросить отсчёт
        w = self.timer_widgets.get(ch)
        if w:
            lbl = self.relay_countdown_labels.get(ch)
            if lbl:
                lbl.configure(text="", text_color="#9CA3AF")

    def _timer_loop(self, ch: int, duration: int, cycles: int, stop_event: threading.Event):
        """Основной цикл таймера: ON → OFF = 1 цикл."""
        total_cycles = cycles
        is_infinite = (cycles >= 999)
        cycle_num = 0

        while not stop_event.is_set():
            cycle_num += 1
            if not is_infinite and cycle_num > total_cycles:
                break

            # ON фаза
            self.relay.set_channel(ch, True)
            self.after(0, lambda: self._update_relay_button(ch))
            if not self._countdown(ch, duration, stop_event, cycle_num, total_cycles, is_infinite, "ВКЛ"):
                break

            if stop_event.is_set():
                break

            # OFF фаза
            self.relay.set_channel(ch, False)
            self.after(0, lambda: self._update_relay_button(ch))
            if not self._countdown(ch, duration, stop_event, cycle_num, total_cycles, is_infinite, "ВЫКЛ"):
                break

        # Завершение — выключить канал
        if not stop_event.is_set():
            self.relay.set_channel(ch, False)
            self.after(0, lambda: self._update_relay_button(ch))
        self.timer_stop_flags.pop(ch, None)
        self.timer_threads.pop(ch, None)

    def _countdown(self, ch: int, seconds: int, stop_event: threading.Event,
                   cycle_num: int, total_cycles: int, is_infinite: bool,
                   phase: str) -> bool:
        """Обратный отсчёт. Возвращает False если прервано."""
        for remaining in range(seconds, 0, -1):
            if stop_event.is_set():
                return False
            # Обновить label в UI потоке
            h = remaining // 3600
            m = (remaining % 3600) // 60
            s = remaining % 60
            if is_infinite:
                cycle_text = f"Цикл: {cycle_num}/∞"
            else:
                cycle_text = f"Цикл: {cycle_num}/{total_cycles}"
            text = f"Осталось: {h:02d}:{m:02d}:{s:02d}  {cycle_text}  [{phase}]"
            color = COLOR_RELAY_ON if phase == "ВКЛ" else "#E67E22"
            self.after(0, lambda t=text, c=color, ch_=ch: self._update_countdown(ch_, t, c))
            # Ждём 1 секунду с проверкой stop_event каждые 100мс
            for _ in range(10):
                if stop_event.is_set():
                    return False
                time.sleep(0.1)
        return True

    def _update_countdown(self, ch: int, text: str, color: str):
        lbl = self.relay_countdown_labels.get(ch)
        if lbl:
            lbl.configure(text=text, text_color=color)

    # --------------------------------------------------------
    # Запуск/остановка всех таймеров
    # --------------------------------------------------------
    def _start_all_timers(self):
        """Запуск всех таймеров (с учётом режима)."""
        visible = self._get_visible_channels()
        if not visible:
            return

        if self.timer_mode.get() == "parallel":
            for ch in visible:
                self._start_channel_timer(ch)
        else:
            # Последовательный режим
            self._stop_all_timers()
            self.sequential_stop_flag.clear()
            t = threading.Thread(
                target=self._sequential_loop,
                args=(visible,),
                daemon=True
            )
            self.sequential_thread = t
            t.start()

    def _stop_all_timers(self):
        """Остановить все таймеры."""
        # Остановить последовательный режим
        self.sequential_stop_flag.set()
        # Остановить каждый активный таймер
        for ch in list(self.timer_stop_flags.keys()):
            self.timer_stop_flags[ch].set()
        self.timer_stop_flags.clear()
        self.timer_threads.clear()
        # Выключить все видимые каналы и сбросить отсчёт
        for ch in self._get_visible_channels():
            threading.Thread(target=self.relay.set_channel, args=(ch, False), daemon=True).start()
            self.after(0, lambda c=ch: self._update_relay_button(c))
            lbl = self.relay_countdown_labels.get(ch)
            if lbl:
                lbl.configure(text="", text_color="#9CA3AF")

    def _sequential_loop(self, channels: list[int]):
        """Последовательный цикл: каждый канал по очереди ON→OFF."""
        is_infinite = any(self._get_channel_cycles(ch) >= 999 for ch in channels)
        # Для бесконечного режима берём максимальное число циклов среди конечных, иначе большое число
        finite_cycles = [self._get_channel_cycles(ch) for ch in channels if self._get_channel_cycles(ch) < 999]
        max_cycles = max(finite_cycles) if finite_cycles else 999999

        cycle_num = 0
        while cycle_num < max_cycles:
            cycle_num += 1
            if self.sequential_stop_flag.is_set():
                break

            for ch in channels:
                if self.sequential_stop_flag.is_set():
                    break

                duration = self._get_channel_duration(ch)
                if duration <= 0:
                    continue

                # ON
                self.relay.set_channel(ch, True)
                self.after(0, lambda c=ch: self._update_relay_button(c))
                if not self._countdown(ch, duration, self.sequential_stop_flag,
                                       cycle_num, max_cycles, is_infinite, "ВКЛ"):
                    break

                if self.sequential_stop_flag.is_set():
                    break

                # OFF
                self.relay.set_channel(ch, False)
                self.after(0, lambda c=ch: self._update_relay_button(c))
                if not self._countdown(ch, duration, self.sequential_stop_flag,
                                       cycle_num, max_cycles, is_infinite, "ВЫКЛ"):
                    break

        # Завершение
        for ch in channels:
            self.relay.set_channel(ch, False)
            self.after(0, lambda c=ch: self._update_relay_button(c))

    # --------------------------------------------------------
    # Обработчики кнопок D-pad (непрерывное движение)
    # --------------------------------------------------------
    def _get_pan_speed(self) -> int:
        return int(self.pan_speed_slider.get())

    def _get_tilt_speed(self) -> int:
        speed = int(self.tilt_speed_slider.get())
        return -speed if self.tilt_invert_var.get() else speed

    def _pan_left_start(self):
        threading.Thread(target=self.pan_tilt.pan_left,
                         args=(self._get_pan_speed(),), daemon=True).start()

    def _pan_right_start(self):
        threading.Thread(target=self.pan_tilt.pan_right,
                         args=(self._get_pan_speed(),), daemon=True).start()

    def _pan_stop(self):
        threading.Thread(target=self.pan_tilt.stop_pan, daemon=True).start()

    def _tilt_up_start(self):
        # With inversion, up button should move down
        raw = int(self.tilt_speed_slider.get())
        if self.tilt_invert_var.get():
            threading.Thread(target=self.pan_tilt.tilt_down,
                             args=(raw,), daemon=True).start()
        else:
            threading.Thread(target=self.pan_tilt.tilt_up,
                             args=(raw,), daemon=True).start()

    def _tilt_down_start(self):
        # tilt_down() sends $v,-{speed}#, so with inversion we want
        # the opposite direction = tilt_up instead
        raw = int(self.tilt_speed_slider.get())
        if self.tilt_invert_var.get():
            threading.Thread(target=self.pan_tilt.tilt_up,
                             args=(raw,), daemon=True).start()
        else:
            threading.Thread(target=self.pan_tilt.tilt_down,
                             args=(raw,), daemon=True).start()

    def _tilt_stop(self):
        threading.Thread(target=self.pan_tilt.stop_tilt, daemon=True).start()

    def _stop_all(self):
        threading.Thread(target=self.pan_tilt.stop_all, daemon=True).start()

    def _diag_up_left_start(self):
        ps = self._get_pan_speed()
        raw = int(self.tilt_speed_slider.get())
        if self.tilt_invert_var.get():
            threading.Thread(target=lambda: (self.pan_tilt.pan_left(ps), self.pan_tilt.tilt_down(raw)), daemon=True).start()
        else:
            threading.Thread(target=lambda: (self.pan_tilt.pan_left(ps), self.pan_tilt.tilt_up(raw)), daemon=True).start()

    def _diag_up_right_start(self):
        ps = self._get_pan_speed()
        raw = int(self.tilt_speed_slider.get())
        if self.tilt_invert_var.get():
            threading.Thread(target=lambda: (self.pan_tilt.pan_right(ps), self.pan_tilt.tilt_down(raw)), daemon=True).start()
        else:
            threading.Thread(target=lambda: (self.pan_tilt.pan_right(ps), self.pan_tilt.tilt_up(raw)), daemon=True).start()

    def _diag_down_left_start(self):
        ps = self._get_pan_speed()
        raw = int(self.tilt_speed_slider.get())
        if self.tilt_invert_var.get():
            threading.Thread(target=lambda: (self.pan_tilt.pan_left(ps), self.pan_tilt.tilt_up(raw)), daemon=True).start()
        else:
            threading.Thread(target=lambda: (self.pan_tilt.pan_left(ps), self.pan_tilt.tilt_down(raw)), daemon=True).start()

    def _diag_down_right_start(self):
        ps = self._get_pan_speed()
        raw = int(self.tilt_speed_slider.get())
        if self.tilt_invert_var.get():
            threading.Thread(target=lambda: (self.pan_tilt.pan_right(ps), self.pan_tilt.tilt_up(raw)), daemon=True).start()
        else:
            threading.Thread(target=lambda: (self.pan_tilt.pan_right(ps), self.pan_tilt.tilt_down(raw)), daemon=True).start()

    # --------------------------------------------------------
    # Обработчики UI
    # --------------------------------------------------------
    def _on_pan_speed_change(self, value):
        speed = int(value)
        self.pan_speed_label.configure(text=f"Pan: {speed} °/сек")

    def _on_tilt_speed_change(self, value):
        speed = int(value)
        self.tilt_speed_label.configure(text=f"Tilt: {speed} °/сек")

    def _toggle_relay_channel(self, ch: int):
        threading.Thread(target=self._do_toggle_relay, args=(ch,), daemon=True).start()

    def _do_toggle_relay(self, ch: int):
        self.relay.toggle_channel(ch)
        self.after(0, lambda: self._update_relay_button(ch))

    def _update_relay_button(self, ch: int):
        if ch not in self.relay_buttons:
            return
        btn = self.relay_buttons[ch]
        state = self.relay.channel_states[ch]
        name = self.channel_custom_names.get(ch, f"Канал {ch}")
        indicator = "● ВКЛ" if state else "○ ВЫКЛ"
        color = COLOR_RELAY_ON if state else COLOR_RELAY_OFF
        hover = "#27AE60" if state else "#6B6B6B"
        btn.configure(text=f"  {name}    {indicator}",
                      fg_color=color, hover_color=hover)

    # --------------------------------------------------------
    # Фоновые потоки подключения и переподключения
    # --------------------------------------------------------
    def _start_connection_threads(self):
        threading.Thread(target=self._connect_pan_tilt_loop, daemon=True).start()
        threading.Thread(target=self._connect_relay_loop, daemon=True).start()

    def _connect_pan_tilt_loop(self):
        while True:
            if not self.pan_tilt.connected:
                success = self.pan_tilt.connect()
                self.after(0, self._update_pt_status)
                if not success:
                    time.sleep(3)
                    continue
            time.sleep(1)

    def _connect_relay_loop(self):
        while True:
            if not self.relay.connected:
                success = self.relay.connect()
                self.after(0, self._update_rl_status)
                if not success:
                    time.sleep(3)
                    continue
            time.sleep(1)

    # --------------------------------------------------------
    # Обновление статусов в UI
    # --------------------------------------------------------
    def _update_pt_status(self):
        if self.pan_tilt.connected:
            self.pt_status_label.configure(
                text=f"● ОПУ {self.pan_tilt.host}:{self.pan_tilt.port}",
                text_color=COLOR_CONNECTED
            )
        else:
            self.pt_status_label.configure(
                text="● ОПУ",
                text_color=COLOR_DISCONNECTED
            )

    def _update_rl_status(self):
        if self.relay.connected:
            self.rl_status_label.configure(
                text=f"● RelayX3 {self.relay.host}:{self.relay.port}",
                text_color=COLOR_CONNECTED
            )
        else:
            self.rl_status_label.configure(
                text="● RelayX3",
                text_color=COLOR_DISCONNECTED
            )

    # --------------------------------------------------------
    # Опрос позиций Pan/Tilt
    # --------------------------------------------------------
    def _poll_positions(self):
        if self.pan_tilt.connected:
            threading.Thread(target=self._read_positions, daemon=True).start()
        self.after(POLL_INTERVAL_MS, self._poll_positions)

    def _read_positions(self):
        pan = self.pan_tilt.get_pan_position()
        tilt = self.pan_tilt.get_tilt_position()
        self.after(0, lambda: self._set_positions(pan, tilt))

    def _set_positions(self, pan: float | None, tilt: float | None):
        if pan is not None:
            self.pan_pos_label.configure(text=f"Pan: {pan:.1f}°")
        else:
            self.pan_pos_label.configure(text="Pan: --.-°")
        if tilt is not None:
            self.tilt_pos_label.configure(text=f"Tilt: {tilt:.1f}°")
        else:
            self.tilt_pos_label.configure(text="Tilt: --.-°")

    # --------------------------------------------------------
    # Опрос статуса реле
    # --------------------------------------------------------
    def _poll_relay_status(self):
        if self.relay.connected:
            threading.Thread(target=self._read_relay_status, daemon=True).start()
        self.after(2000, self._poll_relay_status)

    def _read_relay_status(self):
        status = self.relay.read_status()
        if status is not None:
            self.relay.channel_states = status
            self.after(0, self._sync_relay_buttons)
            self._broadcast_relay_state()

    def _sync_relay_buttons(self):
        for ch in self.relay_buttons:
            self._update_relay_button(ch)

    # --------------------------------------------------------
    # Сохранение/загрузка настроек
    # --------------------------------------------------------
    def _load_settings(self):
        self._settings = {}
        try:
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    self._settings = json.load(f)
        except Exception:
            self._settings = {}

    def _save_settings(self):
        # Собрать настройки таймеров
        timer_settings = {}
        for ch, w in self.timer_widgets.items():
            try:
                timer_settings[str(ch)] = {
                    "hours": int(w["spin_h"].get() or 0),
                    "minutes": int(w["spin_m"].get() or 0),
                    "seconds": int(w["spin_s"].get() or 0),
                    "cycles": int(w["spin_c"].get() or 1),
                }
            except (ValueError, Exception):
                pass

        # Сохранить предыдущие таймеры если виджеты уже уничтожены
        if not timer_settings:
            timer_settings = self._settings.get("timer_settings", {})

        data = {
            "channel_names": {str(k): v for k, v in self.channel_custom_names.items()},
            "channels_visible": {str(k): v.get() for k, v in self.channel_checkboxes.items()},
            "pan_speed": int(self.pan_speed_slider.get()),
            "tilt_speed": int(self.tilt_speed_slider.get()),
            "tilt_invert": self.tilt_invert_var.get(),
            "timer_settings": timer_settings,
            "timer_mode": self.timer_mode.get(),
            "pt_host": self.pan_tilt.host,
            "pt_port": self.pan_tilt.port,
            "rl_host": self.relay.host,
            "rl_port": self.relay.port,
        }
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # --------------------------------------------------------
    # WebSocket сервер
    # --------------------------------------------------------
    def _run_ws_server(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._ws_loop = loop
        loop.run_until_complete(self._ws_main())

    async def _ws_main(self):
        async with websockets.asyncio.server.serve(
            self._ws_handler, "0.0.0.0", WS_PORT
        ):
            # Run until stop event is set
            while not self._ws_stop_event.is_set():
                await asyncio.sleep(0.5)

    def _get_visible_channels_info(self):
        """Return visible channel list and names for WebSocket messages (mobile).
        Uses the same channels_visible checkboxes as desktop."""
        try:
            visible = [ch for ch in RelayDevice.CHANNELS if self.channel_checkboxes.get(ch) and self.channel_checkboxes[ch].get()]
            names = {str(ch): self.channel_custom_names.get(ch, f"Канал {ch}") for ch in visible}
            return visible, names
        except Exception:
            return [], {}

    async def _ws_handler(self, websocket):
        self._ws_clients.add(websocket)
        try:
            # Send current relay state on connect
            visible, names = self._get_visible_channels_info()
            await websocket.send(json.dumps({
                "type": "relay_state",
                "states": {str(k): v for k, v in self.relay.channel_states.items()},
                "visible": visible,
                "names": names
            }))
            async for message in websocket:
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    continue
                cmd = data.get("cmd", "")
                if cmd == "relay_toggle":
                    ch = int(data.get("channel", 1))
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, self.relay.toggle_channel, ch)
                    # Sync desktop buttons
                    self.after(0, self._sync_relay_buttons)
                    # Send updated state back to all clients
                    visible, names = self._get_visible_channels_info()
                    state_msg = json.dumps({
                        "type": "relay_state",
                        "states": {str(k): v for k, v in self.relay.channel_states.items()},
                        "visible": visible,
                        "names": names
                    })
                    for ws in list(self._ws_clients):
                        try:
                            await ws.send(state_msg)
                        except Exception:
                            pass
                elif cmd == "pan_left":
                    speed = int(data.get("speed", 30))
                    self.pan_tilt.pan_left(speed)
                elif cmd == "pan_right":
                    speed = int(data.get("speed", 30))
                    self.pan_tilt.pan_right(speed)
                elif cmd == "tilt_up":
                    speed = int(data.get("speed", 30))
                    if self.tilt_invert_var.get():
                        self.pan_tilt.tilt_down(speed)
                    else:
                        self.pan_tilt.tilt_up(speed)
                elif cmd == "tilt_down":
                    speed = int(data.get("speed", 30))
                    if self.tilt_invert_var.get():
                        self.pan_tilt.tilt_up(speed)
                    else:
                        self.pan_tilt.tilt_down(speed)
                elif cmd.startswith("diag_"):
                    pan_speed = int(data.get("pan_speed", 30))
                    tilt_speed = int(data.get("tilt_speed", 20))
                    invert = self.tilt_invert_var.get()
                    if cmd == "diag_up_left":
                        self.pan_tilt.pan_left(pan_speed)
                        if invert:
                            self.pan_tilt.tilt_down(tilt_speed)
                        else:
                            self.pan_tilt.tilt_up(tilt_speed)
                    elif cmd == "diag_up_right":
                        self.pan_tilt.pan_right(pan_speed)
                        if invert:
                            self.pan_tilt.tilt_down(tilt_speed)
                        else:
                            self.pan_tilt.tilt_up(tilt_speed)
                    elif cmd == "diag_down_left":
                        self.pan_tilt.pan_left(pan_speed)
                        if invert:
                            self.pan_tilt.tilt_up(tilt_speed)
                        else:
                            self.pan_tilt.tilt_down(tilt_speed)
                    elif cmd == "diag_down_right":
                        self.pan_tilt.pan_right(pan_speed)
                        if invert:
                            self.pan_tilt.tilt_up(tilt_speed)
                        else:
                            self.pan_tilt.tilt_down(tilt_speed)
                elif cmd == "stop":
                    self.pan_tilt.stop_all()
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._ws_clients.discard(websocket)

    def _broadcast_relay_state(self):
        """Send relay state to all connected WS clients (thread-safe)."""
        if not self._ws_clients:
            return
        visible, names = self._get_visible_channels_info()
        state_msg = json.dumps({
            "type": "relay_state",
            "states": {str(k): v for k, v in self.relay.channel_states.items()},
            "visible": visible,
            "names": names
        })
        async def _send_all():
            for ws in list(self._ws_clients):
                try:
                    await ws.send(state_msg)
                except Exception:
                    pass
        if hasattr(self, '_ws_loop') and self._ws_loop.is_running():
            asyncio.run_coroutine_threadsafe(_send_all(), self._ws_loop)

    # --------------------------------------------------------
    # HTTP сервер
    # --------------------------------------------------------
    def _run_http_server(self):
        try:
            if not os.path.isdir(WEB_DIR):
                return

            class Handler(SimpleHTTPRequestHandler):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, directory=WEB_DIR, **kwargs)
                def log_message(self, format, *args):
                    pass  # suppress logs

            self._http_server = HTTPServer(("0.0.0.0", HTTP_PORT), Handler)
            self._http_server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._http_server.serve_forever()
        except Exception:
            pass

    # --------------------------------------------------------
    # QR код
    # --------------------------------------------------------
    def _get_local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            try:
                return socket.gethostbyname(socket.gethostname())
            except Exception:
                return "127.0.0.1"

    def _show_qr_popup(self):
        local_ip = self._get_local_ip()
        url = f"http://{local_ip}:{HTTP_PORT}"

        qr = qrcode.QRCode(box_size=10, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").resize((250, 250))

        popup = ctk.CTkToplevel(self)
        popup.title("QR — мобильное управление")
        popup.geometry("300x320")
        popup.resizable(False, False)
        popup.configure(fg_color="#FFFFFF")

        photo = ImageTk.PhotoImage(img)
        label = ctk.CTkLabel(popup, image=photo, text="")
        label.image = photo  # prevent GC
        label.pack(pady=(15, 5))

        url_label = ctk.CTkLabel(popup, text=url,
                                  font=ctk.CTkFont(size=12),
                                  text_color="#333333")
        url_label.pack(pady=(0, 10))

    # --------------------------------------------------------
    # Закрытие приложения
    # --------------------------------------------------------
    def _on_close(self):
        # Остановить таймеры без выключения реле
        self.sequential_stop_flag.set()
        for ch in list(self.timer_stop_flags.keys()):
            self.timer_stop_flags[ch].set()
        self._save_settings()
        self.pan_tilt.disconnect()
        self.relay.disconnect()
        # Остановить серверы в фоне чтобы не подвисать
        self._ws_stop_event.set()
        if self._http_server:
            threading.Thread(target=self._http_server.shutdown, daemon=True).start()
        self.destroy()
        # Принудительный выход чтобы не ждать daemon threads
        os._exit(0)


# ============================================================
# Точка входа
# ============================================================
if __name__ == "__main__":
    app = ControlApp()
    app.mainloop()
