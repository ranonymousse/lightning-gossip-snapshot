"""The published figure, drawn from a results folder and sized for one IEEE column.

One measure on one axis, a single hue that survives grayscale print, embedded fonts and
no title: the caption belongs to the paper. A red corner label marks synthetic data.

`age-cdf` covers the announcements with a recent update observed, the channels that
their nodes still advertise.
"""

import math
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .common import write_json
from .publish import OLD_YEARS, YOUNG_YEARS, load_results, percent, share_younger
from .schema import YEAR_BLOCKS

AGE_AXIS = "Channel funding-output age (nominal years)"
# The title of the age axis stands this many points below its tick labels, where
# matplotlib would leave four.
AGE_AXIS_PAD = 2
# A row between two grid lines holds one line of words, which leaves this height little
# to spare. The figure is lower by the points that the axis title moved up, so the plot
# keeps its size.
WIDTH, HEIGHT = 3.45, 1.7 - (4 - AGE_AXIS_PAD) / 72
# The curve is read at two ages, in whole nominal years: a young one, and the age from
# which the published results count old outputs. A reading counts the outputs on one
# side of its age: those younger than it, or those of that age or more.
READINGS = ((YOUNG_YEARS, "younger"), (OLD_YEARS, "older"))
# Every word and number is set in one size, that of the tick labels. Bold marks a value,
# never a larger size.
TEXT = 7.5
# From one line of a label to the next: the size and a sixth of it.
LINE = TEXT + TEXT / 6
# A label stands this many points from the guide or the frame that it starts at.
GAP = 6
# A tiny arrow on the guide of a reading points to the side that the reading counts. It
# is ARROW points long, and a label on that side starts CLEAR points beyond its head.
ARROW, CLEAR = 5, 4
# Percentage points between two labelled grid lines. One unlabelled line halves each
# band, and a line of words stands in the row between two grid lines.
BAND = 25
ROW = BAND / 2
# Axes, ticks and every word are full black. Only the grid recedes.
INK, GRID, WARNING = "#000000", "#e3e2de", "#a32e31"
# One hue in two steps. The darker one has a contrast above 2:1 against white.
DARK, LIGHT = "#1c5cab", "#86b6ef"
STYLE = {
    "font.family": "serif",
    "font.serif": ["STIXGeneral", "DejaVu Serif"],
    "mathtext.fontset": "stix",
    "font.size": 8,
    "axes.labelsize": 8,
    "axes.labelcolor": INK,
    "axes.edgecolor": INK,
    "axes.linewidth": 0.5,
    "xtick.labelsize": TEXT,
    "ytick.labelsize": TEXT,
    "xtick.color": INK,
    "ytick.color": INK,
    "xtick.major.width": 0.5,
    "ytick.major.width": 0.5,
    "xtick.major.size": 2.5,
    "ytick.major.size": 0,
    "pdf.fonttype": 42,
}


def label(snapshot):
    """The corner label a figure must carry, or None for mainnet data."""
    if snapshot["dataset_kind"] == "synthetic":
        return "SYNTHETIC FIXTURE — NOT A MEASUREMENT"
    return None


@contextmanager
def canvas(target, name, note=None):
    """One column-wide axis; saved as a vector PDF and a 300 dpi PNG on exit."""
    # Keep font caches in the output directory, not a user's global directory.
    previous = os.environ.get("MPLCONFIGDIR")
    os.environ["MPLCONFIGDIR"] = str(target / ".mpl-cache")
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        with plt.rc_context(STYLE):
            fig, ax = plt.subplots(figsize=(WIDTH, HEIGHT), layout="constrained")
            ax.spines[["top", "right", "left"]].set_visible(False)
            ax.grid(axis="y", color=GRID, linewidth=0.5)
            ax.set_axisbelow(True)
            yield ax
            if note:
                ax.set_title(note, fontsize=6.5, color=WARNING, loc="left", pad=3)
            fig.savefig(target / f"{name}.pdf", metadata={"CreationDate": None, "ModDate": None})
            fig.savefig(target / f"{name}.png", dpi=300)
            plt.close(fig)
    finally:
        if previous is None:
            os.environ.pop("MPLCONFIGDIR", None)
        else:
            os.environ["MPLCONFIGDIR"] = previous
        shutil.rmtree(target / ".mpl-cache", ignore_errors=True)


