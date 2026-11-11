"""A simulated 1H NMR of the same wells the LC/MS reads. Spectra are synthetic.

The compounds are placeholders. No real molecule is modeled. Each well's
spectrum holds these signals (ppm, multiplicity, protons):

- internal standard: singlet 6.10, 3H. The same known amount goes into every
  well, equal to the starting amount of substrate (qNMR style), so
  yield = (product integral per H) / (standard integral per H) x 100.
- substrate: doublet 7.32 (J 8 Hz), 2H, and singlet 3.80, 3H.
- product: doublet 7.35 (J 8 Hz), 2H, and singlet 3.86, 3H.
- nuisances: a residual solvent singlet at 2.50 and a broad water peak near 3.33.

The doublets sit side by side (lines at 7.31 and 7.33, then 7.34 and 7.36).
The inner lines are only 4 Hz apart. They are resolved, but their Lorentzian
tails cross any fixed boundary between them, so a fixed integration region
picks up some substrate and loses some product.

Amounts follow the same as-run truth as the LC/MS: the true yield comes from
the ground truth surface at the volumes in the ledger. Each well's spectrum is
then built from the true yield plus its own normal draw (sd yield_noise_sd,
2.0 points), which stands for instrument and sample handling error. That draw
comes from the NMR's own stream, so it is independent of the LC/MS draw for
the same well. The remaining substrate is 1 - conversion, with conversion =
observed yield + 2 to 8 points, as in lcms.py.

Lines are pseudo-Voigt (mostly Lorentzian). Each spectrum also gets Gaussian
noise, a slow polynomial baseline roll, small random shift offsets, and a small
random phase error.

Faults:
- nmr_broad_lines@R: poor shim on round R. Lines are about 4 times wider and
  less Lorentzian.
- nmr_phase_error@R: a zero-order phase error of about 15 degrees on round R.
- nmr_impurity: each well with probability 0.05 gets an extra singlet (3H)
  under the product singlet at 3.86, at 0.02 to 0.20 of the standard's
  amount. That raises the singlet yield by 2 to 20 points, so the size
  straddles the 5 point consistency flag of the evaluator.
A fault named without @R applies to every round.

The NMR draws from its own random stream per well, derived from the seed, the
round, the well and a fixed offset, so adding it never changes an LC/MS
reading, and round 2 never repeats round 1's spectra.

spectra.npz stores the field (spectrometer_mhz) next to the spectra.
load_spectra checks it against the field the evaluator expects (400 MHz unless
told otherwise), because widths and J in Hz turn into ppm through it.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict, Field
from scipy.special import dawsn

from labauto import labware
from labauto.models import Assay, LedgerEntry, Plan
from labauto.virtual.ground_truth import Surface, true_yield
from labauto.virtual.lcms import as_run_conditions

NMR_STREAM = 0x4E4D52  # fixed offset that keeps the NMR random stream apart from the LC/MS
DEFAULT_MHZ = 400.0

STANDARD_PPM, STANDARD_H = 6.10, 3
SIGNALS = {
    # species: list of (center ppm, protons, number of lines split by J)
    "substrate": [(7.32, 2, 2), (3.80, 3, 1)],
    "product": [(7.35, 2, 2), (3.86, 3, 1)],
}
SOLVENT_PPM = 2.50
NMR_FAULTS = ("nmr_broad_lines", "nmr_phase_error", "nmr_impurity")


class NmrSettings(BaseModel):
    """Spectrometer, sample, noise and fault settings. Intensities are in arbitrary units.

    One proton at the amount of the internal standard gives a line of area 1
    (ppm x intensity). With the defaults the standard's peak is about 330
    noise standard deviations tall.
    """

    spectrometer_mhz: float = DEFAULT_MHZ
    points: int = 16384
    ppm_min: float = 0.0
    ppm_max: float = 10.0
    linewidth_hz: float = 1.2
    linewidth_jitter: float = 0.05
    gaussian_fraction: float = 0.1
    j_hz: float = 8.0
    noise_sd: float = 2.0
    baseline_amp: float = 4.0
    baseline_terms: int = 4  # Legendre terms over the whole axis, so degree 3
    yield_noise_sd: float = 2.0
    conversion_extra: tuple[float, float] = (2.0, 8.0)
    shift_sd_ppm: float = 0.003
    species_shift_sd_ppm: float = 0.0008
    phase_sd_deg: float = 1.0
    signal_scale: float = 1.0
    solvent_area: float = 0.6
    water_area: tuple[float, float] = (2.0, 6.0)
    water_ppm: float = 3.33
    water_ppm_sd: float = 0.01
    water_linewidth_hz: tuple[float, float] = (8.0, 15.0)
    broad_factor: tuple[float, float] = (3.5, 4.5)
    broad_gaussian_fraction: float = 0.4
    phase_error_deg: float = 15.0
    impurity_p: float = 0.05
    impurity_amount: tuple[float, float] = (0.02, 0.20)
    impurity_offset_ppm: float = 0.002


class SpectrumParams(BaseModel):
    """Everything that shapes one spectrum except the noise. Amounts are per unit of internal standard."""

    product: float
    substrate: float
    impurity: float = 0.0
    impurity_ppm: float = 3.86
    shift_ppm: float = 0.0
    species_shift_ppm: dict[str, float] = Field(default_factory=dict)
    linewidth_hz: dict[str, float] = Field(default_factory=dict)
    gaussian_fraction: float = 0.1
    phase_deg: float = 0.0
    baseline: list[float] = Field(default_factory=list)
    water_ppm: float = 3.33
    water_area: float = 4.0
    water_linewidth_hz: float = 10.0
    signal_scale: float = 1.0


class NmrPlate(BaseModel):
    """One plate's spectra, plus the hidden truth (kept for tests and scoring, never for evaluators)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    ppm: np.ndarray
    spectra: dict[str, np.ndarray]
    truth: dict[str, dict]
    plate_faults: list[str] = Field(default_factory=list)
    spectrometer_mhz: float = DEFAULT_MHZ


