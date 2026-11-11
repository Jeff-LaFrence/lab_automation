"""NMR readout commands: nmr-check (validation of the spectrum evaluator).

nmr-check makes synthetic spectra over a grid of known yields, in five cases
(clean, broad lines, phase error, low signal, impurity), and scores three
methods against the yield each spectrum was built with: naive fixed region
integration without phasing, the same with a zero-order autophase, and the
line shape fitter. A second set of spectra (the impurity sweep) measures how
often the fitter's consistency flag catches an impurity, by impurity size. It
writes a markdown file and a figure.
"""

from __future__ import annotations

import argparse
import copy
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

CASES: dict[str, dict] = {
    "clean": {},
    "broad_lines": {"broad": True},
    "phase_error": {"phase_error": True},
    "low_signal": {"settings": {"signal_scale": 0.1}},
    "impurity": {"impurity_p": 1.0},
}
YIELDS = (5, 15, 25, 35, 45, 55, 65, 75, 85, 95)
METHODS = {
    "naive": "naive integration, unphased",
    "naive_phased": "naive integration with autophase",
    "fit": "lineshape fit (yield_pct)",
    "naive_doublet": "naive unphased, doublet region",
    "naive_singlet": "naive unphased, singlet region",
    "naive_phased_doublet": "naive autophase, doublet region",
    "naive_phased_singlet": "naive autophase, singlet region",
    "fit_doublet": "fit, doublet",
    "fit_singlet": "fit, singlet",
}
MAIN_METHODS = ("naive", "naive_phased", "fit")
# Impurity size in points of singlet yield: (low, high, label). low <= size < high.
BANDS = ((0.0, 5.0, "under 5"), (5.0, 10.0, "5 to 10"), (10.0, 100.0, "10 and over"))
SWEEP_FACTOR = 4  # impurity sweep spectra per yield level, as a multiple of --replicates
SWEEP_STREAM = 1  # keeps the sweep's random draws apart from the cases'
FIGURE_YIELD = 45


def run_check(
    seed: int = 0,
    replicates: int = 3,
    yields: tuple[int, ...] = YIELDS,
    sweep_replicates: int | None = None,
) -> dict:
    """Synthesize and score every spectrum. Each (yield, replicate) uses the same random
    draws in every case, so the cases differ only by what the case changes.

    The impurity sweep draws its own spectra, each with an impurity of random
    size, and fits each one twice: with the impurity and with it left out (the
    same draws and the same noise otherwise).
    """
    from labauto.nmr_analysis import (
        LineshapeFitter,
        autophase,
        correct_baseline,
        naive_yields,
        naive_yields_autophased,
        one_blas_thread,
    )
    from labauto.virtual.nmr import NmrSettings, draw_params, ppm_axis, render

    fitter = LineshapeFitter()
    rows: list[dict] = []
    sweep: list[dict] = []
    figure: dict[str, tuple] = {}
    with one_blas_thread():
        for case, opts in CASES.items():
            cfg = NmrSettings(**opts.get("settings", {}))
            ppm = ppm_axis(cfg)
            for yi, y in enumerate(yields):
                for rep in range(replicates):
                    rng = np.random.default_rng([seed, yi, rep])
                    conversion = min(100.0, y + rng.uniform(*cfg.conversion_extra))
                    params = draw_params(
                        rng,
                        y / 100,
                        1 - conversion / 100,
                        cfg,
                        broad=opts.get("broad", False),
                        phase_error=opts.get("phase_error", False),
                        impurity_p=opts.get("impurity_p", 0.0),
                    )
                    spectrum = render(ppm, params, cfg, rng)
                    t0 = time.perf_counter()
                    naive = naive_yields(ppm, correct_baseline(ppm, spectrum)[0])
                    t_naive = time.perf_counter() - t0
                    t0 = time.perf_counter()
                    phased = naive_yields_autophased(ppm, spectrum)
                    t_phased = time.perf_counter() - t0
                    t0 = time.perf_counter()
                    fit = fitter.evaluate(ppm, spectrum, f"{case}-{y}-{rep}")
                    t_fit = time.perf_counter() - t0
                    found = autophase(ppm, correct_baseline(ppm, spectrum)[0])[1]
                    rows.append(
                        {
                            "case": case,
                            "true": float(y),
                            "yi": yi,
                            "rep": rep,
                            "impurity_points": 100 * params.impurity,
                            **_naive_row("naive", naive),
                            **_naive_row("naive_phased", phased),
                            "fit_doublet": fit.yield_from_doublet,
                            "fit_singlet": fit.yield_from_singlet,
                            "fit": fit.yield_pct,
                            "flags": list(fit.flags),
                            "excluded": fit.excluded,
                            "is_snr": fit.is_snr,
                            "autophase_err_deg": found - params.phase_deg,
                            "fit_phase_err_deg": None
                            if fit.phase_deg is None
                            else fit.phase_deg - params.phase_deg,
                            "t_naive": t_naive,
                            "t_naive_phased": t_phased,
                            "t_fit": t_fit,
                        }
                    )
                    if y == FIGURE_YIELD and rep == 0 and case in ("clean", "broad_lines"):
                        figure[case] = (ppm, spectrum, fitter.fit(ppm, spectrum), conversion)

        cfg = NmrSettings()
        ppm = ppm_axis(cfg)
        n_sweep = SWEEP_FACTOR * replicates if sweep_replicates is None else sweep_replicates
        for yi, y in enumerate(yields):
            for rep in range(n_sweep):
                rng = np.random.default_rng([seed, yi, rep, SWEEP_STREAM])
                conversion = min(100.0, y + rng.uniform(*cfg.conversion_extra))
                params = draw_params(rng, y / 100, 1 - conversion / 100, cfg, impurity_p=1.0)
                twin = copy.deepcopy(rng)  # the same noise for the spectrum without the impurity
                with_imp = fitter.evaluate(ppm, render(ppm, params, cfg, rng), "sweep")
                without = params.model_copy(update={"impurity": 0.0})
                clean = fitter.evaluate(ppm, render(ppm, without, cfg, twin), "sweep")
                sweep.append(
                    {
                        "true": float(y),
                        "impurity_points": 100 * params.impurity,
                        "flags": list(with_imp.flags),
                        "clean_flags": list(clean.flags),
                        "doublet": with_imp.yield_from_doublet,
                        "singlet": with_imp.yield_from_singlet,
                        "clean_doublet": clean.yield_from_doublet,
                    }
                )
    return {
        "rows": rows,
        "sweep": sweep,
        "figure": figure,
        "seed": seed,
        "replicates": replicates,
        "sweep_replicates": n_sweep,
    }


