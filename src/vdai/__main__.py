"""Allow ``python -m vdai`` to run the worker CLI."""

from .cli import main

raise SystemExit(main())
