---
title: 'ResistaMet GUI: open-source electrical characterization for Keithley 2400 series sourcemeters'
tags:
  - Python
  - PySide6
  - electrical characterization
  - Keithley sourcemeter
  - four-point probe
  - laboratory software
authors:
  - name: Brenden Ferland
    orcid: 0009-0008-6566-6088
    affiliation: 1
affiliations:
  - name: Institute of Materials Science, University of Connecticut, Storrs, CT, USA
    index: 1
date: 11 May 2026
bibliography: paper.bib
---

# Summary

`ResistaMet GUI` is a desktop application for electrical characterization using Keithley 2400 series source measure units (SMUs) over a GPIB interface. These instruments are commonly used by electrical engineers, materials scientists, and other physical scientists to characterize electrically active devices and materials. This software is bench-written and tested with 6 primary measurement modes: two- and four-wire resistance, constant-voltage and constant-current biasing for long runs (with in-run event annotations), I-V sweep, collinear 4-point probe per ASTM F84, and van der Pauw per ASTM F76 Method A. Each reading carries an instrument uncertainty propagated from the Keithley datasheet's per-range accuracy specs, all the way through to derived quantities like sheet resistance and resistivity. An in-package Keithley simulator, calibrated against captured SCPI traces from our bench Keithley 2420 and replayed in CI, allows users to test and practice each mode without an SMU, and keeps documented hardware quirks from regressing. The software includes live plotting, built-in safety mechanisms for both technicians (touch-safety voltage warnings) and samples (a probe-power envelope), crash-safe streaming output, and many other features and bug fixes hard-earned from using the software in our lab.

# Statement of need

Keithley 2400 Series SMUs are standard equipment in academic and industry laboratories specializing in electrical characterization. A typical experiment can be anything from long-duration fixed-bias logging with event annotations, periodic four-point-probe surveys, I-V sweeps, chronoamperometry, and 2- or 4-wire resistance measurements. These instruments are traditionally driven by hand-written scripts in LabVIEW, MATLAB, or Python libraries like `pymeasure` and `QCoDeS`. All of these require the user to write and maintain code, and none ship four-point-probe analysis. Modern paid software is sold by Tek as `KickStart` for $610–$2,200 per seat, omits four-point-probe analysis, and is restricted to recent touchscreen 2400-series models. Keithley's bundled LabTracer 2 (circa 2006, since discontinued) is a transistor-focused two-SMU I-V configurator with no four-point probe, van der Pauw, long fixed-bias logging, or in-run event annotation. No-code free alternatives are sparse and fragmented, often written in a bespoke manner for a single application. `ResistaMet GUI` covers that combined workflow on the same hardware academic groups already own. It is free, open, and aims to match or exceed paid software. Beyond the instrument and a computer, the only purchase needed is a GPIB to USB adapter.

# State of the field

The closest direct peer to `ResistaMet GUI` is commercial software sold by Tektronix (the OEM for Keithley) called `KickStart`. This is a Windows-only point-and-click GUI sold both as a subscription and perpetual licensing at a per-seat cost of $610 per year or $2,200 to own it. Most features are limited per-instrument and the Keithley 2400 series is limited to only I-V sweep mode. The now-discontinued `LabTracer 2` (c. 2006) was a two-SMU I-V configurator with no four-point-probe or long-duration logging bundled with the instrument.

Researchers needing to use older instruments have other options, though none are complete. Programmatic scripting frameworks (`pymeasure` [@pymeasure], `QCoDeS` [@qcodes]) offer procedure scaffolding and instrument drivers for users capable of writing their own code. Community-developed open source GUIs are most commonly tuned for narrow-scope subfields or tests. `MeaSSUre:I-V` [@oh2023meassure] targets transistor-memory and neuromorphic device characterization. `QKeithleyControl` [@qkeithleycontrol] covers transistor sweeps and solar-cell tracking. `keithleygui` [@keithleygui] is built for organic-FET work, specifically using the Keithley 2600 series. Fébba et al.'s LLM-driven controller [@febba2025] fits single-diode parameters to solar-cell I-V curves and is the published precedent for using a large language model to write Keithley control software — an approach this work also follows (see AI usage disclosure). `GatherLab/four-point-sheet-resistance` [@gatherlab_4pp] covers one specific four-point probe setup.