def _naive_row(key: str, values: tuple[float, float, float] | None) -> dict:
    if values is None:
        return {f"{key}_doublet": None, f"{key}_singlet": None, key: None}
    return {f"{key}_doublet": values[0], f"{key}_singlet": values[1], key: values[2]}


def _errors(rows: list[dict], method: str) -> list[float]:
    """Signed errors in points. A missing value counts as a 100 point error."""
    return [(r[method] - r["true"]) if r[method] is not None else 100.0 for r in rows]


def _stats(err: list[float]) -> dict:
    return {
        "mae": statistics.fmean(abs(e) for e in err),
        "max": max(abs(e) for e in err),
        "bias": statistics.fmean(err),
    }


def _band(points: float) -> str:
    return next(label for lo, hi, label in BANDS if lo <= points < hi)


def summarize(result: dict) -> dict:
    """Error statistics per case and method, flag rates per case, impurity detection by
    size band, and timing."""
    from labauto.nmr_analysis import NMR_CODES, NMR_SIGNAL_INCONSISTENT

    out: dict = {"cases": {}, "timing": {}, "detection": {}, "paired_doublet": {}}
    rows = result["rows"]
    for case in CASES:
        rs = [r for r in rows if r["case"] == case]
        kept = [r for r in rs if not r["excluded"]]
        kept_err = _errors(kept, "fit")
        out["cases"][case] = {
            "n": len(rs),
            "stats": {method: _stats(_errors(rs, method)) for method in METHODS},
            "kept": len(kept),
            "kept_mae": statistics.fmean(abs(e) for e in kept_err) if kept_err else None,
            "kept_max": max(abs(e) for e in kept_err) if kept_err else None,
            "flags": {code: sum(code in r["flags"] for r in rs) for code in NMR_CODES},
            "excluded": sum(r["excluded"] for r in rs),
            "median_snr": statistics.median(r["is_snr"] for r in rs if r["is_snr"] is not None),
            "autophase_err_deg": statistics.fmean(abs(r["autophase_err_deg"]) for r in rs),
            "fit_phase_err_deg": statistics.fmean(
                abs(r["fit_phase_err_deg"]) for r in rs if r["fit_phase_err_deg"] is not None
            ),
        }

    # The clean and impurity cases share every draw, so their spectra pair up.
    clean = {(r["yi"], r["rep"]): r for r in rows if r["case"] == "clean"}
    impure = [r for r in rows if r["case"] == "impurity"]
    for method in ("fit_doublet", "fit_singlet", "fit", "naive_phased_doublet"):
        change = [
            abs(r[method] - clean[(r["yi"], r["rep"])][method])
            for r in impure
            if r[method] is not None and clean[(r["yi"], r["rep"])][method] is not None
        ]
        out["paired_doublet"][method] = {
            "clean": out["cases"]["clean"]["stats"][method],
            "impurity": out["cases"]["impurity"]["stats"][method],
            "change_mean": statistics.fmean(change) if change else None,
            "change_max": max(change) if change else None,
        }

    sweep = result["sweep"]
    groups = {"none": sweep, **{label: [] for _, _, label in BANDS}, "all": sweep}
    for s in sweep:
        groups[_band(s["impurity_points"])].append(s)
    for label, ss in groups.items():
        flags_key = "clean_flags" if label == "none" else "flags"
        moved = [
            abs(s["doublet"] - s["clean_doublet"])
            for s in ss
            if s["doublet"] is not None and s["clean_doublet"] is not None
        ]
        gaps = [s["singlet"] - s["doublet"] for s in ss if s["singlet"] is not None]
        out["detection"][label] = {
            "n": len(ss),
            "inconsistent": sum(NMR_SIGNAL_INCONSISTENT in s[flags_key] for s in ss),
            "any_flag": sum(bool(s[flags_key]) for s in ss),
            "size_range": (
                (min(s["impurity_points"] for s in ss), max(s["impurity_points"] for s in ss))
                if ss and label != "none"
                else None
            ),
            "gap_mean": statistics.fmean(gaps) if gaps and label != "none" else None,
            "doublet_change_mean": statistics.fmean(moved) if moved and label != "none" else None,
            "doublet_change_max": max(moved) if moved and label != "none" else None,
        }

    out["timing"] = {
        "naive": statistics.fmean(r["t_naive"] for r in rows),
        "naive_phased": statistics.fmean(r["t_naive_phased"] for r in rows),
        "fit": statistics.fmean(r["t_fit"] for r in rows),
        "spectra": len(rows),
    }
    return out


