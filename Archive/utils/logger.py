# ============================================================
# utils/logger.py — Coloured terminal logger using Rich
# ============================================================
import threading
from datetime import datetime
from rich.console import Console
from rich.theme import Theme

_lock = threading.Lock()

_theme = Theme({
    "discovery":   "bold bright_cyan",
    "region":      "bold bright_green",
    "booking":     "bold bright_yellow",
    "coordinator": "bold bright_magenta",
    "health":      "bold bright_red",
    "replication": "bold bright_blue",
    "gateway":     "bold cyan",
    "simulation":  "bold red",
    "api":         "dim green",
    "main":        "bold white",
    "success":     "bright_green",
    "warning":     "bright_yellow",
    "error":       "bright_red",
    "info":        "white",
    "debug":       "dim",
})

console = Console(theme=_theme, highlight=False)

_SERVICE_STYLE = {
    "DISCOVERY":   "discovery",
    "REGION":      "region",
    "BOOKING":     "booking",
    "COORDINATOR": "coordinator",
    "HEALTH":      "health",
    "REPLICATION": "replication",
    "GATEWAY":     "gateway",
    "SIMULATION":  "simulation",
    "API":         "api",
    "MAIN":        "main",
}

_LEVEL_STYLE = {
    "INFO":    "info",
    "SUCCESS": "success",
    "WARN":    "warning",
    "ERROR":   "error",
    "DEBUG":   "debug",
}

_region_label = "?"


def set_region(name: str):
    global _region_label
    _region_label = name


def log(service: str, message: str, level: str = "INFO"):
    svc = service.upper()
    svc_style = _SERVICE_STYLE.get(svc, "info")
    lvl_style = _LEVEL_STYLE.get(level.upper(), "info")
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    label = f"[{_region_label}]" if _region_label != "?" else ""

    with _lock:
        console.print(
            f"[dim]{ts}[/dim] [{svc_style}][{svc:12s}][/{svc_style}]"
            f"[dim]{label}[/dim] [{lvl_style}]{message}[/{lvl_style}]"
        )


def banner(title: str):
    with _lock:
        console.rule(f"[bold bright_white] {title} [/bold bright_white]")


def separator():
    with _lock:
        console.rule(style="dim")
