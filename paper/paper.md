---
title: 'ResistaMet GUI: open-source electrical characterization for Keithley 2400/2450 sourcemeters'
tags:
  - Python
  - PyQt5
  - electrical characterization
  - Keithley
  - sourcemeter
  - four-point probe
  - sheet resistance
  - sensor characterization
  - laboratory software
authors:
  - name: Brenden Ferland
    orcid: 0009-0008-6566-6088
    affiliation: 1
affiliations:
  - name: Institute of Materials Science, University of Connecticut, Storrs, CT, USA
    index: 1
date: 30 April 2026
bibliography: paper.bib
---

# Summary

`ResistaMet GUI` is a PyQt5 desktop application for routine electrical characterization with Keithley 2400/2450 sourcemeters. It provides five point-and-click measurement modes from a single instrument: 2- and 4-wire resistance, voltage- and current-source biasing with up to one week of continuous logging, hardware-paced I-V sweeps with optional hysteresis, and four-point-probe sheet-resistance analysis with multi-spot uniformity tracking and current-reversal delta-mode for thermoelectric EMF cancellation. Engineering-notation parameter input ("1mA" rather than "0.001"), event-marked time-series data streams, dual JSON+CSV export with crash-safe checkpointing, and a documented catalog of hardware-tested SCPI quirk fixes for the 2400 series make `ResistaMet` suited to bench researchers in materials and sensor laboratories who own a Keithley sourcemeter but do not write Python.

# Statement of need

Electrical characterization with Keithley 2400/2450 source-measure units is ubiquitous in materials, sensor, and device-physics laboratories. Existing software for these instruments falls into three categories, none of which serves the bench-research workflow that combines long-duration biasing experiments with periodic strain or environmental events, four-point-probe sheet-resistance surveys, and routine resistance measurements on the same instrument.

**(1) Python instrument-control libraries.** `pymeasure` [@pymeasure] and `QCoDeS` [@qcodes] both ship first-class drivers for the Keithley 2400 and 2450 families and provide GUI scaffolding (`pymeasure`'s `ManagedWindow`, `QCoDeS`'s plot-server). However, both require users to subclass `Procedure` or write notebook code, neither contains built-in four-point-probe sheet-resistance analysis, and neither provides a turnkey desktop application aimed at researchers who do not script. Lower-level layers — `PyVISA` [@pyvisa], `InstrumentKit`, and the now-archived `python-ivi` — target driver authors rather than end users.

**(2) Closed-source vendor software.** Keithley's `KickStart` is Windows-only, requires a per-seat subscription or perpetual license ($610–$3,380), and its specialty applications restrict modern I-V workflows to recent touchscreen 2400-series models, excluding the older non-graphical Keithley 2400/2410/2420/2430 still common in academic laboratories. `KickStart` notably contains no four-point-probe application; its "High Resistivity App" addresses ASTM D257 dielectric testing of insulators using electrometers (Keithley 6517A/B), a different instrument class. Comparable bundled software from `Signatone Pro4` and the legacy `LabTracer` is sold with hardware and not separately maintained.

**(3) Direct open-source comparators are narrow or unmaintained.** `GatherLab/four-point-sheet-resistance` (MIT-licensed, last meaningful update 2024-02) is a single-mode tool requiring a separate Keithley 2450 plus a 2100 multimeter and has no published companion paper. `ivankong-n8/Keithley-2400-SourceMeter-Python-Interface` has no license and was last updated in 2018. `keithleygui` [@keithleygui] is well maintained but targets the Keithley 2600 series, not the 2400/2450 family.

The closest peer-reviewed software paper is `MeaSSUre:I-V` [@oh2023meassure], which addresses an analogous gap for the transistor-research community using the same Keithley 2400-series hardware. `ResistaMet GUI` addresses the parallel gap for the materials- and sensor-research community. Where `MeaSSUre:I-V` is sweep-focused on single-shot device characterization (FET output, FET transfer, BJT collector, two-terminal I-V), `ResistaMet` bundles:

- **Long-duration fixed-bias logging** (up to 168 hours, or "run until stopped") with thread-safe event annotations, a documented requirement of cyclic-stress and hydration-stability sensor experiments;
- **Four-point-probe sheet-resistance analysis** with selectable thin-film, semi-infinite, and finite-thin geometric correction models, multi-spot uniformity tracking with histogram visualization, current-reversal delta-mode that cancels thermoelectric EMF [@smits1958], and a configurable probe-safety power envelope that refuses runs whose worst-case `I_source × V_compliance` would damage tungsten-carbide tips or thermally compromise delicate thin-film and conductive-polymer samples;
- **A catalog of hardware-tested SCPI fixes** for the 2400 series — compliance status-bit detection, hardware averaging filter, auto-zero control, offset-compensated ohms, auto source delay, non-concurrent measurement functions, and high-impedance output-off — captured as code rather than as lab folklore, and validated by a stateful simulator whose responses match captured SCPI traces from two distinct 2400-family instruments.

`ResistaMet GUI` has been used to collect electrical data reported in two ChemRxiv preprints from the Adamson Lab at the University of Connecticut: a graphene-stabilized silicone composite study describing piezoresistive sensing and voltage-controlled de-icing [@silicone2026], and a moist electric generator study reporting 120-hour open-circuit voltage stability and parallel/series current scaling [@meg2025]. The software has additionally been adopted by an independent research group for routine four-point-probe sheet-resistance measurements (personal communication).

# Implementation

`ResistaMet GUI` is implemented in Python (≥3.9) using PyQt5 for the user interface and Matplotlib for real-time visualization. Instrument communication uses PyVISA [@pyvisa] over GPIB or USB. Each measurement runs in a Qt worker thread (`MeasurementWorker`); the worker communicates with the main UI exclusively through Qt signals, with shared state guarded by a `threading.Lock`. Long-running measurements are checkpointed to disk every 60 seconds as `.json.tmp` files and finalized as paired JSON and CSV outputs. At connect time the worker parses `*IDN?` and surfaces the model's documented source/measure envelope from a per-model specification table covering the 2400/2401/2410/2420/2425/2430/2440/2450 family.

# Validation

`ResistaMet GUI` ships a stateful in-memory simulator of the Keithley 2400-family SCPI surface (`tests/fakes/fake_keithley.py`), exercised in continuous integration as a drop-in replacement for `pyvisa.ResourceManager`. The simulator is calibrated against 23 captured SCPI traces from a Keithley 2420 (firmware C30) wired to precision reference resistors at three operating decades (100 Ω at 1 mA, 10 kΩ at 100 µA, 1 MΩ at 1 µA) in 4-wire Kelvin connection. Each trace records every `write` and `query` exchange of one measurement scenario; CI replays each trace through the simulator and asserts byte-equivalent configuration-query responses and within-tolerance measurement-query responses with the STAT compliance bit pinned exactly. A separate hardware-in-the-loop test suite (`tests/hardware/`, gated by an environment variable) re-executes the same captures against the bench instrument before each release to detect firmware drift. Cross-model validation against a second physical instrument (Keithley 2400, 1 A, firmware C30, different model and option codes) confirms that 29/29 fixtures and 6/6 documented SCPI quirks reproduce family-wide rather than only on the primary capture instrument.

A self-contained capture tool (`scripts/community_capture.py`) lets any contributor with a Keithley 2400-family instrument, a precision resistor, and PyVISA produce a comparable trace set; submissions are received via a GitHub issue template and replayed automatically in CI on merge. The methodology, validated behaviors, and intentional fidelity gaps (notably the absence of measurement-noise modeling and of a hardware-validated delta-mode trace) are enumerated in `docs/sim_fidelity.md` so reviewers and future contributors can audit the validation envelope without bench access. The package is installable via `pip` and is automatically tested across Python 3.9–3.12 on Linux and Windows in a GitHub Actions matrix.

# AI usage disclosure

The author used Anthropic's Claude (via Claude Code; models Sonnet 4.5 and Opus 4.7) during development of `ResistaMet GUI` and during preparation of this manuscript. AI assistance was used for code review and refactoring, test scaffolding, documentation generation, repository configuration (continuous integration, packaging metadata, community files), competitive-landscape research for this Statement of Need, and drafting of this manuscript. All AI-generated outputs were reviewed, edited, and validated by the author. All design decisions, hardware testing on a Keithley 2420 sourcemeter, scientific interpretation of measurements, and final code rest with the author.

# Acknowledgements

The author thanks Prof. Douglas H. Adamson (University of Connecticut) for providing the laboratory environment, the Keithley 2420 (3 A) and Keithley 2400 (1 A) sourcemeters on which all SCPI fixes and the simulator's cross-model validation were verified, and the broader research context that motivated this work. The author also thanks members of the Adamson Lab for sustained usage feedback during routine electrical characterization, and the independent research group whose adoption helped surface usability issues on hardware configurations beyond the development environment.

# References