def _pct(k: int, n: int) -> str:
    return f"{100 * k / n:.0f}%" if n else "n/a"


def _count(k: int, n: int) -> str:
    return f"{k}/{n} ({_pct(k, n)})" if n else "none drawn"


def _compare(a: float, b: float) -> str:
    """How error a compares with error b, in words."""
    if abs(a - b) < 0.1 * max(a, b, 0.1):
        return "about the same as"
    return "smaller than" if a < b else "larger than"


def _ms(seconds: float) -> str:
    """Milliseconds to two significant figures."""
    ms = 1000 * seconds
    return f"{ms:.2g}" if ms < 100 else f"{ms:.0f}"


def reading(summary: dict) -> list[str]:
    """One sentence group per case, built from the numbers, so the words cannot drift from the tables."""
    from labauto.nmr_analysis import FitLimits

    flag_points = FitLimits().inconsistent_points
    c, det, pair = summary["cases"], summary["detection"], summary["paired_doublet"]

    def s(case, method, key="mae"):
        return c[case]["stats"][method][key]

    def share(case, code):
        return _pct(c[case]["flags"][code], c[case]["n"])

    def caught(label):
        d = det[label]
        return _count(d["inconsistent"], d["n"])

    out = [
        f"- clean: the fit's mean abs error is {s('clean', 'fit'):.2f} points, "
        f"{_compare(s('clean', 'fit'), s('clean', 'naive_phased'))} naive integration with "
        f"autophase ({s('clean', 'naive_phased'):.2f}). Naive integration of the singlet region "
        f"alone gives {s('clean', 'naive_phased_singlet'):.2f}; its doublet region gives "
        f"{s('clean', 'naive_phased_doublet'):.2f}, because the line tails of the two doublets "
        "cross the fixed boundary between them at 7.335 ppm.",
        f"- broad_lines: naive integration with autophase grows to "
        f"{s('broad_lines', 'naive_phased'):.2f} (doublet region "
        f"{s('broad_lines', 'naive_phased_doublet'):.2f}); the fit's error is "
        f"{s('broad_lines', 'fit'):.2f}. NMR_BROAD_LINES is raised on "
        f"{share('broad_lines', 'NMR_BROAD_LINES')} of these spectra.",
        f"- phase_error: unphased naive integration is off by "
        f"{s('phase_error', 'naive', 'bias'):+.2f} points on average. With autophase it is "
        f"off by {s('phase_error', 'naive_phased', 'bias'):+.2f} (mean abs error "
        f"{s('phase_error', 'naive_phased'):.2f}), which is the fair comparison. The fit's mean "
        f"abs error is {s('phase_error', 'fit'):.2f}, and NMR_PHASE_CORRECTED is raised on "
        f"{share('phase_error', 'NMR_PHASE_CORRECTED')}. The phase each finds is off by "
        f"{c['phase_error']['autophase_err_deg']:.1f} degrees (autophase) and "
        f"{c['phase_error']['fit_phase_err_deg']:.1f} degrees (fit) on average.",
        f"- low_signal: the fit's errors reach {s('low_signal', 'fit', 'max'):.2f} points. "
        f"NMR_LOW_SNR is raised on {share('low_signal', 'NMR_LOW_SNR')} of these spectra and "
        f"{_pct(c['low_signal']['excluded'], c['low_signal']['n'])} are excluded. Naive "
        f"integration with autophase is {_compare(s('low_signal', 'naive_phased'), s('low_signal', 'naive'))} "
        f"unphased here ({s('low_signal', 'naive_phased'):.2f} against "
        f"{s('low_signal', 'naive'):.2f}): a phase taken from a weak standard is off by "
        f"{c['low_signal']['autophase_err_deg']:.1f} degrees on average (against "
        f"{c['clean']['autophase_err_deg']:.1f} in the clean case), and a small phase error "
        "lets the dispersion tail of the large water peak change the area in the singlet region.",
        f"- impurity: the fitted doublet's mean abs error is {s('impurity', 'fit_doublet'):.2f}, "
        f"against {s('clean', 'fit_doublet'):.2f} on the same spectra without the impurity "
        f"(mean change {pair['fit_doublet']['change_mean']:.2f}, largest "
        f"{pair['fit_doublet']['change_max']:.2f}). The impurity goes into the singlet "
        f"(bias {s('impurity', 'fit_singlet', 'bias'):+.2f}), and so half of it into yield_pct "
        f"(bias {s('impurity', 'fit', 'bias'):+.2f}). The fit does not remove an impurity; it "
        f"keeps the doublet clean of it and flags the gap when it is over "
        f"{flag_points:g} points.",
        f"- impurity sweep: NMR_SIGNAL_INCONSISTENT catches {caught('under 5')} impurities under "
        f"5 points, {caught('5 to 10')} from 5 to 10 points, and {caught('10 and over')} of 10 "
        f"points and over. On the same spectra without the impurity it is raised on "
        f"{caught('none')}. The flag keeps the well, so a person or the reviewer decides what to "
        f"do with it. {det['under 5']['n'] - det['under 5']['any_flag']} of the "
        f"{det['under 5']['n']} impurities under 5 points pass every per-spectrum flag of the fitter unflagged (plate-level rules do not run in this check).",
    ]
    return out


