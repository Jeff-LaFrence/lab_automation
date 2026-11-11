"""The simulated NMR and its evaluator: determinism, recovery, fault signatures, and isolation."""

from __future__ import annotations

import builtins
import io
import json
import statistics

import numpy as np
import pytest
from conftest import ROOT, make_design

from labauto.cli import main
from labauto.execute import lcms_seed, ledger_from_plan, measure_and_analyze
from labauto.expand import expand
from labauto.models import Design
from labauto.nmr_analysis import (
    EVALUATORS,
    NMR_BROAD_LINES,
    NMR_FIT_POOR,
    NMR_LOW_SNR,
    NMR_PHASE_CORRECTED,
    NMR_SIGNAL_INCONSISTENT,
    LineshapeFitter,
    NmrFit,
    analyze_plate_nmr,
    autophase,
    control_means,
    correct_baseline,
    naive_yields,
    naive_yields_autophased,
)
from labauto.virtual.ground_truth import reference_expected, surface_for, true_yield
from labauto.virtual.lcms import as_run_conditions, read_plate
from labauto.virtual.nmr import (
    NmrSettings,
    active_faults,
    draw_params,
    load_spectra,
    ppm_axis,
    read_plate_nmr,
    render,
    spectrometer_mhz,
    write_spectra,
)

FITTER = LineshapeFitter()


@pytest.fixture
def small(assay):
    """8 wells: 2 conditions in duplicate plus the 4 control wells."""
    plan = expand(make_design(), assay)
    return plan, ledger_from_plan(plan)


def spectrum(y: float, seed: int, settings: NmrSettings | None = None, **faults):
    """One synthetic spectrum with a known yield; conversion is yield + 5, capped at 100."""
    cfg = settings or NmrSettings()
    rng = np.random.default_rng([seed, int(y * 10)])
    params = draw_params(rng, y / 100, 1 - min(100.0, y + 5) / 100, cfg, **faults)
    ppm = ppm_axis(cfg)
    return ppm, render(ppm, params, cfg, rng), params


# ---------------------------------------------------------------- the simulator


def test_same_seed_gives_the_same_spectra_and_another_seed_does_not(small, assay):
    plan, ledger = small
    a = read_plate_nmr(ledger, plan, assay, surface_for(0), seed=11)
    b = read_plate_nmr(ledger, plan, assay, surface_for(0), seed=11)
    c = read_plate_nmr(ledger, plan, assay, surface_for(0), seed=12)
    assert list(a.spectra) == list(b.spectra) and len(a.spectra) == 8
    assert all(np.array_equal(a.spectra[w], b.spectra[w]) for w in a.spectra)
    assert a.truth == b.truth
    assert not any(np.array_equal(a.spectra[w], c.spectra[w]) for w in a.spectra)


def test_nmr_follows_the_as_run_volumes(small, assay):
    """Like the LC/MS, the NMR reads what was dispensed, not what was designed."""
    plan, ledger = small
    target = next(w for w in plan.wells if w.role == "sample")
    for e in ledger:
        if e.dest_well == target.well and e.reagent == "catalyst":
            e.volume_ul = 12.0
    plate = read_plate_nmr(ledger, plan, assay, surface_for(0), seed=3)
    vols = {e.reagent: e.volume_ul for e in ledger if e.dest_well == target.well}
    expected = true_yield(as_run_conditions(vols, assay), surface_for(0))
    assert plate.truth[target.well]["true_yield"] == pytest.approx(expected)
    assert expected != pytest.approx(true_yield(target.conditions, surface_for(0)))


def test_round_1_and_round_2_differ_for_the_same_seed(small, assay):
    plan, ledger = small
    one = read_plate_nmr(ledger, plan, assay, surface_for(0), seed=11, round_number=1)
    two = read_plate_nmr(ledger, plan, assay, surface_for(0), seed=11, round_number=2)
    assert list(one.spectra) == list(two.spectra)
    assert not any(np.array_equal(one.spectra[w], two.spectra[w]) for w in one.spectra)
    assert all(
        one.truth[w]["observed_yield"] != two.truth[w]["observed_yield"] for w in one.spectra
    )


