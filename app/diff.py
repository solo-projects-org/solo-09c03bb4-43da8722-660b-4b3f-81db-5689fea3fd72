"""Cross-version route timetable comparison.

Pairing rules (used by the route diff endpoint):

* Trips are paired one-to-one, and only between trips of the two versions
  whose *complete* ordered ``stop_id`` sequence is identical — trip ids may
  have been renumbered between versions, so they play no role in pairing.
* A pair is only allowed when the absolute difference of the first-stop
  arrival times (service-day seconds, so ``25:10:00`` compares as 90700) is
  within the configured offset limit.
* Among all allowed pairings the number of pairs is maximised first; ties are
  broken by minimising the sum of absolute first-stop time differences.

Because first-stop times are scalars, an optimal pairing can always be
arranged to be non-crossing: for ``a1 <= a2`` and ``b1 <= b2`` the uncrossed
pairs ``(a1, b1), (a2, b2)`` are allowed whenever the crossed pairs
``(a1, b2), (a2, b1)`` are, and their total cost is no larger.  Sorting each
side by first-stop time and running an LCS-style dynamic program therefore
yields the exact optimum.  All orderings are made deterministic by
tie-breaking on ``trip_id``, so repeated calls return identical results.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple


@dataclass
class TripStop:
    stop_sequence: int
    stop_id: str
    arrival_time: str
    arrival_seconds: int


@dataclass
class TripSchedule:
    trip_id: str
    stops: List[TripStop]

    @property
    def signature(self) -> Tuple[str, ...]:
        """The complete ordered stop_id sequence used as the pairing key."""
        return tuple(stop.stop_id for stop in self.stops)

    @property
    def first_arrival_seconds(self) -> int:
        return self.stops[0].arrival_seconds


@dataclass
class TripPairing:
    from_trip: TripSchedule
    to_trip: TripSchedule


@dataclass
class RouteDiffResult:
    # Sorted by (from_trip_id, to_trip_id) for a stable response.
    pairings: List[TripPairing]
    removed_trip_ids: List[str]  # in the old version but unpaired, sorted
    added_trip_ids: List[str]    # in the new version but unpaired, sorted


def _better(candidate: Tuple[int, int], best: Tuple[int, int]) -> bool:
    """(pairs, cost) ordering: more pairs wins, then lower total cost."""
    return candidate[0] > best[0] or (candidate[0] == best[0] and candidate[1] < best[1])


def _match_group(
    old_trips: List[TripSchedule],
    new_trips: List[TripSchedule],
    max_offset_seconds: int,
) -> List[Tuple[TripSchedule, TripSchedule]]:
    """Optimally pair the trips of one shared stop-sequence group."""
    old_sorted = sorted(
        old_trips, key=lambda trip: (trip.first_arrival_seconds, trip.trip_id)
    )
    new_sorted = sorted(
        new_trips, key=lambda trip: (trip.first_arrival_seconds, trip.trip_id)
    )
    n, m = len(old_sorted), len(new_sorted)

    # dp[i][j] = best (pairs, total_cost) for old_sorted[:i] x new_sorted[:j].
    dp = [[(0, 0)] * (m + 1) for _ in range(n + 1)]
    choice = [[""] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        old_first = old_sorted[i - 1].first_arrival_seconds
        for j in range(1, m + 1):
            # Fixed tie-break order keeps the result deterministic.
            best = dp[i][j - 1]
            step = "skip_new"
            if _better(dp[i - 1][j], best):
                best = dp[i - 1][j]
                step = "skip_old"
            diff = abs(old_first - new_sorted[j - 1].first_arrival_seconds)
            if diff <= max_offset_seconds:
                prev_pairs, prev_cost = dp[i - 1][j - 1]
                candidate = (prev_pairs + 1, prev_cost + diff)
                if _better(candidate, best):
                    best = candidate
                    step = "match"
            dp[i][j] = best
            choice[i][j] = step

    pairs: List[Tuple[TripSchedule, TripSchedule]] = []
    i, j = n, m
    while i > 0 and j > 0:
        step = choice[i][j]
        if step == "match":
            pairs.append((old_sorted[i - 1], new_sorted[j - 1]))
            i -= 1
            j -= 1
        elif step == "skip_old":
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    return pairs


def compute_route_diff(
    from_trips: List[TripSchedule],
    to_trips: List[TripSchedule],
    max_offset_seconds: int,
) -> RouteDiffResult:
    """Pair trips of one route between two versions.

    ``from_trips``/``to_trips`` are the route's trips in the old/new version;
    every trip must have at least one stop (guaranteed for published feeds).
    """
    from_groups: Dict[Tuple[str, ...], List[TripSchedule]] = {}
    for trip in from_trips:
        from_groups.setdefault(trip.signature, []).append(trip)
    to_groups: Dict[Tuple[str, ...], List[TripSchedule]] = {}
    for trip in to_trips:
        to_groups.setdefault(trip.signature, []).append(trip)

    pairings: List[TripPairing] = []
    paired_from: set[str] = set()
    paired_to: set[str] = set()
    for signature, old_group in from_groups.items():
        new_group = to_groups.get(signature)
        if not new_group:
            continue
        for old_trip, new_trip in _match_group(
            old_group, new_group, max_offset_seconds
        ):
            pairings.append(TripPairing(from_trip=old_trip, to_trip=new_trip))
            paired_from.add(old_trip.trip_id)
            paired_to.add(new_trip.trip_id)

    pairings.sort(key=lambda p: (p.from_trip.trip_id, p.to_trip.trip_id))
    removed = sorted(
        trip.trip_id for trip in from_trips if trip.trip_id not in paired_from
    )
    added = sorted(
        trip.trip_id for trip in to_trips if trip.trip_id not in paired_to
    )
    return RouteDiffResult(
        pairings=pairings, removed_trip_ids=removed, added_trip_ids=added
    )