`ResistaMet GUI` covers a combined materials-characterization workflow that, to my knowledge, has not been previously available in a single point-and-click application. There are six measurement modes, backed up by measurement standards where available: resistance over time, fixed-bias modes for current and voltage, a dedicated mode for collinear 4-point probe measurements (ASTM F84-aligned) [@astm_f84; @smits1958], ASTM F76 van der Pauw resistivity with the §11.1 homogeneity gate [@astm_f76], and an I-V sweep mode. ResistaMet is explicitly designed for a broad array of uses; while individual programs may be better suited for niche use cases, I aim to have the best general-purpose application for Keithley 2400 series electrical characterization.

# Software design

`ResistaMet GUI` evolved from a set of bespoke CLI scripts used for sensor and power-generation monitoring over long windows. Eventually, non-computer-savvy lab members needed to use the software as well and had difficulty with the abrasive CLI interface. The GUI was thus born, and has been developed from the ground up with a philosophy of 'per-user' organization and personalization. This unlocked a better scientific user experience in the form of live feedback and easily switchable controls. The name `ResistaMet` comes from a weak and now embarrassing portmanteau of 'resistance + metrology', as the software originally measured only resistance over time for electrically conductive foams. Unfortunately, I've determined that it is too late for a proper rename.

As different needs were flagged by labmates and myself, more features were added. Voltage and current source covered chronoamperometry, electrochemistry, and device biasing. Four-point probe added in-app analysis grounded in standards for the lab's Signatone S-302 stand, replacing a workflow of raw I-V capture in Keithley's bundled LabTracer 2 (circa 2006, since discontinued) plus hand calculation. I-V sweep added transistor and diode work as a 'standard' feature, and van der Pauw mode handles samples where a collinear four-point probe head will not seat correctly.

Over time, the software evolved through bench necessities and frustrations. For example, an unexpectedly conductive sample resulted in sending far too much power to a Signatone SP4 tungsten-carbide collinear probe tip, resulting in it melting. In response to this, I added over-power protections. While running a van der Pauw measurement, it became abundantly clear that swapping electrodes needs to have a visual guide to prevent confusion.

The implementation originally used PyQt5 and matplotlib but was migrated to PySide6 due to Qt5 EOL and weaker copyleft. pyqtgraph was used for enhanced visualization at higher framerates. NumPy [@harris2020array] is used for buffer statistics and four-point-probe math, and PyVISA [@pyvisa] for instrument communication on Python 3.9 or later. The output section of settings offers HDF5 (when `h5py` is installed). Gzip compression is off by default because lab tools like Excel and Origin cannot open `.gz` and the core philosophy is that this software should be usable by someone who doesn't know what a `.gz` file *is*. 

Most per-mode SCPI sequences were tested and corrected against a live Keithley 2420 (firmware C30) across two parallel passes in April 2026. The first pass walked each command individually with `:SYST:ERR?` checks. Five defects surfaced, including silent re-ordering of `:FORM:ELEM` outputs and a misaligned compliance bit. The second pass was a full read of the ~300-page Keithley 2400-series User Manual [@keithley2400manual], which surfaced on-instrument features the GUI was not using (hardware sweep engine, auto-zero `ONCE`, hardware averaging filter, offset-compensated ohms, cable-null reference) that allowed for faster or more accurate measurements. An in-package SCPI simulator (described in Validation) keeps the fixes from regressing in CI, and a `--simulate` flag exposes it to end users so labmates can learn the workflow without GPIB hardware and reviewers can review without owning a Keithley. Per-reading measurement uncertainty is reported based on values from the manual and Keithley datasheet.