def test_the_nmr_yield_noise_is_its_own_not_the_lcms_noise(assay):
    """Each well's NMR yield draw comes from the NMR stream, not from default_rng(seed),
    so it is independent of the LC/MS draw for the same well."""
    design = Design.model_validate(
        json.loads((ROOT / "examples" / "designs" / "good_factorial.json").read_text())
    )
    plan = expand(design, assay)
    ledger = ledger_from_plan(plan)
    seed, cfg = 11, NmrSettings()
    plate = read_plate_nmr(ledger, plan, assay, surface_for(0), seed)
    readings = read_plate(ledger, plan, assay, surface_for(0), seed, 1, [])
    lcms = {r.well: r.observed_yield - r.true_yield for r in readings}
    nmr = {w: t["observed_yield"] - t["true_yield"] for w, t in plate.truth.items()}
    wells = [w for w in lcms if 1 < plate.truth[w]["true_yield"] < 99]  # away from the clip
    assert len(wells) > 20 and set(nmr) == set(lcms)
    assert all(abs(nmr[w] - lcms[w]) > 1e-6 for w in wells)
    z = np.array([nmr[w] / cfg.yield_noise_sd for w in wells])
    plain = np.random.default_rng(seed).normal(0.0, 1.0, size=4 * len(wells))
    assert not any(np.any(np.isclose(z, v, atol=1e-9)) for v in plain)
    r = np.corrcoef(z, [lcms[w] for w in wells])[0, 1]
    assert abs(r) < 0.5


def test_faults_by_round_and_unknown_faults(small, assay):
    assert active_faults(["nmr_broad_lines@2", "injection_failure", "plate_drift@2"], 1) == set()
    assert active_faults(["nmr_broad_lines@2", "nmr_impurity"], 2) == {
        "nmr_broad_lines",
        "nmr_impurity",
    }
    assert active_faults(["nmr_phase_error"], 5) == {"nmr_phase_error"}
    with pytest.raises(ValueError, match="unknown NMR fault"):
        active_faults(["nmr_bad_shim@1"], 1)


def test_spectra_files_round_trip_and_hold_no_truth(small, assay, tmp_path):
    plan, ledger = small
    plate = read_plate_nmr(ledger, plan, assay, surface_for(0), seed=5, faults=["nmr_impurity"])
    path = write_spectra(tmp_path, plate)
    assert path == tmp_path / "spectra.npz" and (tmp_path / "truth.json").exists()
    with np.load(path) as data:
        assert set(data.files) == {"ppm", "spectrometer_mhz", *plate.spectra}
    ppm, spectra = load_spectra(tmp_path)
    assert np.array_equal(ppm, plate.ppm)
    for well, y in plate.spectra.items():
        assert np.allclose(spectra[well], y, rtol=1e-6, atol=1e-3)
    truth = json.loads((tmp_path / "truth.json").read_text())
    assert truth["synthetic"] is True and set(truth["wells"]) == set(plate.spectra)


def test_the_spectra_file_carries_the_field_and_the_loader_checks_it(small, assay, tmp_path):
    plan, ledger = small
    plate = read_plate_nmr(
        ledger, plan, assay, surface_for(0), 5, settings=NmrSettings(spectrometer_mhz=600.0)
    )
    write_spectra(tmp_path, plate)
    assert spectrometer_mhz(tmp_path) == 600.0
    with pytest.raises(ValueError, match="600 MHz"):
        load_spectra(tmp_path)  # the evaluator's 400 MHz is expected by default
    ppm, spectra = load_spectra(tmp_path, expected_mhz=600.0)
    assert set(spectra) == set(plate.spectra)
    with pytest.raises(ValueError, match="assumes 400 MHz"):
        analyze_plate_nmr(ppm, spectra, plan, assay, LineshapeFitter(), spectrometer_mhz=600.0)
    fits, _ = analyze_plate_nmr(ppm, spectra, plan, assay, spectrometer_mhz=600.0)
    for f in fits:
        assert abs(f.yield_pct - plate.truth[f.well]["observed_yield"]) < 1.5
        assert f.linewidth_hz < 2.0


