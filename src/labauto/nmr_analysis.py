"""Turn 1H NMR spectra into yields, with diagnostics and quality flags.

The evaluator is a slot. Anything with a `name` and an `evaluate(ppm,
intensity, well) -> NmrFit` method fits it. The one built here is a classical
line shape fitter. It is the purpose-built, per-spectrum evaluator; a review
model reads its output, not the spectra.

1. Baseline: fit a low order polynomial to regions with no signal and subtract it.
   The noise level is measured on the same regions.
2. Fit: nonlinear least squares (scipy.optimize.least_squares) of the known
   signal patterns in three small regions only (aromatic, methoxy, standard).
   Fitted: one zero-order phase and one shift offset for the whole spectrum,
   the standard's line width, and J (between 7.5 and 8.5 Hz). Every sample
   signal (substrate doublet, product doublet, substrate singlet, product
   singlet) has its own small shift offset and its own width ratio to the
   standard (0.6 to 1.6). The standard and the two doublets share one Gaussian
   share for the line shape; the two singlets share another. So the doublet and
   singlet estimates share only the phase, the overall shift, and the
   standard's area and width, and something extra under a singlet cannot pull a
   doublet with it. Areas and
   a straight local baseline per region are solved by linear least squares at
   each step.
3. Yield from the product doublet and from the product singlet, each against
   the internal standard per proton. yield_pct is their plain mean, flagged or
   not; NMR_SIGNAL_INCONSISTENT says when the two disagree.

For comparison, naive_yield_pct integrates fixed ppm regions of the baseline
corrected spectrum, with no fitting and no phase correction (unphased).
naive_yields_autophased does the same after a zero-order phase taken from the
internal standard alone.

The signal patterns below are the evaluator's own reference data for the
placeholder compounds. It does not read the simulator's settings or truth. It
assumes a 400 MHz spectrometer unless built with another field. load_spectra
checks the field stored with the spectra (400 MHz unless told otherwise), and
analyze_plate_nmr, when given the field, checks it against the evaluator's.

Quality codes, per spectrum:
- NMR_FIT_POOR: the fit leaves more than 1.5 times the noise. Excluded.
- NMR_LOW_SNR: the internal standard peak is under 50 noise standard deviations. Excluded.
- NMR_SIGNAL_INCONSISTENT: the doublet and singlet yields differ by more than 5 points. Kept.
- NMR_PHASE_CORRECTED: the fitted phase is more than 5 degrees off. Kept.
- NMR_BROAD_LINES: the standard's line is wider than 2.5 Hz. Kept.
Per condition, as in analysis.py: REPLICATE_SPREAD and LOW_REPLICATES.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from pydantic import BaseModel, Field
from scipy.optimize import least_squares
from scipy.signal import hilbert
from scipy.special import dawsn

from labauto import labware
from labauto.blas import one_blas_thread
from labauto.models import Assay, Plan

NMR_FIT_POOR = "NMR_FIT_POOR"
NMR_LOW_SNR = "NMR_LOW_SNR"
NMR_SIGNAL_INCONSISTENT = "NMR_SIGNAL_INCONSISTENT"
NMR_PHASE_CORRECTED = "NMR_PHASE_CORRECTED"
NMR_BROAD_LINES = "NMR_BROAD_LINES"
NMR_CODES = (
    NMR_FIT_POOR,
    NMR_LOW_SNR,
    NMR_SIGNAL_INCONSISTENT,
    NMR_PHASE_CORRECTED,
    NMR_BROAD_LINES,
)

DEFAULT_MHZ = 400.0


@dataclass(frozen=True)
class Pattern:
    """One signal the fitter knows, and the fitted parameters that move it."""

    name: str
    region: str
    center: float  # ppm
    protons: int
    lines: int  # 2 = a doublet split by J
    shift: str | None  # its own shift offset, added to the overall one
    width: str | None  # its width as a ratio to the standard's
    shape: str  # its Gaussian share


PATTERNS = (
    Pattern("standard", "standard", 6.10, 3, 1, None, None, "gaussian_fraction"),
    Pattern(
        "substrate_doublet",
        "aromatic",
        7.32,
        2,
        2,
        "substrate_doublet_shift_ppm",
        "substrate_doublet_width_ratio",
        "gaussian_fraction",
    ),
    Pattern(
        "product_doublet",
        "aromatic",
        7.35,
        2,
        2,
        "product_doublet_shift_ppm",
        "product_doublet_width_ratio",
        "gaussian_fraction",
    ),
    Pattern(
        "substrate_singlet",
        "methoxy",
        3.80,
        3,
        1,
        "substrate_singlet_shift_ppm",
        "substrate_singlet_width_ratio",
        "singlet_gaussian_fraction",
    ),
    Pattern(
        "product_singlet",
        "methoxy",
        3.86,
        3,
        1,
        "product_singlet_shift_ppm",
        "product_singlet_width_ratio",
        "singlet_gaussian_fraction",
    ),
)
J_HZ = 8.0
FIT_REGIONS = {"aromatic": (7.24, 7.43), "methoxy": (3.72, 3.94), "standard": (6.02, 6.18)}
BASELINE_WINDOWS = ((0.2, 2.2), (4.3, 5.8), (6.4, 7.0), (7.7, 9.8))
BASELINE_ORDER = 4  # Legendre polynomial degree over the whole axis
AUTOPHASE_HALF_WIDTH_PPM = 0.03  # autophase window around the standard, and each flank's width
NAIVE_REGIONS = {
    "standard": (6.04, 6.16),
    "product_doublet": (7.335, 7.40),
    "product_singlet": (3.83, 3.92),
}

# name: (lower bound, upper bound). Signals share the sample's shim, so their
# widths are fitted as ratios to the standard's.
BOUNDS = {
    "phase_deg": (-90.0, 90.0),
    "shift_ppm": (-0.03, 0.03),
    "substrate_doublet_shift_ppm": (-0.004, 0.004),
    "product_doublet_shift_ppm": (-0.004, 0.004),
    "substrate_singlet_shift_ppm": (-0.004, 0.004),
    "product_singlet_shift_ppm": (-0.004, 0.004),
    "standard_width_hz": (0.3, 30.0),
    "substrate_doublet_width_ratio": (0.6, 1.6),
    "product_doublet_width_ratio": (0.6, 1.6),
    "substrate_singlet_width_ratio": (0.6, 1.6),
    "product_singlet_width_ratio": (0.6, 1.6),
    "gaussian_fraction": (0.0, 1.0),
    "singlet_gaussian_fraction": (0.0, 1.0),
    "j_hz": (7.5, 8.5),
}
PARAMS = tuple(BOUNDS)
LOWER = np.array([lo for lo, _ in BOUNDS.values()])
UPPER = np.array([hi for _, hi in BOUNDS.values()])
_AT = {name: i for i, name in enumerate(PARAMS)}
# The components each parameter moves. The phase moves none: it only rotates them.
MOVES = {
    name: [
        c
        for c in PATTERNS
        if name in (c.shift, c.width, c.shape)
        or name in ("shift_ppm", "standard_width_hz")
        or (name == "j_hz" and c.lines == 2)
    ]
    for name in PARAMS
}


class NmrFit(BaseModel):
    """One spectrum, evaluated. Yields are in percent. None means the evaluator does not report it."""

    well: str
    yield_pct: float | None = None
    conversion_pct: float | None = None
    naive_yield_pct: float | None = None
    yield_from_doublet: float | None = None
    yield_from_singlet: float | None = None
    rmse_over_noise: float | None = None
    is_snr: float | None = None
    linewidth_hz: float | None = None
    phase_deg: float | None = None
    shift_offset_ppm: float | None = None
    flags: list[str] = Field(default_factory=list)
    excluded: bool = False
    exclude_reason: str = ""


class NmrCondition(BaseModel):
    """Per condition statistics over wells not excluded, the same way analysis.py does them."""

    condition_index: int
    conditions: dict[str, float] | None = None
    wells: list[str]
    mean: float | None = None
    sd: float | None = None
    n: int = 0
    flags: list[str] = Field(default_factory=list)


class SpectrumEvaluator(Protocol):
    """Anything that turns one spectrum into numbers plus diagnostics."""

    name: str

    def evaluate(self, ppm: np.ndarray, intensity: np.ndarray, well: str) -> NmrFit: ...


class FitLimits(BaseModel):
    """Thresholds for the per spectrum quality codes."""

    max_rmse_over_noise: float = 1.5
    min_is_snr: float = 50.0
    inconsistent_points: float = 5.0
    phase_flag_deg: float = 5.0
    broad_flag_hz: float = 2.5


# ---------------------------------------------------------------- shared steps


def _in(ppm: np.ndarray, lo: float, hi: float) -> np.ndarray:
    return (ppm >= lo) & (ppm <= hi)


def correct_baseline(ppm: np.ndarray, intensity: np.ndarray, order: int = BASELINE_ORDER):
    """Subtract a polynomial fitted to the signal free windows. Returns (corrected, noise sd)."""
    mask = np.zeros(ppm.shape, dtype=bool)
    for lo, hi in BASELINE_WINDOWS:
        mask |= _in(ppm, lo, hi)
    mid, half = (ppm[0] + ppm[-1]) / 2, (ppm[-1] - ppm[0]) / 2
    u = (ppm - mid) / half
    x, y = u[mask], intensity[mask]
    keep = np.ones(x.shape, dtype=bool)
    for _ in range(2):
        coef = np.polynomial.legendre.legfit(x[keep], y[keep], order)
        resid = y - np.polynomial.legendre.legval(x, coef)
        sd = 1.4826 * np.median(np.abs(resid[keep] - np.median(resid[keep])))
        keep = np.abs(resid) < 5 * sd if sd > 0 else keep
    corrected = intensity - np.polynomial.legendre.legval(u, coef)
    return corrected, float(sd)


def naive_yields(ppm: np.ndarray, corrected: np.ndarray) -> tuple[float, float, float] | None:
    """Fixed region integrals of a baseline corrected spectrum, no fitting.

    The axis is first referenced so the standard's tallest point sits at 6.10
    ppm, as routine processing does. Returns (from the doublet region, from
    the singlet region, their mean), or None when the standard's region
    integrates to nothing.
    """
    dx = abs(float(ppm[1] - ppm[0]))
    std = _in(ppm, 6.0, 6.2)
    ref = float(ppm[std][np.argmax(corrected[std])]) - 6.10
    area = {
        k: float(corrected[_in(ppm, lo + ref, hi + ref)].sum() * dx)
        for k, (lo, hi) in NAIVE_REGIONS.items()
    }
    per_h = area["standard"] / 3
    if per_h <= 0:
        return None
    doublet = 100 * (area["product_doublet"] / 2) / per_h
    singlet = 100 * (area["product_singlet"] / 3) / per_h
    return doublet, singlet, (doublet + singlet) / 2


def naive_yield(ppm: np.ndarray, intensity: np.ndarray) -> float | None:
    """The naive (unphased) yield of a raw spectrum, or None if its axis does not cover the regions used."""
    ppm, intensity = np.asarray(ppm, float), np.asarray(intensity, float)
    needed = [*BASELINE_WINDOWS, *NAIVE_REGIONS.values(), (6.0, 6.2)]
    if ppm.size != intensity.size or not all(_in(ppm, lo, hi).sum() > 2 for lo, hi in needed):
        return None
    naive = naive_yields(ppm, correct_baseline(ppm, intensity)[0])
    return None if naive is None else naive[2]


def autophase(ppm: np.ndarray, corrected: np.ndarray) -> tuple[np.ndarray, float]:
    """Zero-order phase taken from the internal standard alone, as routine processing does.

    A stored spectrum is real, so the imaginary part is rebuilt with a Hilbert
    transform along an ascending ppm axis. The phase is the angle of the
    standard's complex integral over a window centred on its tallest point in
    magnitude (which does not depend on the phase). The mean of two short flanks
    beside the window is taken off first: the dispersion tails of far peaks (the
    water above all) would otherwise tilt the phase of a weak standard.
    Returns (the rephased real spectrum, the phase removed, in degrees).
    """
    flip = ppm[0] > ppm[-1]
    x, y = (ppm[::-1], corrected[::-1]) if flip else (ppm, corrected)
    analytic = hilbert(y)
    std = _in(x, 6.0, 6.2)
    centre = float(x[std][np.argmax(np.abs(analytic[std]))])
    lo, hi = centre - AUTOPHASE_HALF_WIDTH_PPM, centre + AUTOPHASE_HALF_WIDTH_PPM
    inside = _in(x, lo, hi)
    flanks = _in(x, lo - AUTOPHASE_HALF_WIDTH_PPM, lo) | _in(x, hi, hi + AUTOPHASE_HALF_WIDTH_PPM)
    phase = float(np.angle((analytic[inside] - analytic[flanks].mean()).sum()))
    phased = (analytic * np.exp(-1j * phase)).real
    return (phased[::-1] if flip else phased), float(np.degrees(phase))


def naive_yields_autophased(
    ppm: np.ndarray, intensity: np.ndarray
) -> tuple[float, float, float] | None:
    """Naive integration with autophase: baseline, zero-order phase from the standard,
    baseline again, then the same fixed regions as naive_yields."""
    ppm, intensity = np.asarray(ppm, float), np.asarray(intensity, float)
    phased, _ = autophase(ppm, correct_baseline(ppm, intensity)[0])
    return naive_yields(ppm, correct_baseline(ppm, phased)[0])


def line(ppm: np.ndarray, center: float, fwhm_ppm: float, gaussian_fraction: float):
    """Unit area pseudo-Voigt line: absorption (real part) and dispersion (imaginary part)."""
    d = ppm - center
    gamma = fwhm_ppm / 2
    lorentz = (gamma + 1j * d) / (np.pi * (d * d + gamma * gamma))
    sigma = fwhm_ppm / (2 * np.sqrt(2 * np.log(2)))
    u = d / (sigma * np.sqrt(2))
    gauss = (np.exp(-u * u) + 1j * (2 / np.sqrt(np.pi)) * dawsn(u)) / (sigma * np.sqrt(2 * np.pi))
    return (1 - gaussian_fraction) * lorentz + gaussian_fraction * gauss


def peak_height(fwhm_ppm: float, gaussian_fraction: float) -> float:
    """Height of a unit area pseudo-Voigt line."""
    lorentz = 2 / (np.pi * fwhm_ppm)
    gauss = 2 * np.sqrt(np.log(2) / np.pi) / fwhm_ppm
    return float((1 - gaussian_fraction) * lorentz + gaussian_fraction * gauss)


# ---------------------------------------------------------------- the classical fitter


@dataclass
class LineshapeResult:
    """The fitted parameters and areas, kept so the fit can be drawn."""

    params: dict[str, float]
    areas: dict[str, float]
    noise_sd: float
    rmse_over_noise: float
    corrected: np.ndarray
    naive: tuple[float, float, float] | None
    local_baseline: dict[str, tuple[float, float]]
    mhz: float

    def component(self, ppm: np.ndarray, name: str) -> np.ndarray:
        """One fitted component (area times its phased line shape) on any ppm points."""
        theta = np.array([self.params[p] for p in PARAMS])
        pattern = next(p for p in PATTERNS if p.name == name)
        rot = np.exp(1j * np.deg2rad(theta[_AT["phase_deg"]]))
        return self.areas[name] * (rot * _shape(ppm, theta, self.mhz, pattern)).real

    def baseline(self, ppm: np.ndarray, region: str) -> np.ndarray:
        """The straight local baseline the fit used in one region."""
        lo, hi = FIT_REGIONS[region]
        b0, b1 = self.local_baseline[region]
        return b0 + b1 * (ppm - (lo + hi) / 2) / ((hi - lo) / 2)


def _shape(x: np.ndarray, theta: np.ndarray, mhz: float, pattern: Pattern) -> np.ndarray:
    """One component's unit area line shape at the points x, before phasing."""
    c = pattern.center + theta[_AT["shift_ppm"]]
    if pattern.shift:
        c += theta[_AT[pattern.shift]]
    width = theta[_AT["standard_width_hz"]]
    if pattern.width:
        width *= theta[_AT[pattern.width]]
    fwhm, g = width / mhz, theta[_AT[pattern.shape]]
    if pattern.lines == 1:
        return line(x, c, fwhm, g)
    half = theta[_AT["j_hz"]] / mhz / 2
    return 0.5 * (line(x, c - half, fwhm, g) + line(x, c + half, fwhm, g))


