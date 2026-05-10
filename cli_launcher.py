import json
import locale
import os
import re
import secrets
import shutil
import subprocess
import sys

from contextlib import contextmanager
import time as time_mod

from datetime import datetime
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


try:
    import requests
except ImportError:
    requests = None

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich.prompt import Confirm, Prompt
    from rich.table import Table
except ImportError:

    def _strip_markup(value):
        if not isinstance(value, str):
            return str(value)
        return re.sub(r"\[[^\]]+\]", "", value)

    class Group:
        def __init__(self, *items) -> None:
            self.items = items

        def __str__(self) -> str:
            return "\n".join(_strip_markup(item) for item in self.items)

    class Panel:
        def __init__(self, renderable, **kwargs) -> None:
            self.renderable = renderable

        def __str__(self) -> str:
            return _strip_markup(self.renderable)

    class Table:
        def __init__(self, title=None, **kwargs) -> None:
            self.title = title
            self.rows = []

        def add_column(self, *args, **kwargs):
            return None

        def add_row(self, *row):
            self.rows.append(row)

        def __str__(self) -> str:
            lines = []
            if self.title:
                lines.append(_strip_markup(self.title))
            lines.extend(" | ".join(_strip_markup(cell) for cell in row) for row in self.rows)
            return "\n".join(lines)

    class Live:
        def __init__(self, **kwargs) -> None:
            self.last_renderable = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def update(self, renderable):
            self.last_renderable = renderable
            print(_strip_markup(str(renderable)))

    class SpinnerColumn:
        pass

    class TextColumn:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class Progress:
        def __init__(self, *args, **kwargs) -> None:
            self.last_description = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def add_task(self, description, total=None):
            self.last_description = description
            print(_strip_markup(description))
            return 1

        def update(self, task_id, description=None):
            if description and description != self.last_description:
                self.last_description = description
                print(_strip_markup(description))

    class Prompt:
        @staticmethod
        def ask(message, choices=None, default=None, show_choices=True, **kwargs):
            suffix = ""
            if choices and show_choices:
                suffix = f" ({'/'.join(choices)})"
            if default is not None:
                suffix = f"{suffix} [{default}]"
            value = input(f"{_strip_markup(message)}{suffix}: ").strip()
            if not value and default is not None:
                value = str(default)
            if choices and value not in choices:
                raise ValueError(f"Ожидается одно из значений: {', '.join(choices)}")
            return value

    class Confirm:
        @staticmethod
        def ask(message, default=False, **kwargs):
            prompt = "Y/n" if default else "y/N"
            value = input(f"{_strip_markup(message)} [{prompt}]: ").strip().lower()
            if not value:
                return default
            return value in {"y", "yes", "1", "true"}

    class Console:
        def print(self, *args, **kwargs):
            print(*(_strip_markup(str(arg)) for arg in args))

        def log(self, *args, **kwargs):
            self.print(*args)

        @contextmanager
        def status(self, message):
            self.print(message)
            yield


def ensure_utf8_locale():
    try:
        current_locale = locale.getlocale()
        if current_locale and current_locale[1] == "UTF-8":
            return
    except Exception:
        pass

    console.print("[yellow]⏳ Проверка и установка локали UTF-8...[/yellow]")

    os.environ["LC_ALL"] = "en_US.UTF-8"
    os.environ["LANG"] = "en_US.UTF-8"

    result = subprocess.run(["locale", "-a"], capture_output=True, text=True)
    if "en_US.utf8" not in result.stdout.lower():
        console.print("[blue]Добавляю локаль en_US.UTF-8 в систему...[/blue]")
        try:
            subprocess.run(["sudo", "locale-gen", "en_US.UTF-8"], check=True)
            subprocess.run(["sudo", "update-locale", "LANG=en_US.UTF-8"], check=True)
            console.print("[green]Локаль успешно установлена.[/green]")
        except Exception as e:
            console.print(f"[red]❌ Ошибка при установке локали: {e}[/red]")
    else:
        console.print("[green]Локаль UTF-8 уже доступна в системе.[/green]")


try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

console = Console()
ensure_utf8_locale()

BACK_DIR = os.path.expanduser("~/.solobot_backups")
TEMP_DIR = os.path.expanduser("~/.solobot_tmp")
PROJECT_DIR = os.path.abspath(os.path.dirname(__file__))
IS_ROOT_DIR = PROJECT_DIR == "/root"
GITHUB_REPO = "https://github.com/Vladless/Solo_bot"
GHCR_IMAGE = os.environ.get("GHCR_IMAGE", "vladless/solo-brick").strip() or "vladless/solo-brick"
DEFAULT_SERVICE_NAME = "bot.service"
VENV_PYTHON = os.path.join(PROJECT_DIR, "venv", "bin", "python")


class HttpResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)