# ---------------------------------------------------------------- the evaluator


@pytest.mark.parametrize("y", [0, 5, 30, 60, 95, 100])
def test_fitter_recovers_a_noiseless_spectrum_within_half_a_point(y):
    """No noise, but the baseline roll, shift offsets, small phase and nuisance peaks stay."""
    ppm, intensity, _ = spectrum(y, seed=1, settings=NmrSettings(noise_sd=0.0))
    fit = FITTER.evaluate(ppm, intensity, "E1")
    assert abs(fit.yield_from_doublet - y) < 0.5
    assert abs(fit.yield_from_singlet - y) < 0.5
    assert abs(fit.yield_pct - y) < 0.5
    assert abs(fit.conversion_pct - min(100.0, y + 5)) < 0.5


def test_fitter_recovers_noisy_spectra_within_tolerance():
    """Tolerance: 1.5 points per spectrum, and the mean error within 0.3 points.

    With the default noise the internal standard stands about 330 noise sd
    tall, so each fitted area is good to a few tenths of a percent of the
    standard, and the yield error is a few tenths of a point (about 0.3 sd for
    the mean of the two signals). 1.5 points is about five times that, so a
    pass is not luck, and the mean check catches a bias of a third of a point.
    """
    errors = []
    for i, y in enumerate(np.linspace(2, 98, 24)):
        ppm, intensity, _ = spectrum(float(y), seed=100 + i)
        fit = FITTER.evaluate(ppm, intensity, "E1")
        assert fit.flags == [] and not fit.excluded
        errors.append(fit.yield_pct - y)
        assert abs(fit.yield_from_doublet - y) < 2.0
        assert abs(fit.yield_from_singlet - y) < 1.5
    assert max(abs(e) for e in errors) < 1.5
    assert abs(statistics.fmean(errors)) < 0.3


def test_broad_lines_raise_the_linewidth_and_are_flagged(small, assay):
    plan, ledger = small
    faults = ["nmr_broad_lines@2"]
    for round_number, broad in ((1, False), (2, True)):
        plate = read_plate_nmr(ledger, plan, assay, surface_for(0), 7, round_number, faults)
        assert plate.plate_faults == (["nmr_broad_lines"] if broad else [])
        fits, _ = analyze_plate_nmr(plate.ppm, plate.spectra, plan, assay)
        for f in fits:
            assert (NMR_BROAD_LINES in f.flags) is broad
            assert (f.linewidth_hz > 3.5) if broad else (f.linewidth_hz < 2.0)
            assert not f.excluded
            assert abs(f.yield_pct - plate.truth[f.well]["observed_yield"]) < 2.0


def test_phase_error_is_corrected_and_flagged(small, assay):
    plan, ledger = small
    plate = read_plate_nmr(ledger, plan, assay, surface_for(0), 7, 1, ["nmr_phase_error@1"])
    fits, _ = analyze_plate_nmr(plate.ppm, plate.spectra, plan, assay)
    for f in fits:
        truth = plate.truth[f.well]
        assert NMR_PHASE_CORRECTED in f.flags
        assert abs(f.phase_deg - truth["phase_deg"]) < 2.0
        assert abs(f.yield_pct - truth["observed_yield"]) < 1.5


def test_impurity_sets_signal_inconsistent_on_that_well(small, assay):
    """The probability is raised for this test so a small plate gets hit. Impurity
    sizes straddle the flag threshold, so only wells hit clearly above it must be flagged."""
    plan, ledger = small
    cfg = NmrSettings(impurity_p=0.5)
    assert cfg.impurity_amount == (0.02, 0.20)
    plate = read_plate_nmr(ledger, plan, assay, surface_for(0), 7, 1, ["nmr_impurity"], cfg)
    hit = {w for w, t in plate.truth.items() if t["impurity"]}
    big = {w for w in hit if plate.truth[w]["impurity_amount"] > 0.07}
    assert big and hit != set(plate.spectra)
    assert plate.plate_faults == []
    fits, _ = analyze_plate_nmr(plate.ppm, plate.spectra, plan, assay)
    for f in fits:
        flagged = NMR_SIGNAL_INCONSISTENT in f.flags
        if f.well not in hit:
            assert not flagged
        if f.well in big:
            assert flagged
        if flagged:
            assert f.well in hit and f.yield_from_singlet > f.yield_from_doublet + 5