class LineshapeFitter:
    """The classical evaluator: baseline correction, then least squares of the known patterns."""

    name = "lineshape least squares (classical)"

    def __init__(self, spectrometer_mhz: float = DEFAULT_MHZ, limits: FitLimits | None = None):
        self.spectrometer_mhz = self.mhz = float(spectrometer_mhz)
        self.limits = limits or FitLimits()

    def fit(self, ppm: np.ndarray, intensity: np.ndarray) -> LineshapeResult | None:
        """Fit one spectrum. Returns None if the fit cannot be made.

        Each region is its own small linear problem (areas plus a straight
        baseline), sharing the nonlinear parameters. A component is evaluated
        only in its own region; tails from other regions are left to the
        local baselines.

        The Jacobian is a forward difference, as scipy's default, but each
        parameter step recomputes only the components that parameter moves
        and only the regions they sit in, which makes the fit about twice as fast.
        """
        corrected, noise = correct_baseline(ppm, intensity)
        blocks = []
        for region, (lo, hi) in FIT_REGIONS.items():
            m = _in(ppm, lo, hi)
            x = ppm[m]
            u = (x - (lo + hi) / 2) / ((hi - lo) / 2)
            comps = [p for p in PATTERNS if p.region == region]
            blocks.append((region, x, corrected[m], np.column_stack([np.ones_like(x), u]), comps))
        rows, start = {}, 0
        for region, x, *_ in blocks:
            rows[region] = slice(start, start + x.size)
            start += x.size
        x_of = {b[0]: b[1] for b in blocks}

        def shapes_at(theta, comps=PATTERNS):
            return {c.name: _shape(x_of[c.region], theta, self.mhz, c) for c in comps}

        def region_fit(block, rot, shapes):
            _, _, y, base, comps = block
            design = np.column_stack([*((rot * shapes[c.name]).real for c in comps), base])
            beta = np.linalg.lstsq(design, y, rcond=None)[0]
            return beta, y - design @ beta

        def rotation(theta):
            return np.exp(1j * np.deg2rad(theta[_AT["phase_deg"]]))

        def solve(theta):
            rot, shapes = rotation(theta), shapes_at(theta)
            fitted = {b[0]: region_fit(b, rot, shapes) for b in blocks}
            return {r: f[0] for r, f in fitted.items()}, np.concatenate(
                [f[1] for f in fitted.values()]
            )

        def jac(theta):
            rot, shapes = rotation(theta), shapes_at(theta)
            base = {b[0]: region_fit(b, rot, shapes)[1] for b in blocks}
            out = np.zeros((start, len(PARAMS)))
            for k, name in enumerate(PARAMS):
                h = np.sqrt(np.finfo(float).eps) * max(1.0, abs(theta[k]))
                if theta[k] + h > UPPER[k]:
                    h = -h
                step = theta.copy()
                step[k] += h
                moved = MOVES[name]
                new = {**shapes, **shapes_at(step, moved)}
                regions = {c.region for c in moved} if moved else set(rows)
                for b in blocks:
                    if b[0] in regions:
                        r = region_fit(b, rotation(step), new)[1]
                        out[rows[b[0]], k] = (r - base[b[0]]) / h
            return out

        theta0 = self._start(ppm, corrected, blocks)
        try:
            out = least_squares(
                lambda t: solve(t)[1], theta0, jac=jac, bounds=(LOWER, UPPER), x_scale="jac"
            )
        except (ValueError, np.linalg.LinAlgError):
            return None
        betas, resid = solve(out.x)
        areas, local = {}, {}
        for region, _, _, _, comps in blocks:
            beta = betas[region]
            for k, c in enumerate(comps):
                areas[c.name] = float(beta[k])
            local[region] = (float(beta[len(comps)]), float(beta[len(comps) + 1]))
        if not all(np.isfinite(v) for v in areas.values()):
            return None
        return LineshapeResult(
            params=dict(zip(PARAMS, map(float, out.x), strict=True)),
            areas=areas,
            noise_sd=noise,
            rmse_over_noise=float(np.sqrt(np.mean(resid**2)) / noise) if noise > 0 else 0.0,
            corrected=corrected,
            naive=naive_yields(ppm, corrected),
            local_baseline=local,
            mhz=self.mhz,
        )

    def _start(self, ppm: np.ndarray, corrected: np.ndarray, blocks: list) -> np.ndarray:
        """Starting values for the fit.

        The shift comes from the standard's tallest point. Width and phase come
        from fitting the standard's absorption and dispersion as two free areas
        (a linear problem) at a few trial widths.
        """
        std = _in(ppm, 6.0, 6.2)
        peak = ppm[std][np.argmax(corrected[std])]
        lo, hi = BOUNDS["shift_ppm"]
        start = {name: 0.0 for name in PARAMS}
        start.update({name: 1.0 for name in PARAMS if name.endswith("_width_ratio")})
        start.update(
            shift_ppm=float(np.clip(peak - 6.10, lo * 0.95, hi * 0.95)),
            gaussian_fraction=0.2,
            singlet_gaussian_fraction=0.2,
            j_hz=J_HZ,
        )
        _, x, y, base, comps = next(b for b in blocks if b[0] == "standard")
        best = None
        for width in (1.2, 2.5, 5.0, 10.0):
            theta = np.array([start[name] for name in PARAMS])
            theta[_AT["standard_width_hz"]] = width
            shape = _shape(x, theta, self.mhz, comps[0])
            design = np.column_stack([shape.real, -shape.imag, base])
            beta = np.linalg.lstsq(design, y, rcond=None)[0]
            cost = float(np.sum((y - design @ beta) ** 2))
            if best is None or cost < best[0]:
                phase = float(np.degrees(np.arctan2(beta[1], beta[0])))
                lo, hi = BOUNDS["phase_deg"]
                theta[_AT["phase_deg"]] = float(np.clip(phase, lo * 0.95, hi * 0.95))
                best = (cost, theta)
        return best[1]

    def evaluate(self, ppm: np.ndarray, intensity: np.ndarray, well: str) -> NmrFit:
        """Yields, diagnostics and quality flags for one spectrum."""
        res = self.fit(np.asarray(ppm, float), np.asarray(intensity, float))
        lim = self.limits
        if res is None or res.areas["standard"] <= 0:
            return NmrFit(
                well=well,
                naive_yield_pct=None if res is None or res.naive is None else res.naive[2],
                flags=[NMR_FIT_POOR],
                excluded=True,
                exclude_reason=NMR_FIT_POOR,
            )
        a, p = res.areas, res.params
        per_h = a["standard"] / 3
        doublet = 100 * (a["product_doublet"] / 2) / per_h
        singlet = 100 * (a["product_singlet"] / 3) / per_h
        substrate = ((a["substrate_doublet"] / 2) + (a["substrate_singlet"] / 3)) / 2 / per_h
        height = peak_height(p["standard_width_hz"] / self.mhz, p["gaussian_fraction"])
        snr = a["standard"] * height / res.noise_sd if res.noise_sd > 0 else float("inf")
        fit = NmrFit(
            well=well,
            yield_pct=(doublet + singlet) / 2,
            conversion_pct=100 * (1 - substrate),
            naive_yield_pct=None if res.naive is None else res.naive[2],
            yield_from_doublet=doublet,
            yield_from_singlet=singlet,
            rmse_over_noise=res.rmse_over_noise,
            is_snr=snr,
            linewidth_hz=p["standard_width_hz"],
            phase_deg=p["phase_deg"],
            shift_offset_ppm=p["shift_ppm"],
        )
        if res.rmse_over_noise > lim.max_rmse_over_noise:
            fit.flags.append(NMR_FIT_POOR)
        if snr < lim.min_is_snr:
            fit.flags.append(NMR_LOW_SNR)
        if abs(doublet - singlet) > lim.inconsistent_points:
            fit.flags.append(NMR_SIGNAL_INCONSISTENT)
        if abs(p["phase_deg"]) > lim.phase_flag_deg:
            fit.flags.append(NMR_PHASE_CORRECTED)
        if p["standard_width_hz"] > lim.broad_flag_hz:
            fit.flags.append(NMR_BROAD_LINES)
        reasons = [f for f in fit.flags if f in (NMR_FIT_POOR, NMR_LOW_SNR)]
        if reasons:
            fit.excluded = True
            fit.exclude_reason = "; ".join(reasons)
        return fit


