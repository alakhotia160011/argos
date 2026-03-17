"""Autoresearch: self-improving agent prompt optimisation.

Inspired by Karpathy's autoresearch — treats prompts as weights and Sharpe
ratio as the loss function.  The loop:

1. Find the worst-performing agent (lowest Sharpe, not in cooldown)
2. Ask Claude to generate ONE targeted prompt modification
3. Create a git feature branch with the modified prompt
4. Observe for N trading days
5. If Sharpe improved → merge; else → revert
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from typing import Any

from src.config import (
    AUTORESEARCH_COOLDOWN_DAYS,
    AUTORESEARCH_OBSERVATION_DAYS,
    Settings,
)
from src.agents.scorecard import Scorecard
from src.utils.llm import call_agent
from src.utils import git_ops

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class Modification:
    """Record of a single autoresearch modification attempt."""

    day: int
    date: str
    agent: str
    modification: str
    branch_name: str
    pre_sharpe: float | None = None
    post_sharpe: float | None = None
    kept: bool | None = None  # None = still observing
    observation_start_day: int = 0


# ---------------------------------------------------------------------------
# Modification generation
# ---------------------------------------------------------------------------

MODIFICATION_SYSTEM_PROMPT = """\
You are an autoresearch agent that improves trading agent prompts.

You will receive:
1. The current prompt for an underperforming agent
2. The agent's recent recommendations and their outcomes
3. The agent's current Sharpe ratio

Your task: propose ONE specific, targeted modification to the prompt that
addresses a clear failure pattern in the recommendations.

Rules:
- Make exactly ONE change. Do not rewrite the entire prompt.
- The change should be surgical: add a rule, a filter, a threshold, or a constraint.
- Focus on the most common failure mode in the recent recommendations.
- Be specific. "Add momentum filter" is too vague. "Add rule: do not issue
  high-conviction LONG signals when the sector ETF 20-day return is negative"
  is good.
- Output the FULL modified prompt (not just the diff).

Format your response as:

## Modification Description
[One sentence describing what you changed and why]