The 4PP mode implements ASTM F84-02's correction-factor decomposition $F = F_2 \cdot F(w/S) \cdot F_T$ [@astm_f84; @smits1958] with unit tests pinned to the standard's tabulated reference values for finite sample diameter, thickness (out to $w/S = 2.0$), and temperature (n- and p-type silicon). The van der Pauw worker implements F76-08 Method A [@astm_f76] as a state machine that emits `geometry_ready`, blocks on a `threading.Event` while the user reconnects four corner clips, sources +I and −I, and enforces `:OUTP OFF` between geometries. F76 §11.1's homogeneity gate runs against the eight resulting voltages. Standards are enforced in software with sensible defaults while still exposing raw per-reading values, so users can run their own calculations when needed.

![ResistaMet GUI four-point-probe tab: live readout, per-spot statistics panel, and sheet-resistance histogram populated from a multi-spot survey. The Advanced panel exposes ASTM F84 correction factors, current-reversal (delta) mode, and the probe-power safety envelope.](../docs/screenshots/04_four_point_probe.png){#fig:fpp width=90%}

# Validation

A functional Keithley simulator was built during the validation process for both reviewers without access to an instrument and for training/demonstration purposes. A Keithley 2420 (firmware C30) was wired to reference resistors at three different operating decades (100 Ω, 10 kΩ, 1 MΩ) in an asymmetric 4-wire configuration (symmetry was not required for this as instrument response was desired, not perfect accuracy). The FakeKeithley was written backwards from these traces; everything is reverse-engineered from real instrument behavior, not written from specs. The simulator is cross-validated against a Keithley 2400, and there is an open invitation for the community to validate it against their own instrument using `scripts/community_capture.py`.

End-to-end tests drive every measurement feature through the UI-to-CSV pipeline and check validity against a simulated DUT. A hardware-in-the-loop suite executes against the bench instrument as a secondary validation if one is available. Accepted submissions are replayed automatically in CI on Python 3.9–3.12 across Linux and Windows.

# Research impact statement

`ResistaMet GUI` has been used to collect electrical data for two publications within the Adamson lab at UConn. Voltage and current were both measured over time for a peer-reviewed study in *Journal of Materials Chemistry A* for moist-electric generators based on graphene hydrogel composites [@meg2026]. The software was not cited directly as it was not known to be possible. The software was also used for piezoresistive characterization and voltage-controlled de-icing [@silicone2026]. The Sun group at UConn Institute of Materials Science has adopted the software for four-point probe measurements.

# AI usage disclosure

Claude (Anthropic) was used extensively as a coding assistant; the majority of the source code, test scaffolding, and manuscript drafts were AI-generated under the author's direction, using Claude models from 3.7 Sonnet (Feb 2025) through Opus 4.7 (Apr 2026). All AI output was grounded against the Keithley Series 2400 SMU Datasheet and User Manual, ASTM F84 and F76, and SCPI traces captured from a live Keithley 2420 (firmware C30). Every measurement mode was bench-tested on that instrument by the author. Design decisions, scientific interpretation, and final responsibility for the code and claims in this paper rest with the author.


# Acknowledgements

Special thanks to Prof. Douglas H. Adamson (UConn) for providing the instrumentation required for this project and for allowing me to pursue interests like this despite not being direct materials research. Thanks to Adamson group members Jakiya Sultana Joya, Rumesha Anthoni M. Pererage, and Mahdad Mahmoudi for both intentionally and unintentionally breaking the software during debugging.

This work was supported in part by the U.S. Department of Education Graduate Assistance in Areas of National Need (GAANN) program under award P200A240111 to the University of Connecticut Polymer Program (PI: Mu-Ping Nieh). The contents do not necessarily represent the policy of the Department of Education, and you should not assume endorsement by the Federal Government.

# References