def write_markdown(result: dict, summary: dict, md_path: Path, png_path: Path, command: str):
    """docs/nmr.md: the tables, the settings they came from, and the command."""
    from labauto.nmr_analysis import (
        BASELINE_ORDER,
        BOUNDS,
        NAIVE_REGIONS,
        NMR_CODES,
        FitLimits,
        LineshapeFitter,
    )
    from labauto.planner_eval import git_commit
    from labauto.virtual.nmr import NmrSettings

    cfg, lim = NmrSettings(), FitLimits()
    cases = summary["cases"]
    n_case = next(iter(cases.values()))["n"]
    n_sweep = summary["detection"]["all"]["n"]
    j_lo, j_hi = BOUNDS["j_hz"]
    imp_lo, imp_hi = (100 * a for a in cfg.impurity_amount)

    def region(name):
        lo, hi = NAIVE_REGIONS[name]
        return f"{lo:g} to {hi:g}"

    lines = [
        "# NMR readout check",
        "",
        "The spectra and the compounds in them are synthetic. The substrate, product and internal "
        "standard are placeholder species with made up signals. No real molecule and no real "
        "spectrometer is modeled. These numbers say how the evaluator behaves on this simulator, "
        "not how it would do on real samples.",
        "",
        f"Command: `{command}`",
        "",
        "## What was measured",
        "",
        f"Each case has {n_case} spectra: true yields of {YIELDS[0]} to {YIELDS[-1]} % in steps "
        f"of 10, {result['replicates']} replicates each, seed {result['seed']}. "
        "Conversion is the yield plus 2 to 8 points, so some substrate is always left except near "
        "100 %. Each replicate uses the same random draws in every case, so the cases differ only "
        "by what the case changes. The error of a method is its yield minus the yield the "
        "spectrum was built with, in percentage points.",
        "",
        "Each spectrum here is built at exactly the yield in the table. Plate spectra (as "
        "`labauto nmr` and the reviewer fault cases make them) are not: each well starts from "
        "its true yield plus an "
        f"independent normal draw with sd {cfg.yield_noise_sd:g} points, which stands for "
        "instrument and sample handling error. That draw comes from the NMR's own random "
        "stream, so it is independent of the LC/MS draw for the same well. nmr-check skips "
        "that draw, so the errors below are evaluator error only.",
        "",
        f"Simulated spectra: {cfg.points} points from {cfg.ppm_min:g} to {cfg.ppm_max:g} ppm at "
        f"{cfg.spectrometer_mhz:g} MHz. Lines {cfg.linewidth_hz:g} Hz wide at half height, "
        f"{100 * (1 - cfg.gaussian_fraction):.0f}% Lorentzian, Gaussian noise with sd "
        f"{cfg.noise_sd:g}, a slow polynomial baseline, random shift offsets and a random phase "
        f"of about {cfg.phase_sd_deg:g} degree. Internal standard singlet at 6.10 ppm (3H). "
        "Substrate: doublet 7.32 ppm (J 8 Hz, 2H) and singlet 3.80 ppm (3H). Product: doublet "
        "7.35 ppm (J 8 Hz, 2H) and singlet 3.86 ppm (3H). The doublets sit side by side, with "
        "inner lines 4 Hz apart (7.33 and 7.34 ppm). The lines are resolved, but their tails "
        "cross any fixed boundary between them. "
        "Nuisance peaks: a solvent singlet at 2.50 ppm and broad water near 3.33 ppm.",
        "",
        "Cases:",
        "",
        "- clean: the defaults above.",
        f"- broad_lines: poor shim, lines {cfg.broad_factor[0]:g} to {cfg.broad_factor[1]:g} times "
        f"wider and {100 * (1 - cfg.broad_gaussian_fraction):.0f}% Lorentzian.",
        f"- phase_error: a zero-order phase error of {cfg.phase_error_deg:g} degrees.",
        "- low_signal: a dilute sample. Every sample signal is 10 times smaller; the noise is the same.",
        f"- impurity: an extra 3H singlet under the product singlet in every spectrum, "
        f"{imp_lo:g} to {imp_hi:g} % of the standard's amount. It raises the singlet's yield by "
        f"{imp_lo:g} to {imp_hi:g} points, so its size straddles the {lim.inconsistent_points:g} "
        "point consistency flag.",
        "",
        f"Impurity sweep: {n_sweep} more spectra ({result['sweep_replicates']} per yield level, "
        "their own random draws), each with an impurity drawn the same way. Each is fitted twice: "
        "with the impurity, and with the impurity left out but every other draw and the noise "
        "the same. This measures how often the consistency flag catches an impurity of a given "
        "size, and how often it fires without one.",
        "",
        "Methods:",
        "",
        "- naive integration, unphased: subtract the same polynomial baseline the fitter uses, "
        f"then sum fixed ppm regions (standard {region('standard')}, product doublet "
        f"{region('product_doublet')}, product singlet {region('product_singlet')}) after "
        "referencing the axis so the standard's tallest point is at 6.10 ppm. No fitting and no "
        "phase correction. Its yield is the mean of the doublet and singlet estimates. The "
        "evaluator reports this one as `naive_yield_pct`.",
        "- naive integration with autophase: the same, after a zero-order phase correction "
        "taken from the internal standard alone (the imaginary part is rebuilt with a Hilbert "
        "transform, and the phase is the angle of the standard's complex integral). This is "
        "the fair comparison for phase errors.",
        f"- lineshape fit: `{LineshapeFitter.name}` in `src/labauto/nmr_analysis.py`. Least squares "
        "of the known signal patterns with fitted phase, shifts, line widths and line shape. "
        "Each of the four sample signals has its own shift offset and width ratio, and the "
        "singlets have their own line shape share, so the doublet and singlet are independent "
        "estimates that share only the phase, the overall shift, and the standard's area "
        "and line width. Its yield "
        "(`yield_pct`) is the plain mean of the doublet and singlet estimates, flagged or not.",
        "",
        "### Why the fitter's errors here are a lower bound",
        "",
        "The fitter is scored on the simulator it was written next to. It knows almost "
        "everything the simulator does:",
        "",
        "- the same pseudo-Voigt line function (a Lorentzian and Gaussian mix, with matching "
        "dispersion parts for phasing);",
        "- the same centres, multiplicities and proton counts for every signal;",
        f"- J bounded to {j_lo:g} to {j_hi:g} Hz around the simulated {cfg.j_hz:g} Hz;",
        "- first-order doublets with equal line heights (no roofing), as simulated;",
        "- a zero-order phase only, which is the only phase error the simulator makes;",
        f"- a baseline family that contains the simulator's: the simulator adds a Legendre "
        f"polynomial of degree {cfg.baseline_terms - 1} over the whole axis, and the fitter "
        f"subtracts a degree {BASELINE_ORDER} Legendre fit over the same axis.",
        "",
        "Not modelled by either: T1 weighting (signals that relax at different rates between "
        "scans), 13C satellites, second-order roofing of close doublets, first-order (frequency "
        "dependent) phase errors, and asymmetric line shapes from poor shims. Real spectra have "
        "all of these, so real errors would be larger.",
        "",
        "## Yield error by case and method",
        "",
        "All spectra, flagged or not. Points of yield.",
        "",
        "| Case | Method | Mean abs error | Max abs error | Mean error (bias) |",
        "|---|---|---|---|---|",
    ]
    for case, c in cases.items():
        for method in MAIN_METHODS:
            s = c["stats"][method]
            lines.append(
                f"| {case} | {METHODS[method]} | {s['mae']:.2f} | {s['max']:.2f} | {s['bias']:+.2f} |"
            )
    lines += ["", "## What the numbers show", "", *reading(summary)]
    signal_cols = (
        "naive_doublet",
        "naive_singlet",
        "naive_phased_doublet",
        "naive_phased_singlet",
        "fit_doublet",
        "fit_singlet",
    )
    lines += [
        "",
        "## Error by signal",
        "",
        "Mean abs error / max abs error, points of yield, all spectra.",
        "",
        "| Case | " + " | ".join(METHODS[m] for m in signal_cols) + " |",
        "|---|" + "---|" * len(signal_cols),
    ]
    for case, c in cases.items():
        cells = [f"{c['stats'][m]['mae']:.2f} / {c['stats'][m]['max']:.2f}" for m in signal_cols]
        lines.append(f"| {case} | " + " | ".join(cells) + " |")

    pair = summary["paired_doublet"]
    lines += [
        "",
        "## Clean and impurity side by side",
        "",
        f"The clean and impurity cases are the same {n_case} spectra except for the impurity. "
        "Mean abs error / max abs error in points, and how far each estimate moved when the "
        "impurity was added (mean / largest).",
        "",
        "| Estimate | Clean | With impurity | Change, same spectrum |",
        "|---|---|---|---|",
    ]
    for method, label in (
        ("fit_doublet", "fit, doublet"),
        ("fit_singlet", "fit, singlet"),
        ("fit", "fit, yield_pct"),
        ("naive_phased_doublet", "naive autophase, doublet region"),
    ):
        p = pair[method]
        change = (
            "n/a" if p["change_mean"] is None else f"{p['change_mean']:.2f} / {p['change_max']:.2f}"
        )
        lines.append(
            f"| {label} | {p['clean']['mae']:.2f} / {p['clean']['max']:.2f} | "
            f"{p['impurity']['mae']:.2f} / {p['impurity']['max']:.2f} | {change} |"
        )

    det = summary["detection"]
    lines += [
        "",
        "## Impurity detection by size",
        "",
        f"Impurity sweep, {n_sweep} spectra. Size is how many points the impurity adds to the "
        "singlet's yield (100 times its amount relative to the standard). The flag is "
        f"NMR_SIGNAL_INCONSISTENT (doublet and singlet more than {lim.inconsistent_points:g} "
        "points apart). Any flag counts every per spectrum code the fitter raised. The first "
        "row is the same spectra with the impurity left out, so its rates are false alarms. "
        "Doublet change is how far the fitted doublet moved when the impurity was added.",
        "",
        "| Impurity size, points | Spectra | Sizes drawn | NMR_SIGNAL_INCONSISTENT | Any flag | "
        "Fitted singlet minus doublet, mean | Doublet change, mean / largest |",
        "|---|---|---|---|---|---|---|",
    ]
    for label, d in det.items():
        name = "none (same spectra, no impurity)" if label == "none" else label
        if label == "all":
            name = "all sizes"
        sizes = "n/a" if d["size_range"] is None else "{:.1f} to {:.1f}".format(*d["size_range"])
        gap = "n/a" if d["gap_mean"] is None else f"{d['gap_mean']:+.2f}"
        moved = (
            "n/a"
            if d["doublet_change_mean"] is None
            else f"{d['doublet_change_mean']:.2f} / {d['doublet_change_max']:.2f}"
        )
        lines.append(
            f"| {name} | {d['n']} | {sizes} | {_count(d['inconsistent'], d['n'])} | "
            f"{_count(d['any_flag'], d['n'])} | {gap} | {moved} |"
        )

    lines += [
        "",
        "## Quality flags raised by the fitter",
        "",
        f"Share of spectra with each flag. Thresholds: fit residual over {lim.max_rmse_over_noise:g} "
        f"times the noise (NMR_FIT_POOR), standard peak under {lim.min_is_snr:g} noise sd "
        f"(NMR_LOW_SNR), doublet and singlet yields more than {lim.inconsistent_points:g} points "
        f"apart (NMR_SIGNAL_INCONSISTENT), fitted phase over {lim.phase_flag_deg:g} degrees "
        f"(NMR_PHASE_CORRECTED), standard line wider than {lim.broad_flag_hz:g} Hz "
        "(NMR_BROAD_LINES). Fit poor and low SNR exclude the well; the others keep it.",
        "",
        "| Case | Spectra | Median standard SNR | "
        + " | ".join(NMR_CODES)
        + " | Excluded | Fit max abs error, kept spectra |",
        "|---|---|---|" + "---|" * len(NMR_CODES) + "---|---|",
    ]
    for case, c in cases.items():
        kept_max = "none kept" if c["kept_max"] is None else f"{c['kept_max']:.2f}"
        lines.append(
            f"| {case} | {c['n']} | {c['median_snr']:.0f} | "
            + " | ".join(_pct(c["flags"][code], c["n"]) for code in NMR_CODES)
            + f" | {_pct(c['excluded'], c['n'])} | {kept_max} |"
        )
    t = summary["timing"]
    lines += [
        "",
        "## Speed",
        "",
        f"Approximate. Mean wall time per spectrum over the {t['spectra']} case spectra, from one "
        "run on the machine that ran the command, with linear algebra on one thread. These "
        "times depend on the machine and on what else it was doing; read them as an order of "
        "magnitude.",
        "",
        "| Method | Milliseconds per spectrum, about |",
        "|---|---|",
        f"| naive integration, unphased | {_ms(t['naive'])} |",
        f"| naive integration with autophase | {_ms(t['naive_phased'])} |",
        f"| lineshape fit | {_ms(t['fit'])} |",
        "",
        "## Figure",
        "",
        f"The 7.2 to 7.5 ppm region of one spectrum at {FIGURE_YIELD} % yield, clean and with "
        "broad lines, with the fitted product and substrate doublets drawn on top. The dashed "
        "line is the fixed boundary naive integration uses between the two doublets.",
        "",
        f"![NMR fit]({Path(os.path.relpath(png_path, md_path.parent)).as_posix()})",
        "",
        f"Date: {datetime.now(timezone.utc).date().isoformat()}  ",
        f"Git commit: {git_commit()}",
        "",
    ]
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(lines), encoding="utf-8")