def ppm_axis(settings: NmrSettings | None = None) -> np.ndarray:
    """The chemical shift axis, ascending."""
    cfg = settings or NmrSettings()
    return np.linspace(cfg.ppm_min, cfg.ppm_max, cfg.points)


def complex_line(ppm: np.ndarray, center: float, fwhm_ppm: float, gaussian_fraction: float):
    """A pseudo-Voigt line of unit area: absorption in the real part, dispersion in the imaginary part."""
    d = ppm - center
    gamma = fwhm_ppm / 2.0
    lorentz = (gamma + 1j * d) / (np.pi * (d * d + gamma * gamma))
    sigma = fwhm_ppm / (2.0 * np.sqrt(2.0 * np.log(2.0)))
    u = d / (sigma * np.sqrt(2.0))
    gauss = (np.exp(-u * u) + 1j * (2.0 / np.sqrt(np.pi)) * dawsn(u)) / (
        sigma * np.sqrt(2.0 * np.pi)
    )
    return (1.0 - gaussian_fraction) * lorentz + gaussian_fraction * gauss


def draw_params(
    rng: np.random.Generator,
    product: float,
    substrate: float,
    settings: NmrSettings | None = None,
    *,
    broad: bool = False,
    phase_error: bool = False,
    impurity_p: float = 0.0,
) -> SpectrumParams:
    """Draw the nuisances for one spectrum. The draws happen in a fixed order whatever the
    faults, so turning a fault on changes only what that fault touches."""
    cfg = settings or NmrSettings()
    shift = rng.normal(0.0, cfg.shift_sd_ppm)
    species_shift = {s: rng.normal(0.0, cfg.species_shift_sd_ppm) for s in ("standard", *SIGNALS)}
    jitter = {
        s: 1.0 + rng.normal(0.0, cfg.linewidth_jitter)
        for s in ("standard", *SIGNALS, "solvent", "impurity")
    }
    broad_factor = rng.uniform(*cfg.broad_factor)
    phase = rng.normal(0.0, cfg.phase_sd_deg)
    baseline = list(rng.uniform(-1.0, 1.0, size=cfg.baseline_terms) * cfg.baseline_amp / 2.0)
    water_ppm = cfg.water_ppm + rng.normal(0.0, cfg.water_ppm_sd)
    water_area = rng.uniform(*cfg.water_area)
    water_width = rng.uniform(*cfg.water_linewidth_hz)
    hit = rng.random() < impurity_p
    amount = rng.uniform(*cfg.impurity_amount)
    offset = rng.uniform(-cfg.impurity_offset_ppm, cfg.impurity_offset_ppm)

    factor = broad_factor if broad else 1.0
    widths = {s: max(0.2, cfg.linewidth_hz * j * factor) for s, j in jitter.items()}
    return SpectrumParams(
        product=product,
        substrate=substrate,
        impurity=amount if hit else 0.0,
        impurity_ppm=SIGNALS["product"][1][0] + offset,
        shift_ppm=shift,
        species_shift_ppm=species_shift,
        linewidth_hz=widths,
        gaussian_fraction=cfg.broad_gaussian_fraction if broad else cfg.gaussian_fraction,
        phase_deg=phase + (cfg.phase_error_deg if phase_error else 0.0),
        baseline=baseline,
        water_ppm=water_ppm,
        water_area=water_area,
        water_linewidth_hz=water_width * (factor if broad else 1.0),
        signal_scale=cfg.signal_scale,
    )


