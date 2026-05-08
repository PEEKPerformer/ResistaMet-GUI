#!/usr/bin/env python3
"""In-repo launcher. The canonical entry point lives in
``resistamet_gui/__main__.py`` so ``python resistamet-gui.py`` (development)
and the ``resistamet-gui`` console script (after ``pip install``) behave
identically.
"""
from resistamet_gui.__main__ import main


if __name__ == "__main__":
    main()