def plot_fit(figure: dict, png_path: Path) -> None:
    """Two panels: the doublets side by side, data with the fitted components drawn on top."""
    from labauto.config import repo_root

    os.environ.setdefault("MPLCONFIGDIR", str(repo_root() / ".labauto" / "mpl"))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ink, muted, faint = "#0b0b0b", "#6f6e69", "#b0afa8"
    product, substrate = "#2a78d6", "#eb6834"
    titles = {"clean": "Clean", "broad_lines": "Broad lines (poor shim)"}
    panels = [c for c in ("clean", "broad_lines") if c in figure]
    fig, axes = plt.subplots(1, len(panels), figsize=(5.2 * len(panels), 4.2), dpi=120)
    axes = np.atleast_1d(axes)
    for ax, case in zip(axes, panels, strict=True):
        ppm, _, res, conversion = figure[case]
        m = (ppm >= 7.2) & (ppm <= 7.5)
        x, data = ppm[m], res.corrected[m]
        prod = res.component(x, "product_doublet")
        sub = res.component(x, "substrate_doublet")
        inside = (x >= 7.24) & (x <= 7.43)
        total = prod + sub + res.baseline(x, "aromatic")
        resid = data - total
        offset = -(0.12 * float(np.max(data)) + 5 * res.noise_sd)
        ax.plot(x, data, color=muted, linewidth=1.0, label="spectrum (baseline corrected)")
        ax.plot(x, prod, color=product, linewidth=2, label="fitted product doublet")
        ax.plot(x, sub, color=substrate, linewidth=2, label="fitted substrate doublet")
        ax.plot(x[inside], total[inside], color=ink, linewidth=1, linestyle=":", label="fit total")
        ax.plot(
            x[inside], resid[inside] + offset, color=faint, linewidth=0.8, label="residual (offset)"
        )
        ax.axvline(7.335, color=ink, linewidth=0.8, linestyle="--", label="naive region boundary")
        ax.set_xlim(7.5, 7.2)
        ax.set_xlabel("Chemical shift (ppm)")
        ax.set_title(
            f"{titles[case]}: yield {FIGURE_YIELD} %, substrate left {100 - conversion:.0f} %",
            fontsize=10,
        )
        ax.grid(alpha=0.25)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    axes[0].set_ylabel("Intensity (arbitrary units)")
    axes[0].legend(loc="upper left", fontsize=7, frameon=False)
    fig.suptitle("Synthetic spectra, placeholder compounds", fontsize=11)
    fig.tight_layout()
    png_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png_path)
    plt.close(fig)


