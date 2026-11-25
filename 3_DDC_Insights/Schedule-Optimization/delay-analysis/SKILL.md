---
name: "delay-analysis"
description: "Analyze construction schedule delays for claims and recovery. Perform time impact analysis, identify delay causes, calculate damages, and document for disputes."
homepage: "https://datadrivenconstruction.io"
metadata: {"openclaw": {"emoji": "⏱️", "os": ["darwin", "linux", "win32"], "homepage": "https://datadrivenconstruction.io", "requires": {"bins": ["python3"]}}}
---
# Delay Analysis

## Overview

Analyze construction schedule delays for project recovery and claims. Perform time impact analysis (TIA), identify concurrent delays, calculate delay damages, and prepare documentation for dispute resolution.

> "Proper delay analysis is essential for fair resolution of construction disputes" — DDC Community

## Delay Analysis Methods

```
┌─────────────────────────────────────────────────────────────────┐
│                    DELAY ANALYSIS METHODS                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  As-Planned vs As-Built    │    Time Impact Analysis (TIA)      │
│  ─────────────────────     │    ────────────────────────────    │
│  Compare original to       │    Insert delay events into        │
│  actual schedule           │    schedule to measure impact      │
│                            │                                     │
│  Windows Analysis          │    Collapsed As-Built              │
│  ────────────────          │    ─────────────────               │
│  Divide project into       │    Remove delays from as-built     │
│  time periods              │    to find "but-for" completion    │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Technical Implementation

```python
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set, Tuple
from datetime import datetime, timedelta
from enum import Enum
from collections import defaultdict
from fractions import Fraction
import math

class DelayType(Enum):
    EXCUSABLE_COMPENSABLE = "excusable_compensable"      # Owner caused - time + money
    EXCUSABLE_NON_COMPENSABLE = "excusable_non_compensable"  # Neither party - time only
    NON_EXCUSABLE = "non_excusable"                      # Contractor caused - no relief
    CONCURRENT = "concurrent"                            # Both parties - complex

class DelayCause(Enum):
    OWNER_CHANGE = "owner_change"
    LATE_INFORMATION = "late_information"
    DIFFERING_CONDITIONS = "differing_conditions"
    PERMIT_DELAY = "permit_delay"
    WEATHER = "weather"
    LABOR_SHORTAGE = "labor_shortage"
    MATERIAL_DELAY = "material_delay"
    SUBCONTRACTOR = "subcontractor"
    COORDINATION = "coordination"
    ACCESS = "access"
    FORCE_MAJEURE = "force_majeure"

@dataclass
class DelayEvent:
    id: str
    description: str
    cause: DelayCause
    delay_type: DelayType
    start_date: datetime
    end_date: datetime
    affected_activities: List[str]
    responsible_party: str
    documented: bool = True
    supporting_docs: List[str] = field(default_factory=list)
    calculated_impact: int = 0  # days; only meaningful once impact_analyzed is True
    concurrent_with: List[str] = field(default_factory=list)
    impact_analyzed: bool = False

@dataclass
class ScheduleVersion:
    version_id: str
    version_type: str  # baseline, update, as-built
    data_date: datetime
    completion_date: datetime
    activities: Dict[str, Dict]  # activity_id -> {start, finish, duration}

@dataclass
class WindowPeriod:
    window_id: str
    start_date: datetime
    end_date: datetime
    planned_progress: float
    actual_progress: float
    delay_days: int
    delay_events: List[str]
    responsible_parties: Dict[str, int]  # party -> delay days

@dataclass
class DelayAnalysisReport:
    project_name: str
    analysis_date: datetime
    original_completion: datetime
    actual_completion: datetime
    total_delay: int
    excusable_delay: int
    non_excusable_delay: int
    concurrent_delay: int
    delay_events: List[DelayEvent]
    delay_by_cause: Dict[str, int]
    delay_by_party: Dict[str, int]
    recommended_extension: int
    potential_damages: float
    unanalyzed_events: List[str] = field(default_factory=list)

