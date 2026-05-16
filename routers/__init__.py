"""FastAPI router modules.

Each module here owns a single domain's HTTP surface and is mounted by
``main.py`` near the bottom of that file (after all helpers + dependencies are
defined). Keeping the include-router calls in one place means startup order is
explicit and grep-friendly.

Routers MUST import shared dependencies (``get_db``, ``get_current_user``,
SQLAlchemy models, helpers) from ``main`` lazily-after-helpers-exist by virtue
of being imported only at the end of ``main.py``. Do NOT import routers at the
top of main.py - it would create a circular import.
"""
