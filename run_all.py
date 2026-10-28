"""
Запускает web, scheduler и worker в одном терминале.
"""

import socket
import os
import signal
import subprocess
import sys
import threading
import time

HOSTNAME = socket.gethostname()
LOCAL_IP = socket.gethostbyname(HOSTNAME)

# Цвета ANSI
COLORS = {
    'web': '\033[36m',  # голубой
    'scheduler': '\033[33m',  # жёлтый
    'worker': '\033[32m',  # зелёный
    'system': '\033[35m',  # фиолетовый
    'reset': '\033[0m',
}

def _uvicorn_flags() -> list:
    """Флаги uvicorn из окружения (reload и access-log)."""
    flags = []
    if os.getenv('DEBUG', '1') == '1':
        flags.append('--reload')
    if os.getenv('UVICORN_ACCESS_LOG', '1') != '1':
        # При высокой нагрузке access-log дублирует метрики и забивает логи.
        flags.append('--no-access-log')
    return flags


PROCESSES = [
    ('web', ['uvicorn', 'config.asgi:application', '--host', LOCAL_IP, '--port', '8888',
             '--timeout-graceful-shutdown', '10'] + _uvicorn_flags()),
    ('scheduler', ['python', 'manage.py', 'run_scheduler']),
    ('worker', ['python', 'manage.py', 'run_tasks_worker']),
]

_STOP_SIGNAL = None


def _request_stop(signum, frame):
    """SIGTERM/SIGINT → корректная остановка всех процессов (не падение)."""
    global _STOP_SIGNAL
    _STOP_SIGNAL = signal.Signals(signum).name
    raise KeyboardInterrupt

# Ширина префикса для выравнивания
PREFIX_WIDTH = max(len(name) for name, _ in PROCESSES) + 2


def stream_output(name, stream, color):
    """Читает поток (stdout/stderr) процесса и выводит с префиксом."""
    prefix = f'{color}{name:>{PREFIX_WIDTH}}{COLORS["reset"]} | '
    try:
        for line in iter(stream.readline, ''):
            if line:
                # Убираем перевод строки и выводим с префиксом
                print(f'{prefix}{line.rstrip()}', flush=True)
    except Exception:
        pass
    finally:
        stream.close()


def log_system(message, color_name='system'):
    """Выводит системное сообщение."""
    color = COLORS.get(color_name, COLORS['system'])
    prefix = f'{color}{"system":>{PREFIX_WIDTH}}{COLORS["reset"]} | '
    print(f'{prefix}{message}', flush=True)


def main():
    # Включаем поддержку ANSI на Windows
    if sys.platform == 'win32':
        os.system('')  # активирует ANSI escape sequences

    # Корректная остановка по SIGTERM/SIGINT (docker stop, Ctrl+C, рестарт).
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _request_stop)
        except (ValueError, OSError):
            pass

    # Настройка виртуального окружения.
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUNBUFFERED'] = '1'

    procs = []
    threads = []

    try:
        for name, cmd in PROCESSES:
            log_system(f'Запуск {name}...')

            if cmd[0] == 'python':
                cmd.insert(1, '-u')

            # Создаём процесс с отдельными stdout/stderr
            p = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # объединяем stderr с stdout
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,  # line-buffered
                env=env,
            )
            procs.append((name, p))

            # Запускаем поток для чтения вывода
            t = threading.Thread(
                target=stream_output,
                args=(name, p.stdout, COLORS[name]),
                daemon=True,
            )
            t.start()
            threads.append(t)

            time.sleep(0.3)

        log_system('Все процессы запущено. Для остановки нажмите сочетание клавиш Ctrl+C для остановки.')
        log_system(f'Web: http://{LOCAL_IP}:8888')
        print()

        # Ждём, пока все процессы живы
        while True:
            for name, p in procs:
                if p.poll() is not None:
                    log_system(f'{name} exited with code {p.returncode}', 'system')
                    raise KeyboardInterrupt
            time.sleep(1)

    except KeyboardInterrupt:
        print()
        reason = _STOP_SIGNAL or 'SIGINT'
        log_system(
            f'Получен {reason} — останавливаем сервер и фоновые процессы...'
        )

        # Сначала просим все процессы завершиться (uvicorn — graceful shutdown,
        # чтобы дообслужить in-flight запросы).
        for name, p in procs:
            if p.poll() is None:
                log_system(f'Останавливаем {name} (pid={p.pid})...')
                p.terminate()

        # Даём время на корректное завершение, не убивая сразу.
        deadline = time.time() + 15
        while time.time() < deadline:
            if all(p.poll() is not None for _, p in procs):
                break
            time.sleep(0.2)

        # Принудительно добиваем, если кто-то не завершился.
        for name, p in procs:
            if p.poll() is None:
                log_system(
                    f'{name} не завершился за 15 с — принудительная остановка.'
                )
                p.kill()

        # Ждём потоки вывода.
        for t in threads:
            t.join(timeout=1)

        log_system('Все процессы остановлены.')


if __name__ == '__main__':
    main()