def impure(y: float, seed: int, amount: float):
    """One spectrum with an impurity of a set size, and the same spectrum without it
    (the same draws and the same noise)."""
    ppm, _, params = spectrum(y, seed, impurity_p=1.0)
    cfg = NmrSettings()
    with_imp = render(
        ppm, params.model_copy(update={"impurity": amount}), cfg, np.random.default_rng([seed, 1])
    )
    without = render(
        ppm, params.model_copy(update={"impurity": 0.0}), cfg, np.random.default_rng([seed, 1])
    )
    return ppm, with_imp, without


def test_yield_pct_is_the_plain_mean_of_doublet_and_singlet():
    """The documented combination, flagged or not: an impurity moves yield_pct by half its size."""
    cases = [spectrum(y, seed=30 + i)[:2] for i, y in enumerate((10, 50, 90))]
    cases += [impure(40, 31, 0.15)[:2], spectrum(60, 32, phase_error=True)[:2]]
    for ppm, intensity in cases:
        fit = FITTER.evaluate(ppm, intensity, "E1")
        assert fit.yield_pct == pytest.approx((fit.yield_from_doublet + fit.yield_from_singlet) / 2)


def test_impurity_detection_depends_on_size():
    """Impurities of 2 to 4.5 points sit under the 5 point flag; 12 to 20 points sit above it.
    So detection is measured, not built in: the small ones mostly pass, the large ones are caught."""

    def caught(amounts):
        hits = 0
        for i, a in enumerate(amounts):
            ppm, intensity, _ = impure(20 + 10 * i, 50 + i, float(a))
            hits += NMR_SIGNAL_INCONSISTENT in FITTER.evaluate(ppm, intensity, "E1").flags
        return hits

    small, large = np.linspace(0.02, 0.045, 6), np.linspace(0.12, 0.20, 6)
    assert caught(small) < len(small)
    assert caught(large) == len(large)


def test_the_doublet_stays_accurate_under_an_impurity():
    """The singlet has its own shift, width and line shape, so an impurity under it cannot drag the doublet."""
    for i, y in enumerate((10, 50, 90)):
        ppm, with_imp, without = impure(y, 60 + i, 0.20)
        a = FITTER.evaluate(ppm, with_imp, "E1")
        b = FITTER.evaluate(ppm, without, "E1")
        assert abs(a.yield_from_doublet - b.yield_from_doublet) < 0.3
        assert abs(a.yield_from_doublet - y) < 2.0
        assert a.yield_from_singlet - b.yield_from_singlet == pytest.approx(20, abs=2.0)
        assert NMR_SIGNAL_INCONSISTENT in a.flags and NMR_SIGNAL_INCONSISTENT not in b.flags


def test_a_large_baseline_roll_is_corrected():
    """A roll 100 times the default. Naive integration of the raw spectrum is far off, and
    the noise measured on the raw signal free windows is far too large; after
    correct_baseline both are right, and the fit is as good as on a flat baseline."""
    cfg = NmrSettings(baseline_amp=400.0)
    for seed in (0, 1, 2):
        ppm, intensity, params = spectrum(40, seed, settings=cfg)
        assert max(abs(c) for c in params.baseline) > 50
        raw = naive_yields(ppm, intensity)
        assert raw is None or abs(raw[2] - 40) > 10
        windows = np.zeros(ppm.shape, dtype=bool)
        for lo, hi in ((0.2, 2.2), (4.3, 5.8), (6.4, 7.0), (7.7, 9.8)):
            windows |= (ppm >= lo) & (ppm <= hi)
        assert np.std(intensity[windows]) > 5 * cfg.noise_sd
        corrected, noise = correct_baseline(ppm, intensity)
        assert noise == pytest.approx(cfg.noise_sd, rel=0.1)
        assert abs(naive_yields(ppm, corrected)[2] - 40) < 3
        fit = FITTER.evaluate(ppm, intensity, "E1")
        assert fit.flags == [] and abs(fit.yield_pct - 40) < 1.5