def render(
    ppm: np.ndarray,
    params: SpectrumParams,
    settings: NmrSettings | None = None,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Build the real (phased) spectrum from its parameters, then add the baseline and noise."""
    cfg = settings or NmrSettings()
    hz = 1.0 / cfg.spectrometer_mhz  # ppm per Hz
    g = params.gaussian_fraction
    total = np.zeros(ppm.shape, dtype=complex)

    def add(center: float, area: float, width_hz: float, lines: int = 1, shape_g: float = g):
        nonlocal total
        if area == 0:
            return
        offsets = [0.0] if lines == 1 else [-cfg.j_hz * hz / 2, cfg.j_hz * hz / 2]
        for off in offsets:
            total += (area / lines) * complex_line(ppm, center + off, width_hz * hz, shape_g)

    scale = params.signal_scale
    ref = params.shift_ppm
    add(
        STANDARD_PPM + ref + params.species_shift_ppm.get("standard", 0.0),
        STANDARD_H * scale,
        params.linewidth_hz["standard"],
    )
    for species, amount in (("substrate", params.substrate), ("product", params.product)):
        for center, protons, lines in SIGNALS[species]:
            add(
                center + ref + params.species_shift_ppm.get(species, 0.0),
                protons * amount * scale,
                params.linewidth_hz[species],
                lines,
            )
    add(params.impurity_ppm + ref, 3 * params.impurity * scale, params.linewidth_hz["impurity"])
    add(SOLVENT_PPM + ref, cfg.solvent_area, params.linewidth_hz["solvent"])
    add(params.water_ppm + ref, params.water_area, params.water_linewidth_hz, shape_g=0.0)

    phase = np.deg2rad(params.phase_deg)
    spectrum = (np.exp(1j * phase) * total).real
    u = (ppm - (cfg.ppm_min + cfg.ppm_max) / 2) / ((cfg.ppm_max - cfg.ppm_min) / 2)
    spectrum += np.polynomial.legendre.legval(u, params.baseline) if params.baseline else 0.0
    if rng is not None and cfg.noise_sd > 0:
        spectrum += rng.normal(0.0, cfg.noise_sd, size=ppm.shape)
    return spectrum


def active_faults(faults: list[str] | None, round_number: int) -> set[str]:
    """The NMR faults that apply on this round. LC/MS faults in the list are ignored."""
    out = set()
    for f in faults or []:
        name, _, at = f.partition("@")
        if not name.startswith("nmr_"):
            continue
        if name not in NMR_FAULTS or (at and not at.isdigit()):
            raise ValueError(f"unknown NMR fault {f!r}; known: {', '.join(NMR_FAULTS)}")
        if not at or int(at) == round_number:
            out.add(name)
    return out


def well_rng(seed: int, round_number: int, well: str) -> np.random.Generator:
    """The random stream for one well's NMR, separate from the LC/MS stream."""
    col, row = labware.column_order_key(well)
    return np.random.default_rng([NMR_STREAM, seed, round_number, col, row])


def read_plate_nmr(
    ledger: list[LedgerEntry],
    plan: Plan,
    assay: Assay,
    surface: Surface,
    seed: int,
    round_number: int = 1,
    faults: list[str] | None = None,
    settings: NmrSettings | None = None,
) -> NmrPlate:
    """Record a spectrum of every well that received liquid. Same inputs and seed give the same spectra."""
    cfg = settings or NmrSettings()
    active = active_faults(faults, round_number)
    volumes: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for e in ledger:
        if e.dest_labware == plan.plate:
            volumes[e.dest_well][e.reagent] += e.volume_ul
    ppm = ppm_axis(cfg)
    spectra: dict[str, np.ndarray] = {}
    truth: dict[str, dict] = {}
    for well in sorted(volumes, key=labware.column_order_key):
        cond = as_run_conditions(dict(volumes[well]), assay)
        true = true_yield(cond, surface)
        rng = well_rng(seed, round_number, well)
        observed = float(np.clip(true + rng.normal(0.0, cfg.yield_noise_sd), 0.0, 100.0))
        conversion = float(min(100.0, observed + rng.uniform(*cfg.conversion_extra)))
        params = draw_params(
            rng,
            observed / 100.0,
            1.0 - conversion / 100.0,
            cfg,
            broad="nmr_broad_lines" in active,
            phase_error="nmr_phase_error" in active,
            impurity_p=cfg.impurity_p if "nmr_impurity" in active else 0.0,
        )
        spectra[well] = render(ppm, params, cfg, rng)
        truth[well] = {
            "true_yield": true,
            "observed_yield": observed,
            "conversion": conversion,
            "impurity": params.impurity > 0,
            "impurity_amount": params.impurity,
            "linewidth_hz": params.linewidth_hz["standard"],
            "phase_deg": params.phase_deg,
            "shift_ppm": params.shift_ppm,
        }
    plate_faults = sorted(active - {"nmr_impurity"})
    return NmrPlate(
        ppm=ppm,
        spectra=spectra,
        truth=truth,
        plate_faults=plate_faults,
        spectrometer_mhz=cfg.spectrometer_mhz,
    )


def write_spectra(folder: Path, plate: NmrPlate) -> Path:
    """Write spectra.npz (the ppm axis and one array per well) and, separately, truth.json.

    Evaluators and reviewers read only spectra.npz. truth.json is for tests and scoring.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "spectra.npz"
    arrays = {well: np.asarray(y, dtype=np.float32) for well, y in plate.spectra.items()}
    np.savez_compressed(
        path, ppm=plate.ppm, spectrometer_mhz=np.array(plate.spectrometer_mhz), **arrays
    )
    (folder / "truth.json").write_text(
        json.dumps(
            {
                "synthetic": True,
                "note": "Hidden truth of a simulated NMR plate. Evaluators and reviewers never read this file.",
                "plate_faults": plate.plate_faults,
                "wells": plate.truth,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def spectrometer_mhz(folder: Path) -> float:
    """The field stored with spectra.npz, in MHz. Accepts the folder or the file."""
    path = Path(folder)
    if path.is_dir():
        path = path / "spectra.npz"
    with np.load(path, allow_pickle=False) as data:
        if "spectrometer_mhz" not in data.files:
            raise ValueError(f"{path.name} does not say which field it was recorded at")
        return float(data["spectrometer_mhz"])


def load_spectra(
    folder: Path, expected_mhz: float | None = DEFAULT_MHZ
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Read spectra.npz back as (ppm axis, {well: intensities}). Accepts the folder or the file.

    The file also stores the field. Unless expected_mhz is None, it must match:
    the default is the field the classical evaluator assumes (400 MHz), so a
    plate recorded at another field cannot be fitted with the wrong Hz to ppm
    scale without notice. Read the field with spectrometer_mhz().
    """
    path = Path(folder)
    if path.is_dir():
        path = path / "spectra.npz"
    if expected_mhz is not None:
        mhz = spectrometer_mhz(path)
        if abs(mhz - expected_mhz) > 1e-6:
            raise ValueError(
                f"{path.name} was recorded at {mhz:g} MHz, not the expected {expected_mhz:g} MHz"
            )
    with np.load(path, allow_pickle=False) as data:
        ppm = np.asarray(data["ppm"], dtype=float)
        wells = {
            k: np.asarray(data[k], dtype=float)
            for k in data.files
            if k not in ("ppm", "spectrometer_mhz")
        }
    return ppm, dict(sorted(wells.items(), key=lambda kv: labware.column_order_key(kv[0])))
