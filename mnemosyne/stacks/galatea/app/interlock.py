"""Power-off interlock — the one safety rule of this platform.

Pure function, no I/O: the caller fetches a *fresh* printer snapshot and passes
the plain values in. That keeps the rule trivially testable and makes it
impossible to accidentally decide on cached data.

Fail closed: if we cannot see the printer, we do not assume it is idle.
"""

from dataclasses import dataclass

# States in which cutting power would kill a print or an unanswered dialog.
# BUSY is included deliberately: the firmware uses it for homing, calibration
# and other operations we should not interrupt either.
ACTIVE_STATES = frozenset({"PRINTING", "PAUSED", "ATTENTION", "BUSY"})


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str            # machine-readable, used as metric label
    message: str           # shown to the user as-is

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason, "message": self.message}


def evaluate_power_off(
    reachable: bool,
    state: str | None,
    nozzle_c: float | None,
    nozzle_max_c: float,
    force: bool = False,
) -> Decision:
    if force:
        return Decision(True, "forced", "Power cut forced by user — interlock bypassed.")

    if not reachable:
        return Decision(
            False,
            "printer_unreachable",
            "Printer status unknown: PrusaLink does not answer. "
            "Check the printer display before forcing power off.",
        )

    if state in ACTIVE_STATES:
        return Decision(
            False,
            "print_active",
            f"Printer is {state.lower()}. Stop or finish the job before cutting power.",
        )

    if nozzle_c is None:
        return Decision(
            False,
            "nozzle_unknown",
            "Nozzle temperature unknown. Check the printer display before forcing power off.",
        )

    if nozzle_c > nozzle_max_c:
        return Decision(
            False,
            "hotend_hot",
            f"Hotend at {nozzle_c:.0f} °C. Power stays on until it is below "
            f"{nozzle_max_c:.0f} °C, so the heatbreak fan can finish cooling.",
        )

    return Decision(True, "safe", "Printer idle and cool.")