EVALUATORS: dict[str, Callable[[], SpectrumEvaluator]] = {"lineshape": LineshapeFitter}
"""Evaluators by name. Only the classical fitter is here. A trained model that
implements SpectrumEvaluator can register under its own name and be passed to
analyze_plate_nmr in its place."""


# ---------------------------------------------------------------- one plate


def analyze_plate_nmr(
    ppm: np.ndarray,
    spectra: dict[str, np.ndarray],
    plan: Plan,
    assay: Assay,
    evaluator: SpectrumEvaluator | None = None,
    spectrometer_mhz: float | None = None,
) -> tuple[list[NmrFit], list[NmrCondition]]:
    """Evaluate every spectrum, then aggregate per condition the way analysis.py does for LC/MS.

    When spectrometer_mhz (the field the spectra were recorded at) is given and
    the evaluator states its own field, the two must match: a fitter that
    converts Hz to ppm at the wrong field gets every width and J wrong.
    """
    ev = evaluator or (
        LineshapeFitter(spectrometer_mhz) if spectrometer_mhz is not None else LineshapeFitter()
    )
    assumed = getattr(ev, "spectrometer_mhz", None)
    if (
        spectrometer_mhz is not None
        and assumed is not None
        and abs(assumed - spectrometer_mhz) > 1e-6
    ):
        raise ValueError(
            f"spectra were recorded at {spectrometer_mhz:g} MHz but the evaluator assumes {assumed:g} MHz"
        )
    fits = []
    with one_blas_thread():
        for well in sorted(spectra, key=labware.column_order_key):
            fit = ev.evaluate(ppm, spectra[well], well)
            if fit.naive_yield_pct is None:
                fit.naive_yield_pct = naive_yield(ppm, spectra[well])
            fits.append(fit)
    by_well = {f.well: f for f in fits}
    groups: dict[int, list] = defaultdict(list)
    for pw in plan.wells:
        if pw.role == "sample" and pw.condition_index is not None and pw.well in by_well:
            groups[pw.condition_index].append(pw)
    spread_max = assay.qc.replicate_spread_max
    conditions = []
    for ci, pws in sorted(groups.items()):
        wells = sorted((pw.well for pw in pws), key=labware.column_order_key)
        valid = [
            by_well[w].yield_pct
            for w in wells
            if not by_well[w].excluded and by_well[w].yield_pct is not None
        ]
        flags = []
        if len(valid) >= 2 and max(valid) - min(valid) > spread_max:
            flags.append("REPLICATE_SPREAD")
        if len(valid) < 2:
            flags.append("LOW_REPLICATES")
        for w in wells:
            by_well[w].flags += flags
        conditions.append(
            NmrCondition(
                condition_index=ci,
                conditions=pws[0].conditions,
                wells=wells,
                mean=statistics.fmean(valid) if valid else None,
                sd=statistics.stdev(valid) if len(valid) >= 2 else None,
                n=len(valid),
                flags=flags,
            )
        )
    return fits, conditions


def control_means(fits: list[NmrFit], plan: Plan) -> dict[str, float | None]:
    """Mean NMR yield of the negative and reference control wells, over wells not excluded."""
    roles = {pw.well: pw.role for pw in plan.wells}
    out: dict[str, float | None] = {}
    for key, role in (("negative", "negative_control"), ("reference", "reference_control")):
        vals = [
            f.yield_pct
            for f in fits
            if roles.get(f.well) == role and not f.excluded and f.yield_pct is not None
        ]
        out[key] = statistics.fmean(vals) if vals else None
    return out


def plate_flags(fits: list[NmrFit]) -> list[str]:
    """Per spectrum codes raised on at least half of the plate's wells, as plate level flags."""
    if not fits:
        return []
    return [
        code
        for code in (NMR_BROAD_LINES, NMR_PHASE_CORRECTED, NMR_LOW_SNR, NMR_FIT_POOR)
        if sum(code in f.flags for f in fits) * 2 >= len(fits)
    ]
