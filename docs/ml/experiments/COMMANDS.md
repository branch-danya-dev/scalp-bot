# Выполненные команды и локальные артефакты

Рабочий каталог:
`C:/Users/workingspace/.codex/worktrees/parallel-scenarios-ml/scalp-bot`.
Python `.venv/Scripts/python.exe`, Windows3.13.15. Родитель и spawned child
проверены на импорт именно этого worktree (validation/imports.txt и shadow-report).

```powershell
python -m pip install --no-deps -e ".[dev,ml]"
python -m scalp_bot.ml build-dataset --source G:/scalp-bot/data/audit-two-hour-20260927/inputs.sqlite --output data/ml/impulse-v1-final --capture-id 790eebd906ac4a41baf69eccb5b69ae3
python -m scalp_bot.ml train --dataset data/ml/impulse-v1-final --output models/ml/impulse-v1-final
python -m scalp_bot.ml evaluate --dataset data/ml/impulse-v1-final --model models/ml/impulse-v1-final
python -m scalp_bot.ml predict --dataset data/ml/impulse-v1-final --model models/ml/impulse-v1-final --row 9000
python -m scalp_bot.ml shadow --dataset data/ml/impulse-v1-final --model models/ml/impulse-v1-final --output data/ml/shadow-v1-final --limit 200
python scripts/check-ml-shadow-offline.py models/ml/impulse-v1-final docs/ml/experiments/engine-shadow-parity.json
python scripts/check-transport-load.py G:/scalp-bot/data/audit-two-hour-20260927/inputs.sqlite docs/implementation/transport-load.json
python scripts/test_preflight.py
python -m pip list --format=freeze --exclude-editable
```

Команды использовали явный путь к Python выше. До финального editable install
зависимости были установлены через dev extra и отдельные pinned ML-пакеты. Повторная сборка/train/shadow требует
НОВОГО output-каталога; имеющиеся артефакты не перезаписываются. Для установки в
новой среде: `python -m pip install -r docs/ml/experiments/environment-windows-py313.txt`,
затем editable install проекта. Этот файл — exact version snapshot фактической
среды, не переносимый cross-platform lock. pip freeze в Windows worktree с Git-dir
на G: ошибался на разных дисках; pip list дал полный список без editable записи.

Dataset также принимает оригинальный *.inputs.jsonl.gz вместо read-only SQLite.
Источник выбран по фактическому capture membership; SQLite — индекс raw, а не
список сделок. Source subset hash в manifest даёт проверку равенства входных событий.
Сборка/параметры зафиксированы до training; test-порог не подбирался.

Финальные `*-final` артефакты авторитетны. Ранние impulse-v1 и impulse-v1-verified
оставлены локально как superseded: сначала исправлена привязка activation к
историческому времени, затем в snapshot добавлена причинная исполнимая котировка,
отдельная от будущей label entry. Предыдущие метрики не выбирались по качеству.
CatBoost сохраняет служебную metadata, поэтому SHA нового retrain может отличаться;
exact reload parity проверяется для опубликованного артефакта.

Вес model.cbm, logistic.npz, dataset.jsonl и forecasts.jsonl находятся в игнорируемых
models/ml и data/ml. Полные manifests с абсолютными путями/hash, параметры и метрики
в Git; срок хранения локальный до удаления владельцем. Vendor raw не распространяется.
Логи команд — docs/implementation/validation/*-final.*, полная оценка — evaluation.json.

Финальный editable install с build isolation выполнен успешно. Попытка без
build isolation отдельно сохранена как неуспешная: отсутствовал setuptools.build_meta.