## Modified Prompt
[The complete modified prompt text]
"""


async def generate_modification(
    agent_name: str,
    current_prompt: str,
    recent_recs: list[dict],
    current_sharpe: float,
) -> tuple[str, str]:
    """Generate a targeted prompt modification.

    Returns (modification_description, new_prompt_text).
    """
    user_msg = (
        f"## Agent: {agent_name}\n"
        f"## Current Sharpe: {current_sharpe:.3f}\n\n"
        f"## Current Prompt\n```\n{current_prompt}\n```\n\n"
        f"## Recent Recommendations (most recent last)\n"
        f"```json\n{json.dumps(recent_recs, indent=2, default=str)}\n```\n\n"
        "Analyse the failure patterns and propose ONE targeted modification."
    )

    response = await call_agent(MODIFICATION_SYSTEM_PROMPT, user_msg, max_tokens=8192)

    # Parse response
    description = ""
    new_prompt = ""

    if "## Modification Description" in response and "## Modified Prompt" in response:
        parts = response.split("## Modified Prompt")
        desc_part = parts[0]
        prompt_part = parts[1] if len(parts) > 1 else ""

        # Extract description
        if "## Modification Description" in desc_part:
            description = desc_part.split("## Modification Description")[1].strip()

        # Extract prompt (strip outer code fences only, preserve internal ones)
        new_prompt = prompt_part.strip()
        if new_prompt.startswith("```"):
            lines = new_prompt.split("\n")
            # Only strip first and last lines if they are code fences
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            new_prompt = "\n".join(lines)
    else:
        # Fallback: use entire response as prompt
        description = "Unstructured modification"
        new_prompt = response

    return description.strip(), new_prompt.strip()


# ---------------------------------------------------------------------------
# Autoresearch loop
# ---------------------------------------------------------------------------


class AutoresearchEngine:
    """Manages the autoresearch lifecycle: propose, observe, keep/revert."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.prompt_dir = self.settings.prompt_dir
        self.log_path = self.settings.state_dir / "autoresearch_log.json"
        self.active_path = self.settings.state_dir / "autoresearch_active.json"
        self.modifications: list[Modification] = []
        self.active_mod: Modification | None = None
        self._load()

    def _load(self) -> None:
        if self.log_path.exists():
            data = json.loads(self.log_path.read_text())
            self.modifications = [Modification(**m) for m in data]

        if self.active_path.exists():
            data = json.loads(self.active_path.read_text())
            self.active_mod = Modification(**data)

    def save(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text(
            json.dumps([asdict(m) for m in self.modifications], indent=2)
        )
        if self.active_mod:
            self.active_path.write_text(json.dumps(asdict(self.active_mod), indent=2))
        elif self.active_path.exists():
            self.active_path.unlink()

    async def maybe_propose(
        self, scorecard: Scorecard, current_day: int, current_date: date
    ) -> Modification | None:
        """Propose a modification if conditions are met.

        Conditions:
        - No active modification being observed
        - An agent is underperforming and not in cooldown
        """
        if self.active_mod is not None:
            logger.debug("Autoresearch: active modification in progress, skipping")
            return None

        agent_name = scorecard.worst_agent(current_day, AUTORESEARCH_COOLDOWN_DAYS)
        if agent_name is None:
            logger.debug("Autoresearch: no eligible agent for modification")
            return None

        current_sharpe = scorecard.agent_sharpe(agent_name)
        logger.info(
            "Autoresearch: targeting %s (Sharpe=%.3f)", agent_name, current_sharpe
        )

        # Load current prompt
        prompt_path = self.prompt_dir / f"{agent_name}.md"
        if not prompt_path.exists():
            logger.warning("No prompt file for %s, skipping", agent_name)
            return None
        current_prompt = prompt_path.read_text()

        # Get recent recommendations
        recent = scorecard.get_recent_recs(agent_name, 20)
        recent_dicts = [
            {
                "date": r.date,
                "ticker": r.ticker,
                "direction": r.direction,
                "conviction": r.conviction,
                "forward_return_5d": r.forward_return_5d,
                "weighted_return": r.weighted_return,
            }
            for r in recent
        ]

        # Generate modification
        description, new_prompt = await generate_modification(
            agent_name, current_prompt, recent_dicts, current_sharpe
        )

        if not new_prompt:
            logger.warning("Autoresearch: empty modification generated, skipping")
            return None

        # Create git branch and apply (backup original prompt first)
        branch_name = f"autoresearch/{agent_name}-day{current_day}"
        original_prompt = current_prompt
        backup_path = prompt_path.with_suffix(".md.bak")
        backup_path.write_text(original_prompt)

        try:
            repo = git_ops.get_repo()
            git_ops.create_branch(repo, branch_name)
            prompt_path.write_text(new_prompt)
            git_ops.commit_to_branch(
                repo, branch_name, prompt_path,
                f"autoresearch: {description[:80]}",
            )
            # Git succeeded, remove backup
            backup_path.unlink(missing_ok=True)
        except Exception as e:
            logger.error("Git operation failed: %s", e)
            # Restore original prompt from backup
            prompt_path.write_text(original_prompt)
            backup_path.unlink(missing_ok=True)
            logger.info("Restored original prompt for %s after git failure", agent_name)
            return None

        mod = Modification(
            day=current_day,
            date=current_date.isoformat(),
            agent=agent_name,
            modification=description,
            branch_name=branch_name,
            pre_sharpe=current_sharpe,
            observation_start_day=current_day,
        )
        self.active_mod = mod
        scorecard.mark_modified(agent_name, current_day)
        self.save()

        logger.info("Autoresearch: proposed modification for %s: %s", agent_name, description)
        return mod

    def maybe_evaluate(self, scorecard: Scorecard, current_day: int) -> Modification | None:
        """Check if the active modification's observation period is over.

        Returns the evaluated modification if evaluation happened, None otherwise.
        """
        if self.active_mod is None:
            return None

        days_elapsed = current_day - self.active_mod.observation_start_day
        if days_elapsed < AUTORESEARCH_OBSERVATION_DAYS:
            logger.debug(
                "Autoresearch: observing %s (day %d/%d)",
                self.active_mod.agent,
                days_elapsed,
                AUTORESEARCH_OBSERVATION_DAYS,
            )
            return None

        # Observation period complete — evaluate
        agent = self.active_mod.agent
        new_sharpe = scorecard.agent_sharpe(agent)
        self.active_mod.post_sharpe = new_sharpe

        pre = self.active_mod.pre_sharpe or 0.0
        if new_sharpe > pre:
            # Keep the modification
            self.active_mod.kept = True
            try:
                repo = git_ops.get_repo()
                git_ops.keep_and_cleanup(repo, self.active_mod.branch_name)
            except Exception as e:
                logger.warning("Git merge failed (keeping prompt anyway): %s", e)

            logger.info(
                "Autoresearch KEPT: %s Sharpe %.3f → %.3f (%s)",
                agent, pre, new_sharpe, self.active_mod.modification,
            )
        else:
            # Revert the modification
            self.active_mod.kept = False
            try:
                repo = git_ops.get_repo()
                git_ops.revert_and_cleanup(repo, self.active_mod.branch_name)
            except Exception as e:
                logger.warning("Git revert failed: %s", e)

            logger.info(
                "Autoresearch REVERTED: %s Sharpe %.3f → %.3f (%s)",
                agent, pre, new_sharpe, self.active_mod.modification,
            )

        self.modifications.append(self.active_mod)
        evaluated = self.active_mod
        self.active_mod = None
        self.save()
        return evaluated

    def stats(self) -> dict[str, Any]:
        """Return summary statistics."""
        total = len(self.modifications)
        kept = sum(1 for m in self.modifications if m.kept)
        reverted = sum(1 for m in self.modifications if m.kept is False)
        return {
            "total_modifications": total,
            "kept": kept,
            "reverted": reverted,
            "keep_rate_pct": round(kept / total * 100, 1) if total else 0,
            "active": self.active_mod is not None,
        }