def fade(ax, xs, ys):
    """Fill under a curve in the light blue, strongest at the top and gone at the axis."""
    from matplotlib.colors import to_rgb
    from matplotlib.patches import Polygon

    red, green, blue = to_rgb(LIGHT)
    ramp = [[(red, green, blue, 0.56 * (step / 255) ** 1.25)] for step in range(256)]
    # Unresampled, so the PDF holds the ramp itself and the outline stays a vector path.
    image = ax.imshow(
        ramp,
        extent=(xs[0], xs[-1], 0, max(ys)),
        origin="lower",
        aspect="auto",
        interpolation="none",
        zorder=1,
    )
    outline = Polygon(
        [(xs[0], 0), *zip(xs, ys, strict=True), (xs[-1], 0)], facecolor="none", edgecolor="none"
    )
    ax.add_patch(outline)
    image.set_clip_path(outline)


def words(ax, lines, place, across, side, gap=GAP):
    """A label in one size beside a place, centred on the rows it takes.

    A row is the space between two neighbouring grid lines. A label takes one row for
    each of its lines and is centred on them, so a grid line passes between two lines of
    words and through none. A line is a list of (words, bold) parts. `place` gives where
    the label starts across, in the transform `across`, and the share in the middle of
    its rows. `side` is the edge of the label that faces the place, `gap` points away.
    """
    from matplotlib.offsetbox import AnnotationBbox, HPacker, TextArea, VPacker

    def part(text, bold):
        style = {"fontsize": TEXT, "fontweight": "bold" if bold else "normal", "color": INK}
        return TextArea(text, textprops=style)

    # A word space between the parts of a line, and a sixth of the size between lines:
    # the lines of a label stay much closer to each other than to the next label.
    rows = [
        HPacker(children=[part(*each) for each in line], pad=0, sep=TEXT / 4, align="baseline")
        for line in lines
    ]
    left = side == "left"
    box = AnnotationBbox(
        VPacker(children=rows, pad=0, sep=LINE - TEXT, align=side),
        place,
        xycoords=(across, ax.transData),
        xybox=(gap if left else -gap, 0),
        boxcoords="offset points",
        box_alignment=(0 if left else 1, 0.5),
        frameon=False,
        pad=0,
        zorder=5,
    )
    # The words stand inside the axes and never move them.
    box.set_in_layout(False)
    ax.add_artist(box)


def pointer(ax, place, older):
    """A tiny arrow from a guide towards the outputs that the reading beside it counts.

    It starts on the guide, level with the second line of the reading centred on `place`,
    the line that names the age, and points left to the younger outputs or right to the
    older ones. It is drawn in the colour and width of the guide, so that it reads as a
    part of it.
    """
    from matplotlib.transforms import offset_copy

    arrow = ax.annotate(
        "",
        xy=place,
        xycoords=offset_copy(ax.transData, fig=ax.figure, y=-LINE / 2, units="points"),
        xytext=(ARROW if older else -ARROW, 0),
        textcoords="offset points",
        arrowprops={
            "arrowstyle": "<|-,head_length=0.4,head_width=0.2",
            "mutation_scale": 5,
            "color": DARK,
            "linewidth": 0.6,
            "shrinkA": 0,
            "shrinkB": 0,
        },
        zorder=2,
    )
    arrow.set_in_layout(False)