class DelayAnalyzer:
    """Analyze construction schedule delays."""

    # Daily delay costs by project size
    DEFAULT_DAILY_COSTS = {
        "small": 5000,      # < $10M
        "medium": 15000,    # $10M - $50M
        "large": 40000,     # $50M - $200M
        "mega": 100000      # > $200M
    }

    def __init__(self, project_name: str, contract_completion: datetime):
        self.project_name = project_name
        self.contract_completion = contract_completion
        self.delay_events: Dict[str, DelayEvent] = {}
        self.schedule_versions: Dict[str, ScheduleVersion] = {}
        self.window_periods: List[WindowPeriod] = []
        # event_id -> snapshot of the event as it was when its TIA ran
        self.impact_analyses: Dict[str, Dict] = {}
        self.daily_cost = self.DEFAULT_DAILY_COSTS["medium"]

    def set_daily_delay_cost(self, cost: float):
        """Set daily delay cost for damages calculation."""
        self.daily_cost = cost

    def add_schedule_version(self, version_id: str, version_type: str,
                            data_date: datetime, completion_date: datetime,
                            activities: Dict[str, Dict]) -> ScheduleVersion:
        """Add schedule version for analysis."""
        version = ScheduleVersion(
            version_id=version_id,
            version_type=version_type,
            data_date=data_date,
            completion_date=completion_date,
            activities=activities
        )
        self.schedule_versions[version_id] = version
        return version

    def add_delay_event(self, id: str, description: str,
                       cause: DelayCause, delay_type: DelayType,
                       start_date: datetime, end_date: datetime,
                       affected_activities: List[str],
                       responsible_party: str,
                       supporting_docs: List[str] = None) -> DelayEvent:
        """Add delay event for analysis."""
        event = DelayEvent(
            id=id,
            description=description,
            cause=cause,
            delay_type=delay_type,
            start_date=start_date,
            end_date=end_date,
            affected_activities=affected_activities,
            responsible_party=responsible_party,
            supporting_docs=supporting_docs or []
        )
        self.impact_analyses.pop(id, None)
        self.delay_events[id] = event
        return event

    def perform_as_planned_vs_as_built(self) -> Dict:
        """Perform As-Planned vs As-Built analysis."""
        baseline = self.schedule_versions.get("baseline")
        as_built = self.schedule_versions.get("as_built")

        if not baseline or not as_built:
            raise ValueError("Need baseline and as-built schedules")

        total_delay = (as_built.completion_date - baseline.completion_date).days

        # Analyze each activity
        activity_delays = []
        for act_id, baseline_act in baseline.activities.items():
            if act_id in as_built.activities:
                as_built_act = as_built.activities[act_id]

                baseline_finish = baseline_act['finish']
                actual_finish = as_built_act['finish']

                if isinstance(baseline_finish, str):
                    baseline_finish = datetime.fromisoformat(baseline_finish)
                if isinstance(actual_finish, str):
                    actual_finish = datetime.fromisoformat(actual_finish)

                delay = (actual_finish - baseline_finish).days

                if delay > 0:
                    activity_delays.append({
                        'activity_id': act_id,
                        'planned_finish': baseline_finish,
                        'actual_finish': actual_finish,
                        'delay_days': delay
                    })

        return {
            'method': 'As-Planned vs As-Built',
            'baseline_completion': baseline.completion_date,
            'actual_completion': as_built.completion_date,
            'total_delay': total_delay,
            'activity_delays': sorted(activity_delays, key=lambda x: -x['delay_days'])
        }

    def perform_time_impact_analysis(self, delay_event_id: str) -> Dict:
        """Perform Time Impact Analysis for specific delay event."""
        if delay_event_id not in self.delay_events:
            raise ValueError(f"Delay event {delay_event_id} not found")

        event = self.delay_events[delay_event_id]

        # Find schedule version just before delay
        pre_delay_schedule = None
        for version in sorted(self.schedule_versions.values(),
                            key=lambda v: v.data_date, reverse=True):
            if version.data_date < event.start_date:
                pre_delay_schedule = version
                break

        if not pre_delay_schedule:
            pre_delay_schedule = self.schedule_versions.get("baseline")

        if not pre_delay_schedule:
            raise ValueError("No pre-delay schedule found")

        # Calculate impact
        original_completion = pre_delay_schedule.completion_date
        delay_duration = max(0, (event.end_date - event.start_date).days)

        # Check if delay is on critical path
        critical_impact = False
        for act_id in event.affected_activities:
            if act_id in pre_delay_schedule.activities:
                act = pre_delay_schedule.activities[act_id]
                if act.get('is_critical', False):
                    critical_impact = True
                    break

        if critical_impact:
            impact_days = delay_duration
            new_completion = original_completion + timedelta(days=delay_duration)
        else:
            # Need to check float
            impact_days = max(0, delay_duration - 5)  # Simplified - assume 5 days float
            new_completion = original_completion + timedelta(days=impact_days)

        event.calculated_impact = impact_days
        event.impact_analyzed = True
        self.impact_analyses[delay_event_id] = {
            'event': event,
            'start_date': event.start_date,
            'end_date': event.end_date,
            'affected_activities': tuple(event.affected_activities),
            'impact_days': impact_days,
        }

        return {
            'method': 'Time Impact Analysis',
            'delay_event': event.id,
            'delay_description': event.description,
            'delay_duration': delay_duration,
            'critical_path_impact': critical_impact,
            'schedule_impact_days': impact_days,
            'original_completion': original_completion,
            'impacted_completion': new_completion,
            'delay_type': event.delay_type.value,
            'responsible_party': event.responsible_party
        }

    def identify_concurrent_delays(self) -> List[Tuple[str, str, int]]:
        """Identify concurrent delay events."""
        concurrent = []

        events = list(self.delay_events.values())
        for i, event1 in enumerate(events):
            for event2 in events[i+1:]:
                # Check for overlap
                overlap_start = max(event1.start_date, event2.start_date)
                overlap_end = min(event1.end_date, event2.end_date)

                if overlap_start < overlap_end:
                    overlap_days = (overlap_end - overlap_start).days
                    concurrent.append((event1.id, event2.id, overlap_days))

                    event1.concurrent_with.append(event2.id)
                    event2.concurrent_with.append(event1.id)

        return concurrent

    def perform_windows_analysis(self, window_days: int = 30) -> List[WindowPeriod]:
        """Perform windows analysis by dividing project into periods."""
        baseline = self.schedule_versions.get("baseline")
        as_built = self.schedule_versions.get("as_built")

        if not baseline or not as_built:
            raise ValueError("Need baseline and as-built schedules")

        windows = []
        current_start = baseline.data_date
        window_num = 1

        while current_start < as_built.completion_date:
            window_end = min(
                current_start + timedelta(days=window_days),
                as_built.completion_date
            )

            # Find delay events in this window
            window_events = [
                e.id for e in self.delay_events.values()
                if e.start_date < window_end and e.end_date > current_start
            ]

            # Calculate delay by party
            party_delays = defaultdict(int)
            for event_id in window_events:
                event = self.delay_events[event_id]
                overlap_start = max(event.start_date, current_start)
                overlap_end = min(event.end_date, window_end)
                days = (overlap_end - overlap_start).days
                party_delays[event.responsible_party] += days

            window = WindowPeriod(
                window_id=f"W{window_num:02d}",
                start_date=current_start,
                end_date=window_end,
                planned_progress=0.0,  # Would calculate from schedule
                actual_progress=0.0,
                delay_days=sum(party_delays.values()),
                delay_events=window_events,
                responsible_parties=dict(party_delays)
            )
            windows.append(window)

            current_start = window_end
            window_num += 1

        self.window_periods = windows
        return windows

    def _analyzed_impact_days(self, event_id: str, event: DelayEvent) -> Optional[Set[int]]:
        """Calendar days (as ordinals) on which an event delayed completion.

        Returns None when the event has no time impact analysis, or when its
        dates or activities changed after the analysis ran. The impact falls
        on the last days of the event window, after any float is consumed.
        """
        record = self.impact_analyses.get(event_id)
        if (record is None
                or record['event'] is not event
                or record['start_date'] != event.start_date
                or record['end_date'] != event.end_date
                or record['affected_activities'] != tuple(event.affected_activities)):
            return None
        end = event.end_date.toordinal()
        return set(range(end - record['impact_days'], end))

    def _classify_delay_days(self) -> Dict:
        """Merge analyzed impacts into day sets so overlapping days count once.

        Days where an excusable impact overlaps a non-excusable or CONCURRENT
        impact are concurrent: they extend time but earn neither delay
        damages nor liquidated damages.
        """
        days_by_type = {delay_type: set() for delay_type in DelayType}
        days_by_event: Dict[str, Set[int]] = {}
        unanalyzed = []

        for event_id, event in self.delay_events.items():
            days = self._analyzed_impact_days(event_id, event)
            if days is None:
                unanalyzed.append(event_id)
                continue
            days_by_event[event_id] = days
            days_by_type[event.delay_type] |= days

        compensable = days_by_type[DelayType.EXCUSABLE_COMPENSABLE]
        time_only = days_by_type[DelayType.EXCUSABLE_NON_COMPENSABLE]
        excusable = compensable | time_only
        non_excusable = days_by_type[DelayType.NON_EXCUSABLE]
        concurrent = days_by_type[DelayType.CONCURRENT] | (excusable & non_excusable)

        return {
            'extension': excusable,
            'compensable': compensable - concurrent,
            'time_only': time_only - concurrent,
            'non_excusable': non_excusable - concurrent,
            'concurrent': concurrent,
            'by_event': days_by_event,
            'unanalyzed': unanalyzed,
        }

    def _overrun_days(self, actual_completion: Optional[datetime]) -> Optional[int]:
        """Days actual completion ran past contract completion (never negative)."""
        if actual_completion is None:
            as_built = self.schedule_versions.get("as_built")
            if as_built is None:
                return None
            actual_completion = as_built.completion_date
        return max(0, (actual_completion - self.contract_completion).days)

    @staticmethod
    def _apportion(amounts: Dict[str, Fraction], total: int) -> Dict[str, int]:
        """Scale amounts down to whole days summing to min(total, sum(amounts))."""
        available = sum(amounts.values())
        target = min(total, math.floor(available))
        if target <= 0 or available <= 0:
            return {key: 0 for key in amounts}
        quotas = {key: Fraction(value) * target / available for key, value in amounts.items()}
        result = {key: math.floor(quota) for key, quota in quotas.items()}
        leftover = target - sum(result.values())
        by_remainder = sorted(quotas, key=lambda key: -(quotas[key] - result[key]))
        for key in by_remainder[:leftover]:
            result[key] += 1
        return result

    def calculate_delay_damages(self, actual_completion: Optional[datetime] = None) -> Dict:
        """Calculate potential delay damages from analyzed impacts only.

        Events without a current time impact analysis are excluded and listed
        in 'unanalyzed_events'. When the actual completion is known (passed in,
        or taken from an "as_built" schedule version), the time extension and
        compensable days are capped at the actual overrun, and liquidated
        damages at the overrun left unexcused by that extension.
        """
        days = self._classify_delay_days()
        extension = len(days['extension'])
        excusable_compensable = len(days['compensable'])
        excusable_non_compensable = len(days['time_only'])
        non_excusable = len(days['non_excusable'])

        overrun = self._overrun_days(actual_completion)
        if overrun is not None:
            extension = min(extension, overrun)
            excusable_compensable = min(excusable_compensable, extension)
            excusable_non_compensable = min(excusable_non_compensable, extension)
            non_excusable = min(non_excusable, overrun - extension)

        contractor_damages = excusable_compensable * self.daily_cost
        owner_ld = non_excusable * self.daily_cost

        return {
            'excusable_compensable_days': excusable_compensable,
            'excusable_non_compensable_days': excusable_non_compensable,
            'non_excusable_days': non_excusable,
            'recommended_time_extension': extension,
            'contractor_delay_damages': contractor_damages,
            'owner_liquidated_damages': owner_ld,
            'daily_rate_used': self.daily_cost,
            'overrun_days': overrun,
            'unanalyzed_events': days['unanalyzed']
        }

    def _attribute_days(self, day_set: Set[int], days_by_event: Dict[str, Set[int]],
                        total: int, key_of) -> Dict[str, int]:
        """Share each day among the events covering it, then scale to total."""
        shares: Dict[str, Fraction] = defaultdict(Fraction)
        for day in day_set:
            covering = [event_id for event_id, days in days_by_event.items() if day in days]
            for event_id in covering:
                shares[key_of(self.delay_events[event_id])] += Fraction(1, len(covering))
        return self._apportion(dict(shares), total)

    def generate_analysis_report(self, actual_completion: datetime) -> DelayAnalysisReport:
        """Generate comprehensive delay analysis report.

        Every day figure in the report is limited to the actual overrun, so a
        project that finished on time or early reports no delay award.
        """
        total_delay = (actual_completion - self.contract_completion).days
        overrun = max(0, total_delay)

        days = self._classify_delay_days()
        damages = self.calculate_delay_damages(actual_completion)
        extension = damages['recommended_time_extension']

        extension_days = days['extension']
        extension_split = self._apportion({
            'excusable': len(extension_days - days['concurrent']),
            'concurrent': len(extension_days & days['concurrent']),
        }, extension)

        unexcused_days = days['non_excusable'] | (days['concurrent'] - extension_days)
        unexcused_split = self._apportion({
            'non_excusable': len(days['non_excusable']),
            'concurrent': len(days['concurrent'] - extension_days),
        }, overrun - extension)
        unexcused = sum(unexcused_split.values())

        delay_by_cause = defaultdict(int)
        delay_by_party = defaultdict(int)
        for day_set, total in ((extension_days, extension), (unexcused_days, unexcused)):
            for cause, count in self._attribute_days(
                    day_set, days['by_event'], total, lambda e: e.cause.value).items():
                delay_by_cause[cause] += count
            for party, count in self._attribute_days(
                    day_set, days['by_event'], total, lambda e: e.responsible_party).items():
                delay_by_party[party] += count

        return DelayAnalysisReport(
            project_name=self.project_name,
            analysis_date=datetime.now(),
            original_completion=self.contract_completion,
            actual_completion=actual_completion,
            total_delay=total_delay,
            excusable_delay=extension_split['excusable'],
            non_excusable_delay=unexcused_split['non_excusable'],
            concurrent_delay=extension_split['concurrent'] + unexcused_split['concurrent'],
            delay_events=list(self.delay_events.values()),
            delay_by_cause={k: v for k, v in delay_by_cause.items() if v},
            delay_by_party={k: v for k, v in delay_by_party.items() if v},
            recommended_extension=extension,
            potential_damages=damages['contractor_delay_damages'],
            unanalyzed_events=days['unanalyzed']
        )

    def generate_report_markdown(self, report: DelayAnalysisReport) -> str:
        """Generate markdown report."""
        lines = [
            "# Delay Analysis Report",
            "",
            f"**Project:** {report.project_name}",
            f"**Analysis Date:** {report.analysis_date.strftime('%Y-%m-%d')}",
            "",
            "## Schedule Summary",
            "",
            f"| Milestone | Date |",
            f"|-----------|------|",
            f"| Contract Completion | {report.original_completion.strftime('%Y-%m-%d')} |",
            f"| Actual Completion | {report.actual_completion.strftime('%Y-%m-%d')} |",
            f"| **Total Delay** | **{report.total_delay} days** |",
            "",
            "## Delay Classification",
            "",
            f"| Category | Days |",
            f"|----------|------|",
            f"| Excusable Delay | {report.excusable_delay} |",
            f"| Non-Excusable Delay | {report.non_excusable_delay} |",
            f"| Concurrent Delay | {report.concurrent_delay} |",
            "",
            "## Delay by Cause",
            ""
        ]

        for cause, days in sorted(report.delay_by_cause.items(), key=lambda x: -x[1]):
            lines.append(f"- **{cause}**: {days} days")

        lines.extend([
            "",
            "## Delay by Responsible Party",
            ""
        ])

        for party, days in sorted(report.delay_by_party.items(), key=lambda x: -x[1]):
            lines.append(f"- **{party}**: {days} days")

        lines.extend([
            "",
            "## Recommendations",
            "",
            f"- **Recommended Time Extension:** {report.recommended_extension} days",
            f"- **Potential Delay Damages:** ${report.potential_damages:,.0f}",
            ""
        ])

        if report.unanalyzed_events:
            lines.extend([
                "## Excluded Events (no current time impact analysis)",
                ""
            ])
            for event_id in report.unanalyzed_events:
                event = self.delay_events.get(event_id)
                label = f"{event.description} ({event.responsible_party})" if event else ""
                lines.append(f"- {event_id}: {label} — not counted until analyzed")
            lines.append("")

        return "\n".join(lines)
