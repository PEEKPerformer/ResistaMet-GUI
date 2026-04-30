# Contributing to ResistaMet GUI

Thanks for your interest in improving ResistaMet GUI. This is a small academic/lab software project, so the contribution process is light — but here's what to expect.

## Reporting bugs and requesting features

Open an issue at https://github.com/PEEKPerformer/ResistaMet-GUI/issues. Please use the issue templates if a relevant one exists. For bug reports, the most useful thing you can include is:

- ResistaMet GUI version (`Help → About`, or check `resistamet_gui/constants.py`)
- Operating system + Python version
- Keithley model and connection type (GPIB, USB, Prologix)
- A copy of the relevant log output (set log level to DEBUG if possible)
- The exact steps to reproduce, including any settings used

## Asking questions / getting help

For usage questions or clarification, open a GitHub issue tagged as `question`. There is no separate forum or chat — issues are the venue for everything.

## Submitting a pull request

1. Fork the repo and create a topic branch off `main`: `git checkout -b fix/whatever-it-is`
2. Make your changes. Keep PRs focused — one logical change per PR is easier to review.
3. Run the test suite locally before pushing:
   ```bash
   QT_QPA_PLATFORM=offscreen pytest tests/ -v
   ```
4. If you change instrument behavior (SCPI commands, sweep logic, calculations), please test against real hardware if possible. Note in the PR which instrument and firmware you used.
5. Update `README.md` and the relevant section of the version history if your change is user-visible.
6. Open the PR. CI will run the test suite on Linux and Windows across Python 3.9-3.12.

## Code style

- Follow existing patterns in the file you're editing.
- Pure functions for math (see `resistamet_gui/calculations.py`).
- Thread-safe state with `threading.Lock` in `workers.py`; no direct UI access from worker threads — use Qt signals.
- Add new constants to `resistamet_gui/constants.py`, not inline.
- New tests for new behavior. The bar is "the test would have caught the bug if you'd had it before."

## Hardware notes

- Many SCPI commands behave differently across Keithley 2400 series variants. If you find a model-specific quirk, document it in `instrument.py` near the relevant call.
- Compliance detection uses the magic number `9.9e37` from Keithley firmware (see `KEITHLEY_COMPLIANCE_MAGIC_NUMBER` in constants).

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By participating, you agree to abide by it.

## License

Contributions are accepted under the project's existing MIT License with Academic Recognition Clause. By submitting a PR, you agree your contribution is licensed under the same terms.