def age_cdf(snapshot, rows, target, note=None):
    """Share of the announcements with a recent update observed whose funding output is
    younger than a given age.

    The curve has one point per published row. It is read in plain words at two ages,
    the share younger than the first and the share older than the second, and the totals
    that the shares refer to stand in the open corner under it. Every line of words has
    a row between two grid lines to itself, clear of the guides. An empty row parts two
    labels, except the two totals, which stand in neighbouring rows as a pair.
    """
    xs = [0.0] + [row["age_below_blocks"] / YEAR_BLOCKS for row in rows]
    ys = [0.0] + [100 * row["cumulative_fraction"] for row in rows]
    # The oldest row ends at a whole nominal year, which is where the axis ends.
    edge = max(1, math.ceil(xs[-1]))
    group = snapshot["counts"]["update_recency"]["recent_update_observed"]
    with canvas(target, "age-cdf", note) as ax:
        fade(ax, xs, ys)
        ax.plot(xs, ys, color=DARK, linewidth=1.8, solid_joinstyle="round", zorder=3)
        for years, side in READINGS:
            # A share is exact only where a published row ends at the age. In a small
            # group no row may end there, and the figure then shows no marker, guide or
            # reading for that age. The last row ends at the edge of the axis, where every
            # output is younger and nothing is left to read.
            younger = share_younger(rows, years)
            if younger is None or younger == 1:
                continue
            share = 100 * float(younger)
            # Guides from the point on the curve to both axes.
            ax.plot(
                [0, years, years],
                [share, share, 0],
                color=DARK,
                linewidth=0.6,
                linestyle=(0, (1.2, 2.2)),
                zorder=2,
            )
            ax.plot(
                [years],
                [share],
                "o",
                color=DARK,
                markersize=5.2,
                markeredgecolor="white",
                markeredgewidth=1.1,
                zorder=4,
            )
            # The two rows right below the point, to the right of the guide. The words
            # give the counted share in whole percent, and the arrow points to its side.
            older = side == "older"
            counted = percent(1 - younger if older else younger)
            reading = [
                [(f"{counted}%", True), (f"are {side}", False)],
                [(f"than {years} year{'' if years == 1 else 's'}", False)],
            ]
            place = (years, ROW * (share // ROW) - ROW)
            words(ax, reading, place, ax.transData, "left", ARROW + CLEAR if older else GAP)
            pointer(ax, place, older)
        # The base of the shares in the open corner. An empty row parts the two totals from
        # the reading above them, and the grid line between their rows parts one total from
        # the other. Numbers wear ink, never the series colour.
        announcements = [
            [(f"{group['channels']:,}", True), ("announcements", False)],
            [("with a recent update", False)],
        ]
        keys = [
            [(f"{group['distinct_funding_keys']:,}", True), ("distinct public", False)],
            [("funding keys", False)],
        ]
        words(ax, announcements, (1, 3 * ROW), ax.transAxes, "right")
        words(ax, keys, (1, ROW), ax.transAxes, "right")
        ax.set_xlabel(AGE_AXIS, fontsize=TEXT, labelpad=AGE_AXIS_PAD)
        # The title says what the shares count. That they are cumulative is in the readings
        # ("are younger than"), and without that word the title fits beside its axis.
        ax.set_ylabel("Share of announcements", fontsize=TEXT)
        ax.set(xlim=(0, edge), ylim=(0, 104))
        ax.set_xticks(range(edge + 1))
        # A short tick marks each half year, as the unlabelled grid lines halve the bands.
        ax.set_xticks([year + 0.5 for year in range(edge)], minor=True)
        ax.tick_params(axis="x", which="minor", length=1.4, width=0.5)
        labelled = range(0, 101, BAND)
        ax.set_yticks(labelled, [f"{share}%" for share in labelled])
        # One unlabelled line halves each band into two rows.
        ax.set_yticks([share + ROW for share in labelled[:-1]], minor=True)
        ax.grid(axis="y", which="minor", color=GRID, linewidth=0.25)
        # The share axis is drawn like the age axis: a line that ends at 100%, a tick at
        # each labelled share and a short one at each half band.
        ax.spines["left"].set_visible(True)
        ax.spines["left"].set_bounds(0, 100)
        ax.tick_params(axis="y", which="major", length=2.5)
        ax.tick_params(axis="y", which="minor", length=1.4, width=0.5)


def draw(results, target):
    """Draw the figure of loaded results into an existing folder; returns its label."""
    if not results["rows"]:
        raise ValueError("the results hold no age distribution to draw")
    note = label(results["snapshot"])
    age_cdf(results["snapshot"], results["rows"], target, note)
    return note


def redraw(results: Path, output: Path) -> dict:
    """Draw the figure again from a results folder that still matches its checksums.

    No capture is read. A synthetic label always stays.
    """
    loaded = load_results(results)
    if output.exists() or output.is_symlink():
        raise ValueError("output directory exists; choose a new destination")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".gossip-figures-", dir=output.parent))
    try:
        record = {
            "label": draw(loaded, staging),
            "inputs_sha256": {
                name: loaded["checksums"][name] for name in ("snapshot.json", "age-cdf.csv")
            },
        }
        write_json(staging / "figures.json", record)
        staging.rename(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return record