def http_get(url: str, *, params=None, timeout: int = 10) -> HttpResponse:
    if requests is not None:
        response = requests.get(url, params=params, timeout=timeout)
        return HttpResponse(response.status_code, response.text)

    final_url = url
    if params:
        final_url = f"{url}?{urlencode(params)}"
    request = Request(final_url, headers={"User-Agent": "SoloBot-CLI"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return HttpResponse(response.status, response.read().decode("utf-8"))
    except HTTPError as error:
        return HttpResponse(error.code, error.read().decode("utf-8", errors="replace"))
    except URLError:
        return HttpResponse(599, "")


def detect_service_name() -> str:
    config_path = os.path.join(PROJECT_DIR, "config.py")
    if os.path.isfile(config_path):
        try:
            with open(config_path, encoding="utf-8") as config_file:
                config_text = config_file.read()
            match = re.search(r"BOT_SERVICE\s*=\s*['\"]([^'\"]+)['\"]", config_text)
            if match:
                return match.group(1)
        except Exception:
            pass
    return DEFAULT_SERVICE_NAME


def refresh_service_name() -> str:
    global SERVICE_NAME, SYSTEMD_SERVICE_PATH
    SERVICE_NAME = detect_service_name()
    SYSTEMD_SERVICE_PATH = os.path.join("/etc/systemd/system", SERVICE_NAME)
    return SERVICE_NAME


SERVICE_NAME = refresh_service_name()


def is_ascii_only(value: str) -> bool:
    """Проверка, что строка содержит только ASCII."""
    return all(ord(ch) < 128 for ch in value)


def _parse_tag_version(tag_name: str) -> tuple[int, ...]:
    """Извлекает кортеж (major, minor, patch, ...) из тега для сортировки. v.5.1 -> (5, 1), v4 -> (4, 0)."""
    s = tag_name.strip().lstrip("v.")
    parts = []
    for part in re.split(r"[.\s]+", s):
        try:
            parts.append(int(part))
        except ValueError:
            break
    return tuple(parts) if parts else (0,)


def warn_english_only():
    """Предупреждение о необходимости английской раскладки."""
    console.print("[red]Обнаружен ввод с неанглийской раскладкой.[/red]")
    console.print("[yellow]Пожалуйста, переключите раскладку на ENG и введите снова.[/yellow]")


def safe_confirm(message: str, **kwargs) -> bool:
    """Безопасный Confirm.ask с защитой от русской раскладки."""
    while True:
        try:
            result = Confirm.ask(message, **kwargs)
            return result
        except UnicodeDecodeError:
            warn_english_only()


def safe_prompt(message: str, **kwargs) -> str:
    """Безопасный Prompt.ask с защитой от русской раскладки.

    Не-ASCII символы тихо фильтруются. Предупреждение появляется только
    если после фильтрации в строке не осталось значимого ASCII (т.е. ввод
    был полностью на не-английской раскладке).
    """
    while True:
        try:
            value = Prompt.ask(message, **kwargs)
        except UnicodeDecodeError:
            warn_english_only()
            continue
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            continue
        if isinstance(value, str) and not is_ascii_only(value):
            cleaned = "".join(ch for ch in value if ord(ch) < 128)
            if not cleaned.strip():
                warn_english_only()
                continue
            return cleaned
        return value


if IS_ROOT_DIR:
    _required_paths = ("requirements.txt", "main.py")
    _has_project = all(os.path.exists(os.path.join(PROJECT_DIR, p)) for p in _required_paths)
    _has_config = os.path.exists(os.path.join(PROJECT_DIR, "config.py"))

    if _has_project or _has_config:
        console.print("[bold red]КРИТИЧЕСКАЯ ОШИБКА:[/bold red]")
        console.print("[red]Обнаружена установка бота прямо в корневой папке (/root).[/red]")
        console.print("[red]Это крайне опасно и может привести к потере данных![/red]")
        console.print("[red]Рекомендуется перенести бота в отдельную папку, например /root/Solo_bot[/red]")
        console.print("[red]Обновление заблокировано в целях безопасности.[/red]")
        sys.exit(1)

    _target_dir = "/root/Solo_bot"
    os.makedirs(_target_dir, exist_ok=True)
    _target_path = os.path.join(_target_dir, os.path.basename(__file__))
    try:
        shutil.move(__file__, _target_path)
    except Exception as e:
        console.print(f"[red]Не удалось перенести launcher в {_target_dir}: {e}[/red]")
        sys.exit(1)
    os.chdir(_target_dir)
    console.print(f"[green]✓ Launcher перенесён в {_target_dir}[/green]")
    console.print("[dim]Перезапуск из новой папки...[/dim]")
    os.execv(sys.executable, [sys.executable, _target_path, *sys.argv[1:]])


def run_with_status(
    cmd,
    *,
    status_text: str,
    cwd: str | None = None,
    check: bool = False,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    with console.status(f"[bold cyan]{status_text}[/bold cyan]", spinner="dots"):
        result = subprocess.run(
            cmd, cwd=cwd, env=env, capture_output=True, text=True, check=False
        )
    if result.returncode != 0:
        if result.stdout:
            console.print(result.stdout)
        if result.stderr:
            console.print(f"[red]{result.stderr.rstrip()}[/red]")
        if check:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr
            )
    return result


def is_service_exists(service_name):
    result = subprocess.run(["systemctl", "list-unit-files", service_name], capture_output=True, text=True)
    return service_name in result.stdout


def get_runtime_user() -> str:
    return os.environ.get("SUDO_USER") or subprocess.check_output(["whoami"], text=True).strip()


def has_project_code() -> bool:
    required_paths = ("requirements.txt", "main.py")
    return all(os.path.exists(os.path.join(PROJECT_DIR, path)) for path in required_paths)


def has_local_config() -> bool:
    return os.path.exists(os.path.join(PROJECT_DIR, "config.py"))


def bootstrap_project_files(branch: str = "main") -> bool:
    refresh_service_name()
    if has_project_code():
        return True

    console.print("[yellow]Полный проект рядом не найден. Подтягиваю файлы бота...[/yellow]")
    install_core_packages_if_needed()
    install_rsync_if_needed()

    subprocess.run(["rm", "-rf", TEMP_DIR], check=False)
    clone_result = run_with_status(
        ["git", "clone", "--depth", "1", "--branch", branch, GITHUB_REPO, TEMP_DIR],
        status_text=f"Клонирование {GITHUB_REPO} (ветка {branch})",
    )
    if clone_result.returncode != 0:
        console.print("[red]❌ Не удалось скачать проект из GitHub.[/red]")
        return False

    rsync_cmd = ["rsync", "-a", f"{TEMP_DIR}/", f"{PROJECT_DIR}/"]
    if has_local_config():
        rsync_cmd.insert(2, "--exclude=config.py")
    if os.path.exists(os.path.join(PROJECT_DIR, "handlers", "texts.py")):
        rsync_cmd.insert(2, "--exclude=handlers/texts.py")
    if os.path.exists(os.path.join(PROJECT_DIR, "handlers", "buttons.py")):
        rsync_cmd.insert(2, "--exclude=handlers/buttons.py")
    if os.path.exists(os.path.join(PROJECT_DIR, "core", "redis_cache.py")):
        rsync_cmd.insert(2, "--exclude=core/redis_cache.py")
    if os.path.exists(os.path.join(PROJECT_DIR, "img")):
        rsync_cmd.insert(2, "--exclude=img")
    if os.path.exists(os.path.join(PROJECT_DIR, "modules")):
        rsync_cmd.insert(2, "--exclude=modules")
    rsync_cmd.insert(2, "--exclude=.git")

    sync_result = run_with_status(rsync_cmd, status_text="Распаковка файлов проекта")
    subprocess.run(["rm", "-rf", TEMP_DIR], check=False)
    if sync_result.returncode != 0:
        console.print("[red]❌ Не удалось распаковать файлы проекта.[/red]")
        return False

    refresh_service_name()
    console.print("[green]Файлы проекта подготовлены.[/green]")
    return True


def install_core_packages_if_needed():
    missing_packages = []

    if shutil.which("git") is None:
        missing_packages.append("git")
    if shutil.which("rsync") is None:
        missing_packages.append("rsync")

    python312_path = shutil.which("python3.12")
    if python312_path is None:
        missing_packages.extend(["python3.12", "python3.12-venv"])
    else:
        venv_check = subprocess.run(
            [python312_path, "-m", "venv", "--help"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if venv_check.returncode != 0:
            missing_packages.append("python3.12-venv")

    if not missing_packages:
        return

    unique_packages = list(dict.fromkeys(missing_packages))
    console.print(f"[yellow]Устанавливаю системные пакеты: {', '.join(unique_packages)}[/yellow]")
    run_with_status(["sudo", "apt", "update"], status_text="apt update", check=True)
    run_with_status(
        ["sudo", "apt", "install", "-y", *unique_packages],
        status_text=f"Установка: {', '.join(unique_packages)}",
        check=True,
    )


def build_systemd_service() -> str:
    run_user = get_runtime_user()
    return (
        "[Unit]\n"
        "Description=SoloBot Telegram bot\n"
        "After=network.target\n\n"
        "[Service]\n"
        f"User={run_user}\n"
        f"WorkingDirectory={PROJECT_DIR}\n"
        f"ExecStart={VENV_PYTHON} {os.path.join(PROJECT_DIR, 'main.py')}\n"
        "Restart=always\n"
        "RestartSec=10\n"
        "TimeoutStopSec=10\n"
        "KillMode=control-group\n"
        'Environment="PYTHONUNBUFFERED=1"\n'
        'LimitNOFILE=65536\n\n'
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def ensure_systemd_service() -> bool:
    refresh_service_name()
    console.print(f"[yellow]Проверяю systemd-службу {SERVICE_NAME}...[/yellow]")
    service_text = build_systemd_service()
    service_exists = os.path.exists(SYSTEMD_SERVICE_PATH)

    if service_exists:
        try:
            with open(SYSTEMD_SERVICE_PATH, encoding="utf-8") as service_file:
                if service_file.read() == service_text:
                    console.print(f"[green]Служба {SERVICE_NAME} уже настроена.[/green]")
                    return True
        except Exception:
            pass

    try:
        subprocess.run(
            ["sudo", "tee", SYSTEMD_SERVICE_PATH],
            input=service_text,
            text=True,
            stdout=subprocess.DEVNULL,
            check=True,
        )
        subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
        console.print(f"[green]Служба {SERVICE_NAME} настроена.[/green]")
        return True
    except Exception as e:
        console.print(f"[red]❌ Не удалось настроить службу {SERVICE_NAME}: {e}[/red]")
        return False


def initialize_database() -> bool:
    if not os.path.exists(VENV_PYTHON):
        console.print("[yellow]Инициализация базы пропущена: виртуальное окружение ещё не создано.[/yellow]")
        return False
    console.print("[yellow]Инициализация базы данных...[/yellow]")
    try:
        subprocess.run(
            [
                VENV_PYTHON,
                "-c",
                "import asyncio; from database.setup.init_db import init_db; asyncio.run(init_db())",
            ],
            cwd=PROJECT_DIR,
            check=True,
        )
        console.print("[green]База данных успешно инициализирована.[/green]")
        return True
    except Exception as e:
        console.print(f"[red]❌ Не удалось инициализировать базу данных: {e}[/red]")
        return False


def enable_and_start_service(start_now: bool = True) -> None:
    refresh_service_name()
    subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
    subprocess.run(["sudo", "systemctl", "enable", SERVICE_NAME], check=True)
    if start_now:
        subprocess.run(["sudo", "systemctl", "restart", SERVICE_NAME], check=True)
        console.print(f"[green]Служба {SERVICE_NAME} включена и запущена.[/green]")
    else:
        console.print(
            f"[yellow]Служба {SERVICE_NAME} включена, но не запущена. Проверьте config.py и доступность базы данных.[/yellow]"
        )


def is_runtime_ready() -> bool:
    refresh_service_name()
    if not has_project_code():
        return False
    return os.path.exists(VENV_PYTHON) and is_service_exists(SERVICE_NAME)


def install_bot():
    console.print(
        Panel(
            "[white]CLI подготовит окружение, установит зависимости, создаст systemd-службу "
            "и попробует инициализировать базу данных. Если проекта ещё нет рядом, CLI сначала скачает его автоматически.[/white]",
            border_style="green",
            title="[bold green]Автоматическая установка SoloBot[/bold green]",
            padding=(1, 2),
        )
    )

    if not safe_confirm("[bold green]Запустить автоматическую установку?[/bold green]", default=True):
        return

    try:
        branch = "main"
        if not has_project_code():
            use_beta = safe_confirm("[yellow]Скачать beta/dev ветку вместо стабильной?[/yellow]", default=False)
            branch = "dev" if use_beta else "main"
        if not bootstrap_project_files(branch=branch):
            return
        refresh_service_name()
        install_core_packages_if_needed()
        install_dependencies()
        db_ready = initialize_database()
        if not ensure_systemd_service():
            return
        fix_permissions()
        enable_and_start_service(start_now=db_ready)
        console.print("[green]✅ Установка SoloBot завершена.[/green]")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]❌ Ошибка во время установки: {e}[/red]")


def prompt_install_if_needed():
    if is_runtime_ready():
        return

    refresh_service_name()
    has_project = has_project_code()
    has_venv = has_project and os.path.exists(VENV_PYTHON)
    has_service = has_project and is_service_exists(SERVICE_NAME)

    if not has_project:
        console.print(
            Panel(
                "[white]В этой папке ещё нет установки.[/white]\n\n"
                "[bold]SoloBot состоит из двух независимых частей:[/bold]\n"
                "  • [cyan]Telegram-бот[/cyan] — продажа VPN-ключей в ТГ\n"
                "    (пункт меню [bold]9 — Установить / переустановить бота[/bold])\n"
                "  • [cyan]Веб-сайт[/cyan] — личный кабинет для клиентов\n"
                "    (пункт меню [bold]10 — 🌐 Веб-сайт[/bold])\n\n"
                "[white]Можно установить только одно из двух, либо оба.[/white]\n"
                "[white]Выберите нужный пункт в меню ниже.[/white]",
                border_style="cyan",
                title="[bold green]Первый запуск[/bold green]",
                padding=(1, 2),
            )
        )
        return

    missing_labels: list[str] = []
    if not has_venv:
        missing_labels.append("Python virtual environment (venv/) с зависимостями")
    if not has_service:
        missing_labels.append(f"systemd-служба {SERVICE_NAME} (автозапуск)")
    if not missing_labels:
        return
    bullets = "\n".join(f"  • {label}" for label in missing_labels)
    console.print(
        Panel(
            "[white]Установка бота частично нарушена.[/white]\n"
            f"[yellow]Не хватает:[/yellow]\n{bullets}\n\n"
            "[white]CLI допустит недостающие части — исходники и настройки не трогаются.[/white]",
            border_style="yellow",
            title="[bold yellow]Починка установки бота[/bold yellow]",
            padding=(1, 2),
        )
    )
    if safe_confirm("[green]Выполнить починку сейчас?[/green]", default=True):
        install_bot()


def print_logo():
    logo_lines = [
        "███████╗ ██████╗ ██╗      ██████╗ ██████╗  ██████╗ ████████╗",
        "██╔════╝██╔═══██╗██║     ██╔═══██╗██╔══██╗██╔═══██╗╚══██╔══╝",
        "███████╗██║   ██║██║     ██║   ██║██████╔╝██║   ██║   ██║   ",
        "╚════██║██║   ██║██║     ██║   ██║██╔══██╗██║   ██║   ██║   ",
        "███████║╚██████╔╝███████╗╚██████╔╝██████╔╝╚██████╔╝   ██║   ",
        "╚══════╝ ╚═════╝ ╚══════╝ ╚═════╝ ╚═════╝  ╚═════╝    ╚═╝   ",
    ]

    with Live(refresh_per_second=10) as live:
        display = []
        for line in logo_lines:
            display.append(f"[bold cyan]{line}[/bold cyan]")
            panel = Panel(Group(*display), border_style="cyan", padding=(0, 2), expand=False)
            live.update(panel)
            sleep(0.07)

    local_version = get_local_version() or "unknown"
    last_update = get_last_update_date() or "unknown"
    console.print(f"[bold green]Директория бота:[/bold green] [yellow]{PROJECT_DIR}[/yellow]")
    console.print(f"[bold green]Установленная версия:[/bold green] [yellow]{local_version}[/yellow]")
    console.print(f"[bold green]Последнее обновление:[/bold green] [yellow]{last_update}[/yellow]\n")


def list_backups():
    if not os.path.isdir(BACK_DIR):
        return []
    pairs = []
    for name in os.listdir(BACK_DIR):
        path = os.path.join(BACK_DIR, name)
        if os.path.isdir(path):
            try:
                mtime = os.path.getmtime(path)
            except Exception:
                mtime = 0
            pairs.append((mtime, path))
    pairs.sort(reverse=True)
    return [p for _, p in pairs]


def prune_old_backups():
    backups = list_backups()
    for path in backups[3:]:
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            subprocess.run(["sudo", "rm", "-rf", path])


def backup_project() -> str | None:
    from datetime import datetime

    os.makedirs(BACK_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(BACK_DIR, f"backup-{ts}")
    console.print("[yellow]Создаётся резервная копия проекта...[/yellow]")
    with console.status("[bold cyan]Копирование файлов...[/bold cyan]"):
        result = subprocess.run(["cp", "-r", PROJECT_DIR, dst], check=False)
    if result.returncode != 0:
        console.print("[red]❌ Не удалось создать бэкап[/red]")
        return None
    console.print(f"[green]Бэкап сохранён в: {dst}[/green]")
    prune_old_backups()
    return dst


def _restore_backup_unattended(backup_path: str) -> bool:
    if not backup_path or not os.path.isdir(backup_path):
        return False
    if is_service_exists(SERVICE_NAME):
        subprocess.run(["sudo", "systemctl", "stop", SERVICE_NAME], check=False)
    install_rsync_if_needed()
    result = run_with_status(
        ["rsync", "-a", "--delete", f"{backup_path}/", f"{PROJECT_DIR}/"],
        status_text="Откат из бэкапа",
    )
    return result.returncode == 0


def _build_update_rsync_excludes(update_buttons: bool, update_img: bool, update_redis_cache: bool) -> list[str]:
    excludes = []
    if not update_img:
        excludes.append("--exclude=img")
    if not update_buttons:
        excludes.append("--exclude=handlers/buttons.py")
    if not update_redis_cache:
        excludes.append("--exclude=core/redis_cache.py")
    excludes.append("--exclude=modules")
    excludes.append("--exclude=static/web_uploads")
    return excludes


def restore_from_backup():
    from datetime import datetime

    backups = list_backups()[:3]
    if not backups:
        console.print(f"[red]❌ Бэкапы не найдены: {BACK_DIR}[/red]")
        return

    console.print("\n[bold green]Доступные бэкапы:[/bold green]")
    shown = []
    for idx, path in enumerate(backups, 1):
        try:
            mtime = os.path.getmtime(path)
            dt = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            dt = "unknown"
        console.print(f"[cyan]{idx}.[/cyan] {os.path.basename(path)}  [dim]{dt}[/dim]")
        shown.append((idx, path))

    try:
        choice = safe_prompt(
            "[bold blue]Выберите номер бэкапа[/bold blue]",
            choices=[str(i) for i, _ in shown],
        )
    except Exception:
        return

    sel_path = shown[int(choice) - 1][1]

    console.print("[red]Внимание: текущие файлы проекта будут перезаписаны выбранным бэкапом.[/red]")
    if not safe_confirm("[yellow]Продолжить восстановление из бэкапа?[/yellow]"):
        return

    if is_service_exists(SERVICE_NAME):
        console.print("[blue]Останавливаю службу перед восстановлением...[/blue]")
        subprocess.run(["sudo", "systemctl", "stop", SERVICE_NAME])

    install_rsync_if_needed()

    console.print("[yellow]Копирую файлы из бэкапа в проект...[/yellow]")
    rc = subprocess.run(
        ["rsync", "-a", "--delete", f"{sel_path}/", f"{PROJECT_DIR}/"],
        check=False,
    ).returncode
    if rc != 0:
        console.print("[red]❌ Ошибка rsync при восстановлении[/red]")
        return

    install_dependencies()
    fix_permissions()
    restart_service()
    console.print("[green]✅ Восстановление из бэкапа завершено[/green]")


def _sync_rpc_files() -> bool:
    core_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "core")
    os.makedirs(core_dir, exist_ok=True)
    cachebuster = str(int(time_mod.time()))
    base_url = "https://raw.githubusercontent.com/Vladless/Solo_bot/dev/core"
    targets = [
        ("__init__.py", os.path.join(core_dir, "__init__.py")),
        (
            "rpc.cpython-312-x86_64-linux-gnu.so",
            os.path.join(core_dir, "rpc.cpython-312-x86_64-linux-gnu.so"),
        ),
    ]
    updated: list[str] = []
    for name, path in targets:
        try:
            req = Request(
                f"{base_url}/{name}?v={cachebuster}",
                headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
            )
            with urlopen(req, timeout=20) as resp:
                remote_bytes = resp.read()
        except Exception as e:
            console.print(f"[red]Не удалось скачать core/{name}: {e}[/red]")
            continue
        if not remote_bytes:
            console.print(f"[red]core/{name}: пустой ответ от GitHub[/red]")
            continue
        local_bytes = None
        if os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    local_bytes = f.read()
            except Exception:
                local_bytes = None
        if local_bytes == remote_bytes:
            continue
        try:
            with open(path, "wb") as f:
                f.write(remote_bytes)
            updated.append(f"core/{name}")
        except Exception as e:
            console.print(f"[red]Не удалось записать core/{name}: {e}[/red]")
    if updated:
        console.print(f"[green]Обновлены: {', '.join(updated)}[/green]")
        import sys as _sys
        for mod_name in list(_sys.modules.keys()):
            if mod_name == "core" or mod_name == "core.rpc" or mod_name.startswith("core."):
                del _sys.modules[mod_name]
        return True
    return False


def auto_update_cli():
    console.print("[yellow]Проверка обновлений CLI...[/yellow]")
    try:
        url = "https://raw.githubusercontent.com/Vladless/Solo_bot/dev/cli_launcher.py"
        response = http_get(url, timeout=10)
        if response.status_code != 200:
            console.print("[red]Не удалось получить обновление CLI[/red]")
            return

        latest_text = response.text
        current_path = os.path.realpath(__file__)
        with open(current_path, encoding="utf-8") as f:
            current_text = f.read()

        rpc_updated = _sync_rpc_files()

        if current_text != latest_text:
            console.print("[green]Доступна новая версия CLI. Обновляю...[/green]")
            with open(current_path, "w", encoding="utf-8") as f:
                f.write(latest_text)
            os.chmod(current_path, 0o644)
            console.print("[green]CLI обновлён. Перезапуск...[/green]")
            os.execv(sys.executable, [sys.executable, current_path])
        elif rpc_updated:
            console.print("[green]core/rpc обновлён. Перезапуск CLI...[/green]")
            os.execv(sys.executable, [sys.executable, current_path])
        else:
            console.print("[green]CLI уже актуален[/green]")
    except Exception as e:
        console.print(f"[red]❌ Ошибка при автообновлении CLI: {e}[/red]")


def fix_permissions():
    console.print("[yellow]Восстанавливаю владельца и права доступа к проекту...[/yellow]")

    try:
        user = os.environ.get("SUDO_USER") or subprocess.check_output(["whoami"], text=True).strip()
        console.log(f"[cyan]Используем пользователь: {user}[/cyan]")

        for root, dirs, files in os.walk(PROJECT_DIR):
            for dir in dirs:
                if dir == "__pycache__":
                    pycache_path = os.path.join(root, dir)
                    subprocess.run(["sudo", "rm", "-rf", pycache_path], check=True)
            for file in files:
                if file.endswith(".pyc"):
                    pyc_path = os.path.join(root, file)
                    subprocess.run(["sudo", "rm", "-f", pyc_path], check=True)

        console.log("[blue]Изменение владельца на весь проект...[/blue]")
        subprocess.run(["sudo", "chown", "-R", f"{user}:{user}", PROJECT_DIR], check=True)

        console.log("[blue]Изменение прав доступа (u=rwX,go=rX)...[/blue]")
        subprocess.run(["sudo", "chmod", "-R", "u=rwX,go=rX", PROJECT_DIR], check=True)

        launcher_path = os.path.join(PROJECT_DIR, "cli_launcher.py")
        if os.path.exists(launcher_path):
            console.log("[blue]Установка флага +x для cli_launcher.py...[/blue]")
            subprocess.run(["chmod", "+x", launcher_path], check=True)

        console.print(f"[green]Все права восстановлены для пользователя [bold]{user}[/bold][/green]")

    except Exception as e:
        console.print(f"[red]❌ Ошибка при установке прав: {e}[/red]")


def install_rsync_if_needed():
    install_core_packages_if_needed()


def clean_project_dir_safe(update_buttons=False, update_img=False, update_redis_cache=False):
    console.print("[yellow]Очистка проекта перед обновлением...[/yellow]")

    preserved_paths = set()

    preserved_paths.update([
        os.path.join(PROJECT_DIR, "config.py"),
        os.path.join(PROJECT_DIR, "handlers", "texts.py"),
        os.path.join(PROJECT_DIR, ".git"),
        os.path.join(PROJECT_DIR, "modules"),
        os.path.join(PROJECT_DIR, "static"),
        os.path.join(PROJECT_DIR, "static", "web_uploads"),
    ])

    for root, dirs, files in os.walk(os.path.join(PROJECT_DIR, "modules")):
        for name in dirs + files:
            preserved_paths.add(os.path.join(root, name))

    for root, dirs, files in os.walk(os.path.join(PROJECT_DIR, "static", "web_uploads")):
        for name in dirs + files:
            preserved_paths.add(os.path.join(root, name))

    if not update_buttons:
        preserved_paths.add(os.path.join(PROJECT_DIR, "handlers", "buttons.py"))

    if not update_img:
        preserved_paths.add(os.path.join(PROJECT_DIR, "img"))
        for root, dirs, files in os.walk(os.path.join(PROJECT_DIR, "img")):
            for name in dirs + files:
                preserved_paths.add(os.path.join(root, name))

    if not update_redis_cache:
        preserved_paths.add(os.path.join(PROJECT_DIR, "core", "redis_cache.py"))

    for root, dirs, files in os.walk(PROJECT_DIR, topdown=False):
        for file in files:
            path = os.path.join(root, file)
            if path in preserved_paths:
                continue
            try:
                os.remove(path)
            except PermissionError:
                subprocess.run(["sudo", "rm", "-f", path])
            except Exception as e:
                console.print(f"[red]Не удалось удалить файл: {path}: {e}[/red]")

        for dir in dirs:
            dir_path = os.path.join(root, dir)

            if os.path.abspath(dir_path) in [
                os.path.join(PROJECT_DIR, "handlers"),
                os.path.join(PROJECT_DIR, "img"),
                os.path.join(PROJECT_DIR, "modules"),
                os.path.join(PROJECT_DIR, "static"),
                os.path.join(PROJECT_DIR, "static", "web_uploads"),
            ]:
                continue

            if os.path.abspath(dir_path).startswith(os.path.join(PROJECT_DIR, "modules") + os.sep):
                continue

            if os.path.abspath(dir_path).startswith(os.path.join(PROJECT_DIR, "static", "web_uploads") + os.sep):
                continue

            try:
                os.rmdir(dir_path)
            except Exception:
                subprocess.run(["sudo", "rm", "-rf", dir_path])


def install_git_if_needed():
    install_core_packages_if_needed()


def install_dependencies():
    console.print("[blue]Установка зависимостей...[/blue]")
    install_core_packages_if_needed()

    python312_path = shutil.which("python3.12")
    if not python312_path:
        console.print("[red]Не найден python3.12 в системе[/red]")
        console.print("[yellow]Установите Python 3.12: sudo apt install python3.12 python3.12-venv[/yellow]")
        sys.exit(1)

    with Progress(
        SpinnerColumn(style="green"),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        task_id = progress.add_task(description="Создание виртуального окружения...", total=None)
        try:
            if os.path.exists("venv"):
                shutil.rmtree("venv")
                console.print("[yellow]Удалён старый venv[/yellow]")

            subprocess.run([python312_path, "-m", "venv", "venv"], check=True)

            progress.update(task_id, description="Установка зависимостей...")
            subprocess.run(
                [os.path.join("venv", "bin", "pip"), "install", "-r", "requirements.txt"],
                check=True,
                cwd=PROJECT_DIR,
            )

            progress.update(task_id, description="Установка завершена")

        except subprocess.CalledProcessError as e:
            progress.update(task_id, description="❌ Ошибка при установке")
            console.print(f"[red]❌ Ошибка: {e}[/red]")


def restart_service():
    if ensure_systemd_service():
        console.print("[blue]🚀 Перезапуск службы...[/blue]")
        with console.status("[bold yellow]Перезапуск...[/bold yellow]"):
            subprocess.run(["sudo", "systemctl", "enable", SERVICE_NAME], check=False)
            subprocess.run(["sudo", "systemctl", "restart", SERVICE_NAME])


def _extract_version_from_versioning(text: str) -> str | None:
    match = re.search(r'["\'](v\.\d+(?:[.-][\w\d]+)*)', text)
    return match.group(1) if match else None


def get_local_version():
    path = os.path.join(PROJECT_DIR, "utils", "versioning.py")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                version = _extract_version_from_versioning(f.read())
            if version:
                return version
        except Exception:
            pass

    try:
        result = subprocess.run(
            ["git", "-C", PROJECT_DIR, "describe", "--tags", "--always"],
            capture_output=True,
            text=True,
            check=False,
        )
        version = result.stdout.strip()
        if result.returncode == 0 and version:
            return version
    except Exception:
        pass

    path = os.path.join(PROJECT_DIR, "bot.py")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        for line in f:
            match = re.search(r'version\s*=\s*["\'](.+?)["\']', line)
            if match:
                return match.group(1)
    return None


def get_last_update_date():
    try:
        result = subprocess.run(
            ["git", "-C", PROJECT_DIR, "log", "-1", "--format=%cd", "--date=format:%Y-%m-%d %H:%M:%S"],
            capture_output=True,
            text=True,
            check=False,
        )
        value = result.stdout.strip()
        if result.returncode == 0 and value:
            return value
    except Exception:
        pass

    excluded_dirs = {".git", "venv", ".venv", "__pycache__", "build", "dist"}
    latest_mtime = 0.0
    for root, dirs, files in os.walk(PROJECT_DIR):
        dirs[:] = [d for d in dirs if d not in excluded_dirs]
        for file_name in files:
            path = os.path.join(root, file_name)
            try:
                latest_mtime = max(latest_mtime, os.path.getmtime(path))
            except Exception:
                continue
    if latest_mtime <= 0:
        return None
    return datetime.fromtimestamp(latest_mtime).strftime("%Y-%m-%d %H:%M:%S")


def get_remote_version(branch="main"):
    try:
        url = f"https://raw.githubusercontent.com/Vladless/Solo_bot/{branch}/utils/versioning.py"
        response = http_get(url, timeout=10)
        if response.status_code == 200:
            version = _extract_version_from_versioning(response.text)
            if version:
                return version
    except Exception:
        pass
    try:
        url = f"https://raw.githubusercontent.com/Vladless/Solo_bot/{branch}/bot.py"
        response = http_get(url, timeout=10)
        if response.status_code == 200:
            for line in response.text.splitlines():
                match = re.search(r'version\s*=\s*["\'](.+?)["\']', line)
                if match:
                    return match.group(1)
    except Exception:
        return None
    return None


def update_from_beta():
    local_version = get_local_version()
    remote_version = get_remote_version(branch="dev")

    console.print(
        Panel(
            "[bold red]Обновление на DEV / BETA-ветку[/bold red]\n\n"
            "[white]"
            "• Dev-ветка может содержать изменения, которые ещё находятся в доработке.\n"
            "• Возможны ошибки и непредсказуемое поведение отдельных функций, особенно режима стран.\n\n"
            "• BETA-версии бота в первую очередь ориентированы на опытных пользователей, "
            "готовых протестировать новые возможности и осознанно работать с обновлённым функционалом.\n"
            "[/white]\n\n"
            "[yellow]Перед началом обновления CLI автоматически создаёт резервную копию проекта, "
            "что позволит при необходимости безопасно восстановиться из бэкапа.[/yellow]",
            border_style="red",
            title="[bold red]Нестабильная ветка разработки[/bold red]",
            padding=(1, 2),
        )
    )

    if local_version and remote_version:
        console.print(f"[cyan]Локальная версия: {local_version} | Последняя в dev: {remote_version}[/cyan]")
        if local_version == remote_version:
            if not safe_confirm("[yellow]Версия актуальна. Обновить всё равно?[/yellow]"):
                return

    if not safe_confirm(
        "[bold red]Продолжить обновление на dev-ветку с учётом возможных особенностей работы?[/bold red]"
    ):
        return

    console.print("[red]ВНИМАНИЕ! Папка бота будет перезаписана![/red]")
    if not safe_confirm("[red]Продолжить обновление?[/red]"):
        return

    update_buttons = safe_confirm("[yellow]Обновлять файл buttons.py?[/yellow]", default=False)
    update_img = safe_confirm("[yellow]Обновлять папку img?[/yellow]", default=False)
    update_redis_cache = safe_confirm("[yellow]Обновлять файл core/redis_cache.py?[/yellow]", default=False)

    backup_path = backup_project()
    if not backup_path and not safe_confirm(
        "[yellow]Бэкап не создан. Продолжить обновление БЕЗ бэкапа?[/yellow]", default=False
    ):
        return
    install_git_if_needed()
    install_rsync_if_needed()

    try:
        os.chdir(PROJECT_DIR)
        subprocess.run(["rm", "-rf", TEMP_DIR])

        clone_result = run_with_status(
            ["git", "clone", "--depth=1000000", "-b", "dev", GITHUB_REPO, TEMP_DIR],
            status_text=f"Клонирование dev-ветки {GITHUB_REPO}",
        )
        if clone_result.returncode != 0:
            raise RuntimeError("git clone dev не удался")

        subprocess.run(["sudo", "rm", "-rf", os.path.join(PROJECT_DIR, "venv")])
        clean_project_dir_safe(
            update_buttons=update_buttons,
            update_img=update_img,
            update_redis_cache=update_redis_cache,
        )

        rsync_cmd = (
            ["rsync", "-a"]
            + _build_update_rsync_excludes(update_buttons, update_img, update_redis_cache)
            + [f"{TEMP_DIR}/", f"{PROJECT_DIR}/"]
        )
        rsync_result = run_with_status(rsync_cmd, status_text="Применение обновления (rsync)")
        if rsync_result.returncode != 0:
            raise RuntimeError("rsync обновления не удался")

        modules_path = os.path.join(PROJECT_DIR, "modules")
        if not os.path.exists(modules_path):
            try:
                os.makedirs(modules_path, exist_ok=True)
            except Exception:
                pass

        if os.path.exists(os.path.join(TEMP_DIR, ".git")):
            subprocess.run(["cp", "-r", os.path.join(TEMP_DIR, ".git"), PROJECT_DIR])

        subprocess.run(["rm", "-rf", TEMP_DIR])

        install_dependencies()
        fix_permissions()
        restart_service()
        console.print("[green]Обновление с ветки dev завершено.[/green]")
    except Exception as e:
        console.print(f"[red]❌ Обновление упало: {e}[/red]")
        if backup_path and safe_confirm(
            "[yellow]Откатить проект из свежего бэкапа?[/yellow]", default=True
        ):
            if _restore_backup_unattended(backup_path):
                console.print(f"[green]✓ Проект восстановлен из {backup_path}[/green]")
                restart_service()
            else:
                console.print(
                    f"[red]Автооткат не удался. Восстановите вручную: пункт 8 меню → {backup_path}[/red]"
                )
        else:
            console.print(
                f"[yellow]Для ручного отката: пункт 8 меню → {backup_path or 'нет бэкапа'}[/yellow]"
            )


def _do_update_to_tag(tag_name: str, update_buttons: bool, update_img: bool, update_redis_cache: bool) -> None:
    """Общая логика обновления до указанного тега (релиз или произвольный тег)."""
    subprocess.run(["rm", "-rf", TEMP_DIR])
    run_with_status(
        ["git", "clone", "--branch", tag_name, "--depth", "1", GITHUB_REPO, TEMP_DIR],
        status_text=f"Клонирование тега {tag_name}",
        check=True,
    )

    console.print("[red]Начинается перезапись файлов бота![/red]")
    subprocess.run(["sudo", "rm", "-rf", os.path.join(PROJECT_DIR, "venv")])
    clean_project_dir_safe(
        update_buttons=update_buttons,
        update_img=update_img,
        update_redis_cache=update_redis_cache,
    )

    rsync_cmd = (
        ["rsync", "-a"]
        + _build_update_rsync_excludes(update_buttons, update_img, update_redis_cache)
        + [f"{TEMP_DIR}/", f"{PROJECT_DIR}/"]
    )
    rsync_result = run_with_status(rsync_cmd, status_text=f"Применение тега {tag_name} (rsync)")
    if rsync_result.returncode != 0:
        raise RuntimeError(f"rsync тега {tag_name} не удался")

    modules_path = os.path.join(PROJECT_DIR, "modules")
    if not os.path.exists(modules_path):
        console.print("[yellow]Папка modules отсутствует — создаю вручную...[/yellow]")
        try:
            os.makedirs(modules_path, exist_ok=True)
            console.print("[green]Папка modules успешно создана.[/green]")
        except Exception as e:
            console.print(f"[red]❌ Не удалось создать папку modules: {e}[/red]")

    if os.path.exists(os.path.join(TEMP_DIR, ".git")):
        subprocess.run(["cp", "-r", os.path.join(TEMP_DIR, ".git"), PROJECT_DIR])

    subprocess.run(["rm", "-rf", TEMP_DIR])

    install_dependencies()
    fix_permissions()
    restart_service()
    console.print(f"[green]Обновление до {tag_name} завершено.[/green]")


def update_from_release():
    if not safe_confirm("[yellow]Подтвердите обновление Solobot до релиза или патча[/yellow]"):
        return

    console.print("[red]ВНИМАНИЕ! Папка бота будет полностью перезаписана![/red]")
    console.print("[red]  Исключения: папка img, файл handlers/buttons.py и файл core/redis_cache.py[/red]")
    if not safe_confirm("[red]Вы точно хотите продолжить?[/red]"):
        return

    update_buttons = safe_confirm("[yellow]Обновлять файл buttons.py?[/yellow]", default=False)
    update_img = safe_confirm("[yellow]Обновлять папку img?[/yellow]", default=False)
    update_redis_cache = safe_confirm("[yellow]Обновлять файл core/redis_cache.py?[/yellow]", default=False)

    backup_path = backup_project()
    if not backup_path and not safe_confirm(
        "[yellow]Бэкап не создан. Продолжить обновление БЕЗ бэкапа?[/yellow]", default=False
    ):
        return
    install_git_if_needed()
    install_rsync_if_needed()

    try:
        rel_resp = http_get(
            "https://api.github.com/repos/Vladless/Solo_bot/releases",
            timeout=10,
        )
        releases = rel_resp.json() if rel_resp.status_code == 200 else []
        release_tag_names = {r["tag_name"] for r in releases}

        tags_resp = http_get(
            "https://api.github.com/repos/Vladless/Solo_bot/tags",
            params={"per_page": 50},
            timeout=10,
        )
        if tags_resp.status_code != 200:
            raise ValueError("Не удалось получить список тегов")
        tags_data = tags_resp.json()
        all_tag_names = [t["name"] for t in tags_data]

        tag_names = [name for name in all_tag_names if _parse_tag_version(name)[0] >= 4]
        tag_names.sort(key=_parse_tag_version)

        if not tag_names:
            raise ValueError("Нет доступных тегов (ожидаются версии начиная с 4)")

        console.print("\n[bold green]Релизы и патчи:[/bold green]")
        for idx, name in enumerate(tag_names, 1):
            label = " [dim](релиз)[/dim]" if name in release_tag_names else " [dim](патч)[/dim]"
            console.print(f"[cyan]{idx}.[/cyan] {name}{label}")

        choices = [str(i) for i in range(1, len(tag_names) + 1)]
        selected = safe_prompt(
            "[bold blue]Выберите номер версии[/bold blue]",
            choices=choices,
        )
        tag_name = tag_names[int(selected) - 1]

        if not safe_confirm(f"[yellow]Установить {tag_name}?[/yellow]"):
            return

        _do_update_to_tag(tag_name, update_buttons, update_img, update_redis_cache)

    except Exception as e:
        console.print(f"[red]❌ Ошибка при обновлении: {e}[/red]")
        if backup_path and safe_confirm(
            "[yellow]Откатить проект из свежего бэкапа?[/yellow]", default=True
        ):
            if _restore_backup_unattended(backup_path):
                console.print(f"[green]✓ Проект восстановлен из {backup_path}[/green]")
                restart_service()
            else:
                console.print(
                    f"[red]Автооткат не удался. Восстановите вручную: пункт 8 меню → {backup_path}[/red]"
                )
        else:
            console.print(
                f"[yellow]Для ручного отката: пункт 8 меню → {backup_path or 'нет бэкапа'}[/yellow]"
            )


WEB_IMAGE_REPO = "ghcr.io/vladless/solo-brick"
WEB_CONTAINER_NAME = "solo-brick"
WEB_DIR = os.path.join(os.path.expanduser("~"), "solo-brick")
WEB_TAG_FILE = os.path.join(WEB_DIR, ".image-tag")
WEB_TAG_DEFAULT = "latest"
WEB_TAG_CHOICES = ("latest", "dev")


def _web_image(tag: str) -> str:
    return f"{WEB_IMAGE_REPO}:{tag or WEB_TAG_DEFAULT}"


def _get_saved_web_tag() -> str:
    try:
        with open(WEB_TAG_FILE) as f:
            tag = f.read().strip()
        if tag in WEB_TAG_CHOICES:
            return tag
    except Exception:
        pass
    return WEB_TAG_DEFAULT


def _save_web_tag(tag: str) -> None:
    try:
        os.makedirs(WEB_DIR, exist_ok=True)
        with open(WEB_TAG_FILE, "w") as f:
            f.write(tag)
    except Exception:
        pass


def _ensure_web_logs_dir() -> None:
    logs_dir = os.path.join(WEB_DIR, "logs")
    try:
        os.makedirs(logs_dir, exist_ok=True)
        os.chown(logs_dir, 1001, 1001)
    except PermissionError:
        try:
            subprocess.run(["sudo", "chown", "-R", "1001:1001", logs_dir], check=False)
        except Exception:
            pass
    except Exception:
        pass


def _read_env_value(env_path: str, key: str) -> str:
    """Читает значение ключа из .env файла, если файл существует."""
    if not os.path.exists(env_path):
        return ""
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def _ensure_plugin_builder_token(env_path: str) -> tuple[str, bool]:
    """Возвращает (token, is_new): существующий PLUGIN_BUILDER_TOKEN из .env или свежий 64-hex."""
    existing = _read_env_value(env_path, "PLUGIN_BUILDER_TOKEN")
    if existing and len(existing) >= 32:
        return existing, False
    return secrets.token_hex(32), True


def _generate_vapid_keys() -> tuple[str, str] | None:
    """VAPID keypair (P-256). Returns (public_b64url, private_b64url) или None."""
    try:
        import base64

        from cryptography.hazmat.primitives.asymmetric import ec
    except Exception:
        return None
    priv = ec.generate_private_key(ec.SECP256R1())
    priv_bytes = priv.private_numbers().private_value.to_bytes(32, "big")
    pub_numbers = priv.public_key().public_numbers()
    pub_bytes = b"\x04" + pub_numbers.x.to_bytes(32, "big") + pub_numbers.y.to_bytes(32, "big")

    def _b64url(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

    return _b64url(pub_bytes), _b64url(priv_bytes)


def _ask_web_tag(default: str = WEB_TAG_DEFAULT) -> str:
    console.print(
        "\n[bold]Канал обновлений:[/bold]\n"
        "  [cyan]1[/cyan] — [green]latest[/green]  стабильный (из ветки main)\n"
        "  [cyan]2[/cyan] — [yellow]dev[/yellow]     тестовый (последний коммит dev)"
    )
    default_choice = "2" if default == "dev" else "1"
    choice = safe_prompt(
        "[bold blue]Выберите канал[/bold blue]",
        choices=["1", "2"],
        default=default_choice,
        show_choices=False,
    )
    return "dev" if choice == "2" else "latest"


def _find_local_web_source() -> str | None:
    candidates = [
        os.path.join(PROJECT_DIR, "web-app"),
        os.path.join(os.path.dirname(PROJECT_DIR), "web-app"),
        os.path.join(os.path.expanduser("~"), "Solo_bot", "web-app"),
    ]
    for path in candidates:
        if (
            os.path.isdir(path)
            and os.path.isfile(os.path.join(path, "package.json"))
            and os.path.isfile(os.path.join(path, "Dockerfile"))
        ):
            return path
    return None


def _copy_local_web_source(src: str, dst: str) -> bool:
    subprocess.run(["rm", "-rf", dst], check=False)
    if shutil.which("rsync"):
        result = subprocess.run(
            [
                "rsync",
                "-a",
                "--exclude=node_modules",
                "--exclude=.next",
                "--exclude=.git",
                "--exclude=.env",
                "--exclude=.env.local",
                "--exclude=.env.production",
                "--exclude=logs",
                "--exclude=.deploy",
                "--exclude=.data",
                "--exclude=.claude",
                f"{src}/",
                f"{dst}/",
            ],
            check=False,
        )
        if result.returncode != 0:
            return False
    else:
        try:
            shutil.copytree(
                src,
                dst,
                ignore=shutil.ignore_patterns(
                    "node_modules",
                    ".next",
                    ".git",
                    ".env",
                    ".env.local",
                    ".env.production",
                    "logs",
                    ".deploy",
                    ".data",
                    ".claude",
                ),
            )
        except Exception:
            return False
    return os.path.isfile(os.path.join(dst, "package.json"))


def _prepare_web_sources(dst: str) -> bool:
    local = _find_local_web_source()
    if local:
        console.print(f"[cyan]Найден локальный web-app: {local}[/cyan]")
        if _copy_local_web_source(local, dst):
            console.print("[green]✓ Локальные исходники скопированы[/green]")
            return True
        console.print("[yellow]Не удалось скопировать локальные исходники.[/yellow]")

    console.print("[red]❌ Локальные исходники web-app не найдены и не удалось использовать.[/red]")
    console.print(
        "[yellow]Проверьте, что пакет ghcr.io/vladless/solo-brick публичен, либо что рядом с CLI лежит каталог web-app.[/yellow]"
    )
    return False


def _pull_web_image(tag: str) -> bool:
    image = _web_image(tag)
    console.print(f"[cyan]Загрузка готового образа: {image}[/cyan]")
    result = subprocess.run(
        ["docker", "pull", image],
        check=False,
    )
    return result.returncode == 0


def _build_web_image(src_dir: str, tag: str) -> bool:
    if not os.path.isfile(os.path.join(src_dir, "package.json")):
        if not _prepare_web_sources(src_dir):
            return False
    if not os.path.isfile(os.path.join(src_dir, "Dockerfile")):
        console.print("[red]❌ В исходниках нет Dockerfile[/red]")
        return False
    console.print("[cyan]Сборка Docker-образа (несколько минут)...[/cyan]")
    result = subprocess.run(
        ["docker", "build", "-t", _web_image(tag), "."],
        cwd=src_dir,
        check=False,
    )
    if result.returncode != 0:
        console.print("[red]❌ Ошибка сборки. Проверьте логи выше.[/red]")
        return False
    return True


def _ensure_web_image(src_dir: str, tag: str, force_pull: bool = False) -> bool:
    if _pull_web_image(tag):
        console.print(f"[green]✓ Образ {_web_image(tag)} получен из GHCR[/green]")
        return True

    console.print("[yellow]Не удалось скачать образ из GHCR. Пробую локальную сборку.[/yellow]")
    return _build_web_image(src_dir, tag)

def _ensure_rpc_module() -> bool:
    try:
        import core.rpc  # noqa: F401
        return True
    except ImportError:
        pass
    _sync_rpc_files()
    try:
        import core.rpc  # noqa: F401
        return True
    except ImportError:
        return False


def _check_feature(name: str) -> bool:
    _ensure_rpc_module()
    try:
        from core.rpc import check_feature

        return check_feature(name)
    except Exception:
        if name == "web":
            return True
        return False


def _authorize_web_install(code: str, password: str) -> bool:
    _ensure_rpc_module()
    try:
        from core.rpc import authorize_web_install
        return authorize_web_install(code, password, out=console.print)
    except Exception:
        pass

    if os.path.exists(VENV_PYTHON):
        script = (
            "import json, re, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "creds = json.loads(sys.stdin.read())\n"
            "from core.rpc import authorize_web_install\n"
            "def out(msg):\n"
            "    print(re.sub(r'\\[/?[a-zA-Z #0-9]+\\]', '', str(msg)), flush=True)\n"
            "ok = authorize_web_install(creds['code'], creds['password'], out=out)\n"
            "sys.exit(0 if ok else 1)\n"
        )
        try:
            result = subprocess.run(
                [VENV_PYTHON, "-c", script, PROJECT_DIR],
                input=json.dumps({"code": code, "password": password}),
                text=True,
                cwd=PROJECT_DIR,
            )
            return result.returncode == 0
        except Exception:
            pass

    console.print("[red]❌ Не удалось загрузить модуль проверки лицензии[/red]")
    console.print(
        "[yellow]Запустите CLI через Python 3.12, или установите бот в этой папке для использования его venv.[/yellow]"
    )
    return False


def _ensure_docker():
    """Проверяет/устанавливает Docker."""
    if shutil.which("docker"):
        try:
            subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            return True
        except subprocess.CalledProcessError:
            console.print("[yellow]Docker установлен, но не запущен.[/yellow]")
            subprocess.run(["sudo", "systemctl", "start", "docker"], check=False)
            return True
    console.print("[cyan]Установка Docker...[/cyan]")
    try:
        subprocess.run("curl -fsSL https://get.docker.com | sh", shell=True, check=True)
        subprocess.run(["sudo", "systemctl", "enable", "docker"], check=False)
        subprocess.run(["sudo", "systemctl", "start", "docker"], check=False)
        return True
    except subprocess.CalledProcessError:
        console.print("[red]❌ Не удалось установить Docker.[/red]")
        return False


def _port_owner(port: int) -> str | None:
    try:
        result = subprocess.run(
            ["ss", "-ltnp", f"sport = :{port}"],
            capture_output=True, text=True, timeout=3,
        )
        out = (result.stdout or "").strip()
        if result.returncode == 0 and "LISTEN" in out:
            lines = [l for l in out.splitlines() if "LISTEN" in l]
            if lines:
                match = re.search(r'users:\(\("([^"]+)"', lines[0])
                return match.group(1) if match else "занят"
    except Exception:
        return None
    return None


def _check_http_ports_free() -> bool:
    conflicts = []
    for port in (80, 443):
        owner = _port_owner(port)
        if owner and owner != "nginx":
            conflicts.append(f"{port} → {owner}")
    if not conflicts:
        return True
    console.print(
        Panel(
            "[white]Порты HTTP/HTTPS заняты не-nginx процессом:[/white]\n"
            + "\n".join(f"  • [bold]{c}[/bold]" for c in conflicts)
            + "\n\n[white]Остановите конфликтующий процесс и повторите.[/white]",
            border_style="red",
            title="[bold red]Порты заняты[/bold red]",
            padding=(1, 2),
        )
    )
    return False


def _ensure_nginx():
    """Проверяет/устанавливает nginx."""
    if not _check_http_ports_free():
        return False
    if shutil.which("nginx"):
        return True
    try:
        run_with_status(["sudo", "apt-get", "update"], status_text="apt update", check=True)
        run_with_status(
            ["sudo", "apt-get", "install", "-y", "nginx"],
            status_text="Установка nginx",
            check=True,
        )
        subprocess.run(["sudo", "systemctl", "enable", "nginx"], check=False)
        subprocess.run(["sudo", "systemctl", "start", "nginx"], check=False)
        return True
    except subprocess.CalledProcessError:
        console.print("[yellow]Не удалось установить nginx автоматически.[/yellow]")
        return False


def _public_ip() -> str | None:
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            response = http_get(url, timeout=5)
            ip = (response.text or "").strip()
            if response.status_code == 200 and ip:
                return ip
        except Exception:
            continue
    return None


def _resolve_domain_ip(domain: str) -> str | None:
    try:
        import socket
        infos = socket.getaddrinfo(domain, None, socket.AF_INET, socket.SOCK_STREAM)
        if infos:
            return infos[0][4][0]
    except Exception:
        return None
    return None


def _dns_precheck(domain: str) -> bool:
    console.print(f"[dim]Проверяю DNS для {domain}...[/dim]")
    resolved = _resolve_domain_ip(domain)
    if not resolved:
        console.print(
            Panel(
                f"[white]DNS-имя [bold]{domain}[/bold] не резолвится в IP.[/white]\n"
                "[white]Добавьте A-запись в DNS и дождитесь пропагации (5–30 мин).[/white]",
                border_style="red",
                title="[bold red]DNS не настроен[/bold red]",
                padding=(1, 2),
            )
        )
        return False
    local = _public_ip()
    if local and resolved != local:
        console.print(
            Panel(
                f"[white]DNS [bold]{domain}[/bold] указывает на [yellow]{resolved}[/yellow],[/white]\n"
                f"[white]а этот сервер имеет IP [yellow]{local}[/yellow].[/white]\n\n"
                "[white]Поправьте A-запись, дождитесь пропагации и повторите.[/white]",
                border_style="red",
                title="[bold red]DNS указывает не на этот сервер[/bold red]",
                padding=(1, 2),
            )
        )
        return False
    console.print(f"[green]✓ DNS ок: {domain} → {resolved}[/green]")
    return True


def _wait_for_web_container(web_port: int, timeout_sec: int = 60) -> bool:
    import socket

    deadline = time_mod.time() + timeout_sec
    with console.status(f"[bold cyan]Ожидание контейнера на :{web_port}...[/bold cyan]", spinner="dots"):
        while time_mod.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", web_port), timeout=2):
                    return True
            except Exception:
                sleep(2)
    return False


def _check_bot_api_reachable(api_url: str) -> bool:
    probe = api_url.rstrip("/") + "/health"
    console.print(f"[dim]Проверяю доступность API: {probe}[/dim]")
    try:
        response = http_get(probe, timeout=5)
        if 200 <= response.status_code < 500:
            console.print(f"[green]✓ API отвечает ({response.status_code})[/green]")
            return True
        console.print(f"[yellow]API ответил {response.status_code}[/yellow]")
        return False
    except Exception as e:
        console.print(
            Panel(
                f"[white]API [bold]{api_url}[/bold] недоступен: {e}[/white]\n\n"
                f"[white]Проверьте: DNS, nginx, SSL, firewall, бот запущен.[/white]",
                border_style="red",
                title="[bold red]Bot API недоступен[/bold red]",
                padding=(1, 2),
            )
        )
        return False


def _web_nginx_snippet(domain: str, web_port: int) -> str:
    """Locations для веб-приложения — можно вставить в существующий server-блок."""
    return f"""    # --- Solo web-app ({domain}) ---
    client_max_body_size 100m;

    location /_next/static/ {{
        proxy_pass http://127.0.0.1:{web_port};
        proxy_cache_valid 200 365d;
        add_header Cache-Control "public, immutable, max-age=31536000";
    }}

    location = /sw.js {{
        proxy_pass http://127.0.0.1:{web_port};
        add_header Cache-Control "no-cache";
    }}

    location / {{
        proxy_pass http://127.0.0.1:{web_port};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 90s;
    }}
    # --- /Solo web-app ---"""


def _print_manual_nginx_hint(domain: str, web_port: int) -> None:
    snippet = _web_nginx_snippet(domain, web_port)
    console.print(
        Panel(
            "[white]CLI не трогал ваш nginx. Вставьте блоки ниже в существующий\n"
            f"[cyan]server {{ ... server_name {domain}; ... }}[/cyan] (HTTPS-блок),\n"
            "рядом с другими [cyan]location[/cyan] бота, и перезагрузите nginx:\n"
            "[dim]sudo nginx -t && sudo systemctl reload nginx[/dim]",
            border_style="yellow",
            title="[bold yellow]Ручная настройка nginx[/bold yellow]",
            padding=(1, 2),
        )
    )
    console.print(f"\n[dim]---8<--- snippet ---8<---[/dim]\n{snippet}\n[dim]---8<--- end ---8<---[/dim]\n")


def _nginx_domain_conflict(domain: str) -> str | None:
    """Возвращает путь конфига, в котором уже объявлен server_name = domain."""
    sites_dir = "/etc/nginx/sites-enabled"
    if not os.path.isdir(sites_dir):
        return None
    try:
        for entry in os.listdir(sites_dir):
            path = os.path.join(sites_dir, entry)
            try:
                real = os.path.realpath(path)
                with open(real) as f:
                    text = f.read()
            except Exception:
                continue
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped.startswith("server_name"):
                    continue
                names = stripped.rstrip(";").split()[1:]
                if domain in names:
                    return real
    except Exception:
        return None
    return None


def _setup_nginx(domain, web_port=3000):
    """Настраивает отдельный nginx server-блок для веб-приложения."""
    conf = f"""server {{
    listen 80;
    server_name {domain};
{_web_nginx_snippet(domain, web_port)}
}}"""
    conf_path = f"/etc/nginx/sites-available/solo-{domain}"
    enabled_path = f"/etc/nginx/sites-enabled/solo-{domain}"
    try:
        with open("/tmp/_solo_nginx.conf", "w") as f:
            f.write(conf)
        subprocess.run(["sudo", "cp", "/tmp/_solo_nginx.conf", conf_path], check=True)
        subprocess.run(["sudo", "ln", "-sf", conf_path, enabled_path], check=True)
        subprocess.run(["sudo", "nginx", "-t"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["sudo", "systemctl", "reload", "nginx"], check=True)
        return True
    except subprocess.CalledProcessError:
        console.print("[yellow]Не удалось настроить nginx.[/yellow]")
        return False


def _setup_ssl(domain):
    """Получает SSL сертификат через certbot."""
    if not _dns_precheck(domain):
        return False
    if not shutil.which("certbot"):
        try:
            run_with_status(
                ["sudo", "apt-get", "install", "-y", "certbot", "python3-certbot-nginx"],
                status_text="Установка certbot",
                check=True,
            )
        except subprocess.CalledProcessError:
            console.print("[yellow]Не удалось установить certbot.[/yellow]")
            return False
    try:
        subprocess.run(
            [
                "sudo", "certbot", "--nginx", "-d", domain,
                "--non-interactive", "--agree-tos",
                "--register-unsafely-without-email", "--redirect",
            ],
            check=True,
        )
        return True
    except subprocess.CalledProcessError:
        console.print(
            Panel(
                f"[white]Сертификат не удалось выпустить. Причина обычно —[/white]\n"
                f"[white]DNS [bold]{domain}[/bold] ещё не указывает на сервер, либо порт 80/443 закрыт.[/white]\n\n"
                f"[yellow]Сайт без SSL открывать нельзя.[/yellow] После пропагации DNS:\n"
                f"  1. [bold]dig +short {domain}[/bold]\n"
                f"  2. [bold]sudo certbot --nginx -d {domain}[/bold]",
                border_style="yellow",
                title="[bold yellow]⚠ SSL отложен[/bold yellow]",
                padding=(1, 2),
            )
        )
        return False


def install_website():
    """Устанавливает веб-приложение (сайт) через Docker."""
    if not _check_feature("web"):
        console.print("[yellow]Эта функция недоступна в текущей версии. Обновите бота.[/yellow]")
        return

    show_website_version_banner()
    console.print(
        Panel(
            "[white]CLI установит Docker, скачает готовый образ сайта, настроит nginx и SSL.\n"
            "Бэкенд (бот) может быть на этом же сервере или на другом.[/white]",
            border_style="green",
            title="[bold green]Установка веб-приложения[/bold green]",
            padding=(1, 2),
        )
    )

    console.print(
        Panel(
            "[bold cyan]Вариант A:[/bold cyan] Бот и сайт на одном сервере\n"
            "  → API вызывается локально внутри сервера\n\n"
            "[bold cyan]Вариант B:[/bold cyan] Сайт на отдельном сервере\n"
            "  → API вызывается по домену (например api.example.com)\n"
            "  → На сервере бота должен быть nginx+SSL перед API и открыт порт 443",
            border_style="dim",
            title="[dim]Варианты размещения[/dim]",
            padding=(1, 2),
        )
    )

    if not safe_confirm("[bold green]Начать установку сайта?[/bold green]", default=True):
        return

    console.print("\n[bold][0/5] Авторизация[/bold]")
    console.print("[dim]Введите логин и пароль от вашего кабинета на сайте Solo.[/dim]")
    console.print("[dim]Данные используются только для проверки лицензии и нигде не сохраняются.[/dim]\n")

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        lc_code = safe_prompt("[cyan]Логин (Client Code)[/cyan]")
        if not lc_code or not lc_code.strip():
            console.print("[red]Логин обязателен.[/red]")
            return
        try:
            import getpass
            lc_pass = getpass.getpass("  Пароль: ")
        except Exception:
            lc_pass = safe_prompt("[cyan]Пароль[/cyan]")
        if not lc_pass or not lc_pass.strip():
            console.print("[red]Пароль обязателен.[/red]")
            return

        ok = _authorize_web_install(lc_code.strip(), lc_pass.strip())
        lc_code = None
        lc_pass = None
        if ok:
            break
        if attempt < max_attempts:
            console.print(f"[yellow]Попытка {attempt}/{max_attempts} не прошла.[/yellow]")
            if not safe_confirm("[cyan]Повторить ввод?[/cyan]", default=True):
                return
        else:
            console.print(
                "[red]Исчерпаны попытки авторизации. Проверьте логин/пароль на сайте Solo и повторите установку.[/red]"
            )
            return

    console.print("\n[bold][1/5] Docker[/bold]")
    if not _ensure_docker():
        return

    console.print("\n[bold][2/5] Настройки[/bold]\n")

    console.print(
        "[dim]Домен, по которому будет открываться сайт.\nDNS (A-запись) должна уже указывать на IP этого сервера.[/dim]"
    )
    domain = safe_prompt("[cyan]Домен сайта[/cyan] (например vpn.example.com)")
    if not domain or not domain.strip():
        console.print("[red]Домен обязателен.[/red]")
        return
    domain = domain.strip()

    try:
        from config import API_PORT as _BOT_API_PORT

        _bot_api_port = int(_BOT_API_PORT)
    except Exception:
        _bot_api_port = 3004

    console.print("\n[dim]Где запущен бот?[/dim]")
    bot_location = safe_prompt(
        "[cyan]Размещение бота[/cyan]: [1] на этом же сервере  [2] на другом сервере",
        choices=["1", "2"],
        default="1",
        show_choices=False,
    )
    api_domain = ""
    if bot_location == "1":
        api_url = f"http://host.docker.internal:{_bot_api_port}"
        console.print(
            Panel(
                f"[white]API: [bold]{api_url}[/bold] (через docker host-gateway)[/white]\n\n"
                f"[dim]Требования к боту на этом сервере:[/dim]\n"
                f"  • Бот запущен на хосте и слушает [bold]0.0.0.0:{_bot_api_port}[/bold]\n"
                f"  • В config.py: [bold]API_HOST=\"0.0.0.0\"[/bold], [bold]API_PORT={_bot_api_port}[/bold]",
                border_style="dim",
                title="[dim]Размещение: один сервер[/dim]",
                padding=(1, 2),
            )
        )
    else:
        console.print(
            "\n[dim]Домен, по которому web-контейнер будет ходить на API бота.\nНа сервере бота должен стоять nginx+SSL перед портом API.[/dim]"
        )
        api_domain = safe_prompt("[cyan]Домен API бота[/cyan] (например api.example.com)")
        if not api_domain or not api_domain.strip():
            console.print("[red]Домен API обязателен.[/red]")
            return
        api_domain = api_domain.strip().replace("https://", "").replace("http://", "").strip("/")
        api_url = f"https://{api_domain}"
        console.print(
            Panel(
                f"[white]API: [bold]{api_url}[/bold][/white]\n\n"
                f"[yellow]На сервере бота настройте:[/yellow]\n"
                f"  • nginx: [bold]https://{api_domain}[/bold] → [bold]http://127.0.0.1:{_bot_api_port}[/bold]\n"
                f"  • SSL сертификат (certbot --nginx -d {api_domain})\n"
                f"  • config.py: [bold]API_HOST=\"0.0.0.0\"[/bold], [bold]API_PORT={_bot_api_port}[/bold]\n"
                f"  • Опционально firewall: порт {_bot_api_port} открыт только с IP web-сервера",
                border_style="yellow",
                title="[bold yellow]Размещение: разные серверы[/bold yellow]",
                padding=(1, 2),
            )
        )
        if not safe_confirm("[cyan]Всё настроено на сервере бота?[/cyan]", default=True):
            console.print("[yellow]Настройте сервер бота и повторите установку.[/yellow]")
            return
        if not _check_bot_api_reachable(api_url):
            if not safe_confirm(
                "[yellow]API недоступен. Продолжить всё равно (сайт не заработает без API)?[/yellow]",
                default=False,
            ):
                return

    console.print(
        "\n[dim]Внутренний порт, на котором запустится сайт.\nNginx проксирует на него запросы. Менять нужно только если порт занят.[/dim]"
    )
    web_port = safe_prompt("[cyan]Порт сайта[/cyan]", default="3000")

    console.print(
        "\n[dim]Для push-уведомлений на сайте (колокольчик).\nМожно сгенерировать ключи прямо сейчас (приватный ключ печатается — сохраните его).\nЕсли push не нужны — пропустите.[/dim]"
    )
    vapid_key = ""
    vapid_action = safe_prompt(
        "[cyan]VAPID ключи[/cyan]: [1] сгенерировать  [2] ввести публичный ключ вручную  [3] пропустить",
        choices=["1", "2", "3"],
        default="1",
        show_choices=False,
    )
    if vapid_action == "1":
        pair = _generate_vapid_keys()
        if pair is None:
            console.print("[yellow]Не удалось сгенерировать (нет cryptography). Введите вручную или пропустите.[/yellow]")
            vapid_key = safe_prompt("[cyan]VAPID Public Key[/cyan] (Enter — пропустить)", default="")
        else:
            vapid_pub, vapid_priv = pair
            vapid_key = vapid_pub
            vapid_file = os.path.expanduser(f"~/.solobot_vapid_{domain}.txt")
            py_snippet = (
                f'VAPID_PUBLIC_KEY = "{vapid_pub}"\n'
                f'VAPID_PRIVATE_KEY = "{vapid_priv}"\n'
                f'VAPID_CLAIMS_EMAIL = "mailto:admin@{domain}"\n'
            )
            vapid_saved = True
            try:
                with open(vapid_file, "w", encoding="utf-8") as f:
                    f.write(
                        f"# VAPID keypair for {domain}\n"
                        f"# Сгенерировано: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"# Вставьте эти строки КАК ЕСТЬ в config.py бота и перезапустите.\n\n"
                        + py_snippet
                    )
                os.chmod(vapid_file, 0o600)
            except Exception:
                vapid_saved = False
            saved_hint = (
                f"[green]✓ Ключи сохранены в файл:[/green] [bold]{vapid_file}[/bold] [dim](chmod 600)[/dim]"
                if vapid_saved
                else "[red]⚠ Не удалось записать файл — скопируйте строки ниже СЕЙЧАС.[/red]"
            )
            console.print("\n[bold yellow]VAPID keypair[/bold yellow]")
            console.print(saved_hint)
            console.print("[dim]Скопируйте строки ниже КАК ЕСТЬ (с кавычками) в config.py бота:[/dim]\n")
            console.print(py_snippet)
            console.print(
                "[yellow]Публичный ключ CLI пропишет в web .env автоматически.\n"
                "Приватный ключ и email добавьте в config.py бота и перезапустите.[/yellow]\n"
            )
    elif vapid_action == "2":
        vapid_key = safe_prompt("[cyan]VAPID Public Key[/cyan]", default="")

    console.print(
        "\n[dim]Cloudflare Turnstile защищает формы логина от ботов.\nПолучите ключ на dash.cloudflare.com → Turnstile.\nЕсли не нужно — пропустите, формы будут работать без CAPTCHA.[/dim]"
    )
    turnstile_key = safe_prompt("[cyan]Turnstile Site Key[/cyan] (Enter — пропустить)", default="")

    console.print(
        "\n[dim]Username Telegram-бота (без @) для кнопки «Войти через Telegram» на сайте.\nЕсли не нужно — пропустите.[/dim]"
    )
    tg_bot_username = safe_prompt("[cyan]Telegram Bot Username[/cyan] (Enter — пропустить)", default="")

    console.print(
        "\n[dim]Для отправки email-кодов (логин, подтверждение, сброс пароля).\nЕсли не нужно — пропустите, регистрация по email+паролю будет работать без этого.[/dim]"
    )
    smtp_host = safe_prompt("[cyan]SMTP Host[/cyan] (Enter — пропустить)", default="")
    smtp_user = ""
    smtp_password = ""
    smtp_from = ""
    if smtp_host:
        smtp_user = safe_prompt("[cyan]SMTP User[/cyan]", default="")
        try:
            import getpass

            smtp_password = getpass.getpass("  SMTP Password: ")
        except Exception:
            smtp_password = safe_prompt("[cyan]SMTP Password[/cyan]", default="")
        smtp_from = safe_prompt("[cyan]Email From[/cyan]", default=smtp_user)

    web_tag = _ask_web_tag(default=_get_saved_web_tag())

    setup_ssl = safe_confirm("[cyan]Установить SSL (Let's Encrypt)?[/cyan]", default=True)

    site_url = f"https://{domain}" if setup_ssl else f"http://{domain}"

    console.print(f"\n  Домен:   [green]{domain}[/green]")
    console.print(f"  Backend: [green]{api_url}[/green]")
    console.print(f"  Канал:   [green]{web_tag}[/green]")
    console.print(f"  SSL:     [green]{'Да' if setup_ssl else 'Нет'}[/green]")

    if not safe_confirm("\n[yellow]Всё верно?[/yellow]", default=True):
        return

    console.print("\n[bold][3/5] Запуск сайта[/bold]")
    os.makedirs(WEB_DIR, exist_ok=True)

    from urllib.parse import urlparse

    parsed_api = urlparse(api_url)
    api_port_from_url = ""
    if parsed_api.port is not None:
        api_port_from_url = str(parsed_api.port)
    elif parsed_api.scheme == "https":
        api_port_from_url = "443"
    elif parsed_api.scheme == "http":
        api_port_from_url = "80"

    env_path = os.path.join(WEB_DIR, ".env")
    plugin_builder_token, plugin_builder_token_is_new = _ensure_plugin_builder_token(env_path)
    with open(env_path, "w") as f:
        f.write(f"API_URL={api_url}\n")
        f.write(f"API_BASE_URL={api_url}\n")
        f.write(f"NEXT_PUBLIC_API_URL={api_url}\n")
        f.write(f"NEXT_PUBLIC_API_BASE_URL={api_url}\n")
        f.write(f"NEXT_PUBLIC_API_PORT={api_port_from_url}\n")
        f.write(f"NEXT_PUBLIC_SITE_URL={site_url}\n")
        f.write(f"NEXT_PUBLIC_VAPID_PUBLIC_KEY={vapid_key}\n")
        f.write(f"NEXT_PUBLIC_TURNSTILE_SITE_KEY={turnstile_key}\n")
        f.write("NEXT_PUBLIC_LOG_LEVEL=info\n")
        f.write(f"WEB_PORT={web_port}\n")
        f.write(f"PLUGIN_BUILDER_TOKEN={plugin_builder_token}\n")
        if tg_bot_username:
            f.write(f"NEXT_PUBLIC_TELEGRAM_BOT_USERNAME={tg_bot_username}\n")
        if smtp_host:
            f.write(f"EMAIL_SMTP_HOST={smtp_host}\n")
            f.write("EMAIL_SMTP_PORT=465\n")
            f.write(f"EMAIL_SMTP_USER={smtp_user}\n")
            f.write(f"EMAIL_SMTP_PASSWORD={smtp_password}\n")
            f.write(f"EMAIL_FROM={smtp_from}\n")

    if plugin_builder_token_is_new:
        console.print(
            Panel(
                f"[bold]PLUGIN_BUILDER_TOKEN[/bold] = {plugin_builder_token}\n\n"
                "[yellow]Токен защищает plugin-builder API от посторонних.\n"
                "Сохраните, если планируете использовать внешний билд-воркер для custom-elements —\n"
                "воркер должен слать этот же токен в заголовке Authorization: Bearer <token>.[/yellow]",
                border_style="yellow",
                title="[bold yellow]PLUGIN_BUILDER_TOKEN — сгенерирован[/bold yellow]",
                padding=(1, 2),
            )
        )

    src_dir = os.path.join(WEB_DIR, "src")
    if not _ensure_web_image(src_dir, web_tag):
        return
    _save_web_tag(web_tag)

    compose_path = os.path.join(WEB_DIR, "docker-compose.yml")
    with open(compose_path, "w") as f:
        f.write(f"""name: {WEB_CONTAINER_NAME}

services:
  web:
    image: {_web_image(web_tag)}
    container_name: {WEB_CONTAINER_NAME}
    ports:
      - "127.0.0.1:{web_port}:3000"
    env_file:
      - .env
    restart: unless-stopped
    extra_hosts:
      - "host.docker.internal:host-gateway"
    healthcheck:
      test: ["CMD", "node", "-e", "fetch('http://127.0.0.1:3000/api/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
    volumes:
      - ./logs:/app/logs
""")

    _ensure_web_logs_dir()
    console.print("[cyan]Запуск контейнера...[/cyan]")
    subprocess.run(["docker", "compose", "up", "-d"], cwd=WEB_DIR, check=True)

    if _wait_for_web_container(int(web_port), timeout_sec=60):
        console.print(f"[green]✅ Контейнер запущен и отвечает на порту {web_port}[/green]")
    else:
        console.print(
            Panel(
                f"[white]Контейнер запущен, но не отвечает на http://127.0.0.1:{web_port} за 60 сек.[/white]\n"
                f"[white]Проверьте логи:[/white]\n"
                f"  [bold]cd {WEB_DIR} && docker compose logs -f[/bold]",
                border_style="yellow",
                title="[bold yellow]⚠ Healthcheck не прошёл[/bold yellow]",
                padding=(1, 2),
            )
        )

    console.print("\n[bold][4/5] Nginx[/bold]")
    nginx_configured = False
    conflict_path = _nginx_domain_conflict(domain)
    if conflict_path:
        console.print(
            f"[yellow]⚠ На домене [bold]{domain}[/bold] уже есть nginx-конфиг:[/yellow] {conflict_path}\n"
            "[yellow]Автонастройка создала бы второй server-блок — это может конфликтовать с ботом.[/yellow]"
        )
        do_auto = safe_confirm(
            "[cyan]Всё равно создать отдельный server-блок?[/cyan] (Нет — покажу snippet для ручной вставки)",
            default=False,
        )
    else:
        do_auto = safe_confirm("[cyan]Настроить nginx автоматически?[/cyan]", default=True)

    if do_auto:
        if _ensure_nginx() and _setup_nginx(domain, int(web_port)):
            console.print(f"[green]✅ nginx настроен для {domain}[/green]")
            nginx_configured = True
        else:
            console.print("[yellow]Авто-настройка не удалась, покажу snippet.[/yellow]")
            _print_manual_nginx_hint(domain, int(web_port))
    else:
        _print_manual_nginx_hint(domain, int(web_port))

    console.print("\n[bold][5/5] SSL[/bold]")
    ssl_deferred = False
    if setup_ssl and not nginx_configured:
        console.print("[yellow]SSL пропущен: автоконфигурация certbot --nginx требует автонастройки nginx.[/yellow]")
        console.print("[dim]После ручной правки nginx запустите: sudo certbot --nginx -d " + domain + "[/dim]")
        ssl_deferred = True
        setup_ssl = False
    if setup_ssl:
        if _setup_ssl(domain):
            console.print("[green]✅ SSL сертификат установлен[/green]")
        else:
            ssl_deferred = True
    elif not ssl_deferred:
        console.print("[dim]SSL пропущен[/dim]")

    smtp_hint = ""
    if not smtp_host:
        smtp_hint = "\n\n[yellow]⚠ SMTP не настроен — вход по email-коду и сброс пароля не будут работать.\n  Настройте позже через: меню → Управление сайтом → Изменить настройки[/yellow]"

    bot_note = (
        f"\n\n[yellow]⚠ На сервере бота установите в [bold]config.py[/bold]:[/yellow]\n"
        f"  SITE_URL = \"{site_url}\"\n"
        f"[dim]  (используется для TG WebApp-кнопок и gift-ссылок)[/dim]\n"
        f"[dim]  После правки перезапустите бота.[/dim]"
    )

    if ssl_deferred:
        header = (
            f"[bold yellow]Сайт собран, но SSL ещё не получен.[/bold yellow]\n"
            f"[white]Откроется по [bold]{site_url}[/bold] только после выпуска сертификата.[/white]\n\n"
            f"[cyan]Что сделать:[/cyan]\n"
            f"  1. [bold]dig +short {domain}[/bold] — должен вернуть IP этого сервера\n"
            f"  2. [bold]sudo certbot --nginx -d {domain}[/bold]"
        )
        border = "yellow"
        title = "[bold yellow]⚠ Установка почти завершена[/bold yellow]"
    else:
        header = f"[bold green]Сайт доступен: {site_url}[/bold green]"
        border = "green"
        title = "[bold green]✅ Установка завершена[/bold green]"

    console.print(
        Panel(
            f"{header}{smtp_hint}{bot_note}\n\n"
            f"[white]Управление:[/white]\n"
            f"  cd {WEB_DIR}\n"
            f"  docker compose logs -f       [dim]— логи[/dim]\n"
            f"  docker compose restart       [dim]— перезапуск[/dim]\n"
            f"  docker compose down          [dim]— остановка[/dim]\n"
            f"  nano .env                    [dim]— настройки[/dim]",
            border_style=border,
            title=title,
            padding=(1, 2),
        )
    )


def _read_env_domain() -> str | None:
    env_path = os.path.join(WEB_DIR, ".env")
    if not os.path.isfile(env_path):
        return None
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("NEXT_PUBLIC_SITE_URL="):
                    url = line.split("=", 1)[1].strip()
                    return url.replace("https://", "").replace("http://", "").strip("/") or None
    except Exception:
        return None
    return None


def _web_container_status() -> str:
    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "--format", "{{.State}}"],
            cwd=WEB_DIR, capture_output=True, text=True, timeout=5,
        )
        states = [s.strip() for s in (result.stdout or "").splitlines() if s.strip()]
        if not states:
            return "[dim]не запущен[/dim]"
        running = sum(1 for s in states if s.lower() == "running")
        total = len(states)
        if running == total:
            return f"[green]running ({running}/{total})[/green]"
        return f"[yellow]{running}/{total} running[/yellow]"
    except Exception:
        return "[dim]статус неизвестен[/dim]"


def uninstall_website():
    if not os.path.exists(WEB_DIR):
        console.print("[yellow]Сайт не установлен (папка отсутствует).[/yellow]")
        return

    domain = _read_env_domain()
    console.print(
        Panel(
            f"[bold red]Вы собираетесь полностью удалить сайт.[/bold red]\n\n"
            f"[white]Будет удалено:[/white]\n"
            f"  • Docker-контейнеры и volumes (данные кабинета)\n"
            f"  • Docker-образ {_web_image(_get_saved_web_tag())}\n"
            f"  • Папка проекта [bold]{WEB_DIR}[/bold] (.env, логи)\n"
            + (f"  • Nginx-конфиг [bold]/etc/nginx/sites-*/solo-{domain}[/bold]\n" if domain else "")
            + (f"  • SSL-сертификат для [bold]{domain}[/bold]\n" if domain else "")
            + "\n[yellow]Действие необратимо. Рекомендуется сделать бэкап БД заранее.[/yellow]",
            border_style="red",
            title="[bold red]⚠ Удаление сайта[/bold red]",
            padding=(1, 2),
        )
    )

    if not safe_confirm("[bold red]Продолжить удаление?[/bold red]", default=False):
        return
    confirm_text = safe_prompt(
        "[red]Введите [bold]DELETE[/bold] заглавными чтобы подтвердить[/red]",
        default="",
    )
    if confirm_text.strip() != "DELETE":
        console.print("[yellow]Удаление отменено.[/yellow]")
        return

    if os.path.exists(os.path.join(WEB_DIR, "docker-compose.yml")):
        run_with_status(
            ["docker", "compose", "down", "-v", "--remove-orphans"],
            status_text="Остановка и удаление контейнеров",
            cwd=WEB_DIR,
        )

    try:
        tag = _get_saved_web_tag()
        run_with_status(
            ["docker", "image", "rm", "-f", _web_image(tag)],
            status_text=f"Удаление образа {_web_image(tag)}",
        )
    except Exception:
        pass

    if domain:
        for path in (
            f"/etc/nginx/sites-enabled/solo-{domain}",
            f"/etc/nginx/sites-available/solo-{domain}",
        ):
            subprocess.run(["sudo", "rm", "-f", path], check=False)
        subprocess.run(["sudo", "systemctl", "reload", "nginx"], check=False)

        if shutil.which("certbot"):
            subprocess.run(
                ["sudo", "certbot", "delete", "--non-interactive", "--cert-name", domain],
                check=False,
            )

    subprocess.run(["sudo", "rm", "-rf", WEB_DIR], check=False)

    if domain:
        vapid_file = os.path.expanduser(f"~/.solobot_vapid_{domain}.txt")
        if os.path.exists(vapid_file):
            try:
                os.remove(vapid_file)
            except Exception:
                pass

    console.print("[green]✅ Сайт удалён.[/green]")


def manage_website():
    """Меню управления сайтом."""
    if not _check_feature("web"):
        console.print("[yellow]Эта функция недоступна в текущей версии. Обновите бота.[/yellow]")
        return
    show_website_version_banner()
    if not os.path.exists(os.path.join(WEB_DIR, "docker-compose.yml")):
        console.print("[yellow]Сайт не установлен.[/yellow]")
        if safe_confirm("[green]Установить сейчас?[/green]", default=True):
            install_website()
        return

    tag = _get_saved_web_tag()
    status = _web_container_status()
    console.print(
        f"[bold]Образ:[/bold] [cyan]{_web_image(tag)}[/cyan]  [bold]Статус:[/bold] {status}"
    )

    table = Table(title="Управление сайтом", title_style="bold cyan", header_style="bold blue")
    table.add_column("№", justify="center", style="cyan", no_wrap=True)
    table.add_column("Действие", style="white")
    table.add_row("1", "Показать статус")
    table.add_row("2", "Показать логи")
    table.add_row("3", "Перезапустить")
    table.add_row("4", "Остановить")
    table.add_row("5", "Обновить (пересборка + restart)")
    table.add_row("6", "Изменить настройки (.env)")
    table.add_row("7", "Показать .env")
    table.add_row("8", "Переустановить")
    table.add_row("9", "[red]Удалить сайт[/red]")
    table.add_row("10", "Назад")
    console.print(table)

    choice = safe_prompt(
        "[bold blue]👉 Выберите действие[/bold blue]",
        choices=[str(i) for i in range(1, 11)], show_choices=False,
    )

    if choice == "1":
        subprocess.run(["docker", "compose", "ps"], cwd=WEB_DIR)
    elif choice == "2":
        subprocess.run(["docker", "compose", "logs", "--tail", "80", "-f"], cwd=WEB_DIR)
    elif choice == "3":
        subprocess.run(["docker", "compose", "restart"], cwd=WEB_DIR)
        console.print("[green]✅ Перезапущено[/green]")
    elif choice == "4":
        subprocess.run(["docker", "compose", "down"], cwd=WEB_DIR)
        console.print("[yellow]Сайт остановлен[/yellow]")
    elif choice == "5":
        src_dir = os.path.join(WEB_DIR, "src")
        show_website_version_banner()
        current_tag = _get_saved_web_tag()
        console.print(f"[dim]Текущий канал: [green]{current_tag}[/green][/dim]")
        web_tag = _ask_web_tag(default=current_tag)
        if not safe_confirm("[green]Продолжить обновление?[/green]", default=True):
            return
        console.print("[cyan]Обновление образа...[/cyan]")
        if not _ensure_web_image(src_dir, web_tag, force_pull=True):
            return
        compose_path = os.path.join(WEB_DIR, "docker-compose.yml")
        if web_tag != current_tag:
            try:
                with open(compose_path) as f:
                    compose = f.read()
                compose = compose.replace(
                    f"image: {_web_image(current_tag)}",
                    f"image: {_web_image(web_tag)}",
                    1,
                )
                with open(compose_path, "w") as f:
                    f.write(compose)
            except Exception as e:
                console.print(f"[yellow]Не удалось обновить docker-compose.yml: {e}[/yellow]")
        try:
            with open(compose_path) as f:
                compose = f.read()
            if "host.docker.internal:host-gateway" not in compose:
                patched = compose.replace(
                    "    restart: unless-stopped\n",
                    "    restart: unless-stopped\n"
                    "    extra_hosts:\n"
                    "      - \"host.docker.internal:host-gateway\"\n",
                    1,
                )
                if patched != compose:
                    with open(compose_path, "w") as f:
                        f.write(patched)
                    console.print("[dim]docker-compose.yml: добавлен extra_hosts: host.docker.internal → host-gateway[/dim]")
        except Exception as e:
            console.print(f"[yellow]Не удалось пропатчить extra_hosts в docker-compose.yml: {e}[/yellow]")
        _save_web_tag(web_tag)
        _ensure_web_logs_dir()
        subprocess.run(["docker", "compose", "up", "-d", "--force-recreate"], cwd=WEB_DIR)
        console.print(f"[green]✅ Обновлено до канала {web_tag}[/green]")
    elif choice == "6":
        env_path = os.path.join(WEB_DIR, ".env")
        editor = os.environ.get("EDITOR", "nano")
        subprocess.run([editor, env_path])
        if safe_confirm("[cyan]Перезапустить сайт с новыми настройками?[/cyan]", default=True):
            subprocess.run(["docker", "compose", "restart"], cwd=WEB_DIR)
    elif choice == "7":
        env_path = os.path.join(WEB_DIR, ".env")
        if not os.path.isfile(env_path):
            console.print(f"[yellow].env не найден: {env_path}[/yellow]")
        else:
            try:
                with open(env_path, encoding="utf-8") as f:
                    content = f.read()
                console.print(
                    Panel(
                        content or "[dim]пусто[/dim]",
                        border_style="cyan",
                        title=f"[bold cyan]{env_path}[/bold cyan]",
                        padding=(1, 2),
                    )
                )
            except Exception as e:
                console.print(f"[red]Не удалось прочитать .env: {e}[/red]")
    elif choice == "8":
        install_website()
    elif choice == "9":
        uninstall_website()


def show_update_menu():
    if IS_ROOT_DIR:
        console.print("[red]Обновление невозможно: бот находится в /root[/red]")
        console.print("[yellow]Перенесите бота в отдельную папку и повторите попытку[/yellow]")
        return

    table = Table(title="Выберите способ обновления", title_style="bold green")
    table.add_column("№", justify="center", style="cyan", no_wrap=True)
    table.add_column("Источник", style="white")
    table.add_row("1", "Обновить до BETA")
    table.add_row("2", "Обновить до релиза (релизы и патчи)")
    table.add_row("3", "Назад в меню")

    console.print(table)
    choice = safe_prompt("[bold blue]Введите номер[/bold blue]", choices=["1", "2", "3"])

    if choice == "1":
        update_from_beta()
    elif choice == "2":
        update_from_release()


_SEMVER_CLI_RE = re.compile(
    r"^v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:-(?P<pre>[0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


def _parse_solo_brick_semver(tag: str):
    match = _SEMVER_CLI_RE.match(tag.strip())
    if not match:
        return None
    major = int(match.group("major"))
    minor = int(match.group("minor"))
    patch = int(match.group("patch"))
    pre_raw = match.group("pre")
    if not pre_raw:
        return (major, minor, patch, 1, ())
    ids = []
    for part in pre_raw.split("."):
        if part.isdigit():
            ids.append((0, int(part)))
        else:
            ids.append((1, part))
    return (major, minor, patch, 0, tuple(ids))


def read_installed_solo_brick_version() -> str | None:
    """Версия установленного Solo-brick по лейблу докер-образа."""
    for image_ref in (f"ghcr.io/{GHCR_IMAGE}:latest", f"ghcr.io/{GHCR_IMAGE}"):
        try:
            result = subprocess.run(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    '{{index .Config.Labels "org.opencontainers.image.version"}}',
                    image_ref,
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            label = (result.stdout or "").strip()
            if result.returncode == 0 and label and label != "<no value>":
                return label
        except Exception:
            continue
    return None


def fetch_latest_ghcr_tag(image: str) -> str | None:
    try:
        token_resp = http_get(f"https://ghcr.io/token?scope=repository:{image}:pull", timeout=8)
        if token_resp.status_code != 200:
            return None
        token = str(token_resp.json().get("token") or "").strip()
        if not token:
            return None
        req = Request(
            f"https://ghcr.io/v2/{image}/tags/list",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        with urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        tags = payload.get("tags") or []
        versions = []
        for raw in tags:
            parsed = _parse_solo_brick_semver(str(raw))
            if parsed is not None:
                versions.append((parsed, str(raw)))
        if not versions:
            return None
        versions.sort(key=lambda item: item[0], reverse=True)
        return versions[0][1]
    except Exception:
        return None


def show_website_version_banner():
    """Короткий баннер с установленной и доступной версией сайта."""
    installed = read_installed_solo_brick_version()
    with console.status("[cyan]Проверка версии Solo-brick...[/cyan]"):
        latest = fetch_latest_ghcr_tag(GHCR_IMAGE)
    installed_str = installed if installed else "не определено"
    latest_str = latest if latest else "недоступно"
    tag = ""
    if installed and latest:
        cur = _parse_solo_brick_semver(installed)
        nxt = _parse_solo_brick_semver(latest)
        if cur and nxt and nxt > cur:
            tag = "  [bold yellow]⚡ Доступно обновление[/bold yellow]"
        elif cur and nxt:
            tag = "  [green]✅ Актуально[/green]"
    console.print(
        f"[dim]Solo-brick:[/dim] установлено [bold]{installed_str}[/bold] · доступно [bold]{latest_str}[/bold]{tag}"
    )


def show_menu():
    bot_installed = has_project_code()
    bot_runtime_ready = (
        bot_installed and os.path.exists(VENV_PYTHON) and is_service_exists(SERVICE_NAME)
    )

    def fmt(text: str, enabled: bool) -> str:
        return text if enabled else f"[dim]{text}  — нужен пункт 9[/dim]"

    table = Table(title="Solobot CLI v0.5.8", title_style="bold magenta", header_style="bold blue")
    table.add_column("№", justify="center", style="cyan", no_wrap=True)
    table.add_column("Операция", style="white")
    table.add_row("1", fmt("Запустить бота (systemd)", bot_runtime_ready))
    table.add_row("2", fmt("Запустить напрямую: venv/bin/python main.py", bot_installed and os.path.exists(VENV_PYTHON)))
    table.add_row("3", fmt("Перезапустить бота (systemd)", bot_runtime_ready))
    table.add_row("4", fmt("Остановить бота (systemd)", bot_runtime_ready))
    table.add_row("5", fmt("Показать логи (80 строк)", bot_runtime_ready))
    table.add_row("6", fmt("Показать статус", bot_runtime_ready))
    table.add_row("7", fmt("Обновить Solobot", bot_installed))
    table.add_row("8", "Восстановить из бэкапа")
    table.add_row("9", "Установить / переустановить бота")
    table.add_row("10", "🌐 Веб-сайт (установка / управление)")
    table.add_row("11", "Выход")
    console.print(table)


def main():
    os.chdir(PROJECT_DIR)
    auto_update_cli()
    print_logo()
    prompt_install_if_needed()
    try:
        while True:
            refresh_service_name()
            show_menu()
            choice = safe_prompt(
                "[bold blue]👉 Введите номер действия[/bold blue]",
                choices=[str(i) for i in range(1, 12)],
                show_choices=False,
            )
            if choice == "1":
                if is_service_exists(SERVICE_NAME):
                    subprocess.run(["sudo", "systemctl", "start", SERVICE_NAME])
                else:
                    console.print(f"[yellow]Служба {SERVICE_NAME} не найдена.[/yellow]")
                    if safe_confirm("[green]Установить бота и создать службу сейчас?[/green]", default=True):
                        install_bot()
            elif choice == "2":
                if not os.path.exists(VENV_PYTHON):
                    console.print("[yellow]Виртуальное окружение ещё не создано.[/yellow]")
                    if safe_confirm(
                        "[green]Подготовить окружение через автоматическую установку?[/green]", default=True
                    ):
                        install_bot()
                    continue
                try:
                    ver_out = subprocess.run(
                        [VENV_PYTHON, "-c", "import sys; print(sys.version_info[:2])"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if not any(v in ver_out.stdout for v in ("(3, 12)", "(3, 13)", "(3, 14)")):
                        console.print(
                            f"[yellow]⚠ venv использует Python {ver_out.stdout.strip()} — ожидается 3.12+.[/yellow]"
                        )
                        if not safe_confirm("[cyan]Запустить всё равно?[/cyan]", default=False):
                            continue
                except Exception:
                    pass
                if safe_confirm("[green]Вы действительно хотите запустить main.py вручную?[/green]"):
                    subprocess.run(["venv/bin/python", "main.py"])
            elif choice == "3":
                if is_service_exists(SERVICE_NAME):
                    if safe_confirm("[yellow]Вы действительно хотите перезапустить бота?[/yellow]"):
                        subprocess.run(["sudo", "systemctl", "restart", SERVICE_NAME])
                else:
                    console.print(f"[red]❌ Служба {SERVICE_NAME} не найдена.[/red]")
            elif choice == "4":
                if is_service_exists(SERVICE_NAME):
                    if safe_confirm("[red]Вы уверены, что хотите остановить бота?[/red]"):
                        subprocess.run(["sudo", "systemctl", "stop", SERVICE_NAME])
                else:
                    console.print(f"[red]❌ Служба {SERVICE_NAME} не найдена.[/red]")
            elif choice == "5":
                if is_service_exists(SERVICE_NAME):
                    subprocess.run([
                        "sudo",
                        "journalctl",
                        "-u",
                        SERVICE_NAME,
                        "-n",
                        "80",
                        "--no-pager",
                    ])
                else:
                    console.print(f"[red]❌ Служба {SERVICE_NAME} не найдена.[/red]")
            elif choice == "6":
                if is_service_exists(SERVICE_NAME):
                    subprocess.run(["sudo", "systemctl", "status", SERVICE_NAME])
                else:
                    console.print(f"[red]❌ Служба {SERVICE_NAME} не найдена.[/red]")
            elif choice == "7":
                show_update_menu()
            elif choice == "8":
                restore_from_backup()
            elif choice == "9":
                install_bot()
            elif choice == "10":
                manage_website()
            elif choice == "11":
                console.print("[bold cyan]Выход из CLI. Удачного дня![/bold cyan]")
                break
    except KeyboardInterrupt:
        console.print("\n[bold red]⏹ Прерывание. Выход из CLI.[/bold red]")


if __name__ == "__main__":
    main()
