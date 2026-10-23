#!/usr/bin/env python3
"""Периодическая проверка новых статей (расписание сбора новостей).

Запускает сбор новостей по расписанию: либо через фиксированный интервал
(--every), либо по CRON-подобному выражению (--cron). Ведёт журнал запусков
(время, сколько статей, статус), чтобы можно было видеть историю и не запускать
сбор чаще, чем нужно.

Расписание не зависит от внешнего планировщика (cron ОС): скрипт сам крутит
цикл и вызывает сбор. Это удобно для демо и для запуска в Docker.

Запуск из корня проекта:
    .venv_news/bin/python news/scheduler.py --every 3600          # каждый час
    .venv_news/bin/python news/scheduler.py --every 3600 --once   # один проход
    .venv_news/bin/python news/scheduler.py --cron "0 */6 * * *"  # каждые 6 ч
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Журнал запусков (пишется рядом с корпусом).
DEFAULT_STATE = "data/processed/news_bulk/schedule_state.json"


def _load_state(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {"runs": [], "last_run": None}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"runs": [], "last_run": None}


def _save_state(path: str, state: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_run(state: dict, run: dict) -> None:
    state["runs"].append(run)
    # Храним не более 200 последних запусков.
    state["runs"] = state["runs"][-200:]
    state["last_run"] = run


def _run_collect(args) -> dict:
    """Вызывает dump_large.py и возвращает сводку."""
    cmd = [sys.executable, "news/dump_large.py"]
    if args.target is not None:
        cmd += ["--target", str(args.target)]
    if args.category:
        for c in args.category:
            cmd += ["--category", c]
    if args.out_dir:
        cmd += ["--out-dir", args.out_dir]
    if args.cache_dir:
        cmd += ["--cache-dir", args.cache_dir]
    start = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=args.collect_timeout)
        ok = proc.returncode == 0
        return {
            "ok": ok, "duration_s": round(time.time() - start, 1),
            "rc": proc.returncode, "tail": proc.stdout.strip().splitlines()[-12:],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "duration_s": round(time.time() - start, 1),
                "rc": -1, "tail": ["TIMEOUT"]}


def _cron_matches(cron_expr: str, now: datetime) -> bool:
    """Грубая проверка cron-выражения (минуты часы). Поддержка '*', '*/n', числа."""
    try:
        minute, hour = cron_expr.split()[:2]
    except ValueError:
        return False

    def _match(field: str, value: int) -> bool:
        if field == "*":
            return True
        if "/" in field:
            step = int(field.split("/")[1])
            return value % step == 0
        if "-" in field:
            lo, hi = (int(x) for x in field.split("-"))
            return lo <= value <= hi
        return int(field) == value

    return _match(minute, now.minute) and _match(hour, now.hour)


def _env_int(name: str, default: int) -> int:
    import os
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def main() -> int:
    import os
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--every", type=int, default=_env_int("NEWS_EVERY", 0),
                        help="Интервал между проходами в секундах. 0 = однократно, затем выход.")
    parser.add_argument("--cron", type=str, default="",
                        help="CRON-выражение (минуты часы ...), напр. '0 */6 * * *'.")
    parser.add_argument("--once", action="store_true", help="Выполнить один проход и выйти.")
    parser.add_argument("--state", default=os.environ.get("NEWS_STATE", DEFAULT_STATE),
                        help="Файл журнала запусков.")
    # Параметры сбора, пробрасываются в dump_large.
    parser.add_argument("--target", type=int, default=0)
    parser.add_argument("--category", action="append",
                        choices=["macro", "industry", "business", "official"])
    parser.add_argument("--out-dir", default=os.environ.get("NEWS_OUT_DIR", "data/processed/news_bulk"))
    parser.add_argument("--cache-dir", default=os.environ.get("NEWS_CACHE_DIR", "data/processed/news_cache"))
    parser.add_argument("--collect-timeout", type=int, default=3600,
                        help="Таймаут одного сбора (сек).")
    args = parser.parse_args()

    state = _load_state(args.state)

    if args.once or args.every == 0 and not args.cron:
        run = _run_collect(args)
        run["started_at"] = datetime.now(timezone.utc).isoformat()
        _append_run(state, run)
        _save_state(args.state, state)
        print(json.dumps(run, ensure_ascii=False, indent=2))
        return 0 if run["ok"] else 1

    print(f"Планировщик запущен: every={args.every}s cron='{args.cron}'")
    last_check = 0.0
    try:
        while True:
            now = datetime.now()
            due = False
            if args.every:
                due = (time.time() - last_check) >= args.every
            if args.cron and _cron_matches(args.cron, now):
                due = True
            if due:
                print(f"[{now.isoformat()}] запуск сбора…", flush=True)
                run = _run_collect(args)
                run["started_at"] = now.isoformat()
                _append_run(state, run)
                _save_state(args.state, state)
                print(f"  ok={run['ok']} duration={run['duration_s']}s", flush=True)
                last_check = time.time()
            time.sleep(20)  # проверяем расписание каждые 20 сек
    except KeyboardInterrupt:
        print("\nПланировщик остановлен.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())