def test_autophase_takes_out_a_phase_error_before_naive_integration():
    """The fair naive comparison for a phase error: autophase from the standard, then integrate."""
    for i, y in enumerate((20, 50, 80)):
        ppm, intensity, params = spectrum(y, seed=40 + i, phase_error=True)
        found = autophase(ppm, correct_baseline(ppm, intensity)[0])[1]
        assert abs(found - params.phase_deg) < 2.0
        unphased = naive_yields(ppm, correct_baseline(ppm, intensity)[0])[2]
        phased = naive_yields_autophased(ppm, intensity)[2]
        assert abs(unphased - y) > 5
        assert abs(phased - y) < 4


def test_low_signal_is_flagged_and_excluded():
    ppm, intensity, _ = spectrum(50, seed=4, settings=NmrSettings(signal_scale=0.1))
    fit = FITTER.evaluate(ppm, intensity, "E1")
    assert NMR_LOW_SNR in fit.flags and fit.excluded and fit.is_snr < 50


def test_an_unexpected_signal_makes_the_fit_poor_and_excludes_the_well():
    ppm, intensity, _ = spectrum(50, seed=4)
    gamma = 0.0015
    intensity = intensity + (0.5 / np.pi) * gamma / ((ppm - 7.28) ** 2 + gamma**2)
    fit = FITTER.evaluate(ppm, intensity, "E1")
    assert NMR_FIT_POOR in fit.flags and fit.excluded
    assert fit.rmse_over_noise > 1.5


def test_naive_integration_is_reported_next_to_the_fit():
    ppm, intensity, _ = spectrum(40, seed=2, phase_error=True)
    fit = FITTER.evaluate(ppm, intensity, "E1")
    assert fit.naive_yield_pct is not None
    assert abs(fit.yield_pct - 40) < 1.5
    assert abs(fit.naive_yield_pct - 40) > abs(fit.yield_pct - 40)


def test_the_registry_holds_only_the_classical_fitter():
    assert list(EVALUATORS) == ["lineshape"]
    assert EVALUATORS["lineshape"]().name == "lineshape least squares (classical)"


# ---------------------------------------------------------------- one plate


class FixedEvaluator:
    """A stand-in evaluator: proves the slot works and pins the per condition rules."""

    name = "fixed values (test)"

    def __init__(self, values: dict[str, float | None]):
        self.values = values

    def evaluate(self, ppm, intensity, well):
        v = self.values.get(well, 0.0)
        return NmrFit(well=well, yield_pct=v, excluded=v is None, flags=[] if v else [])


def test_condition_statistics_mirror_the_lcms_analysis(assay):
    design = make_design(
        grid={"catalyst_mol_pct": [1, 2, 4], "base_equiv": [1.0], "cosolvent_pct": [20]},
        replicates=3,
    )
    plan = expand(design, assay)
    by_ci: dict[int, list[str]] = {}
    for w in plan.wells:
        if w.role == "sample":
            by_ci.setdefault(w.condition_index, []).append(w.well)
    values = {w.well: 50.0 for w in plan.wells}
    values[by_ci[0][0]], values[by_ci[0][1]], values[by_ci[0][2]] = 40.0, 50.0, 60.0
    values[by_ci[1][0]], values[by_ci[1][1]] = None, None
    values[by_ci[2][0]], values[by_ci[2][1]], values[by_ci[2][2]] = 10.0, 30.0, 20.0
    spectra = {w.well: np.zeros(3) for w in plan.wells}
    fits, conds = analyze_plate_nmr(np.zeros(3), spectra, plan, assay, FixedEvaluator(values))
    stats = {c.condition_index: c for c in conds}
    assert stats[0].mean == pytest.approx(50.0) and stats[0].sd == pytest.approx(10.0)
    assert stats[0].n == 3 and stats[0].flags == ["REPLICATE_SPREAD"]
    assert stats[1].n == 1 and stats[1].sd is None and stats[1].flags == ["LOW_REPLICATES"]
    assert stats[2].mean == pytest.approx(20.0) and stats[2].flags == ["REPLICATE_SPREAD"]
    flags = {f.well: f.flags for f in fits}
    assert all("REPLICATE_SPREAD" in flags[w] for w in by_ci[0])
    assert all("LOW_REPLICATES" in flags[w] for w in by_ci[1])
    assert stats[0].wells == sorted(by_ci[0], key=lambda w: (int(w[1:]), w[0]))
    assert control_means(fits, plan) == {"negative": 50.0, "reference": 50.0}


