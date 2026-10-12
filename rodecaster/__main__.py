import sys

# Any positional argument (a sub-command) selects the CLI; otherwise start the GUI.
if any(not a.startswith("-") for a in sys.argv[1:]):
    from .cli import main
else:
    from .ui.app import main
sys.exit(main())
