# Project Codex Low-Token Mode Prompt

Используй этот prompt, когда работа идёт внутри `!projects/ha-mcp/`.

- Базовый режим наследуется от root `prompts/CODEX_LOW_TOKEN_MODE_PROMPT.md`.
- Для search-budget задач дополнительно используй root `prompts/CODEX_LOW_TOKEN_SEARCH_BUDGET_PROMPT.md`.
- Сначала читай только `AGENTS.md`, `README.md`, `SECURITY.md`, `docs/FAQ.md` и минимальный набор runtime/build файлов: `pyproject.toml`, `src/`, `tests/`, `homeassistant-addon/`, `homeassistant-addon-dev/`.
- Для кода открывай самый узкий нужный срез: сначала манифесты и entrypoints, затем только релевантные модули и тесты.
- Для поиска используй `rg --files` и `rg -n`, доверяй `.rgignore`; `rg -uu` используй только если задача явно требует ignored paths.
- Ответы и промежуточные апдейты держи компактными, если пользователь не запросил детальный разбор.
