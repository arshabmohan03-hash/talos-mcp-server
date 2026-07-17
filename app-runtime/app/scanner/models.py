"""Data models + severity scoring for the vulnerability scanner."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def weight(self) -> int:
        return {
            "critical": 40,
            "high": 20,
            "medium": 10,
            "low": 4,
            "info": 0,
        }[self.value]

    @property
    def rank(self) -> int:
        order = ["info", "low", "medium", "high", "critical"]
        return order.index(self.value)

    @property
    def emoji(self) -> str:
        return {
            "critical": "🟥",
            "high": "🟧",
            "medium": "🟨",
            "low": "🟦",
            "info": "⬜",
        }[self.value]


class Finding(BaseModel):
    id: str
    title: str
    severity: Severity
    category: str
    description: str
    evidence: str | None = None
    recommendation: str | None = None
    references: list[str] = Field(default_factory=list)


class ScanReport(BaseModel):
    target: str
    final_url: str | None = None
    ip: str | None = None
    checks_run: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    duration_ms: int | None = None

    # ---- derived ----
    @property
    def counts(self) -> dict[str, int]:
        out = {s.value: 0 for s in Severity}
        for f in self.findings:
            out[f.severity.value] += 1
        return out

    @property
    def score(self) -> int:
        score = 100
        for f in self.findings:
            score -= f.severity.weight
        return max(0, score)

    @property
    def grade(self) -> str:
        s = self.score
        if s >= 95:
            return "A+"
        if s >= 85:
            return "A"
        if s >= 75:
            return "B"
        if s >= 60:
            return "C"
        if s >= 40:
            return "D"
        return "F"

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: f.severity.rank, reverse=True)

    def summary(self) -> str:
        c = self.counts
        parts = [f"{c[s.value]} {s.value}" for s in Severity if c[s.value]]
        headline = ", ".join(parts) if parts else "no issues found"
        return f"Grade {self.grade} (score {self.score}/100) — {headline}."

    def to_compact_dict(self) -> dict:
        """Token-efficient view for feeding back to the AI model."""
        return {
            "target": self.target,
            "final_url": self.final_url,
            "ip": self.ip,
            "grade": self.grade,
            "score": self.score,
            "counts": self.counts,
            "checks_run": self.checks_run,
            "errors": self.errors,
            "findings": [
                {
                    "title": f.title,
                    "severity": f.severity.value,
                    "category": f.category,
                    "description": f.description,
                    "evidence": f.evidence,
                    "recommendation": f.recommendation,
                }
                for f in self.sorted_findings()
            ],
        }