```

## Quick Start

```python
from datetime import datetime, timedelta

# Initialize analyzer
analyzer = DelayAnalyzer(
    "Office Tower",
    contract_completion=datetime(2024, 12, 31)
)

# Add schedule versions
analyzer.add_schedule_version(
    "baseline", "baseline",
    datetime(2024, 1, 1),
    datetime(2024, 12, 31),
    activities={"A100": {"finish": datetime(2024, 6, 30), "is_critical": True}}
)

analyzer.add_schedule_version(
    "as_built", "as_built",
    datetime(2025, 3, 15),
    datetime(2025, 3, 15),
    activities={"A100": {"finish": datetime(2024, 8, 15)}}
)

# Add delay events
analyzer.add_delay_event(
    "D001",
    "Owner-directed design change to HVAC system",
    DelayCause.OWNER_CHANGE,
    DelayType.EXCUSABLE_COMPENSABLE,
    datetime(2024, 4, 1),
    datetime(2024, 5, 15),
    ["A100", "A101"],
    "Owner"
)

analyzer.add_delay_event(
    "D002",
    "Unexpected rock encountered in excavation",
    DelayCause.DIFFERING_CONDITIONS,
    DelayType.EXCUSABLE_COMPENSABLE,
    datetime(2024, 3, 15),
    datetime(2024, 4, 30),
    ["A050"],
    "Owner"
)

# Perform analyses - events without a TIA are excluded from extensions and damages
for event_id in analyzer.delay_events:
    tia = analyzer.perform_time_impact_analysis(event_id)
    print(f"TIA Impact {event_id}: {tia['schedule_impact_days']} days")

concurrent = analyzer.identify_concurrent_delays()
print(f"Concurrent delays found: {len(concurrent)}")

# Generate report
report = analyzer.generate_analysis_report(datetime(2025, 3, 15))
print(analyzer.generate_report_markdown(report))
```

## Requirements

```bash
pip install (no external dependencies)
```