def test_adding_nmr_leaves_the_lcms_readings_byte_for_byte_unchanged(assay, tmp_path):
    design = Design.model_validate(
        json.loads((ROOT / "examples" / "designs" / "good_factorial.json").read_text())
    )
    plan = expand(design, assay)
    ledger = ledger_from_plan(plan)
    surface, expected = surface_for(0), reference_expected(assay, 0)
    faults = ["injection_failure", "nmr_impurity", "nmr_phase_error@1"]

    def lcms(name):
        path = tmp_path / name / "lcms_peaks.csv"
        readings, results, _ = measure_and_analyze(
            plan, ledger, assay, surface, 1, 1, faults, expected, peaks_path=path
        )
        return path.read_bytes(), readings, results

    before, readings_before, results_before = lcms("before")
    plate = read_plate_nmr(ledger, plan, assay, surface, lcms_seed(1, 1), 1, faults)
    analyze_plate_nmr(plate.ppm, plate.spectra, plan, assay)
    after, readings_after, results_after = lcms("after")
    assert before == after
    assert readings_before == readings_after and results_before == results_after
    alone = read_plate(ledger, plan, assay, surface, lcms_seed(1, 1), 1, faults)
    assert alone == readings_before


def test_the_analysis_never_reads_truth_json(small, assay, tmp_path, monkeypatch):
    plan, ledger = small
    plate = read_plate_nmr(ledger, plan, assay, surface_for(0), 9, 1, ["nmr_impurity"])
    write_spectra(tmp_path, plate)
    opened: list[str] = []
    real_open, real_io_open = builtins.open, io.open

    def spy(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    def spy_io(file, *args, **kwargs):
        opened.append(str(file))
        return real_io_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(io, "open", spy_io)
    ppm, spectra = load_spectra(tmp_path)
    fits, conds = analyze_plate_nmr(ppm, spectra, plan, assay)
    monkeypatch.undo()
    assert any(p.endswith("spectra.npz") for p in opened)
    assert not any("truth" in p for p in opened)

    (tmp_path / "truth.json").unlink()
    again = analyze_plate_nmr(*load_spectra(tmp_path), plan, assay)
    assert again == (fits, conds)


# ---------------------------------------------------------------- the check command


def test_nmr_check_writes_both_files(tmp_path, capsys):
    md, png = tmp_path / "nmr.md", tmp_path / "img" / "nmr_fit.png"
    code = main(["nmr-check", "--out", str(md), "--png", str(png), "--replicates", "1"])
    assert code == 0
    text = md.read_text()
    assert "synthetic" in text and "labauto nmr-check" in text
    for case in ("clean", "broad_lines", "phase_error", "low_signal", "impurity"):
        assert f"| {case} |" in text
    assert "Mean abs error" in text and "Milliseconds per spectrum" in text
    assert png.stat().st_size > 10_000 and "img/nmr_fit.png" in text
    assert "naive integration with autophase" in text and "naive integration, unphased" in text
    assert "## Impurity detection by size" in text and "| under 5 |" in text
    assert "## Clean and impurity side by side" in text and "lower bound" in text
    command = next(line for line in text.splitlines() if line.startswith("Command:"))
    assert str(tmp_path) not in text and not any(t.startswith("/") for t in command.split())
    assert "Synthetic spectra" in capsys.readouterr().out