def shown_path(path: str | Path) -> str:
    """A path as the docs may show it: relative to the working folder, or the file name
    alone. Never an absolute path."""
    p = Path(path)
    if not p.is_absolute():
        return p.as_posix()
    try:
        return p.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return p.name


def cmd_nmr_check(args: argparse.Namespace) -> int:
    start = time.perf_counter()
    result = run_check(seed=args.seed, replicates=args.replicates)
    summary = summarize(result)
    md_path, png_path = Path(args.out), Path(args.png)
    command = (
        f"labauto nmr-check --out {shown_path(args.out)} --png {shown_path(args.png)} "
        f"--seed {args.seed}"
    ) + (f" --replicates {args.replicates}" if args.replicates != 3 else "")
    plot_fit(result["figure"], png_path)
    write_markdown(result, summary, md_path, png_path, command)
    print(
        f"{'case':<13} {'naive MAE':>9} {'autophase MAE':>13} {'fit MAE':>8} {'fit max':>8}  flagged"
    )
    for case, c in summary["cases"].items():
        st = c["stats"]
        flagged = sum(1 for r in result["rows"] if r["case"] == case and r["flags"])
        print(
            f"{case:<13} {st['naive']['mae']:9.2f} {st['naive_phased']['mae']:13.2f} "
            f"{st['fit']['mae']:8.2f} {st['fit']['max']:8.2f}  {flagged}/{c['n']}"
        )
    print("impurity size   flagged NMR_SIGNAL_INCONSISTENT")
    for label, d in summary["detection"].items():
        print(f"{label:<15} {_count(d['inconsistent'], d['n'])}")
    print(
        f"Synthetic spectra. Wrote {md_path} and {png_path} in {time.perf_counter() - start:.1f} s."
    )
    return 0


def add_parsers(sub) -> None:
    """Register this module's commands on the main parser."""
    p = sub.add_parser(
        "nmr-check", help="score the NMR evaluator on synthetic spectra with known yields"
    )
    p.add_argument("--out", default="docs/nmr.md")
    p.add_argument("--png", default="docs/img/nmr_fit.png")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--replicates", type=int, default=3, help="spectra per yield level and case")
    p.set_defaults(func=cmd_nmr_check)
